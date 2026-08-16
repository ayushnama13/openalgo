"""
openalgo/replay/dal.py
──────────────────────
Data Access Layer with O(log n) point-in-time lookup and a bounded, invalidatable
day cache for replay.
"""

import os
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta

import pandas as pd
import pytz

from replay.storage import load_day_df
from utils.logging import get_logger

logger = get_logger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# A full trading day for one liquid symbol is ~100k rows x ~84 float columns,
# roughly 60-70 MB in memory. Production is a single Gunicorn worker that never
# restarts, so this cache is deliberately small — the previous lru_cache(128)
# had an ~8 GB ceiling. Override with REPLAY_DAY_CACHE_SIZE if you have the RAM.
_DAY_CACHE_MAX = max(1, int(os.getenv("REPLAY_DAY_CACHE_SIZE", "6")))

# Today's file is still being appended to, so its cache entry must expire quickly.
# Past days are immutable once the session ends and are cached until evicted.
_LIVE_DAY_TTL_SECONDS = 2.0

_day_cache: OrderedDict[tuple[str, str], tuple[float, pd.DataFrame]] = OrderedDict()
_day_cache_lock = threading.Lock()


def _today_str() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def _cached_load_day_df(symbol: str, date_str: str) -> pd.DataFrame:
    key = (symbol.upper(), date_str)
    now = time.time()

    with _day_cache_lock:
        entry = _day_cache.get(key)
        if entry is not None:
            loaded_at, df = entry
            fresh = date_str != _today_str() or (now - loaded_at) < _LIVE_DAY_TTL_SECONDS
            if fresh:
                _day_cache.move_to_end(key)
                return df

    # Loaded outside the lock: reading a day can take seconds and must not block
    # every other reader. A concurrent duplicate load is wasteful but harmless.
    df = load_day_df(symbol.upper(), date_str)

    with _day_cache_lock:
        _day_cache[key] = (now, df)
        _day_cache.move_to_end(key)
        while len(_day_cache) > _DAY_CACHE_MAX:
            _day_cache.popitem(last=False)

    return df


def invalidate_day(symbol: str, date_str: str) -> None:
    """Drop one symbol-day from the cache (called after every write)."""
    with _day_cache_lock:
        _day_cache.pop((symbol.upper(), date_str), None)


def clear_dal_cache() -> None:
    """Clear the in-memory cache for daily symbol dataframes."""
    with _day_cache_lock:
        _day_cache.clear()


def _align_tz(ts_col: pd.Series, ts: pd.Timestamp) -> pd.Timestamp:
    """Make a query timestamp comparable with a stored timestamp column."""
    col_tz = getattr(ts_col.dt, "tz", None)
    if col_tz is None:
        return ts.tz_localize(None) if ts.tz is not None else ts
    if ts.tz is None:
        return ts.tz_localize(IST).tz_convert(col_tz)
    return ts.tz_convert(col_tz)


def get_state(symbol: str, timestamp: datetime) -> dict | None:
    """
    O(log n) point-in-time lookup for symbol at timestamp using binary search.
    Returns the nearest snapshot row at or before timestamp.
    """
    if timestamp.tzinfo is None:
        timestamp = IST.localize(timestamp)
    date_str = timestamp.astimezone(IST).strftime("%Y-%m-%d")
    df = _cached_load_day_df(symbol.upper(), date_str)
    if df.empty or "timestamp" not in df.columns:
        return None

    ts_col = df["timestamp"]
    ts_dt = _align_tz(ts_col, pd.to_datetime(timestamp))

    idx = ts_col.searchsorted(ts_dt, side="right") - 1
    if idx < 0:
        return None
    if idx >= len(df):
        idx = len(df) - 1

    record = df.iloc[idx].to_dict()
    record["timestamp"] = _to_ist_iso(record.get("timestamp"))
    return record


def _to_ist_iso(value) -> str | None:
    """
    Serialize a stored timestamp as IST. Timestamps are normalized to UTC on
    disk so parts from different sources compare correctly, but every consumer
    of replay (charts, the clock, the UI) works in IST.
    """
    if value is None or pd.isna(value):
        return None
    ts = pd.to_datetime(value)
    if ts.tz is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(IST).isoformat()


# A whole session for one symbol is ~100k rows x 84 columns. Serializing that
# unbounded produced multi-hundred-MB JSON responses, so range reads are capped
# and evenly downsampled past the cap rather than truncated (a truncated range
# silently misrepresents the tail of the window).
DEFAULT_RANGE_LIMIT = 20000


def get_range(
    symbol: str,
    t_start: datetime,
    t_end: datetime,
    limit: int | None = DEFAULT_RANGE_LIMIT,
) -> list[dict]:
    """
    Get a time range of tick/depth events for chart rendering or analysis.

    If the window holds more than `limit` rows, it is evenly downsampled to
    `limit` rows (first and last always retained). Pass limit=None for everything.
    """
    if t_start.tzinfo is None:
        t_start = IST.localize(t_start)
    if t_end.tzinfo is None:
        t_end = IST.localize(t_end)

    date_str = t_start.astimezone(IST).strftime("%Y-%m-%d")
    df = _cached_load_day_df(symbol.upper(), date_str)
    if df.empty or "timestamp" not in df.columns:
        return []

    ts_col = df["timestamp"]
    ts_start = _align_tz(ts_col, pd.to_datetime(t_start))
    ts_end = _align_tz(ts_col, pd.to_datetime(t_end))

    # The frame is sorted by timestamp, so slice by position instead of building
    # a full boolean mask over the day.
    lo = int(ts_col.searchsorted(ts_start, side="left"))
    hi = int(ts_col.searchsorted(ts_end, side="right"))
    sub_df = df.iloc[lo:hi]

    if limit is not None and len(sub_df) > limit:
        step = len(sub_df) / float(limit)
        positions = sorted({int(i * step) for i in range(limit)} | {len(sub_df) - 1})
        sub_df = sub_df.iloc[positions]

    sub_df = sub_df.copy()
    sub_df["timestamp"] = sub_df["timestamp"].map(_to_ist_iso)
    # to_dict("records") is an order of magnitude faster than iterrows(), which
    # builds a Series object per row.
    return sub_df.to_dict("records")


