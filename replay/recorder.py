"""
openalgo/replay/recorder.py
───────────────────────────
Records live market ticks and depth into Parquet storage for replay.
"""

import threading
import time
from datetime import datetime, timedelta

import pytz

from replay.storage import save_events
from utils.logging import get_logger

logger = get_logger(__name__)
IST = pytz.timezone("Asia/Kolkata")

_lock = threading.Lock()
_active_recorders: dict[str, dict] = {}

# Cache of RecordingTarget rows so the per-tick hot path (websocket_proxy's
# zmq_listener, potentially a different process than the Flask blueprint that
# writes targets) doesn't hit SQLite on every tick.
_targets_cache: dict = {"loaded_at": 0.0, "date": None, "keys": set()}
_TARGETS_CACHE_TTL_SECONDS = 2.0


class ReplayRecorder:
    def __init__(self, symbol: str, exchange: str, date_str: str | None = None):
        self.symbol = symbol.upper()
        self.exchange = exchange.upper()
        self.date_str = date_str or datetime.now(IST).strftime("%Y-%m-%d")
        self.buffer: list[dict] = []
        self.running = False
        self.thread: threading.Thread | None = None
        self._buf_lock = threading.Lock()

    def start(self):
        with self._buf_lock:
            if self.running:
                return
            self.running = True
        self.thread = threading.Thread(target=self._flush_loop, daemon=True, name=f"ReplayRecorder-{self.symbol}")
        self.thread.start()
        logger.info(f"[ReplayRecorder] Started recording {self.exchange}:{self.symbol} for date {self.date_str}")

    def push_event(self, event_dict: dict):
        """
        Push a normalized tick/depth event into the buffer.
        Expected keys: timestamp, ltp, bid_1..5, bid_qty_1..5, ask_1..5, ask_qty_1..5, volume, oi
        """
        with self._buf_lock:
            self.buffer.append(event_dict)

    def _flush_loop(self):
        while self.running:
            time.sleep(0.5)
            self.flush()

    def flush(self):
        with self._buf_lock:
            if not self.buffer:
                return
            events_to_save = list(self.buffer)
            self.buffer.clear()
        try:
            save_events(self.symbol, self.date_str, events_to_save)
        except Exception as exc:
            logger.error(f"[ReplayRecorder] Error flushing events for {self.symbol}: {exc}")

    def stop(self):
        with self._buf_lock:
            self.running = False
        if self.thread:
            self.thread.join(timeout=2.0)
        self.flush()
        logger.info(f"[ReplayRecorder] Stopped recording {self.exchange}:{self.symbol}")


def _nearest_thursday_expiry(target_dt: datetime) -> str:
    """Nearest Thursday on/after target_dt, formatted like NSE weekly expiries (e.g. 21AUG26)."""
    days_ahead = (3 - target_dt.weekday()) % 7  # Monday=0 .. Thursday=3 .. Sunday=6
    expiry_dt = target_dt + timedelta(days=days_ahead)
    return expiry_dt.strftime("%d%b%y").upper()


def expand_live_nifty_options(symbols: list[dict], date_str: str) -> list[dict]:
    """
    If 'NIFTY' is in symbols, automatically find the Nifty ATM strike from live quote,
    find the nearest active option expiry from database, and construct option symbols
    for all strikes within +-500 points (inclusive, step 50) and append them as NFO contracts.
    """
    expanded = list(symbols)
    has_nifty = any(item["symbol"].upper() == "NIFTY" for item in symbols)
    if not has_nifty:
        return expanded

    # Step 1: Detect live Nifty spot price to determine ATM
    atm_strike = 24000.0  # fallback
    try:
        from services.quotes_service import get_quotes
        quote = get_quotes("NIFTY", "NSE_INDEX")
        if quote and "ltp" in quote:
            first_ltp = quote["ltp"]
            if first_ltp > 0:
                atm_strike = round(first_ltp / 50.0) * 50
    except Exception as exc:
        logger.error(f"[expand_live_nifty_options] Error fetching Nifty live quote: {exc}")

    # Step 2: Query database for nearest option expiry on or after current date
    import re

    from database.symbol import SymToken
    expiry_str = None
    try:
        target_dt = datetime.strptime(date_str, "%Y-%m-%d")
        q = SymToken.query.filter(SymToken.symbol.like('NIFTY%'), SymToken.instrumenttype == 'CE').all()
        n_opt = [s for s in q if re.match(r'^NIFTY\d{2}[A-Z]{3}\d{2}\d+CE$', s.symbol)]

        min_diff = None
        for row in n_opt:
            if not row.expiry:
                continue
            try:
                exp_dt = datetime.strptime(row.expiry.upper(), "%d-%b-%y")
                diff = (exp_dt - target_dt).days
                if diff >= 0:
                    if min_diff is None or diff < min_diff:
                        min_diff = diff
                        expiry_str = row.expiry.upper().replace("-", "")
            except Exception:
                continue
    except Exception as exc:
        logger.error(f"[expand_live_nifty_options] Error finding nearest expiry: {exc}")

    if not expiry_str:
        expiry_str = _nearest_thursday_expiry(target_dt)

    # Step 3: Generate CE/PE symbols ±500 points
    strikes = [atm_strike + i for i in range(-500, 501, 50)]
    for strike in strikes:
        ce_sym = f"NIFTY{expiry_str}{int(strike)}CE"
        pe_sym = f"NIFTY{expiry_str}{int(strike)}PE"

        # Add to expanded symbols
        if not any(item["symbol"].upper() == ce_sym for item in expanded):
            expanded.append({"symbol": ce_sym, "exchange": "NFO"})
        if not any(item["symbol"].upper() == pe_sym for item in expanded):
            expanded.append({"symbol": pe_sym, "exchange": "NFO"})

    return expanded


