"""
openalgo/replay/storage.py
──────────────────────────
Parquet storage helpers for tick and depth events, sorted by timestamp.
"""

import os

import pandas as pd

from replay.metadata_db import SessionLocal, SymbolDateIndex


def get_parquet_path(symbol: str, date_str: str) -> str:
    symbol_dir = os.path.join("data/replay", symbol.upper())
    os.makedirs(symbol_dir, exist_ok=True)
    return os.path.join(symbol_dir, f"{date_str}.parquet")


def save_events(symbol: str, date_str: str, events: list[dict]):
    if not events:
        return
    path = get_parquet_path(symbol, date_str)
    df_new = pd.DataFrame(events)
    # Ensure timestamp column is datetime
    if "timestamp" in df_new.columns:
        df_new["timestamp"] = pd.to_datetime(df_new["timestamp"])

    if os.path.exists(path):
        try:
            df_existing = pd.read_parquet(path)
            df_existing["timestamp"] = pd.to_datetime(df_existing["timestamp"])
            df_combined = pd.concat([df_existing, df_new], ignore_index=True)
        except Exception:
            df_combined = df_new
    else:
        df_combined = df_new

    # Sort by timestamp and drop duplicate exact timestamps if any
    df_combined = df_combined.sort_values("timestamp").drop_duplicates(subset=["timestamp"]).reset_index(drop=True)
    df_combined.to_parquet(path, index=False)

    # Update metadata index
    db = SessionLocal()
    try:
        idx_row = db.query(SymbolDateIndex).filter_by(symbol=symbol.upper(), date=date_str).first()
        if idx_row:
            idx_row.row_count = len(df_combined)
            idx_row.file_path = path
        else:
            idx_row = SymbolDateIndex(
                symbol=symbol.upper(),
                date=date_str,
                file_path=path,
                row_count=len(df_combined)
            )
            db.add(idx_row)
        db.commit()
    finally:
        db.close()

    try:
        from replay.dal import clear_dal_cache
        clear_dal_cache()
    except Exception:
        pass


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


def load_day_df(symbol: str, date_str: str) -> pd.DataFrame:
    path = get_parquet_path(symbol, date_str)
    if not os.path.exists(path):
        return pd.DataFrame(columns=[
            "timestamp", "ltp",
            "bid_1", "bid_qty_1", "bid_2", "bid_qty_2", "bid_3", "bid_qty_3", "bid_4", "bid_qty_4", "bid_5", "bid_qty_5",
            "ask_1", "ask_qty_1", "ask_2", "ask_qty_2", "ask_3", "ask_qty_3", "ask_4", "ask_qty_4", "ask_5", "ask_qty_5",
            "volume", "oi"
        ])
    df = pd.read_parquet(path)
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def prune_old_replay_data(days_to_keep: int = 7) -> int:
    """Prune replay Parquet files and DB index rows older than days_to_keep (default 7 days / weekly retention)."""
    from datetime import datetime, timedelta
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
    cutoff = datetime.now(IST).date() - timedelta(days=days_to_keep)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    db = SessionLocal()
    removed_count = 0
    try:
        old_rows = db.query(SymbolDateIndex).filter(SymbolDateIndex.date < cutoff_str).all()
        for r in old_rows:
            if r.file_path and os.path.exists(r.file_path):
                try:
                    os.remove(r.file_path)
                except Exception:
                    pass
            db.delete(r)
            removed_count += 1
        db.commit()
    except Exception as exc:
        db.rollback()
    finally:
        db.close()
    return removed_count
