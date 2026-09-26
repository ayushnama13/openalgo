"""
openalgo/replay/recorder.py
───────────────────────────
Records live market ticks and depth into Parquet storage for replay.

Recording is tick-driven: `maybe_record_tick` is called from the websocket_proxy
tick loop for every live tick and buffers the ones belonging to an armed symbol.
A single shared flusher thread drains every buffer to disk — never one thread per
symbol (see `.claude/rules/backend.md`: threads are shared module-level singletons).
"""

import os
import threading
import time
from datetime import datetime
from datetime import time as dtime

import pytz

from replay.dal import (
    find_nearest_nifty_expiry,
    nearest_thursday_expiry,
    nifty_option_symbols,
)
from replay.storage import MAX_DEPTH_LEVELS, save_events
from utils.logging import get_logger

logger = get_logger(__name__)
IST = pytz.timezone("Asia/Kolkata")

_lock = threading.RLock()
_active_recorders: dict[str, dict] = {}

# Cache of RecordingTarget rows so the per-tick hot path (websocket_proxy's
# zmq_listener, potentially a different process than the Flask blueprint that
# writes targets) doesn't hit SQLite on every tick.
_targets_cache: dict = {"loaded_at": 0.0, "date": None, "keys": set()}
_targets_cache_lock = threading.Lock()
_TARGETS_CACHE_TTL_SECONDS = 2.0

# Flush interval. The old 0.5s was paired with a full-file rewrite per flush;
# storage is append-only now, but a larger window still means fewer, fatter
# part files, which read back faster.
FLUSH_INTERVAL_SECONDS = float(os.getenv("REPLAY_FLUSH_INTERVAL", "3.0"))

# Recording is gated to the trading session so an idle overnight feed cannot
# pad the tape with stale repeats. Widened slightly on both sides to catch
# pre-open and the post-close settlement prints.
SESSION_START = dtime(9, 0)
SESSION_END = dtime(15, 45)
RECORD_ALL_HOURS = os.getenv("REPLAY_RECORD_ALL_HOURS", "false").lower() == "true"


def is_market_hours(now: datetime | None = None) -> bool:
    """True during the IST trading session on a weekday."""
    if RECORD_ALL_HOURS:
        return True
    now = now or datetime.now(IST)
    if now.weekday() >= 5:  # Saturday, Sunday
        return False
    return SESSION_START <= now.time() <= SESSION_END


class ReplayRecorder:
    """
    Per symbol-day buffer. Owns no thread: `_flusher_loop` drains it.
    """

    def __init__(self, symbol: str, exchange: str, date_str: str | None = None):
        self.symbol = symbol.upper()
        self.exchange = exchange.upper()
        self.date_str = date_str or datetime.now(IST).strftime("%Y-%m-%d")
        self.buffer: list[dict] = []
        self.running = False
        self._buf_lock = threading.Lock()

    def start(self):
        with self._buf_lock:
            if self.running:
                return
            self.running = True
        _ensure_flusher_running()
        logger.info(
            f"[ReplayRecorder] Started recording {self.exchange}:{self.symbol} for date {self.date_str}"
        )

    def push_event(self, event_dict: dict):
        """Push a normalized tick/depth event into the buffer."""
        with self._buf_lock:
            self.buffer.append(event_dict)

    def flush(self):
        with self._buf_lock:
            if not self.buffer:
                return
            events_to_save = self.buffer
            self.buffer = []
        try:
            save_events(self.symbol, self.date_str, events_to_save)
        except Exception:
            logger.exception(f"[ReplayRecorder] Error flushing events for {self.symbol}")

    def stop(self):
        with self._buf_lock:
            self.running = False
        self.flush()
        logger.info(f"[ReplayRecorder] Stopped recording {self.exchange}:{self.symbol}")


# ── Single shared flusher thread ────────────────────────────────────────
# One thread drains every recorder. The previous design started a thread per
# symbol, so an armed NIFTY chain meant ~90 threads each waking twice a second.

