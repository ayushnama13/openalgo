# services/backtest_service.py

import hashlib
import json
import math
import os
import sys
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from typing import Any, Optional

import numpy as np
import pandas as pd
import pytz

from database.backtest_db import (
    BacktestRun,
    BacktestStrategy,
    BacktestTrade,
    db_session,
    save_equity_curve,
)
from database.historify_db import (
    get_catalog_ranges,
    get_data_range,
    get_ohlcv,
    get_symbol_metadata,
    resolve_base_interval,
)
from services.backtest_analytics import (
    calculate_backtest_metrics,
    generate_monthly_returns_heatmap,
    get_benchmark_comparison,
)
from services.backtest_engine import calculate_position_qty  # noqa: F401 (re-exported)
from services.backtest_universes import universe_symbols
from services.strategy_sandbox import (
    BacktestCancelled,
    run_backtest_sandboxed,
    run_strategy_sandboxed,
)
from utils.logging import get_logger

# Import openalgo.ta (Rust-backed indicator library pinned in pyproject.toml).
# Mirrors services/indicator_service.py: with numpy/pandas-series input and
# lowercase names (sma, rsi, supertrend...) exposed through the package root.
try:
    from openalgo import ta
except ImportError:
    try:
        import ta
    except ImportError:
        ta = None

logger = get_logger(__name__)

# Active running job cancel flags: job_id -> threading.Event()
_ACTIVE_JOBS: dict[str, threading.Event] = {}
_ACTIVE_JOBS_LOCK = threading.Lock()

# Shared work queue for backtest runs. A module-level singleton per the
# FD-hygiene rule (threads and executors are shared, never per-call): tasks
# run concurrently up to BACKTEST_WORKERS (default 2), the rest wait in
# the queue with their BacktestRun rows sitting at status "pending".
_BACKTEST_WORKERS = int(os.getenv("BACKTEST_WORKERS", "2"))
_BACKTEST_EXECUTOR = ThreadPoolExecutor(max_workers=_BACKTEST_WORKERS, thread_name_prefix="backtest-run")
_JOB_FUTURES: dict[str, Future] = {}

# Under gunicorn+eventlet these workers are green threads, which are never
# preempted: the bar loop is pure CPU with no I/O, so without an explicit yield
# a 500k-bar 1m run freezes the whole (single) worker for minutes. Hand the hub
# back every N bars — cheap on the dev server's real threads, essential in
# production.
_YIELD_EVERY_BARS = 2000

# Hard cap on symbols per run (memory: all frames live in the sandbox child).
# A hand-picked stock list stays small; a universe portfolio (NIFTY 500) needs
# more, and is bounded instead by the total bar budget below.
_MAX_SYMBOLS = int(os.getenv("BACKTEST_MAX_SYMBOLS", "50"))
_MAX_UNIVERSE_SYMBOLS = int(os.getenv("BACKTEST_MAX_UNIVERSE_SYMBOLS", "600"))
# Total bars across every symbol of one run. 500 symbols x 10 years of daily
# bars is ~1.25M; 500 symbols of 1m bars would be hundreds of millions and
# exhaust the sandbox child's memory, so a run over budget fails up front
# with a message instead of being killed mid-way.
_MAX_TOTAL_BARS = int(os.getenv("BACKTEST_MAX_TOTAL_BARS", "4000000"))

RUN_MODES = ("stock", "universe", "scan")

_TRADE_COLUMNS = {
    "job_id", "symbol", "exchange", "action", "entry_time", "entry_price", "exit_time",
    "exit_price", "quantity", "pnl", "pnl_pct", "holding_period_bars", "entry_reason",
    "exit_reason", "costs", "mae_pct", "mfe_pct",
}


def _cooperative_yield():
    """Yield to the eventlet hub (no-op cost on the threaded dev server)."""
    try:
        from utils.event_bus import socketio
        if socketio:
            socketio.sleep(0)
            return
    except Exception:
        pass
    time.sleep(0)


def register_active_job(job_id: str) -> threading.Event:
    cancel_event = threading.Event()
    with _ACTIVE_JOBS_LOCK:
        _ACTIVE_JOBS[job_id] = cancel_event
    return cancel_event


def unregister_active_job(job_id: str):
    with _ACTIVE_JOBS_LOCK:
        _ACTIVE_JOBS.pop(job_id, None)
        _JOB_FUTURES.pop(job_id, None)


