"""
openalgo/replay/dal.py
──────────────────────
Data Access Layer with O(log n) point-in-time lookup and LRU caching for replay.
"""

from datetime import datetime, timedelta
from functools import lru_cache

import pandas as pd
import pytz

from replay.storage import load_day_df
from utils.logging import get_logger

logger = get_logger(__name__)
IST = pytz.timezone("Asia/Kolkata")


@lru_cache(maxsize=128)
def _cached_load_day_df(symbol: str, date_str: str) -> pd.DataFrame:
    return load_day_df(symbol, date_str)


def clear_dal_cache():
    """Clear the in-memory LRU cache for daily symbol dataframes."""
    _cached_load_day_df.cache_clear()


def get_state(symbol: str, timestamp: datetime) -> dict | None:
    """
    O(log n) point-in-time lookup for symbol at timestamp using binary search.
    Returns the nearest snapshot row at or before timestamp.
    """
    if timestamp.tzinfo is None:
        timestamp = IST.localize(timestamp)
    date_str = timestamp.strftime("%Y-%m-%d")
    df = _cached_load_day_df(symbol.upper(), date_str)
    if df.empty or "timestamp" not in df.columns:
        return None

    ts_col = df["timestamp"]
    ts_dt = pd.to_datetime(timestamp)

    # Ensure tz-awareness alignment between column and query timestamp
    if getattr(ts_col.dt, "tz", None) is None and getattr(ts_dt, "tz", None) is not None:
        ts_dt = ts_dt.tz_localize(None)
    elif getattr(ts_col.dt, "tz", None) is not None and getattr(ts_dt, "tz", None) is None:
        ts_dt = pd.to_datetime(timestamp).tz_localize("UTC")

    # Binary search using searchsorted
    idx = ts_col.searchsorted(ts_dt, side="right") - 1
    if idx < 0:
        idx = 0
    if idx >= len(df):
        idx = len(df) - 1

    row = df.iloc[idx]
    record = row.to_dict()
    if pd.notnull(record.get("timestamp")):
        record["timestamp"] = pd.to_datetime(record["timestamp"]).isoformat()
    return record


def get_range(symbol: str, t_start: datetime, t_end: datetime) -> list[dict]:
    """
    Get a time range of tick/depth events for chart rendering or analysis.
    """
    if t_start.tzinfo is None:
        t_start = IST.localize(t_start)
    if t_end.tzinfo is None:
        t_end = IST.localize(t_end)

    date_str = t_start.strftime("%Y-%m-%d")
    df = _cached_load_day_df(symbol.upper(), date_str)
    if df.empty or "timestamp" not in df.columns:
        return []

    ts_col = df["timestamp"]
    ts_start = pd.to_datetime(t_start)
    ts_end = pd.to_datetime(t_end)

    if getattr(ts_col.dt, "tz", None) is None:
        if getattr(ts_start, "tz", None) is not None:
            ts_start = ts_start.tz_localize(None)
        if getattr(ts_end, "tz", None) is not None:
            ts_end = ts_end.tz_localize(None)

    mask = (ts_col >= ts_start) & (ts_col <= ts_end)
    sub_df = df[mask]

    results = []
    for _, row in sub_df.iterrows():
        d = row.to_dict()
        if pd.notnull(d.get("timestamp")):
            d["timestamp"] = pd.to_datetime(d["timestamp"]).isoformat()
        results.append(d)
    return results


def prefetch_symbol_date(symbol: str, date_str: str):
    """
    Prefetch a symbol day into memory cache.
    """
    _cached_load_day_df(symbol.upper(), date_str)


def expand_nifty_options(session_date_str: str, watchlist: list[str]) -> list[str]:
    """
    If 'NIFTY' is in watchlist, automatically find the Nifty ATM strike on that day,
    find the nearest active option expiry from database, and construct option symbols
    for all strikes within +-500 points (inclusive, step 50) and append them to the watchlist.
    """
    expanded = list(watchlist)
    has_nifty = any(x.upper() == "NIFTY" for x in watchlist)
    if not has_nifty:
        return expanded

    # Step 1: Detect ATM strike from recorded Nifty spot data
    atm_strike = 24000.0  # sensible default fallback
    try:
        df_nifty = _cached_load_day_df("NIFTY", session_date_str)
        if not df_nifty.empty and "ltp" in df_nifty.columns:
            first_ltp = df_nifty["ltp"].iloc[0]
            if pd.notnull(first_ltp) and first_ltp > 0:
                atm_strike = round(float(first_ltp) / 50.0) * 50
    except Exception as exc:
        logger.error(f"[expand_nifty_options] Error detecting ATM strike: {exc}")

    # Step 2: Query database for nearest option expiry on or after session_date
    import re

    from database.symbol import SymToken
    expiry_str = None
    try:
        target_dt = datetime.strptime(session_date_str, "%Y-%m-%d")
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
        logger.error(f"[expand_nifty_options] Error finding nearest expiry: {exc}")

    if not expiry_str:
        # No listed expiry found on/after target_dt (e.g. symbol master not
        # loaded yet) — fall back to the nearest Thursday, matching NSE's
        # weekly-expiry convention, instead of a fixed date that goes stale.
        days_ahead = (3 - target_dt.weekday()) % 7  # Monday=0 .. Thursday=3 .. Sunday=6
        expiry_str = (target_dt + timedelta(days=days_ahead)).strftime("%d%b%y").upper()

    # Step 3: Generate strikes within +-500 points
    strikes = [atm_strike + i for i in range(-500, 501, 50)]
    for strike in strikes:
        ce_sym = f"NIFTY{expiry_str}{int(strike)}CE"
        pe_sym = f"NIFTY{expiry_str}{int(strike)}PE"
        if ce_sym not in expanded:
            expanded.append(ce_sym)
        if pe_sym not in expanded:
            expanded.append(pe_sym)

    return expanded