_flusher_thread: threading.Thread | None = None
_flusher_lock = threading.Lock()
_flusher_stop = threading.Event()


def _flusher_loop():
    while not _flusher_stop.is_set():
        _flusher_stop.wait(FLUSH_INTERVAL_SECONDS)
        if _flusher_stop.is_set():
            break
        try:
            with _lock:
                recorders = [info["recorder"] for info in _active_recorders.values()]
            for rec in recorders:
                try:
                    rec.flush()
                except Exception:
                    logger.exception(f"[ReplayRecorder] Flush failed for {rec.symbol}")
        except Exception:
            logger.exception("[ReplayRecorder] Flusher loop error")


def _ensure_flusher_running():
    global _flusher_thread
    with _flusher_lock:
        if _flusher_thread is not None and _flusher_thread.is_alive():
            return
        _flusher_stop.clear()
        _flusher_thread = threading.Thread(
            target=_flusher_loop, daemon=True, name="ReplayRecorderFlusher"
        )
        _flusher_thread.start()


def stop_flusher():
    """Stop the shared flusher thread after draining every buffer."""
    global _flusher_thread
    _flusher_stop.set()
    with _flusher_lock:
        thread = _flusher_thread
        _flusher_thread = None
    if thread:
        thread.join(timeout=FLUSH_INTERVAL_SECONDS + 2.0)
    with _lock:
        recorders = [info["recorder"] for info in _active_recorders.values()]
    for rec in recorders:
        try:
            rec.flush()
        except Exception:
            logger.exception(f"[ReplayRecorder] Final flush failed for {rec.symbol}")


def expand_live_nifty_options(symbols: list[dict], date_str: str) -> list[dict]:
    """
    If 'NIFTY' is in symbols, resolve the live ATM strike and the nearest listed
    expiry, then append the surrounding option contracts as NFO entries.
    """
    expanded = list(symbols)
    if not any(item["symbol"].upper() == "NIFTY" for item in symbols):
        return expanded

    try:
        target_dt = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        logger.error(f"[expand_live_nifty_options] Invalid date: {date_str}")
        return expanded

    atm_strike = 24000.0  # fallback
    try:
        from services.quotes_service import get_quotes
        quote = get_quotes("NIFTY", "NSE_INDEX")
        ltp = (quote or {}).get("ltp")
        if ltp and float(ltp) > 0:
            atm_strike = round(float(ltp) / 50.0) * 50
    except Exception:
        logger.exception("[expand_live_nifty_options] Error fetching Nifty live quote")

    expiry_str = find_nearest_nifty_expiry(target_dt) or nearest_thursday_expiry(target_dt)

    seen = {item["symbol"].upper() for item in expanded}
    for sym in nifty_option_symbols(atm_strike, expiry_str):
        if sym not in seen:
            expanded.append({"symbol": sym, "exchange": "NFO"})
            seen.add(sym)
    return expanded


def push_live_event_to_recorders(symbol: str, exchange: str, event_dict: dict):
    key = f"{exchange.upper()}:{symbol.upper()}"
    with _lock:
        rec_info = _active_recorders.get(key)
    if rec_info:
        rec_info["recorder"].push_event(event_dict)


def _stop_recorder(key: str):
    with _lock:
        info = _active_recorders.pop(key, None)
    if info:
        try:
            info["recorder"].stop()
        except Exception:
            logger.exception(f"[ReplayRecorder] Error stopping recorder for {key}")


def stop_session_recording() -> dict:
    with _lock:
        keys = list(_active_recorders)
    for key in keys:
        _stop_recorder(key)
    return {"status": "stopped"}


# ── Cross-process recording targets ─────────────────────────────────────
# Recording is driven by rows in RecordingTarget (SQLite) rather than
# in-memory state, because in production the websocket_proxy tick loop that
# actually sees live ticks runs as a separate subprocess from the Flask
# blueprint that starts/stops recording (see CLAUDE.md eventlet/gunicorn
# note). Both processes talk to the same NullPool sqlite DB.