def _track_future(job_id: str, future: Future) -> None:
    """Remember a queued run's future until it finishes.

    The worker can finish (and unregister) before the submitter gets here, so
    removal is a done-callback, not only unregister_active_job: the callback
    fires at once on an already-finished future, and so no stale entry is left.
    It only removes the entry if it still holds this same future.
    """

    def _forget(done: Future) -> None:
        with _ACTIVE_JOBS_LOCK:
            if _JOB_FUTURES.get(job_id) is done:
                del _JOB_FUTURES[job_id]

    with _ACTIVE_JOBS_LOCK:
        _JOB_FUTURES[job_id] = future
    future.add_done_callback(_forget)


def cancel_job(job_id: str) -> bool:
    """Cancels one run, whether it is already running or still queued.

    A running job gets its cancel event set and marks itself cancelled on the
    next bar. A job still waiting in the executor queue never registered a
    cancel event, so it is pulled out of the queue and its row is marked here
    - otherwise a queued job could not be cancelled at all.
    """
    with _ACTIVE_JOBS_LOCK:
        cancel_event = _ACTIVE_JOBS.get(job_id)
        if cancel_event:
            cancel_event.set()
            return True
        future = _JOB_FUTURES.get(job_id)

    if not (future and future.cancel()):
        return False

    with _ACTIVE_JOBS_LOCK:
        _JOB_FUTURES.pop(job_id, None)

    session = db_session()
    try:
        run = session.query(BacktestRun).filter_by(job_id=job_id).first()
        if run and run.status == "pending":
            run.status = "cancelled"
            run.completed_at = datetime.now(pytz.timezone("Asia/Kolkata"))
            run.error_message = "Cancelled by user before the run started"
            session.commit()
        return True
    except Exception as e:
        session.rollback()
        logger.exception(f"Error cancelling queued backtest job {job_id}: {e}")
        return False
    finally:
        db_session.remove()


def _emit_progress(
    job_id: str,
    percent: float,
    current_date: str,
    equity: float,
    trades_count: int,
    batch_id: str | None = None,
    batch_seq: int | None = None,
    interval: str | None = None,
):
    """Emits SocketIO backtest_progress event if SocketIO server is available."""
    try:
        from utils.event_bus import socketio
        if socketio:
            socketio.emit(
                "backtest_progress",
                {
                    "job_id": job_id,
                    "batch_id": batch_id,
                    "batch_seq": batch_seq,
                    "interval": interval,
                    "percent": round(percent, 1),
                    "current_date": current_date,
                    "equity": round(equity, 2),
                    "trades_count": trades_count,
                },
            )
    except Exception as e:
        logger.debug(f"Failed to emit backtest_progress: {e}")


def execute_strategy_dsl(strategy_code: str, df: pd.DataFrame) -> pd.DataFrame:
    """
    Executes Python signal DSL strategy code against historical DataFrame.
    The code should define a function `strategy(df)` or modify `df` in-place
    to set `signal` column (1 = BUY, -1 = SELL/EXIT, 2 = SHORT, -2 = COVER)
    or `buy_signal` / `sell_signal` columns.
    """
    # Runs in a sandboxed child process (see services/strategy_sandbox.py).
    signals = run_strategy_sandboxed(strategy_code, df)
    df = df.copy()
    for col in signals.columns:
        df[col] = signals[col].to_numpy()
    return df


_ENGINE_OPTION_TYPES: dict[str, type | tuple] = {
    "spread_bps": (int, float),
    "impact_bps": (int, float),
    "max_volume_pct": (int, float),
    "trail_pct": (int, float),
    "cost_preset": str,
    "cost_overrides": dict,
    "allow_short": bool,
    "enforce_cash": bool,
    "instrument": str,
    "params": dict,
    "margin_pct_fut": (int, float),
    "margin_pct_short_opt": (int, float),
    "intraday_leverage": (int, float),
}