def prefetch_symbol_date(symbol: str, date_str: str):
    """
    Prefetch a symbol day into memory cache.
    """
    _cached_load_day_df(symbol.upper(), date_str)


NIFTY_OPTION_RE = re.compile(r"^NIFTY\d{2}[A-Z]{3}\d{2}\d+CE$")


def find_nearest_nifty_expiry(target_dt: datetime) -> str | None:
    """
    Nearest listed NIFTY option expiry on or after `target_dt`, formatted the way
    OpenAlgo symbols carry it (e.g. 21AUG26). Returns None when the symbol master
    holds no matching contract.

    Expiry is stored as text (`DD-MON-YY`), so it cannot be range-filtered in SQL.
    The query is narrowed to NFO CE contracts whose symbol starts with NIFTY and
    the date comparison happens in Python — previously this pulled every NIFTY CE
    row in the master and regexed the lot.
    """
    from database.symbol import SymToken

    best_dt: datetime | None = None
    best_str: str | None = None
    try:
        rows = (
            SymToken.query.with_entities(SymToken.symbol, SymToken.expiry)
            .filter(
                SymToken.symbol.like("NIFTY%"),
                SymToken.instrumenttype == "CE",
                SymToken.exchange == "NFO",
                SymToken.expiry.isnot(None),
                SymToken.expiry != "",
            )
            .all()
        )
    except Exception:
        logger.exception("[find_nearest_nifty_expiry] Symbol master query failed")
        return None

    for symbol, expiry in rows:
        if not NIFTY_OPTION_RE.match(symbol or ""):
            continue
        try:
            exp_dt = datetime.strptime(expiry.upper(), "%d-%b-%y")
        except (ValueError, AttributeError):
            continue
        if exp_dt < target_dt:
            continue
        if best_dt is None or exp_dt < best_dt:
            best_dt = exp_dt
            best_str = expiry.upper().replace("-", "")

    return best_str


def nearest_thursday_expiry(target_dt: datetime) -> str:
    """
    Nearest Thursday on/after target_dt, formatted like NSE weekly expiries
    (e.g. 21AUG26). Used only when the symbol master has nothing listed.
    """
    days_ahead = (3 - target_dt.weekday()) % 7  # Monday=0 .. Thursday=3 .. Sunday=6
    return (target_dt + timedelta(days=days_ahead)).strftime("%d%b%y").upper()


# The recorded option chain is capped so that arming "NIFTY" cannot silently
# consume the broker's whole subscription budget (MAX_SYMBOLS_PER_WEBSOCKET x
# MAX_WEBSOCKET_CONNECTIONS = 3000). +-500 points at a 50-point step is 21
# strikes, 42 contracts.
STRIKE_RANGE_POINTS = int(os.getenv("REPLAY_STRIKE_RANGE_POINTS", "500"))
STRIKE_STEP = 50


def nifty_option_symbols(atm_strike: float, expiry_str: str) -> list[str]:
    """CE and PE symbols around the ATM strike for one expiry."""
    symbols = []
    for offset in range(-STRIKE_RANGE_POINTS, STRIKE_RANGE_POINTS + 1, STRIKE_STEP):
        strike = int(atm_strike + offset)
        symbols.append(f"NIFTY{expiry_str}{strike}CE")
        symbols.append(f"NIFTY{expiry_str}{strike}PE")
    return symbols


def expand_nifty_options(session_date_str: str, watchlist: list[str]) -> list[str]:
    """
    If 'NIFTY' is in watchlist, find the Nifty ATM strike from that day's recorded
    tape, resolve the nearest listed expiry, and append the surrounding option
    contracts to the watchlist.
    """
    expanded = list(watchlist)
    if not any(x.upper() == "NIFTY" for x in watchlist):
        return expanded

    try:
        target_dt = datetime.strptime(session_date_str, "%Y-%m-%d")
    except ValueError:
        logger.error(f"[expand_nifty_options] Invalid session date: {session_date_str}")
        return expanded

    atm_strike = 24000.0  # sensible default fallback
    try:
        df_nifty = _cached_load_day_df("NIFTY", session_date_str)
        if not df_nifty.empty and "ltp" in df_nifty.columns:
            first_ltp = df_nifty["ltp"].iloc[0]
            if pd.notnull(first_ltp) and first_ltp > 0:
                atm_strike = round(float(first_ltp) / STRIKE_STEP) * STRIKE_STEP
    except Exception:
        logger.exception("[expand_nifty_options] Error detecting ATM strike")

    expiry_str = find_nearest_nifty_expiry(target_dt) or nearest_thursday_expiry(target_dt)

    seen = {x.upper() for x in expanded}
    for sym in nifty_option_symbols(atm_strike, expiry_str):
        if sym not in seen:
            expanded.append(sym)
            seen.add(sym)
    return expanded
