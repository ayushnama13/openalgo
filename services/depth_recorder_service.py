"""
services/depth_recorder_service.py
────────────────────────────────────
Background service that connects to the OpenAlgo WebSocket proxy, subscribes
to Depth (mode 3) for one or more symbols, and writes every tick to the
depth_ticks SQLite table.

Design:
  - Single thread per recorder session (uses websockets sync-over-asyncio).
  - Batch-inserts ticks every FLUSH_INTERVAL_SECONDS to reduce SQLite I/O.
  - Exponential back-off reconnection (max 60 s) on any error.
  - Market-hours guard: only records between 9:15 and 15:30 IST on weekdays.
  - Exposes a simple start / stop / status API consumed by the blueprint.

Thread safety: a module-level lock protects the shared _state dict.
"""

import json
import os
import sys
import threading
import time
from datetime import datetime, time as dtime

# Use the unpatched OS threading module so recorder threads are real OS threads.
# Under gunicorn+eventlet, threading.Thread is monkey-patched to greenlets that
# share the eventlet event loop; asyncio.run() inside a greenlet deadlocks.
# original_threading.Thread gets a fresh OS thread with no loop attached.
if "eventlet" in sys.modules:
    import eventlet

    _original_threading = eventlet.patcher.original("threading")
else:
    _original_threading = threading

import pytz

from database.depth_recorder_db import (
    _now_ist,
    _safe_float,
    batch_save_ticks,
    get_active_configs,
    get_tick_count,
    upsert_config,
)
from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 31)  # 15:30 + 1 min grace

FLUSH_INTERVAL_SECONDS = float(os.getenv("DEPTH_RECORDER_FLUSH_INTERVAL", "1.0"))
RECONNECT_DELAY_BASE = 2   # seconds
RECONNECT_DELAY_MAX = 60   # seconds

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
_lock = threading.Lock()

# symbol_key -> { thread, stop_event, ticks_buffered, ticks_written, status, ... }
_recorders: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def start_recorder(symbol: str, exchange: str, api_key: str) -> dict:
    """
    Start a background depth recorder for symbol/exchange.
    Returns a status dict immediately — recording happens asynchronously.
    """
    symbol = symbol.upper()
    exchange = exchange.upper()
    key = f"{exchange}:{symbol}"

    with _lock:
        existing = _recorders.get(key)
        if existing and existing.get("status") == "recording":
            return {"status": "already_recording", "symbol": symbol, "exchange": exchange}

        stop_event = _original_threading.Event()
        state = {
            "symbol": symbol,
            "exchange": exchange,
            "api_key": api_key,
            "status": "starting",
            "ticks_written": 0,
            "ticks_buffered": 0,
            "last_tick_time": None,
            "connected": False,
            "error": None,
            "stop_event": stop_event,
            "started_at": _now_ist().isoformat(),
        }
        _recorders[key] = state

    upsert_config(symbol, exchange, "active")

    thread = _original_threading.Thread(
        target=_recorder_loop,
        args=(key, symbol, exchange, api_key, stop_event),
        daemon=True,
        name=f"DepthRecorder-{key}",
    )
    with _lock:
        _recorders[key]["thread"] = thread
    thread.start()

    logger.info(f"[DepthRecorder] Started for {key}")
    return {"status": "started", "symbol": symbol, "exchange": exchange}


def stop_recorder(symbol: str, exchange: str) -> dict:
    """
    Signal the recorder for symbol/exchange to stop.
    Returns immediately; the thread will stop within FLUSH_INTERVAL_SECONDS.
    """
    symbol = symbol.upper()
    exchange = exchange.upper()
    key = f"{exchange}:{symbol}"

    with _lock:
        state = _recorders.get(key)
        if not state:
            return {"status": "not_running", "symbol": symbol, "exchange": exchange}
        state["stop_event"].set()
        state["status"] = "stopping"

    upsert_config(symbol, exchange, "stopped")
    logger.info(f"[DepthRecorder] Stop requested for {key}")
    return {"status": "stopping", "symbol": symbol, "exchange": exchange}


def get_status(symbol: str | None = None, exchange: str | None = None) -> dict | list:
    """
    Return status for one or all recorders.
    If symbol is None, returns a list of all statuses.
    """
    with _lock:
        if symbol:
            key = f"{exchange.upper()}:{symbol.upper()}"
            state = _recorders.get(key, {})
            return _state_to_dict(state)
        return [_state_to_dict(s) for s in _recorders.values()]


def get_all_statuses() -> list[dict]:
    return get_status()