def sanitize_engine_options(options: dict | None) -> dict:
    """Whitelist and type-check the optional engine knobs; reject anything else."""
    if not options:
        return {}
    if not isinstance(options, dict):
        raise ValueError("engine_options must be an object")
    clean: dict[str, Any] = {}
    for key, value in options.items():
        expected = _ENGINE_OPTION_TYPES.get(key)
        if expected is None:
            raise ValueError(f"Unknown engine option {key!r}; allowed: {sorted(_ENGINE_OPTION_TYPES)}")
        if value is None:
            continue
        if isinstance(value, bool) and expected is not bool:
            raise ValueError(f"engine option {key!r} has the wrong type")
        if not isinstance(value, expected):
            raise ValueError(f"engine option {key!r} has the wrong type")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and (
            not math.isfinite(value) or value < 0
        ):
            raise ValueError(f"engine option {key!r} must be a non-negative number")
        clean[key] = value
    preset = clean.get("cost_preset")
    if preset and preset != "auto":
        from portfolio.costs import PRESETS

        if preset not in PRESETS:
            raise ValueError(f"Unknown cost_preset {preset!r}; have {['auto', *sorted(PRESETS)]}")
    if clean.get("max_volume_pct") is not None and not 0 < clean["max_volume_pct"] <= 100:
        raise ValueError("max_volume_pct must be in (0, 100]")
    try:
        json.dumps(clean)
    except TypeError as exc:
        raise ValueError("engine_options must be JSON-serialisable") from exc
    return clean


def _data_fingerprint(bars: dict[str, pd.DataFrame]) -> dict:
    """Per symbol: bar count, first/last timestamp, and a hash of the OHLCV values."""
    out = {}
    for key, df in bars.items():
        digest = hashlib.sha256(
            pd.util.hash_pandas_object(df, index=False).to_numpy().tobytes()
        ).hexdigest()
        out[key] = {
            "bars": int(len(df)),
            "first": str(df["timestamp"].iloc[0]),
            "last": str(df["timestamp"].iloc[-1]),
            "sha256": digest,
        }
    return out


_FO_EXCHANGES = {"NFO", "BFO", "MCX", "CDS", "BCD", "NCDEX"}


def resolve_instruments(keys: list[str]) -> tuple[dict[str, dict], list[str]]:
    """Lot size / contract kind / strike / expiry per symbol, from Historify metadata.

    Cash equity needs nothing (lot 1); contracts without metadata fall back to lot 1
    with a note so the user knows quantities are not lot-aligned.
    """
    out: dict[str, dict] = {}
    notes: list[str] = []
    for key in keys:
        exch, sym = key.split(":", 1)
        if exch.upper() not in _FO_EXCHANGES:
            continue
        meta = get_symbol_metadata(sym, exch) or {}
        itype = str(meta.get("instrumenttype") or "").upper()
        tail = sym.upper()
        if "FUT" in itype or tail.endswith("FUT"):
            kind = "FUT"
        elif itype in ("CE", "PE"):
            kind = itype
        elif tail.endswith(("CE", "PE")):
            kind = tail[-2:]
        else:
            kind = "EQ"
        lot = meta.get("lotsize")
        if not lot or int(lot) < 1:
            notes.append(f"{key}: lot size unknown in Historify metadata, assuming 1 unit.")
            lot = 1
        out[key] = {
            "kind": kind,
            "lot_size": int(lot),
            "strike": meta.get("strike") or None,
            "expiry": str(meta["expiry"]) if meta.get("expiry") else None,
        }
    return out, notes


