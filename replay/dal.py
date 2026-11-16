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

from replay.storage import list_day_files, load_day_df_from_paths
from utils.logging import get_logger

logger = get_logger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# A full trading day for one liquid symbol is ~100k rows x ~84 float columns,
# roughly 60-70 MB in memory. Production is a single Gunicorn worker that never
# restarts, so this cache is deliberately small — the previous lru_cache(128)
# had an ~8 GB ceiling. Override with REPLAY_DAY_CACHE_SIZE if you have the RAM.
_DAY_CACHE_MAX = max(1, int(os.getenv("REPLAY_DAY_CACHE_SIZE", "6")))
_SLIM_CACHE_MAX = max(1, int(os.getenv("REPLAY_SLIM_CACHE_SIZE", "256")))

# Today's file is still being appended to, so its cache entry must expire quickly.
# Past days are immutable once the session ends and are cached until evicted.
_LIVE_DAY_TTL_SECONDS = 2.0

_day_cache: OrderedDict[tuple[str, str], tuple[float, pd.DataFrame, tuple[str, ...]]] = OrderedDict()
_slim_cache: OrderedDict[tuple[str, str], tuple[float, pd.DataFrame, tuple[str, ...]]] = OrderedDict()
_day_cache_lock = threading.Lock()
_expiry_cache: OrderedDict[str, tuple[float, str | None]] = OrderedDict()
_expiry_cache_lock = threading.Lock()
_EXPIRY_CACHE_MAX = 256
_EXPIRY_CACHE_TTL_SECONDS = 600.0


def _today_str() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def _cached_load(
    cache: OrderedDict, max_size: int, symbol: str, date_str: str,
    columns: list[str] | None = None,
) -> pd.DataFrame:
    """Bounded day cache shared by the full-frame and slim caches.

    Past days are immutable and served until evicted. Today's entry expires
    after the live TTL and is then refreshed incrementally: only part files
    that appeared since the last load are read.
    """
    key = (symbol.upper(), date_str)
    now = time.time()
    is_today = date_str == _today_str()

    with _day_cache_lock:
        entry = cache.get(key)
        if entry is not None:
            cache.move_to_end(key)
            loaded_at, df, paths = entry
            if not is_today or (now - loaded_at) < _LIVE_DAY_TTL_SECONDS:
                return df

    # Loaded outside the lock: reading a day can take seconds and must not block
    # every other reader. A concurrent duplicate load is wasteful but harmless.
    current_paths = tuple(list_day_files(*key))
    if entry is not None and set(entry[2]).issubset(current_paths):
        _, df, paths = entry
        seen = set(paths)
        new_paths = [path for path in current_paths if path not in seen]
        if new_paths:
            added = load_day_df_from_paths(key[0], date_str, new_paths, columns)
            df = _normalize_cached_frame(pd.concat([df, added], ignore_index=True))
    else:
        # First load, or a previously seen part vanished (compaction): full reload.
        df = load_day_df_from_paths(key[0], date_str, list(current_paths), columns)

    with _day_cache_lock:
        cache[key] = (now, df, current_paths)
        cache.move_to_end(key)
        while len(cache) > max_size:
            cache.popitem(last=False)

    return df


def _normalize_cached_frame(df: pd.DataFrame) -> pd.DataFrame:
    if "timestamp" not in df.columns:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    df = df.dropna(subset=["timestamp"])
    subset = [c for c in ("timestamp", "ltp", "volume") if c in df.columns]
    return df.drop_duplicates(subset=subset, keep="last").sort_values(
        "timestamp", kind="stable"
    ).reset_index(drop=True)


def _cached_load_day_df(symbol: str, date_str: str) -> pd.DataFrame:
    return _cached_load(_day_cache, _DAY_CACHE_MAX, symbol, date_str)


def invalidate_day(symbol: str, date_str: str) -> None:
    """Drop one symbol-day from the cache (called after every write)."""
    with _day_cache_lock:
        _day_cache.pop((symbol.upper(), date_str), None)
        _slim_cache.pop((symbol.upper(), date_str), None)


def clear_dal_cache() -> None:
    """Clear the in-memory cache for daily symbol dataframes."""
    with _day_cache_lock:
        _day_cache.clear()
        _slim_cache.clear()


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


def get_ltp_states(symbols: list[str], timestamp: datetime) -> dict[str, dict | None]:
    """Return slim point-in-time quote state for each symbol."""
    if timestamp.tzinfo is None:
        timestamp = IST.localize(timestamp)
    date_str = timestamp.astimezone(IST).strftime("%Y-%m-%d")
    result = {}
    columns = ["timestamp", "ltp", "volume", "oi"]
    for item in symbols:
        symbol = item.upper()
        df = _cached_load(_slim_cache, _SLIM_CACHE_MAX, symbol, date_str, columns)
        if df.empty or "timestamp" not in df.columns:
            result[symbol] = None
            continue
        ts_col = df["timestamp"]
        query_ts = _align_tz(ts_col, pd.to_datetime(timestamp))
        idx = int(ts_col.searchsorted(query_ts, side="right")) - 1
        if idx < 0:
            result[symbol] = None
            continue
        row = df.iloc[idx]
        state = {}
        for name in ("ltp", "volume", "oi"):
            value = row.get(name)
            # numpy scalars (int64 in particular) are not JSON serialisable
            state[name] = None if pd.isna(value) else float(value)
        state["timestamp"] = _to_ist_iso(row.get("timestamp"))
        result[symbol] = state
    return result


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
    columns: list[str] | None = None,
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
    selected = None
    if columns is not None:
        selected = ["timestamp"] + [name for name in columns if name in df.columns and name != "timestamp"]
    sub_df = df.iloc[lo:hi]
    if selected is not None:
        sub_df = sub_df[selected]

    if limit is not None and len(sub_df) > limit:
        step = len(sub_df) / float(limit)
        positions = sorted({int(i * step) for i in range(limit)} | {len(sub_df) - 1})
        sub_df = sub_df.iloc[positions]

    sub_df = sub_df.copy()
    ts_values = pd.to_datetime(sub_df["timestamp"], errors="coerce", utc=True).dt.tz_convert(IST)
    sub_df["timestamp"] = [value.isoformat() if pd.notna(value) else None for value in ts_values]
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
    cache_key = target_dt.strftime("%Y-%m-%d")
    now = time.time()
    with _expiry_cache_lock:
        cached = _expiry_cache.get(cache_key)
        if cached and now - cached[0] < _EXPIRY_CACHE_TTL_SECONDS:
            _expiry_cache.move_to_end(cache_key)
            return cached[1]

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

    # A miss usually means the symbol master is not loaded yet: do not pin it.
    if best_str is not None:
        with _expiry_cache_lock:
            _expiry_cache[cache_key] = (now, best_str)
            _expiry_cache.move_to_end(cache_key)
            while len(_expiry_cache) > _EXPIRY_CACHE_MAX:
                _expiry_cache.popitem(last=False)
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
