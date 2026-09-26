"""
openalgo/replay/storage.py
──────────────────────────
Append-only Parquet storage for tick and depth events.

Layout::

    data/replay/<SYMBOL>/<YYYY-MM-DD>/part-<nanos>-<pid>.parquet   (current)
    data/replay/<SYMBOL>/<YYYY-MM-DD>.parquet                      (legacy, read-only)

Writes are **append-only**: every flush drops a new part file and never touches
an existing one. The previous implementation read the whole day back, concatenated,
sorted, de-duplicated and rewrote the entire file on every 500ms flush, which is
O(n^2) in ticks and produced terabytes of write amplification over a session. It
also meant a crash mid-write destroyed the whole day.

Reads merge the legacy file (if present) with every part, sort by timestamp and
return one frame. `compact_day` folds the parts back into a single file once the
day is over.
"""

import os
import re
import threading
import time
from datetime import datetime, timedelta

import pandas as pd
import pytz

from replay.metadata_db import SessionLocal, SymbolDateIndex
from utils.logging import get_logger

logger = get_logger(__name__)
IST = pytz.timezone("Asia/Kolkata")

REPLAY_ROOT = os.path.join("data", "replay")

# Deepest order book any part of replay will read or write. Single source of
# truth shared by the recorder, the feed adapter and the fill simulator.
MAX_DEPTH_LEVELS = 20

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Serializes the read-modify-write of a SymbolDateIndex row within this process.
_index_lock = threading.Lock()


def get_symbol_dir(symbol: str) -> str:
    return os.path.join(REPLAY_ROOT, symbol.upper())


def get_parquet_path(symbol: str, date_str: str) -> str:
    """Legacy single-file path. Still read, never written to any more."""
    return os.path.join(get_symbol_dir(symbol), f"{date_str}.parquet")


def get_day_dir(symbol: str, date_str: str, create: bool = False) -> str:
    """Directory holding the append-only part files for one symbol-day."""
    path = os.path.join(get_symbol_dir(symbol), date_str)
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def _list_part_files(symbol: str, date_str: str) -> list[str]:
    day_dir = get_day_dir(symbol, date_str)
    if not os.path.isdir(day_dir):
        return []
    return sorted(
        os.path.join(day_dir, f)
        for f in os.listdir(day_dir)
        if f.endswith(".parquet")
    )


def save_events(symbol: str, date_str: str, events: list[dict]) -> None:
    """
    Append `events` as a new part file. Never rewrites existing data.
    """
    if not events:
        return

    day_dir = get_day_dir(symbol, date_str, create=True)
    df_new = pd.DataFrame(events)
    if "timestamp" in df_new.columns:
        df_new["timestamp"] = pd.to_datetime(df_new["timestamp"], errors="coerce", utc=True)
        df_new = df_new.dropna(subset=["timestamp"])
        if df_new.empty:
            return

    # Nanosecond clock + pid keeps part names unique across the Flask process
    # and the out-of-process websocket proxy without any coordination.
    part_name = f"part-{time.time_ns()}-{os.getpid()}.parquet"
    tmp_path = os.path.join(day_dir, f".{part_name}.tmp")
    final_path = os.path.join(day_dir, part_name)

    # Write to a dotfile first, then rename: readers only ever see complete parts.
    df_new.to_parquet(tmp_path, index=False)
    os.replace(tmp_path, final_path)

    _bump_index(symbol, date_str, day_dir, len(df_new))

    try:
        from replay.dal import invalidate_day
        invalidate_day(symbol, date_str)
    except Exception:
        logger.exception("[ReplayStorage] Failed to invalidate DAL cache")


def _bump_index(symbol: str, date_str: str, day_dir: str, added_rows: int) -> None:
    """Incrementally update the row count for a symbol-day, no file re-read."""
    with _index_lock:
        db = SessionLocal()
        try:
            row = db.query(SymbolDateIndex).filter_by(symbol=symbol.upper(), date=date_str).first()
            if row:
                row.row_count = (row.row_count or 0) + added_rows
                row.file_path = day_dir
            else:
                db.add(
                    SymbolDateIndex(
                        symbol=symbol.upper(),
                        date=date_str,
                        file_path=day_dir,
                        row_count=added_rows,
                    )
                )
            db.commit()
        except Exception:
            db.rollback()
            logger.exception(f"[ReplayStorage] Failed to update index for {symbol} {date_str}")
        finally:
            db.close()


def list_available(date_str: str | None = None) -> list[dict]:
    """List recorded symbol/date pairs (and row counts) from the index table."""
    db = SessionLocal()
    try:
        q = db.query(SymbolDateIndex)
        if date_str:
            q = q.filter_by(date=date_str)
        rows = q.order_by(SymbolDateIndex.date.desc(), SymbolDateIndex.symbol.asc()).all()
        return [
            {"symbol": r.symbol, "date": r.date, "row_count": r.row_count}
            for r in rows
        ]
    finally:
        db.close()


EMPTY_COLUMNS = ["timestamp", "ltp", "volume", "oi"] + [
    col
    for i in range(1, MAX_DEPTH_LEVELS + 1)
    for col in (f"bid_{i}", f"bid_qty_{i}", f"ask_{i}", f"ask_qty_{i}")
]


