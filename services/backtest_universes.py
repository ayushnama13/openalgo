"""Index universes for universe-mode backtests (NIFTY 50, NIFTY 100, sector indices, ...).

Constituents come from the bundled ``data/index_universes.json`` (NSE Indices
lists, with the date they were taken). They are the CURRENT members: a run over
past years on today's list carries survivorship bias, and every coverage
response says so, so the UI can show the caveat next to the results.
"""

from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytz

from database.historify_db import get_catalog_ranges, resolve_base_interval
from utils.logging import get_logger

logger = get_logger(__name__)

_UNIVERSE_FILE = Path(__file__).resolve().parent.parent / "data" / "index_universes.json"
_IST = pytz.timezone("Asia/Kolkata")

SURVIVORSHIP_NOTE = (
    "Constituents are the current index members, so delisted and removed stocks are "
    "missing from earlier years (survivorship bias): results look better than a "
    "point-in-time universe would."
)


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    try:
        return json.loads(_UNIVERSE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.exception(f"Could not read index universes from {_UNIVERSE_FILE}")
        return {"as_of": None, "source": None, "universes": []}


def list_universes() -> dict[str, Any]:
    """Summary of every universe: id, name, category, exchange, constituent count."""
    data = _load()
    return {
        "as_of": data.get("as_of"),
        "source": data.get("source"),
        "universes": [
            {
                "id": u["id"],
                "name": u["name"],
                "category": u.get("category", "broad"),
                "exchange": u.get("exchange", "NSE"),
                "count": len(u.get("constituents", [])),
            }
            for u in data.get("universes", [])
        ],
    }


def get_universe(universe_id: str) -> dict[str, Any] | None:
    """One universe with its constituents, or None for an unknown id."""
    wanted = (universe_id or "").strip().upper()
    for u in _load().get("universes", []):
        if u["id"] == wanted:
            return u
    return None


def universe_symbols(universe_id: str) -> tuple[dict[str, Any], list[str]]:
    """(universe, ["NSE:RELIANCE", ...]) for a universe id. Raises ValueError if unknown."""
    universe = get_universe(universe_id)
    if universe is None:
        raise ValueError(f"Unknown universe '{universe_id}'")
    exch = universe.get("exchange", "NSE")
    return universe, [f"{exch}:{c['symbol']}" for c in universe.get("constituents", [])]


def _fmt_ts(ts: Any) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), _IST).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return None


def universe_coverage(universe_id: str, interval: str = "D") -> dict[str, Any]:
    """Which constituents have Historify data for ``interval`` (checked on its base interval).

    Returns the universe, one row per constituent (symbol, name, industry,
    has_data, first, last, records) and a summary. Raises ValueError for an
    unknown universe.
    """
    universe, _ = universe_symbols(universe_id)
    base = resolve_base_interval(interval)
    ranges = get_catalog_ranges(universe.get("exchange", "NSE"), base)
    rows = []
    for c in universe.get("constituents", []):
        r = ranges.get(c["symbol"])
        rows.append({
            "symbol": c["symbol"],
            "name": c.get("name", ""),
            "industry": c.get("industry", ""),
            "has_data": bool(r and (r.get("record_count") or 0) > 0),
            "first": _fmt_ts(r["first_timestamp"]) if r else None,
            "last": _fmt_ts(r["last_timestamp"]) if r else None,
            "records": int(r["record_count"] or 0) if r else 0,
        })
    with_data = [r for r in rows if r["has_data"]]
    # The window every covered symbol shares: the latest first bar to the earliest last bar.
    common_first = max((r["first"] for r in with_data if r["first"]), default=None)
    common_last = min((r["last"] for r in with_data if r["last"]), default=None)
    data = _load()
    return {
        "id": universe["id"],
        "name": universe["name"],
        "category": universe.get("category", "broad"),
        "exchange": universe.get("exchange", "NSE"),
        "interval": interval,
        "base_interval": base,
        "as_of": data.get("as_of"),
        "source": data.get("source"),
        "survivorship_note": SURVIVORSHIP_NOTE,
        "count": len(rows),
        "with_data": len(with_data),
        "common_first": common_first,
        "common_last": common_last,
        "constituents": rows,
    }