def add_recording_targets(symbols: list[dict], date_str: str | None = None) -> dict:
    """
    Register a watchlist of symbols to record for date_str (today by default).
    symbols: list of {"symbol": str, "exchange": str}
    """
    from sqlalchemy.exc import IntegrityError

    from replay.metadata_db import RecordingTarget, SessionLocal

    d_str = date_str or datetime.now(IST).strftime("%Y-%m-%d")
    expanded_symbols = expand_live_nifty_options(symbols, d_str)

    db = SessionLocal()
    try:
        existing = {
            f"{r.exchange}:{r.symbol}"
            for r in db.query(RecordingTarget).filter_by(date=d_str).all()
        }
        added = 0
        for item in expanded_symbols:
            sym = item["symbol"].upper()
            ex = item["exchange"].upper()
            key = f"{ex}:{sym}"
            if key in existing:
                continue
            db.add(RecordingTarget(symbol=sym, exchange=ex, date=d_str))
            existing.add(key)
            added += 1
        try:
            db.commit()
        except IntegrityError:
            # The unique index caught a row another process inserted between our
            # read and our commit. The end state is the same either way.
            db.rollback()
            added = 0
    finally:
        db.close()

    with _targets_cache_lock:
        _targets_cache["loaded_at"] = 0.0  # force refresh on next tick

    return {"status": "recording", "count": len(expanded_symbols), "added": added, "date": d_str}


def remove_recording_targets(symbols: list[dict] | None = None, date_str: str | None = None) -> dict:
    """
    Stop recording. If symbols is None, removes every target for date_str
    (today by default). Also stops+flushes any in-process recorder for the
    removed keys in *this* process (the proxy process reconciles its own
    recorders against the target list on its next poll).
    """
    from replay.metadata_db import RecordingTarget, SessionLocal

    d_str = date_str or datetime.now(IST).strftime("%Y-%m-%d")
    db = SessionLocal()
    try:
        q = db.query(RecordingTarget).filter_by(date=d_str)
        if symbols:
            keys = {f"{s['exchange'].upper()}:{s['symbol'].upper()}" for s in symbols}
            rows = [r for r in q.all() if f"{r.exchange}:{r.symbol}" in keys]
        else:
            rows = q.all()
        removed_keys = {f"{r.exchange}:{r.symbol}" for r in rows}
        for r in rows:
            db.delete(r)
        db.commit()
    finally:
        db.close()

    with _targets_cache_lock:
        _targets_cache["loaded_at"] = 0.0

    for key in removed_keys:
        _stop_recorder(key)

    return {"status": "stopped", "count": len(removed_keys), "date": d_str}


def list_recording_targets(date_str: str | None = None) -> list[dict]:
    from replay.metadata_db import RecordingTarget, SessionLocal

    d_str = date_str or datetime.now(IST).strftime("%Y-%m-%d")
    db = SessionLocal()
    try:
        rows = db.query(RecordingTarget).filter_by(date=d_str).all()
        return [{"symbol": r.symbol, "exchange": r.exchange, "date": r.date} for r in rows]
    finally:
        db.close()


def prune_recording_targets(days_to_keep: int = 7) -> int:
    """
    Drop RecordingTarget rows for past dates. They are re-armed daily from the
    auto-record list, so without this the table grows without bound and every
    date-filtered read scans more dead rows.
    """
    from datetime import timedelta

    from replay.metadata_db import RecordingTarget, SessionLocal

    cutoff = (datetime.now(IST).date() - timedelta(days=days_to_keep)).strftime("%Y-%m-%d")
    db = SessionLocal()
    try:
        removed = db.query(RecordingTarget).filter(RecordingTarget.date < cutoff).delete(
            synchronize_session=False
        )
        db.commit()
        return removed or 0
    except Exception:
        db.rollback()
        logger.exception("[ReplayRecorder] Error pruning old recording targets")
        return 0
    finally:
        db.close()