def load_run_bars(
    symbols: list[str],
    exchange: str,
    interval: str,
    start_date: str | None,
    end_date: str | None,
    missing_data_policy: str,
    max_symbols: int | None = None,
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Load, normalise and clean the OHLCV frames for every requested symbol.

    ``max_symbols`` overrides the stock-list cap (universe runs pass the larger
    universe cap). Returns ({"NSE:SBIN": frame}, notes). Raises ValueError when
    nothing is usable or the run exceeds the total bar budget.
    """
    limit = max_symbols or _MAX_SYMBOLS
    if missing_data_policy not in {"skip", "forward_fill", "abort"}:
        raise ValueError("missing_data_policy must be one of skip, forward_fill, abort")

    ist = pytz.timezone("Asia/Kolkata")

    def _bound_timestamp(value, is_end):
        if not value:
            return None
        parsed = pd.Timestamp(value)
        if parsed.tzinfo is None:
            parsed = parsed.tz_localize(ist)
        else:
            parsed = parsed.tz_convert(ist)
        if len(str(value).strip()) == 10 and is_end:
            parsed = parsed.replace(hour=23, minute=59, second=59)
        elif len(str(value).strip()) == 10:
            parsed = parsed.replace(hour=0, minute=0, second=0)
        return int(parsed.timestamp())

    start_ts = _bound_timestamp(start_date, False)
    end_ts = _bound_timestamp(end_date, True)

    keys: list[str] = []
    for raw in symbols or ["SBIN"]:
        sym_clean, exch = _split_symbol(raw, exchange)
        key = f"{exch}:{sym_clean}"
        if key not in keys:
            keys.append(key)
    if len(keys) > limit:
        raise ValueError(f"Too many symbols ({len(keys)}); the limit is {limit}.")

    run_notes: list[str] = []
    bars_by_symbol: dict[str, pd.DataFrame] = {}
    total_bars = 0
    for key in keys:
        exch, sym_clean = key.split(":", 1)
        df = get_ohlcv(
            symbol=sym_clean,
            exchange=exch,
            interval=interval,
            start_timestamp=start_ts,
            end_timestamp=end_ts,
        )
        if df.empty or len(df) < 2:
            if len(keys) == 1:
                raise ValueError(
                    f"No historical data available for {sym_clean} ({exch}) interval {interval} in selected range."
                )
            run_notes.append(f"{key} skipped: no historical data in the selected range.")
            continue
        total_bars += len(df)
        if total_bars > _MAX_TOTAL_BARS:
            raise ValueError(
                f"Run is too large: more than {_MAX_TOTAL_BARS:,} bars across {len(keys)} symbols "
                f"at {interval}. Use a higher timeframe, a shorter date range or a smaller universe."
            )

        # get_ohlcv() returns raw epoch seconds (UTC) in the timestamp column on
        # every code path (raw, intraday-aggregated, daily-aggregated) - convert
        # once here. Everything downstream (day-boundary detection for the daily
        # loss limit, MIS square-off time matching, the final equity-curve index)
        # assumes a "YYYY-MM-DD HH:MM:SS" string and silently misbehaves on a raw
        # epoch value instead of raising.
        df["timestamp"] = (
            pd.to_datetime(df["timestamp"], unit="s", utc=True)
            .dt.tz_convert("Asia/Kolkata")
            .dt.tz_localize(None)
            .astype(str)
        )

        # Handle missing data policy
        if missing_data_policy == "skip":
            df = df.dropna(subset=["open", "high", "low", "close"])
        elif missing_data_policy == "forward_fill":
            df = df.ffill().dropna(subset=["open", "high", "low", "close"])
        elif missing_data_policy == "abort":
            if df[["open", "high", "low", "close"]].isnull().any().any():
                raise ValueError(
                    f"Missing bar data detected in {key} and policy is set to abort."
                )
        df = df.drop_duplicates(subset="timestamp", keep="last").reset_index(drop=True)
        if df.empty:
            raise ValueError(f"{key}: DataFrame is empty after applying missing data policy.")
        bars_by_symbol[key] = df[
            [c for c in ("timestamp", "open", "high", "low", "close", "volume") if c in df.columns]
        ]

    if not bars_by_symbol:
        raise ValueError("No historical data available for any requested symbol.")
    return bars_by_symbol, run_notes


def run_backtest_pipeline(
    job_id: str,
    name: str,
    strategy_code: str,
    symbols: list[str],
    exchange: str,
    interval: str,
    start_date: str | None,
    end_date: str | None,
    initial_capital: float,
    sizing_type: str,
    sizing_value: float,
    slippage_bps: float,
    brokerage_bps: float,
    product_type: str,
    missing_data_policy: str,
    stop_loss_pct: float | None,
    take_profit_pct: float | None,
    daily_loss_limit: float | None,
    max_positions: int,
    engine_options: dict | None = None,
    mode: str = "stock",
):
    """
    Main background execution function for running a backtest job.

    ``mode`` only changes the symbol cap: a "universe" portfolio may hold up to
    the universe cap; "stock" and "scan" (one constituent per run) keep the
    stock-list cap.
    """
    cancel_event = register_active_job(job_id)
    session = db_session()
    # Populated once the run row is loaded, but referenced by the except
    # handler below — a failure before that point (e.g. the status commit)
    # would otherwise raise NameError and mask the real error, leaving the
    # row stuck at "pending"/"running" with no error_message.
    emit_kwargs: dict[str, Any] = {"batch_id": None, "batch_seq": 0, "interval": interval}

    try:
        # Update run status to running
        run_record = session.query(BacktestRun).filter_by(job_id=job_id).first()
        if not run_record:
            logger.error(f"Backtest job {job_id} not found in DB")
            return
        run_record.status = "running"
        session.commit()

        batch_id = getattr(run_record, "batch_id", None)
        batch_seq = getattr(run_record, "batch_seq", 0) or 0
        emit_kwargs.update({"batch_id": batch_id, "batch_seq": batch_seq})

        bars_by_symbol, run_notes = load_run_bars(
            symbols, exchange, interval, start_date, end_date, missing_data_policy,
            max_symbols=_MAX_UNIVERSE_SYMBOLS if mode == "universe" else None,
        )

        instruments, inst_notes = resolve_instruments(list(bars_by_symbol))
        run_notes.extend(inst_notes)

        engine_cfg = {
            "initial_capital": initial_capital,
            "sizing_type": sizing_type,
            "sizing_value": sizing_value,
            "slippage_bps": slippage_bps,
            "brokerage_bps": brokerage_bps,
            "product_type": product_type,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "daily_loss_limit": daily_loss_limit,
            "max_positions": max_positions or 1,
            "interval": interval,
            "instruments": instruments,
            **(engine_options or {}),
        }
        run_record.code_hash = hashlib.sha256(strategy_code.encode("utf-8")).hexdigest()
        fingerprint = _data_fingerprint(bars_by_symbol)
        for key, info in instruments.items():
            if key in fingerprint:
                fingerprint[key]["instrument"] = info
        run_record.data_fingerprint = json.dumps(fingerprint)
        session.commit()

        def _on_progress(pct: float, ts: str, eq: float, n_trades: int) -> None:
            _emit_progress(job_id, pct, ts, eq, n_trades, **emit_kwargs)

        try:
            out = run_backtest_sandboxed(
                strategy_code,
                bars_by_symbol,
                engine_cfg,
                cancel_event=cancel_event,
                progress_cb=_on_progress,
                equity_as="frame",
            )
        except BacktestCancelled:
            run_record.status = "cancelled"
            run_record.completed_at = datetime.now(pytz.timezone("Asia/Kolkata"))
            session.commit()
            _emit_progress(job_id, 100.0, "Cancelled", initial_capital, 0, **emit_kwargs)
            return

        run_notes.extend(out["notes"])
        trades_list: list[dict[str, Any]] = [{"job_id": job_id, **t} for t in out["trades"]]
        equity_frame = out["equity"]
        current_equity = float(equity_frame["equity"].iloc[-1])

        # Save Trade and Equity Records to DB
        equity_series = pd.Series(
            data=equity_frame["equity"].to_numpy(dtype="float64"),
            index=pd.to_datetime(equity_frame["timestamp"]),
        )

        # Calculate metrics
        metrics = calculate_backtest_metrics(equity_series, trades_list, initial_capital, interval=interval)
        heatmap = generate_monthly_returns_heatmap(equity_series)
        metrics["monthly_heatmap"] = heatmap
        if run_notes:
            metrics["notes"] = run_notes

        # Calculate drawdown percentages
        cummax = equity_series.cummax()
        drawdowns = ((equity_series - cummax) / cummax) * 100.0

        save_equity_curve(
            session, job_id, equity_frame["timestamp"].tolist(),
            equity_frame["equity"].to_numpy(), np.round(drawdowns.to_numpy(), 2),
            equity_frame["cash"].to_numpy(), equity_frame["positions_value"].to_numpy(),
        )
        session.bulk_insert_mappings(
            BacktestTrade, [{k: v for k, v in t.items() if k in _TRADE_COLUMNS} for t in trades_list]
        )

        run_record.status = "completed"
        run_record.completed_at = datetime.now(pytz.timezone("Asia/Kolkata"))
        run_record.metrics_json = json.dumps(metrics)
        session.commit()

        _emit_progress(job_id, 100.0, "Completed", current_equity, len(trades_list), **emit_kwargs)

    except Exception as e:
        logger.exception(f"Error executing backtest pipeline for job {job_id}: {e}")
        session.rollback()
        run_record = session.query(BacktestRun).filter_by(job_id=job_id).first()
        if run_record:
            run_record.status = "failed"
            run_record.error_message = str(e)
            run_record.completed_at = datetime.now(pytz.timezone("Asia/Kolkata"))
            session.commit()
        _emit_progress(job_id, 100.0, f"Failed: {e}", 0.0, 0, **emit_kwargs)
    finally:
        unregister_active_job(job_id)
        db_session.remove()


def _split_symbol(symbol: str, exchange: str) -> tuple[str, str]:
    """Splits "NSE:SBIN" into (symbol, exchange); falls back to the exchange arg."""
    if ":" in symbol:
        parts = symbol.split(":")
        return parts[1], parts[0]
    return symbol, exchange


def _plan_runs(
    mode: str,
    name: str,
    symbols: list[str],
    exchange: str,
    intervals: list[str],
    universe: str | None,
) -> tuple[list[dict[str, Any]], list[str], str]:
    """Expand one run request into the list of runs to create.

    Returns (runs, skipped, exchange) where each run is {"interval", "symbols",
    "name"} and ``skipped`` lists universe constituents left out for want of data.

    stock     one run per interval over the hand-picked symbols
    universe  one portfolio run per interval over every constituent with data
    scan      one run per constituent with data, single interval
    """
    if mode not in RUN_MODES:
        raise ValueError(f"mode must be one of {', '.join(RUN_MODES)}")
    is_multi_iv = len(intervals) > 1

    if mode == "stock":
        if not symbols:
            raise ValueError("At least one symbol is required")
        _, exch = _split_symbol(symbols[0], exchange)
        runs = [
            {"interval": iv, "symbols": symbols, "name": f"{name} ({iv})" if is_multi_iv else name}
            for iv in intervals
        ]
        return runs, [], exch

    if not universe:
        raise ValueError("universe is required for universe and scan modes")
    meta, members = universe_symbols(universe)
    exch = meta.get("exchange", "NSE")
    if mode == "scan" and is_multi_iv:
        raise ValueError("A universe scan runs on a single timeframe; pick one interval")

    runs: list[dict[str, Any]] = []
    skipped: list[str] = []
    for iv in intervals:
        ranges = get_catalog_ranges(exch, resolve_base_interval(iv))
        covered = [m for m in members if m.split(":", 1)[1] in ranges]
        skipped = [m.split(":", 1)[1] for m in members if m.split(":", 1)[1] not in ranges]
        if not covered:
            raise ValueError(
                f"No {meta['name']} constituent has {resolve_base_interval(iv)} data in Historify. "
                "Download the universe's history first."
            )
        if mode == "universe":
            if len(covered) > _MAX_UNIVERSE_SYMBOLS:
                raise ValueError(
                    f"Universe has {len(covered)} symbols with data; the limit is {_MAX_UNIVERSE_SYMBOLS}."
                )
            label = f"{name} ({iv})" if is_multi_iv else name
            runs.append({"interval": iv, "symbols": covered, "name": label})
        else:
            runs.extend(
                {"interval": iv, "symbols": [m], "name": f"{name} ({m.split(':', 1)[1]})"}
                for m in covered
            )
    return runs, skipped, exch


def start_backtest_batch(
    name: str,
    strategy_code: str,
    symbols: list[str],
    exchange: str = "NSE",
    intervals: list[str] | None = None,
    interval: str = "1m",
    start_date: str | None = None,
    end_date: str | None = None,
    initial_capital: float = 100000.0,
    sizing_type: str = "fixed_qty",
    sizing_value: float = 1.0,
    slippage_bps: float = 0.0,
    brokerage_bps: float = 0.0,
    product_type: str = "MIS",
    missing_data_policy: str = "skip",
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
    daily_loss_limit: float | None = None,
    max_positions: int = 5,
    engine_options: dict | None = None,
    mode: str = "stock",
    universe: str | None = None,
) -> dict:
    """Fan one run request out into a batch of runs (see ``_plan_runs`` for the modes).

    Every run gets its own BacktestRun row tied by a shared batch_id. A single
    run is a batch of one (batch_id = job_id), so the results UI and the job
    history need no special casing for legacy runs.
    """
    engine_options = sanitize_engine_options(engine_options)
    if not intervals:
        intervals = [interval]
    # Dedupe while preserving the requested order — the batch_seq tab order
    # must match what the user picked.
    seen: set[str] = set()
    clean_intervals: list[str] = []
    for iv in intervals:
        if iv and iv not in seen:
            seen.add(iv)
            clean_intervals.append(iv)
    if not clean_intervals:
        raise ValueError("At least one interval is required")

    runs, skipped, exch = _plan_runs(
        mode, name, symbols or ["SBIN"], exchange, clean_intervals, universe
    )
    batch_id = f"batch_{uuid.uuid4().hex[:12]}" if len(runs) > 1 else None
    universe_id = ((universe or "").strip().upper() or None) if mode != "stock" else None
    common = {
        "strategy_code": strategy_code,
        "exchange": exch,
        "start_date": start_date,
        "end_date": end_date,
        "initial_capital": initial_capital,
        "sizing_type": sizing_type,
        "sizing_value": sizing_value,
        "slippage_bps": slippage_bps,
        "brokerage_bps": brokerage_bps,
        "product_type": product_type,
        "missing_data_policy": missing_data_policy,
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "daily_loss_limit": daily_loss_limit,
        "max_positions": max_positions,
    }
    session = db_session()

    try:
        jobs: list[dict[str, Any]] = []
        for batch_seq, run in enumerate(runs):
            iv = run["interval"]
            job_id = f"bt_{uuid.uuid4().hex[:12]}"
            row = {
                **common,
                "job_id": job_id,
                "name": run["name"],
                "symbols": json.dumps(run["symbols"]),
                "interval": iv,
                "batch_id": batch_id or job_id,
                "batch_name": name,
                "batch_seq": batch_seq,
                "mode": mode,
                "universe": universe_id,
            }
            job_info = {"job_id": job_id, "interval": iv}
            if mode == "scan":
                job_info["symbol"] = run["symbols"][0].split(":", 1)[1]

            # Pre-flight for a stock run: fail fast per timeframe instead of
            # letting one dead interval kill the whole sweep mid-flight.
            # Historify only stores 1m and D — 5m/15m/1h are aggregated from 1m
            # and W/M/Q/Y from D, so the catalog is checked against the BASE
            # interval. Universe and scan runs were already filtered to the
            # constituents with data by _plan_runs.
            if mode == "stock":
                symbol_clean, sym_exch = _split_symbol(run["symbols"][0], exchange)
                base_iv = resolve_base_interval(iv)
                if not get_data_range(symbol_clean, sym_exch, base_iv):
                    session.add(BacktestRun(
                        **row,
                        status="failed",
                        completed_at=datetime.now(pytz.timezone("Asia/Kolkata")),
                        error_message=(
                            f"No historical data available for {symbol_clean} ({sym_exch}) "
                            f"interval {iv} in Historify (needs {base_iv} data)."
                        ),
                    ))
                    session.commit()
                    jobs.append({**job_info, "status": "failed"})
                    continue

            session.add(BacktestRun(
                **row,
                status="pending",
                engine_options=json.dumps(engine_options) if engine_options else None,
            ))
            session.commit()

            future = _BACKTEST_EXECUTOR.submit(
                run_backtest_pipeline,
                job_id=job_id,
                name=run["name"],
                symbols=run["symbols"],
                interval=iv,
                engine_options=engine_options,
                mode=mode,
                **common,
            )
            _track_future(job_id, future)
            jobs.append({**job_info, "status": "pending"})

        return {"batch_id": batch_id or jobs[0]["job_id"], "jobs": jobs, "skipped": skipped}

    except Exception as e:
        session.rollback()
        logger.exception(f"Failed to submit backtest batch: {e}")
        raise
    finally:
        db_session.remove()


def start_backtest_job(
    name: str,
    strategy_code: str,
    symbols: list[str],
    exchange: str = "NSE",
    interval: str = "1m",
    start_date: str | None = None,
    end_date: str | None = None,
    initial_capital: float = 100000.0,
    sizing_type: str = "fixed_qty",
    sizing_value: float = 1.0,
    slippage_bps: float = 0.0,
    brokerage_bps: float = 0.0,
    product_type: str = "MIS",
    missing_data_policy: str = "skip",
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
    daily_loss_limit: float | None = None,
    max_positions: int = 5,
    engine_options: dict | None = None,
) -> str:
    """Submits a single backtest job (a batch of one) and returns its job_id."""
    batch = start_backtest_batch(
        name=name,
        strategy_code=strategy_code,
        symbols=symbols,
        exchange=exchange,
        intervals=[interval],
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        sizing_type=sizing_type,
        sizing_value=sizing_value,
        slippage_bps=slippage_bps,
        brokerage_bps=brokerage_bps,
        product_type=product_type,
        missing_data_policy=missing_data_policy,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        daily_loss_limit=daily_loss_limit,
        max_positions=max_positions,
        engine_options=engine_options,
    )
    return batch["jobs"][0]["job_id"]


def cancel_batch(batch_id: str) -> list[str]:
    """Cancels every sibling run in a batch.

    Running jobs get their cancel event set (the pipeline marks itself
    cancelled on the next bar); queued-but-unstarted futures that can still be
    cancelled are pulled out of the queue and their rows are marked cancelled
    right here.
    """
    session = db_session()
    try:
        rows = session.query(BacktestRun.job_id).filter_by(batch_id=batch_id).all()
        cancelled: list[str] = []
        for (job_id,) in rows:
            with _ACTIVE_JOBS_LOCK:
                cancel_event = _ACTIVE_JOBS.get(job_id)
                future = _JOB_FUTURES.get(job_id)
            if cancel_event:
                cancel_event.set()
                cancelled.append(job_id)
                continue
            if future and future.cancel():
                run = session.query(BacktestRun).filter_by(job_id=job_id).first()
                if run and run.status == "pending":
                    run.status = "cancelled"
                    run.completed_at = datetime.now(pytz.timezone("Asia/Kolkata"))
                    run.error_message = "Cancelled by user (batch cancel)"
                    session.commit()
                with _ACTIVE_JOBS_LOCK:
                    _JOB_FUTURES.pop(job_id, None)
                cancelled.append(job_id)
        return cancelled
    except Exception as e:
        session.rollback()
        logger.exception(f"Error cancelling backtest batch {batch_id}: {e}")
        raise
    finally:
        db_session.remove()


# ---------------------------------------------------------------------------
# Parameter sweeps / walk-forward (see services/backtest_optimizer.py)
# ---------------------------------------------------------------------------
def start_optimization(
    name: str,
    strategy_code: str,
    symbols: list[str],
    optimize: dict,
    exchange: str = "NSE",
    interval: str = "5m",
    start_date: str | None = None,
    end_date: str | None = None,
    initial_capital: float = 100000.0,
    sizing_type: str = "fixed_qty",
    sizing_value: float = 1.0,
    slippage_bps: float = 0.0,
    brokerage_bps: float = 0.0,
    product_type: str = "MIS",
    missing_data_policy: str = "skip",
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
    daily_loss_limit: float | None = None,
    max_positions: int = 5,
    engine_options: dict | None = None,
) -> str:
    """Queue a parameter sweep or walk-forward study; returns its opt_id."""
    from services import backtest_optimizer as opt

    options = sanitize_engine_options(engine_options)
    config = {
        "symbols": symbols,
        "exchange": exchange,
        "interval": interval,
        "start_date": start_date,
        "end_date": end_date,
        "missing_data_policy": missing_data_policy,
        "engine_config": {
            "initial_capital": initial_capital,
            "sizing_type": sizing_type,
            "sizing_value": sizing_value,
            "slippage_bps": slippage_bps,
            "brokerage_bps": brokerage_bps,
            "product_type": product_type,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "daily_loss_limit": daily_loss_limit,
            "max_positions": max_positions or 1,
            "interval": interval,
            **options,
        },
        "optimize": optimize,
    }
    opt_id = opt.create_optimization_record(name, strategy_code, config)

    def loader(cfg: dict):
        bars, notes = load_run_bars(
            cfg["symbols"], cfg["exchange"], cfg["interval"], cfg["start_date"],
            cfg["end_date"], cfg["missing_data_policy"], max_symbols=_MAX_UNIVERSE_SYMBOLS,
        )
        instruments, inst_notes = resolve_instruments(list(bars))
        return bars, notes + inst_notes, instruments

    def work():
        cancel_event = register_active_job(opt_id)
        try:
            opt.run_optimization_job(opt_id, cancel_event, loader)
        finally:
            unregister_active_job(opt_id)

    future = _BACKTEST_EXECUTOR.submit(work)
    _track_future(opt_id, future)
    return opt_id


def cancel_optimization(opt_id: str) -> bool:
    """Cancel a running or queued study."""
    from database.backtest_db import BacktestOptimization

    if cancel_job(opt_id):
        session = db_session()
        try:
            rec = session.query(BacktestOptimization).filter_by(opt_id=opt_id).first()
            if rec and rec.status == "pending":
                rec.status = "cancelled"
                rec.completed_at = datetime.now(pytz.timezone("Asia/Kolkata"))
                rec.error_message = "Cancelled by user before the run started"
                session.commit()
        finally:
            db_session.remove()
        return True
    return False
