# services/backtest_service.py

import json
import math
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
    BacktestEquityCurve,
    BacktestRun,
    BacktestStrategy,
    BacktestTrade,
    db_session,
)
from database.historify_db import get_data_range, get_ohlcv, resolve_base_interval
from services.backtest_analytics import (
    calculate_backtest_metrics,
    generate_monthly_returns_heatmap,
    get_benchmark_comparison,
)
from services.backtest_fill_engine import (
    is_mis_square_off_bar,
    simulate_order_fill,
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
# FD-hygiene rule (threads and executors are shared, never per-call): a 7-TF
# sweep enqueues 7 tasks, at most BATCK_WORKERS run concurrently, the rest
# wait in the queue with their BacktestRun rows sitting at status "pending".
_BACKTEST_WORKERS = 2
_BACKTEST_EXECUTOR = ThreadPoolExecutor(max_workers=_BACKTEST_WORKERS, thread_name_prefix="backtest-run")
_JOB_FUTURES: dict[str, Future] = {}

# Under gunicorn+eventlet these workers are green threads, which are never
# preempted: the bar loop is pure CPU with no I/O, so without an explicit yield
# a 500k-bar 1m run freezes the whole (single) worker for minutes. Hand the hub
# back every N bars — cheap on the dev server's real threads, essential in
# production.
_YIELD_EVERY_BARS = 2000


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
    exec_globals = {
        "pd": pd,
        "np": np,
        "ta": ta,
        "df": df,
    }

    # Safe execution environment
    exec(strategy_code, exec_globals)

    if "strategy" in exec_globals and callable(exec_globals["strategy"]):
        res = exec_globals["strategy"](df.copy())
        if isinstance(res, pd.DataFrame):
            df = res

    # Standardize signal column
    if "signal" not in df.columns:
        df["signal"] = 0
        if "buy_signal" in df.columns and "sell_signal" in df.columns:
            df.loc[df["buy_signal"], "signal"] = 1
            df.loc[df["sell_signal"], "signal"] = -1

    return df


def calculate_position_qty(
    sizing_type: str,
    sizing_value: float,
    current_equity: float,
    current_price: float,
    capital_allocation: float,
) -> float:
    """Calculates trade quantity based on position sizing rules."""
    if current_price <= 0:
        return 0.0

    if sizing_type == "fixed_qty":
        return float(sizing_value)

    elif sizing_type == "pct_equity":
        allocated_val = current_equity * (sizing_value / 100.0)
        return float(math.floor(allocated_val / current_price))

    elif sizing_type == "fixed_capital":
        return float(math.floor(sizing_value / current_price))

    elif sizing_type == "kelly_vol":
        # Volatility-adjusted sizing
        vol_adj_capital = current_equity * min(0.25, max(0.02, sizing_value))
        return float(math.floor(vol_adj_capital / current_price))

    return 1.0


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
):
    """
    Main background execution function for running a backtest job.
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

        # Parse start and end timestamps
        start_ts = int(pd.to_datetime(start_date).timestamp()) if start_date else None
        end_ts = int(pd.to_datetime(end_date).timestamp()) if end_date else None

        # Primary symbol data
        primary_symbol = symbols[0] if symbols else "SBIN"
        if ":" in primary_symbol:
            parts = primary_symbol.split(":")
            exchange = parts[0]
            symbol_clean = parts[1]
        else:
            symbol_clean = primary_symbol

        # Fetch OHLCV data from Historify DB
        df = get_ohlcv(
            symbol=symbol_clean,
            exchange=exchange,
            interval=interval,
            start_timestamp=start_ts,
            end_timestamp=end_ts,
        )

        if df.empty or len(df) < 2:
            raise ValueError(f"No historical data available for {symbol_clean} ({exchange}) interval {interval} in selected range.")

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
            df = df.ffill().bfill()
        elif missing_data_policy == "abort":
            if df[["open", "high", "low", "close"]].isnull().any().any():
                raise ValueError("Missing bar data detected and policy is set to abort.")

        if df.empty:
            raise ValueError("DataFrame is empty after applying missing data policy.")

        # Run signal DSL code
        df = execute_strategy_dsl(strategy_code, df)

        # The engine tracks a single position on a single symbol at a time -
        # there is no pyramiding and no multi-symbol book. A max_positions
        # value above 1 cannot be honored; note it rather than silently
        # accepting a config that implies concurrency the engine doesn't have.
        run_notes: list[str] = []
        if max_positions and max_positions > 1:
            run_notes.append(
                f"max_positions={max_positions} was requested, but this engine runs one "
                "symbol with at most one open position at a time; the setting was not enforced."
            )

        # Simulation loop state variables
        current_equity = initial_capital
        cash = initial_capital
        position_qty = 0.0
        entry_price = 0.0
        entry_time = ""
        entry_bar_idx = 0
        trades_list: list[dict[str, Any]] = []
        equity_records: list[dict[str, Any]] = []

        daily_start_equity = initial_capital
        current_day_str = ""

        total_bars = len(df)

        # Next-bar-open execution. A signal on bar i is normally derived from
        # bar i's close, so acting on it within bar i - which fills at bar i's
        # OPEN - would fill at a price that printed before the signal existed.
        # Bar i therefore trades the signal raised by bar i-1; bar 0 has none.
        signal_values = (
            pd.to_numeric(df["signal"], errors="coerce").fillna(0).astype(int).to_numpy()
        )

        for i in range(total_bars):
            if cancel_event.is_set():
                run_record.status = "cancelled"
                run_record.completed_at = datetime.now(pytz.timezone("Asia/Kolkata"))
                session.commit()
                _emit_progress(job_id, 100.0, "Cancelled", current_equity, len(trades_list), **emit_kwargs)
                return

            bar = df.iloc[i]
            bar_time = str(bar["timestamp"])
            b_open = float(bar["open"])
            b_high = float(bar["high"])
            b_low = float(bar["low"])
            b_close = float(bar["close"])
            b_vol = float(bar.get("volume", 0))
            signal = int(signal_values[i - 1]) if i > 0 else 0

            if i % _YIELD_EVERY_BARS == 0:
                _cooperative_yield()

            # Track day changes for daily loss limit
            day_str = bar_time.split(" ")[0] if " " in bar_time else bar_time.split("T")[0]
            if day_str != current_day_str:
                current_day_str = day_str
                daily_start_equity = current_equity

            # Calculate current market value of positions
            pos_value = position_qty * b_close
            current_equity = cash + pos_value

            # Daily loss limit check
            if daily_loss_limit and daily_loss_limit > 0:
                daily_loss = daily_start_equity - current_equity
                if daily_loss >= daily_loss_limit:
                    # Force exit open position due to daily loss limit
                    if position_qty != 0:
                        filled, exec_price, cost = simulate_order_fill(
                            action="SELL" if position_qty > 0 else "BUY",
                            price_type="MARKET",
                            limit_price=0.0,
                            trigger_price=0.0,
                            bar_open=b_open,
                            bar_high=b_high,
                            bar_low=b_low,
                            bar_close=b_close,
                            volume=b_vol,
                            slippage_bps=slippage_bps,
                            brokerage_bps=brokerage_bps,
                            quantity=abs(position_qty),
                        )
                        if filled:
                            pnl = (exec_price - entry_price) * position_qty - cost
                            pnl_pct = (pnl / (entry_price * abs(position_qty))) * 100.0 if entry_price > 0 else 0.0
                            cash += (position_qty * exec_price) - cost
                            trades_list.append({
                                "job_id": job_id,
                                "symbol": symbol_clean,
                                "exchange": exchange,
                                "action": "BUY" if position_qty > 0 else "SELL",
                                "entry_time": entry_time,
                                "entry_price": round(entry_price, 2),
                                "exit_time": bar_time,
                                "exit_price": round(exec_price, 2),
                                "quantity": abs(position_qty),
                                "pnl": round(pnl, 2),
                                "pnl_pct": round(pnl_pct, 2),
                                "holding_period_bars": i - entry_bar_idx,
                                "entry_reason": "Signal",
                                "exit_reason": "Daily Loss Limit",
                                "costs": round(cost, 2),
                            })
                            position_qty = 0.0
                    # Trading is halted for the rest of the day, but the bar
                    # still has to appear on the equity curve — skipping the
                    # snapshot here left a hole from the breach to midnight and
                    # made the curve's timestamps disagree with the bar series.
                    current_equity = cash + (position_qty * b_close)
                    equity_records.append({
                        "job_id": job_id,
                        "timestamp": bar_time,
                        "equity": round(current_equity, 2),
                        "cash": round(cash, 2),
                        "positions_value": round(position_qty * b_close, 2),
                    })
                    continue

            # Check Stop-Loss % and Take-Profit % rules on active positions
            # (mirrored for shorts: price falling is the profit direction, so
            # the sign of pnl_pct_current flips relative to the long case)
            if position_qty != 0 and entry_price > 0:
                if position_qty > 0:
                    pnl_pct_current = ((b_close - entry_price) / entry_price) * 100.0
                else:
                    pnl_pct_current = ((entry_price - b_close) / entry_price) * 100.0
                exit_triggered = False
                exit_reason = ""

                if stop_loss_pct and pnl_pct_current <= -abs(stop_loss_pct):
                    exit_triggered = True
                    exit_reason = "Stop Loss"
                elif take_profit_pct and pnl_pct_current >= abs(take_profit_pct):
                    exit_triggered = True
                    exit_reason = "Take Profit"

                if exit_triggered:
                    filled, exec_price, cost = simulate_order_fill(
                        action="SELL" if position_qty > 0 else "BUY",
                        price_type="MARKET",
                        limit_price=0.0,
                        trigger_price=0.0,
                        bar_open=b_open,
                        bar_high=b_high,
                        bar_low=b_low,
                        bar_close=b_close,
                        volume=b_vol,
                        slippage_bps=slippage_bps,
                        brokerage_bps=brokerage_bps,
                        quantity=abs(position_qty),
                    )
                    if filled:
                        pnl = (exec_price - entry_price) * position_qty - cost
                        pnl_pct = (pnl / (entry_price * abs(position_qty))) * 100.0
                        cash += (position_qty * exec_price) - cost
                        trades_list.append({
                            "job_id": job_id,
                            "symbol": symbol_clean,
                            "exchange": exchange,
                            "action": "BUY" if position_qty > 0 else "SELL",
                            "entry_time": entry_time,
                            "entry_price": round(entry_price, 2),
                            "exit_time": bar_time,
                            "exit_price": round(exec_price, 2),
                            "quantity": abs(position_qty),
                            "pnl": round(pnl, 2),
                            "pnl_pct": round(pnl_pct, 2),
                            "holding_period_bars": i - entry_bar_idx,
                            "entry_reason": "Signal",
                            "exit_reason": exit_reason,
                            "costs": round(cost, 2),
                        })
                        position_qty = 0.0

            # MIS Auto Square-off Check
            if product_type == "MIS" and position_qty != 0:
                if is_mis_square_off_bar(bar_time, exchange):
                    filled, exec_price, cost = simulate_order_fill(
                        action="SELL" if position_qty > 0 else "BUY",
                        price_type="MARKET",
                        limit_price=0.0,
                        trigger_price=0.0,
                        bar_open=b_open,
                        bar_high=b_high,
                        bar_low=b_low,
                        bar_close=b_close,
                        volume=b_vol,
                        slippage_bps=slippage_bps,
                        brokerage_bps=brokerage_bps,
                        quantity=abs(position_qty),
                    )
                    if filled:
                        pnl = (exec_price - entry_price) * position_qty - cost
                        pnl_pct = (pnl / (entry_price * abs(position_qty))) * 100.0 if entry_price > 0 else 0.0
                        cash += (position_qty * exec_price) - cost
                        trades_list.append({
                            "job_id": job_id,
                            "symbol": symbol_clean,
                            "exchange": exchange,
                            "action": "BUY" if position_qty > 0 else "SELL",
                            "entry_time": entry_time,
                            "entry_price": round(entry_price, 2),
                            "exit_time": bar_time,
                            "exit_price": round(exec_price, 2),
                            "quantity": abs(position_qty),
                            "pnl": round(pnl, 2),
                            "pnl_pct": round(pnl_pct, 2),
                            "holding_period_bars": i - entry_bar_idx,
                            "entry_reason": "Signal",
                            "exit_reason": "MIS Auto Squareoff",
                            "costs": round(cost, 2),
                        })
                        position_qty = 0.0

            # Process Strategy Signals
            # signal: 1 = BUY (open long), -1 = SELL (close long),
            #         2 = SHORT (open short), -2 = COVER (close short)
            if signal == 1 and position_qty == 0:  # BUY Signal
                qty = calculate_position_qty(sizing_type, sizing_value, current_equity, b_close, initial_capital)
                if qty > 0:
                    filled, exec_price, cost = simulate_order_fill(
                        action="BUY",
                        price_type="MARKET",
                        limit_price=0.0,
                        trigger_price=0.0,
                        bar_open=b_open,
                        bar_high=b_high,
                        bar_low=b_low,
                        bar_close=b_close,
                        volume=b_vol,
                        slippage_bps=slippage_bps,
                        brokerage_bps=brokerage_bps,
                        quantity=qty,
                    )
                    if filled:
                        cash -= (qty * exec_price) + cost
                        position_qty = qty
                        entry_price = exec_price
                        entry_time = bar_time
                        entry_bar_idx = i

            elif signal == -1 and position_qty > 0:  # SELL / EXIT Long Signal
                filled, exec_price, cost = simulate_order_fill(
                    action="SELL",
                    price_type="MARKET",
                    limit_price=0.0,
                    trigger_price=0.0,
                    bar_open=b_open,
                    bar_high=b_high,
                    bar_low=b_low,
                    bar_close=b_close,
                    volume=b_vol,
                    slippage_bps=slippage_bps,
                    brokerage_bps=brokerage_bps,
                    quantity=position_qty,
                )
                if filled:
                    pnl = (exec_price - entry_price) * position_qty - cost
                    pnl_pct = (pnl / (entry_price * position_qty)) * 100.0
                    cash += (position_qty * exec_price) - cost
                    trades_list.append({
                        "job_id": job_id,
                        "symbol": symbol_clean,
                        "exchange": exchange,
                        "action": "BUY",
                        "entry_time": entry_time,
                        "entry_price": round(entry_price, 2),
                        "exit_time": bar_time,
                        "exit_price": round(exec_price, 2),
                        "quantity": abs(position_qty),
                        "pnl": round(pnl, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "holding_period_bars": i - entry_bar_idx,
                        "entry_reason": "Signal",
                        "exit_reason": "Signal Exit",
                        "costs": round(cost, 2),
                    })
                    position_qty = 0.0

            elif signal == 2 and position_qty == 0:  # SHORT Signal (open)
                qty = calculate_position_qty(sizing_type, sizing_value, current_equity, b_close, initial_capital)
                if qty > 0:
                    filled, exec_price, cost = simulate_order_fill(
                        action="SELL",
                        price_type="MARKET",
                        limit_price=0.0,
                        trigger_price=0.0,
                        bar_open=b_open,
                        bar_high=b_high,
                        bar_low=b_low,
                        bar_close=b_close,
                        volume=b_vol,
                        slippage_bps=slippage_bps,
                        brokerage_bps=brokerage_bps,
                        quantity=qty,
                    )
                    if filled:
                        cash += (qty * exec_price) - cost
                        position_qty = -qty
                        entry_price = exec_price
                        entry_time = bar_time
                        entry_bar_idx = i

            elif signal == -2 and position_qty < 0:  # COVER Signal (close short)
                filled, exec_price, cost = simulate_order_fill(
                    action="BUY",
                    price_type="MARKET",
                    limit_price=0.0,
                    trigger_price=0.0,
                    bar_open=b_open,
                    bar_high=b_high,
                    bar_low=b_low,
                    bar_close=b_close,
                    volume=b_vol,
                    slippage_bps=slippage_bps,
                    brokerage_bps=brokerage_bps,
                    quantity=abs(position_qty),
                )
                if filled:
                    pnl = (exec_price - entry_price) * position_qty - cost
                    pnl_pct = (pnl / (entry_price * abs(position_qty))) * 100.0
                    cash += (position_qty * exec_price) - cost
                    trades_list.append({
                        "job_id": job_id,
                        "symbol": symbol_clean,
                        "exchange": exchange,
                        "action": "SELL",
                        "entry_time": entry_time,
                        "entry_price": round(entry_price, 2),
                        "exit_time": bar_time,
                        "exit_price": round(exec_price, 2),
                        "quantity": abs(position_qty),
                        "pnl": round(pnl, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "holding_period_bars": i - entry_bar_idx,
                        "entry_reason": "Signal",
                        "exit_reason": "Signal Exit",
                        "costs": round(cost, 2),
                    })
                    position_qty = 0.0

            # Record equity snapshot
            current_equity = cash + (position_qty * b_close)
            equity_records.append({
                "job_id": job_id,
                "timestamp": bar_time,
                "equity": round(current_equity, 2),
                "cash": round(cash, 2),
                "positions_value": round(position_qty * b_close, 2),
            })

            # Emit progress periodically
            if i % max(1, total_bars // 50) == 0 or i == total_bars - 1:
                progress_pct = ((i + 1) / total_bars) * 100.0
                _emit_progress(job_id, progress_pct, bar_time, current_equity, len(trades_list), **emit_kwargs)

        # Save Trade and Equity Records to DB
        equity_series = pd.Series(
            data=[e["equity"] for e in equity_records],
            index=pd.to_datetime([e["timestamp"] for e in equity_records]),
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

        equity_objs = []
        for eq_rec, dd in zip(equity_records, drawdowns, strict=True):
            equity_objs.append(BacktestEquityCurve(
                job_id=job_id,
                timestamp=eq_rec["timestamp"],
                equity=eq_rec["equity"],
                drawdown_pct=round(float(dd), 2),
                cash=eq_rec["cash"],
                positions_value=eq_rec["positions_value"],
            ))

        trade_objs = [BacktestTrade(**t) for t in trades_list]

        session.bulk_save_objects(equity_objs)
        session.bulk_save_objects(trade_objs)

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
) -> dict:
    """Fans one run request out across N timeframes.

    One BacktestRun row per interval, all tied by a shared batch_id. A single
    interval is a batch of one (batch_id = job_id), so the results UI and the
    job history need no special casing for legacy runs.
    """
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

    is_sweep = len(clean_intervals) > 1
    batch_id = f"batch_{uuid.uuid4().hex[:12]}" if is_sweep else None
    session = db_session()

    try:
        symbol_clean, exch = _split_symbol(symbols[0] if symbols else "SBIN", exchange)
        jobs: list[dict[str, Any]] = []

        for batch_seq, iv in enumerate(clean_intervals):
            job_id = f"bt_{uuid.uuid4().hex[:12]}"
            run_batch_id = batch_id or job_id
            # Sibling runs keep the batch label; per-run name disambiguates
            # the timeframe while the single-run path stays byte-identical to
            # what callers passed in.
            run_name = name if not is_sweep else f"{name} ({iv})"

            # Pre-flight: fail fast per timeframe instead of letting one dead
            # interval kill the whole sweep mid-flight. Historify only stores
            # 1m and D — 5m/15m/1h are aggregated from 1m and W/M/Q/Y from D,
            # so the catalog must be checked against the BASE interval. Asking
            # for '5m' directly always misses and would fail every aggregated
            # timeframe in the sweep.
            base_iv = resolve_base_interval(iv)
            range_data = get_data_range(symbol_clean, exch, base_iv)
            if not range_data:
                run_record = BacktestRun(
                    job_id=job_id,
                    name=run_name,
                    status="failed",
                    strategy_code=strategy_code,
                    symbols=json.dumps(symbols),
                    exchange=exch,
                    interval=iv,
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
                    batch_id=run_batch_id,
                    batch_name=name,
                    batch_seq=batch_seq,
                    completed_at=datetime.now(pytz.timezone("Asia/Kolkata")),
                    error_message=(
                        f"No historical data available for {symbol_clean} ({exch}) "
                        f"interval {iv} in Historify (needs {base_iv} data)."
                    ),
                )
                session.add(run_record)
                session.commit()
                jobs.append({"job_id": job_id, "interval": iv, "status": "failed"})
                continue

            run_record = BacktestRun(
                job_id=job_id,
                name=run_name,
                status="pending",
                strategy_code=strategy_code,
                symbols=json.dumps(symbols),
                exchange=exch,
                interval=iv,
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
                batch_id=run_batch_id,
                batch_name=name,
                batch_seq=batch_seq,
            )
            session.add(run_record)
            session.commit()

            future = _BACKTEST_EXECUTOR.submit(
                run_backtest_pipeline,
                job_id=job_id,
                name=run_name,
                strategy_code=strategy_code,
                symbols=symbols,
                exchange=exch,
                interval=iv,
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
            )
            with _ACTIVE_JOBS_LOCK:
                _JOB_FUTURES[job_id] = future
            jobs.append({"job_id": job_id, "interval": iv, "status": "pending"})

        return {"batch_id": batch_id or jobs[0]["job_id"], "jobs": jobs}

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