def _active_target_keys(date_str: str) -> set:
    from replay.metadata_db import RecordingTarget, SessionLocal

    now = time.time()
    with _targets_cache_lock:
        if _targets_cache["date"] == date_str and (now - _targets_cache["loaded_at"]) < _TARGETS_CACHE_TTL_SECONDS:
            return _targets_cache["keys"]

    db = SessionLocal()
    try:
        rows = (
            db.query(RecordingTarget.symbol, RecordingTarget.exchange)
            .filter_by(date=date_str)
            .all()
        )
        keys = {f"{ex}:{sym}" for sym, ex in rows}
    finally:
        db.close()

    with _targets_cache_lock:
        _targets_cache.update(loaded_at=now, date=date_str, keys=keys)
    return keys


# ── Persistent auto-record list ─────────────────────────────────────────
# A one-time "always record these" list (no date), re-armed into today's
# RecordingTarget rows automatically so the user doesn't have to push Start
# Recording every trading day.

_auto_ensure_cache = {"date": None}
_auto_ensure_lock = threading.Lock()


def add_auto_record_symbols(symbols: list[dict]) -> dict:
    """symbols: list of {"symbol": str, "exchange": str}. Persists forever."""
    from sqlalchemy.exc import IntegrityError

    from replay.metadata_db import AutoRecordSymbol, SessionLocal

    db = SessionLocal()
    try:
        existing = {
            f"{r.exchange}:{r.symbol}" for r in db.query(AutoRecordSymbol).all()
        }
        added = 0
        for item in symbols:
            sym = str(item.get("symbol", "")).upper().strip()
            ex = str(item.get("exchange", "")).upper().strip()
            if not sym or not ex:
                continue
            key = f"{ex}:{sym}"
            if key in existing:
                continue
            db.add(AutoRecordSymbol(symbol=sym, exchange=ex))
            existing.add(key)
            added += 1
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            added = 0
    finally:
        db.close()

    with _auto_ensure_lock:
        _auto_ensure_cache["date"] = None  # force re-arm on next tick

    return {"status": "ok", "added": added}


def remove_auto_record_symbols(symbols: list[dict] | None = None) -> dict:
    """symbols: list of {"symbol","exchange"}; omit to clear the whole list."""
    from replay.metadata_db import AutoRecordSymbol, SessionLocal

    db = SessionLocal()
    try:
        q = db.query(AutoRecordSymbol)
        if symbols:
            keys = {f"{s['exchange'].upper()}:{s['symbol'].upper()}" for s in symbols}
            rows = [r for r in q.all() if f"{r.exchange}:{r.symbol}" in keys]
        else:
            rows = q.all()
        removed = len(rows)
        for r in rows:
            db.delete(r)
        db.commit()
    finally:
        db.close()

    # Removing from the persistent list also disarms today, otherwise the symbol
    # keeps recording until midnight.
    if removed:
        try:
            remove_recording_targets(symbols)
        except Exception:
            logger.exception("[ReplayRecorder] Error disarming removed auto-record symbols")

    return {"status": "ok", "removed": removed}


def list_auto_record_symbols() -> list[dict]:
    from replay.metadata_db import AutoRecordSymbol, SessionLocal

    db = SessionLocal()
    try:
        rows = db.query(AutoRecordSymbol).order_by(
            AutoRecordSymbol.exchange, AutoRecordSymbol.symbol
        ).all()
        return [{"symbol": r.symbol, "exchange": r.exchange} for r in rows]
    finally:
        db.close()