def restore_active_recorders():
    """
    Called on boot by app.py after DB tables are ready.
    Re-starts any recorders that were 'active' in the config table.
    The API key must be resolvable from the current broker session.
    """
    try:
        configs = get_active_configs()
        if not configs:
            return

        from database.auth_db import get_api_key_for_tradingview

        # Try to resolve API key for the logged-in user
        # (There's only one user in OpenAlgo, fetch from session / first available)
        from database.user_db import UserDetails
        from database.user_db import db_session as user_session

        user = user_session.query(UserDetails).first()
        if not user:
            logger.info("[DepthRecorder] No user found — skipping auto-restore")
            return

        api_key = get_api_key_for_tradingview(user.username)
        if not api_key:
            logger.info("[DepthRecorder] No API key found — skipping auto-restore")
            return

        for cfg in configs:
            try:
                start_recorder(cfg["symbol"], cfg["exchange"], api_key)
                logger.info(
                    f"[DepthRecorder] Auto-restored: {cfg['exchange']}:{cfg['symbol']}"
                )
            except Exception as exc:
                logger.error(
                    f"[DepthRecorder] Auto-restore failed for "
                    f"{cfg['exchange']}:{cfg['symbol']}: {exc}"
                )
    except Exception:
        logger.exception("[DepthRecorder] restore_active_recorders error")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _state_to_dict(state: dict) -> dict:
    return {
        "symbol": state.get("symbol"),
        "exchange": state.get("exchange"),
        "status": state.get("status"),
        "ticks_written": state.get("ticks_written", 0),
        "ticks_buffered": state.get("ticks_buffered", 0),
        "last_tick_time": state.get("last_tick_time"),
        "connected": state.get("connected", False),
        "error": state.get("error"),
        "started_at": state.get("started_at"),
    }


def _is_market_hours() -> bool:
    """Return True during NSE trading hours on weekdays (IST)."""
    now = datetime.now(IST)
    if now.weekday() >= 5:  # Saturday/Sunday
        return False
    t = now.time()
    return MARKET_OPEN <= t <= MARKET_CLOSE


def _get_ws_url() -> str:
    return os.getenv("WEBSOCKET_URL", "ws://127.0.0.1:8765")


def _normalise_depth_payload(data: dict, symbol: str, exchange: str) -> dict | None:
    """
    Extract depth fields from a WebSocket market_data packet.
    Returns a row-dict ready for batch_save_ticks(), or None if irrelevant.
    """
    if data.get("type") != "market_data":
        return None

    pkt_symbol = str(data.get("symbol", "")).upper().split(":")[0]  # strip :50 suffix
    pkt_exchange = str(data.get("exchange", "")).upper()
    if pkt_symbol != symbol or pkt_exchange != exchange:
        return None

    mode = data.get("mode")
    if mode != 3:
        # We subscribed with mode=Depth; ignore LTP/Quote packets that may
        # arrive on the same connection for other symbols.
        return None

    md = data.get("data") or {}

    # Normalise bids / asks from two possible formats:
    # 1. md["depth"]["buy"] / md["depth"]["sell"]
    # 2. md["bids"] / md["asks"]
    depth_obj = md.get("depth") or {}
    if isinstance(depth_obj, dict):
        buy_levels = depth_obj.get("buy", [])
        sell_levels = depth_obj.get("sell", [])
    else:
        buy_levels, sell_levels = [], []

    bids = buy_levels or md.get("bids", [])
    asks = sell_levels or md.get("asks", [])

    def _lv(levels, i, key):
        try:
            return _safe_float(levels[i].get(key))
        except (IndexError, TypeError):
            return None

    now = _now_ist()
    return {
        "symbol": symbol,
        "exchange": exchange,
        "tick_time": now,
        "ltp": _safe_float(md.get("ltp")),
        "volume": _safe_float(md.get("volume")),
        "oi": _safe_float(md.get("oi")),
        "total_buy_qty": _safe_float(md.get("total_buy_qty")),
        "total_sell_qty": _safe_float(md.get("total_sell_qty")),
        "bid1_price": _lv(bids, 0, "price"),
        "bid1_qty": _lv(bids, 0, "quantity"),
        "bid2_price": _lv(bids, 1, "price"),
        "bid2_qty": _lv(bids, 1, "quantity"),
        "bid3_price": _lv(bids, 2, "price"),
        "bid3_qty": _lv(bids, 2, "quantity"),
        "bid4_price": _lv(bids, 3, "price"),
        "bid4_qty": _lv(bids, 3, "quantity"),
        "bid5_price": _lv(bids, 4, "price"),
        "bid5_qty": _lv(bids, 4, "quantity"),
        "ask1_price": _lv(asks, 0, "price"),
        "ask1_qty": _lv(asks, 0, "quantity"),
        "ask2_price": _lv(asks, 1, "price"),
        "ask2_qty": _lv(asks, 1, "quantity"),
        "ask3_price": _lv(asks, 2, "price"),
        "ask3_qty": _lv(asks, 2, "quantity"),
        "ask4_price": _lv(asks, 3, "price"),
        "ask4_qty": _lv(asks, 3, "quantity"),
        "ask5_price": _lv(asks, 4, "price"),
        "ask5_qty": _lv(asks, 4, "quantity"),
        "raw_json": None,  # Skip raw payload storage to save space; set True to enable
    }