def start_session_recording(symbols: list[dict], date_str: str | None = None) -> dict:
    """
    Start recording a watchlist of symbols.
    symbols: list of {"symbol": str, "exchange": str}
    """
    d_str = date_str or datetime.now(IST).strftime("%Y-%m-%d")
    expanded_symbols = expand_live_nifty_options(symbols, d_str)
    with _lock:
        for item in expanded_symbols:
            sym = item["symbol"].upper()
            ex = item["exchange"].upper()
            key = f"{ex}:{sym}"
            if key not in _active_recorders:
                rec = ReplayRecorder(sym, ex, d_str)
                rec.start()
                _active_recorders[key] = {"recorder": rec, "date": d_str}
    return {"status": "started", "count": len(expanded_symbols), "date": d_str}


def stop_session_recording() -> dict:
    with _lock:
        for _key, info in _active_recorders.items():
            try:
                info["recorder"].stop()
            except Exception:
                pass
        _active_recorders.clear()
    return {"status": "stopped"}


def push_live_event_to_recorders(symbol: str, exchange: str, event_dict: dict):
    key = f"{exchange.upper()}:{symbol.upper()}"
    with _lock:
        rec_info = _active_recorders.get(key)
        if rec_info:
            rec_info["recorder"].push_event(event_dict)


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
    from replay.metadata_db import RecordingTarget, SessionLocal

    d_str = date_str or datetime.now(IST).strftime("%Y-%m-%d")
    expanded_symbols = expand_live_nifty_options(symbols, d_str)

    db = SessionLocal()
    try:
        added = 0
        for item in expanded_symbols:
            sym = item["symbol"].upper()
            ex = item["exchange"].upper()
            exists = db.query(RecordingTarget).filter_by(symbol=sym, exchange=ex, date=d_str).first()
            if not exists:
                db.add(RecordingTarget(symbol=sym, exchange=ex, date=d_str))
                added += 1
        db.commit()
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
        with _lock:
            info = _active_recorders.pop(key, None)
        if info:
            try:
                info["recorder"].stop()
            except Exception:
                logger.exception(f"[ReplayRecorder] Error stopping recorder for {key}")

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


_targets_cache_lock = threading.Lock()


def _active_target_keys(date_str: str) -> set:
    from replay.metadata_db import RecordingTarget, SessionLocal

    now = time.time()
    with _targets_cache_lock:
        if _targets_cache["date"] == date_str and (now - _targets_cache["loaded_at"]) < _TARGETS_CACHE_TTL_SECONDS:
            return _targets_cache["keys"]

    db = SessionLocal()
    try:
        rows = db.query(RecordingTarget).filter_by(date=date_str).all()
        keys = {f"{r.exchange}:{r.symbol}" for r in rows}
    finally:
        db.close()

    with _targets_cache_lock:
        _targets_cache.update(loaded_at=now, date=date_str, keys=keys)
    return keys


def _normalize_tick_event(market_data: dict) -> dict | None:
    ltp = market_data.get("ltp")
    if ltp is None:
        return None

    ts_ms = market_data.get("timestamp")
    if ts_ms:
        try:
            ts = datetime.fromtimestamp(int(ts_ms) / 1000, tz=IST)
        except Exception:
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
    for i in range(20):
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
    today; reconciles start/stop against the target list as it goes.
    """
    date_str = datetime.now(IST).strftime("%Y-%m-%d")
    key = f"{exchange.upper()}:{symbol.upper()}"
    active_keys = _active_target_keys(date_str)

    with _lock:
        is_recording = key in _active_recorders

    if key not in active_keys:
        if is_recording:
            with _lock:
                info = _active_recorders.pop(key, None)
            if info:
                try:
                    info["recorder"].stop()
                except Exception:
                    logger.exception(f"[ReplayRecorder] Error stopping recorder for {key}")
        return

    if not is_recording:
        rec = ReplayRecorder(symbol, exchange, date_str)
        rec.start()
        with _lock:
            _active_recorders[key] = {"recorder": rec, "date": date_str}

    event = _normalize_tick_event(market_data)
    if event is not None:
        push_live_event_to_recorders(symbol, exchange, event)