def load_day_df(symbol: str, date_str: str, columns: list[str] | None = None) -> pd.DataFrame:
    """
    Merge the legacy single file (if any) with every part file for the day.

    Parts may carry different depth-level columns (a broker that only returns
    5 levels writes 5), so frames are aligned by column name and missing levels
    come back as NaN.
    """
    frames: list[pd.DataFrame] = []

    legacy = get_parquet_path(symbol, date_str)
    paths = ([legacy] if os.path.exists(legacy) else []) + _list_part_files(symbol, date_str)

    for path in paths:
        try:
            frames.append(pd.read_parquet(path, columns=columns))
        except Exception:
            # A single corrupt part must not take out the whole day.
            logger.exception(f"[ReplayStorage] Skipping unreadable part {path}")

    if not frames:
        return pd.DataFrame(columns=columns or EMPTY_COLUMNS)

    df = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]

    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
        df = df.dropna(subset=["timestamp"])
        # Identical (timestamp, ltp, volume) rows are genuine duplicates from an
        # overlapping flush; two different ticks sharing a second are not, so the
        # de-dup key is never the timestamp alone.
        subset = [c for c in ("timestamp", "ltp", "volume") if c in df.columns]
        df = df.drop_duplicates(subset=subset, keep="last")
        df = df.sort_values("timestamp", kind="stable").reset_index(drop=True)

    return df


def compact_day(symbol: str, date_str: str) -> int:
    """
    Fold every part file (and the legacy file) for a symbol-day into a single
    part, then delete the originals. Safe to call on a live day, but intended
    for end-of-day. Returns the row count written, or -1 if nothing to do.
    """
    parts = _list_part_files(symbol, date_str)
    legacy = get_parquet_path(symbol, date_str)
    has_legacy = os.path.exists(legacy)

    if len(parts) <= 1 and not has_legacy:
        return -1

    df = load_day_df(symbol, date_str)
    if df.empty:
        return -1

    day_dir = get_day_dir(symbol, date_str, create=True)
    tmp_path = os.path.join(day_dir, ".compact.tmp")
    final_path = os.path.join(day_dir, f"part-{time.time_ns()}-{os.getpid()}-compacted.parquet")

    df.to_parquet(tmp_path, index=False)
    os.replace(tmp_path, final_path)

    for path in parts:
        try:
            os.remove(path)
        except OSError:
            logger.exception(f"[ReplayStorage] Failed to remove compacted part {path}")
    if has_legacy:
        try:
            os.remove(legacy)
        except OSError:
            logger.exception(f"[ReplayStorage] Failed to remove legacy file {legacy}")

    with _index_lock:
        db = SessionLocal()
        try:
            row = db.query(SymbolDateIndex).filter_by(symbol=symbol.upper(), date=date_str).first()
            if row:
                row.row_count = len(df)
                row.file_path = day_dir
                db.commit()
        except Exception:
            db.rollback()
            logger.exception(f"[ReplayStorage] Failed to update index after compaction of {symbol}")
        finally:
            db.close()

    try:
        from replay.dal import invalidate_day
        invalidate_day(symbol, date_str)
    except Exception:
        logger.exception("[ReplayStorage] Failed to invalidate DAL cache after compaction")

    return len(df)


def _remove_day_data(symbol: str, date_str: str) -> None:
    """Delete both the parts directory and the legacy file for a symbol-day."""
    day_dir = get_day_dir(symbol, date_str)
    if os.path.isdir(day_dir):
        for name in os.listdir(day_dir):
            try:
                os.remove(os.path.join(day_dir, name))
            except OSError:
                logger.exception(f"[ReplayStorage] Failed to remove {day_dir}/{name}")
        try:
            os.rmdir(day_dir)
        except OSError:
            pass

    legacy = get_parquet_path(symbol, date_str)
    if os.path.exists(legacy):
        try:
            os.remove(legacy)
        except OSError:
            logger.exception(f"[ReplayStorage] Failed to remove {legacy}")


def prune_old_replay_data(days_to_keep: int = 7) -> int:
    """
    Prune replay data older than `days_to_keep` (default 7 / weekly retention).

    Removes indexed symbol-days *and* orphans on disk — data written before the
    index row landed, or left behind by an interrupted write — so a failed index
    update can never leak a file that lives forever.
    """
    cutoff_str = (datetime.now(IST).date() - timedelta(days=days_to_keep)).strftime("%Y-%m-%d")
    removed_count = 0

    db = SessionLocal()
    try:
        old_rows = db.query(SymbolDateIndex).filter(SymbolDateIndex.date < cutoff_str).all()
        for r in old_rows:
            _remove_day_data(r.symbol, r.date)
            db.delete(r)
            removed_count += 1
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("[ReplayStorage] Error pruning indexed replay data")
    finally:
        db.close()

    removed_count += _prune_orphans(cutoff_str)
    return removed_count


def _prune_orphans(cutoff_str: str) -> int:
    """Sweep data/replay for symbol-days older than cutoff with no index row."""
    if not os.path.isdir(REPLAY_ROOT):
        return 0

    removed = 0
    try:
        symbols = [d for d in os.listdir(REPLAY_ROOT) if os.path.isdir(os.path.join(REPLAY_ROOT, d))]
    except OSError:
        logger.exception("[ReplayStorage] Could not list replay root")
        return 0

    for symbol in symbols:
        symbol_dir = os.path.join(REPLAY_ROOT, symbol)
        try:
            entries = os.listdir(symbol_dir)
        except OSError:
            continue

        for entry in entries:
            full = os.path.join(symbol_dir, entry)
            if os.path.isdir(full):
                date_str = entry
            elif entry.endswith(".parquet"):
                date_str = entry[: -len(".parquet")]
            else:
                continue

            if not _DATE_RE.match(date_str) or date_str >= cutoff_str:
                continue

            _remove_day_data(symbol, date_str)
            removed += 1

        # Drop the symbol directory once it holds nothing.
        try:
            if not os.listdir(symbol_dir):
                os.rmdir(symbol_dir)
        except OSError:
            pass

    return removed