# ---------------------------------------------------------------------------
# Recorder loop (runs in its own daemon thread)
# ---------------------------------------------------------------------------
def _recorder_loop(
    key: str,
    symbol: str,
    exchange: str,
    api_key: str,
    stop_event: threading.Event,
):
    """
    Main thread body.  Runs an asyncio event loop internally so we can use
    the `websockets` library (already a project dependency) rather than
    the blocking websocket-client.  On any error uses exponential back-off.
    """
    import asyncio

    import websockets as _ws

    delay = RECONNECT_DELAY_BASE

    async def _run():
        nonlocal delay
        ws_url = _get_ws_url()
        logger.info(f"[DepthRecorder:{key}] Connecting to {ws_url}")

        with _lock:
            s = _recorders.get(key, {})
            s["status"] = "connecting"
            s["error"] = None

        tick_buffer: list[dict] = []
        last_flush = asyncio.get_running_loop().time()

        async with _ws.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
            # Auth
            await ws.send(json.dumps({"action": "authenticate", "api_key": api_key}))
            auth_raw = await asyncio.wait_for(ws.recv(), timeout=10)
            auth_resp = json.loads(auth_raw)
            if auth_resp.get("status") != "success":
                raise RuntimeError(f"Auth failed: {auth_resp.get('message')}")

            with _lock:
                s = _recorders.get(key, {})
                s["status"] = "recording"
                s["connected"] = True

            logger.info(f"[DepthRecorder:{key}] Authenticated — subscribing Depth")

            await ws.send(json.dumps({
                "action": "subscribe",
                "symbols": [{"symbol": symbol, "exchange": exchange}],
                "mode": "Depth",
            }))

            while not stop_event.is_set():
                now_mono = asyncio.get_running_loop().time()
                if now_mono - last_flush >= FLUSH_INTERVAL_SECONDS and tick_buffer:
                    n = batch_save_ticks(tick_buffer)
                    with _lock:
                        s = _recorders.get(key, {})
                        s["ticks_written"] = s.get("ticks_written", 0) + n
                        s["ticks_buffered"] = 0
                    tick_buffer.clear()
                    last_flush = now_mono

                if not _is_market_hours():
                    logger.info(f"[DepthRecorder:{key}] Market closed — pausing")
                    break

                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=FLUSH_INTERVAL_SECONDS + 0.5)
                except asyncio.TimeoutError:
                    continue

                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                row = _normalise_depth_payload(data, symbol, exchange)
                if row is None:
                    continue

                tick_buffer.append(row)
                with _lock:
                    s = _recorders.get(key, {})
                    s["ticks_buffered"] = len(tick_buffer)
                    s["last_tick_time"] = row["tick_time"].isoformat()

        # Flush remaining
        if tick_buffer:
            n = batch_save_ticks(tick_buffer)
            with _lock:
                s = _recorders.get(key, {})
                s["ticks_written"] = s.get("ticks_written", 0) + n

    while not stop_event.is_set():
        if not _is_market_hours():
            with _lock:
                s = _recorders.get(key, {})
                s["status"] = "waiting_market_hours"
                s["connected"] = False
            stop_event.wait(60)
            continue

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(_run())
            finally:
                loop.close()
                asyncio.set_event_loop(None)
            delay = RECONNECT_DELAY_BASE
        except Exception as exc:
            err_msg = str(exc)
            logger.warning(f"[DepthRecorder:{key}] Error: {err_msg}")
            with _lock:
                s = _recorders.get(key, {})
                s["status"] = "reconnecting"
                s["connected"] = False
                s["error"] = err_msg

        if stop_event.is_set():
            break

        logger.info(f"[DepthRecorder:{key}] Reconnecting in {delay}s")
        stop_event.wait(delay)
        delay = min(delay * 2, RECONNECT_DELAY_MAX)

    with _lock:
        s = _recorders.get(key, {})
        s["status"] = "stopped"
        s["connected"] = False

    logger.info(f"[DepthRecorder:{key}] Stopped")