def ensure_auto_targets_for_today() -> None:
    """
    Idempotent, self-throttled to once per calendar date: reconciles today's
    RecordingTarget rows against the persistent auto-record list, and prunes
    stale rows from past dates. Called from the proxy's recording-sync loop, so
    it must never raise and must be cheap to call repeatedly.

    Reconcile, not just add. The auto-record list is the only arming mechanism,
    so a target with no entry behind it is a leftover that keeps consuming a
    broker subscription slot for a symbol nobody asked to record.
    """
    date_str = datetime.now(IST).strftime("%Y-%m-%d")
    with _auto_ensure_lock:
        if _auto_ensure_cache["date"] == date_str:
            return
        _auto_ensure_cache["date"] = date_str

    try:
        symbols = list_auto_record_symbols()
        if symbols:
            add_recording_targets(symbols, date_str)

        # Drop anything armed today that the auto-record list no longer covers.
        # The list is compared post-expansion, since arming NIFTY legitimately
        # creates option-chain targets that are not themselves list entries.
        wanted = {
            f"{item['exchange'].upper()}:{item['symbol'].upper()}"
            for item in (expand_live_nifty_options(symbols, date_str) if symbols else [])
        }
        stale = [
            {"symbol": row["symbol"], "exchange": row["exchange"]}
            for row in list_recording_targets(date_str)
            if f"{row['exchange']}:{row['symbol']}" not in wanted
        ]
        if stale:
            logger.info(
                f"[ReplayRecorder] Disarming {len(stale)} target(s) no longer on the auto-record list"
            )
            remove_recording_targets(stale, date_str)

        prune_recording_targets()
    except Exception:
        logger.exception("[ReplayRecorder] Error arming auto-record targets for today")
        with _auto_ensure_lock:
            _auto_ensure_cache["date"] = None  # retry on next call


def _normalize_tick_event(market_data: dict) -> dict | None:
    ltp = market_data.get("ltp")
    if ltp is None:
        return None

    ts_ms = market_data.get("timestamp")
    if ts_ms:
        try:
            ts = datetime.fromtimestamp(int(ts_ms) / 1000, tz=IST)
        except (TypeError, ValueError, OSError, OverflowError):
            ts = datetime.now(IST)
    else:
        ts = datetime.now(IST)

    event: dict = {
        "timestamp": ts.isoformat(),
        "ltp": ltp,
        "volume": market_data.get("volume"),
        "oi": market_data.get("oi"),
    }

    depth = market_data.get("depth") or {}
    buys = depth.get("buy") or []
    sells = depth.get("sell") or []

    # Emit only the levels the broker actually sent. Writing a fixed 20 levels
    # for a 5-level feed quadrupled file size with all-null columns; the reader
    # aligns frames by column name, so parts with different level counts merge
    # cleanly.
    levels = min(MAX_DEPTH_LEVELS, max(len(buys), len(sells)))
    for i in range(levels):
        b = buys[i] if i < len(buys) else {}
        s = sells[i] if i < len(sells) else {}
        event[f"bid_{i + 1}"] = b.get("price")
        event[f"bid_qty_{i + 1}"] = b.get("quantity")
        event[f"ask_{i + 1}"] = s.get("price")
        event[f"ask_qty_{i + 1}"] = s.get("quantity")

    return event


def maybe_record_tick(symbol: str, exchange: str, market_data: dict):
    """
    Called from the websocket_proxy tick loop for every live tick. Cheap
    no-op unless this symbol/exchange has an active RecordingTarget for
    today; reconciles start/stop/date-rollover against the target list as it goes.
    """
    now = datetime.now(IST)
    date_str = now.strftime("%Y-%m-%d")
    key = f"{exchange.upper()}:{symbol.upper()}"

    with _lock:
        info = _active_recorders.get(key)

    # Roll over at midnight. Without this the recorder keeps its construction-time
    # date forever and day two of a session appends into day one's file.
    if info is not None and info["date"] != date_str:
        _stop_recorder(key)
        info = None

    if not is_market_hours(now):
        if info is not None:
            _stop_recorder(key)
        return

    if key not in _active_target_keys(date_str):
        if info is not None:
            _stop_recorder(key)
        return

    if info is None:
        rec = ReplayRecorder(symbol, exchange, date_str)
        rec.start()
        with _lock:
            # Another thread may have created it while we were outside the lock.
            existing = _active_recorders.get(key)
            if existing is None:
                _active_recorders[key] = {"recorder": rec, "date": date_str}
            else:
                rec.stop()

    event = _normalize_tick_event(market_data)
    if event is not None:
        push_live_event_to_recorders(symbol, exchange, event)
