"""
database/depth_recorder_db.py
─────────────────────────────
Persists live market-depth (order-book) ticks streamed from the OpenAlgo
WebSocket proxy.  Each row captures one depth update for one symbol with:

  • up to 5 bid / ask levels  (price + qty)
  • LTP, total buy/sell qty, volume, OI
  • IST timestamp

Schema keeps all level data in flat scalar columns so:
  - No JSON blobs that are slow to filter / aggregate
  - Clean SQL queries (WHERE symbol = 'RELIANCE' AND exchange = 'NSE')
  - Simple to export / inspect with any SQL tool

Uses the project-wide engine factory (SQLite + NullPool) so it shares the
same connection-hygiene policy as every other database in OpenAlgo.
"""

import os
from datetime import datetime, timedelta

import pytz
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker

from database.engine_factory import create_db_engine
from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Engine / session (same NullPool pattern as scalping_db.py / flow_db.py)
# ---------------------------------------------------------------------------
DEPTH_RECORDER_DATABASE_URL = os.getenv(
    "DEPTH_RECORDER_DATABASE_URL", "sqlite:///db/depth_recorder.db"
)
engine = create_db_engine(DEPTH_RECORDER_DATABASE_URL)

db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()
Base.query = db_session.query_property()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class DepthTick(Base):
    """One depth snapshot for one symbol at one point in time."""

    __tablename__ = "depth_ticks"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Identity
    symbol = Column(String(60), nullable=False)
    exchange = Column(String(20), nullable=False)

    # Timestamp stored in IST (YYYY-MM-DD HH:MM:SS.ffffff), set from our own
    # receive clock (LTT does not advance between trades, so it cannot order
    # depth updates — see CLAUDE.md / depth analysis notes).
    tick_time = Column(DateTime(timezone=False), nullable=False)

    # Exchange last-trade time (epoch). Only advances on a trade — kept as a
    # secondary column for aligning the depth feed against trade data later.
    ltt = Column(Float, nullable=True)

    # Top-level scalars
    ltp = Column(Float, nullable=True)
    volume = Column(Float, nullable=True)
    oi = Column(Float, nullable=True)
    total_buy_qty = Column(Float, nullable=True)
    total_sell_qty = Column(Float, nullable=True)

    # Bid levels (buy side)
    bid1_price = Column(Float, nullable=True)
    bid1_qty = Column(Float, nullable=True)
    bid1_orders = Column(Integer, nullable=True)
    bid2_price = Column(Float, nullable=True)
    bid2_qty = Column(Float, nullable=True)
    bid2_orders = Column(Integer, nullable=True)
    bid3_price = Column(Float, nullable=True)
    bid3_qty = Column(Float, nullable=True)
    bid3_orders = Column(Integer, nullable=True)
    bid4_price = Column(Float, nullable=True)
    bid4_qty = Column(Float, nullable=True)
    bid4_orders = Column(Integer, nullable=True)
    bid5_price = Column(Float, nullable=True)
    bid5_qty = Column(Float, nullable=True)
    bid5_orders = Column(Integer, nullable=True)

    # Ask levels (sell side)
    ask1_price = Column(Float, nullable=True)
    ask1_qty = Column(Float, nullable=True)
    ask1_orders = Column(Integer, nullable=True)
    ask2_price = Column(Float, nullable=True)
    ask2_qty = Column(Float, nullable=True)
    ask2_orders = Column(Integer, nullable=True)
    ask3_price = Column(Float, nullable=True)
    ask3_qty = Column(Float, nullable=True)
    ask3_orders = Column(Integer, nullable=True)
    ask4_price = Column(Float, nullable=True)
    ask4_qty = Column(Float, nullable=True)
    ask4_orders = Column(Integer, nullable=True)
    ask5_price = Column(Float, nullable=True)
    ask5_qty = Column(Float, nullable=True)
    ask5_orders = Column(Integer, nullable=True)

    # Raw JSON dump of the full depth payload (for reference)
    raw_json = Column(Text, nullable=True)

    # Depth level and full levels JSON for deep order books (>5 levels)
    depth_level = Column(Integer, nullable=True)
    levels_json = Column(Text, nullable=True)

    # Composite indexes for fast time-range queries per symbol
    __table_args__ = (
        Index("ix_depth_ticks_symbol_exchange_time", "symbol", "exchange", "tick_time"),
        Index("ix_depth_ticks_tick_time", "tick_time"),
    )


class DepthSession(Base):
    """Daily summary for the depth tick inventory."""

    __tablename__ = "depth_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(60), nullable=False)
    exchange = Column(String(20), nullable=False)
    day = Column(String(10), nullable=False)
    first_tick = Column(DateTime(timezone=False), nullable=False)
    last_tick = Column(DateTime(timezone=False), nullable=False)
    n_ticks = Column(Integer, nullable=False)

    __table_args__ = (UniqueConstraint("symbol", "exchange", "day"),)


class DepthRecorderConfig(Base):
    """
    Persists recorder configuration (symbol, exchange, status, depth_level).
    Survives server restarts — if 'active' the recorder service
    auto-resumes on boot.
    """

    __tablename__ = "depth_recorder_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(60), nullable=False)
    exchange = Column(String(20), nullable=False)
    # 'active' | 'stopped'
    status = Column(String(10), nullable=False, default="stopped")
    depth_level = Column(Integer, nullable=False, default=5)
    created_at = Column(DateTime(timezone=False), nullable=False)
    updated_at = Column(DateTime(timezone=False), nullable=False)

    # One config per (symbol, exchange) — the same base symbol may be recorded
    # on multiple exchanges (RELIANCE on NSE and BSE at once).
    __table_args__ = (UniqueConstraint("symbol", "exchange"),)


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------
def _ensure_columns(table: str, columns: dict[str, str]) -> None:
    if "sqlite" not in (DEPTH_RECORDER_DATABASE_URL or ""):
        return
    from sqlalchemy import text
    with engine.connect() as conn:
        existing = {
            row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))
        }
        for col_name, col_type in columns.items():
            if col_name not in existing:
                logger.info(f"depth_recorder_db: adding missing column {table}.{col_name}")
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}"))
        conn.commit()


def init_db():
    """Create tables if they don't exist (idempotent)."""
    from database.db_init_helper import init_db_with_logging

    init_db_with_logging(Base, engine, "Depth Recorder DB", logger)
    try:
        _ensure_columns("depth_ticks", {"depth_level": "INTEGER", "levels_json": "TEXT"})
        _ensure_columns("depth_recorder_config", {"depth_level": "INTEGER DEFAULT 5"})
        # Existence checks only: COUNT(*) would scan the whole tick table at every boot.
        if (
            db_session.query(DepthSession.id).first() is None
            and db_session.query(DepthTick.id).first() is not None
        ):
            aggregates = (
                db_session.query(
                    DepthTick.symbol, DepthTick.exchange, func.date(DepthTick.tick_time),
                    func.min(DepthTick.tick_time), func.max(DepthTick.tick_time), func.count(DepthTick.id),
                )
                .group_by(DepthTick.symbol, DepthTick.exchange, func.date(DepthTick.tick_time))
                .all()
            )
            for symbol, exchange, day, first_tick, last_tick, n_ticks in aggregates:
                db_session.add(DepthSession(symbol=symbol, exchange=exchange, day=day,
                                            first_tick=first_tick, last_tick=last_tick, n_ticks=n_ticks))
            db_session.commit()
    except Exception:
        logger.exception("depth_recorder_db: migration failed")
        db_session.rollback()
    finally:
        # Boot runs outside a request: release the connection the checks opened.
        db_session.remove()


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------
def _now_ist() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def save_tick(
    symbol: str,
    exchange: str,
    tick_data: dict,
) -> bool:
    """
    Insert one depth tick row.

    tick_data keys (all optional except 'bids' / 'asks'):
        ltp, volume, oi, total_buy_qty, total_sell_qty,
        bids: list of {price, quantity}  (up to 5)
        asks: list of {price, quantity}  (up to 5)
        raw_json: str (serialised original payload)
    """
    import json

    try:
        bids = tick_data.get("bids") or []
        asks = tick_data.get("asks") or []

        def _p(levels, i, key):
            try:
                return float(levels[i].get(key, 0) or 0) or None
            except (IndexError, TypeError, ValueError):
                return None

        row = DepthTick(
            symbol=symbol,
            exchange=exchange,
            tick_time=_now_ist(),
            ltp=_safe_float(tick_data.get("ltp")),
            volume=_safe_float(tick_data.get("volume")),
            oi=_safe_float(tick_data.get("oi")),
            total_buy_qty=_safe_float(tick_data.get("total_buy_qty")),
            total_sell_qty=_safe_float(tick_data.get("total_sell_qty")),
            depth_level=tick_data.get("depth_level"),
            levels_json=tick_data.get("levels_json"),
            # Bids
            bid1_price=_p(bids, 0, "price"),
            bid1_qty=_p(bids, 0, "quantity"),
            bid2_price=_p(bids, 1, "price"),
            bid2_qty=_p(bids, 1, "quantity"),
            bid3_price=_p(bids, 2, "price"),
            bid3_qty=_p(bids, 2, "quantity"),
            bid4_price=_p(bids, 3, "price"),
            bid4_qty=_p(bids, 3, "quantity"),
            bid5_price=_p(bids, 4, "price"),
            bid5_qty=_p(bids, 4, "quantity"),
            # Asks
            ask1_price=_p(asks, 0, "price"),
            ask1_qty=_p(asks, 0, "quantity"),
            ask2_price=_p(asks, 1, "price"),
            ask2_qty=_p(asks, 1, "quantity"),
            ask3_price=_p(asks, 2, "price"),
            ask3_qty=_p(asks, 2, "quantity"),
            ask4_price=_p(asks, 3, "price"),
            ask4_qty=_p(asks, 3, "quantity"),
            ask5_price=_p(asks, 4, "price"),
            ask5_qty=_p(asks, 4, "quantity"),
            raw_json=tick_data.get("raw_json"),
        )
        db_session.add(row)
        db_session.commit()
        return True
    except Exception:
        logger.exception("depth_recorder_db: error saving tick")
        db_session.rollback()
        return False


def batch_save_ticks(ticks: list[dict]) -> int:
    """
    Bulk-insert a list of pre-built tick dicts (faster than save_tick per tick).
    Returns number of rows inserted.

    Each dict must have: symbol, exchange, tick_time (datetime), plus all the
    scalar fields that DepthTick carries.
    """
    if not ticks:
        return 0
    try:
        groups = {}
        for tick in ticks:
            day = tick["tick_time"].date().isoformat()
            key = (tick["symbol"], tick["exchange"], day)
            current = groups.get(key)
            if current is None:
                groups[key] = [tick["tick_time"], tick["tick_time"], 1]
            else:
                current[0] = min(current[0], tick["tick_time"])
                current[1] = max(current[1], tick["tick_time"])
                current[2] += 1
        db_session.bulk_insert_mappings(DepthTick, ticks)
        for (symbol, exchange, day), (first_tick, last_tick, count) in groups.items():
            summary = db_session.query(DepthSession).filter_by(
                symbol=symbol, exchange=exchange, day=day
            ).first()
            if summary is None:
                summary = DepthSession(symbol=symbol, exchange=exchange, day=day,
                                       first_tick=first_tick, last_tick=last_tick, n_ticks=count)
                db_session.add(summary)
            else:
                summary.first_tick = min(summary.first_tick, first_tick)
                summary.last_tick = max(summary.last_tick, last_tick)
                summary.n_ticks += count
        db_session.commit()
        return len(ticks)
    except Exception:
        logger.exception("depth_recorder_db: batch_save_ticks failed")
        db_session.rollback()
        return 0


def _safe_float(val) -> float | None:
    try:
        f = float(val)
        return f if f == f else None  # NaN guard
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
def upsert_config(
    symbol: str, exchange: str, status: str | None = None, depth_level: int | None = None
) -> dict | None:
    """Create or update recorder config for a symbol."""
    try:
        row = (
            db_session.query(DepthRecorderConfig)
            .filter_by(symbol=symbol.upper(), exchange=exchange.upper())
            .first()
        )
        now = _now_ist()
        if row is None:
            row = DepthRecorderConfig(
                symbol=symbol.upper(),
                exchange=exchange.upper(),
                status=status or "stopped",
                depth_level=depth_level or 5,
                created_at=now,
                updated_at=now,
            )
            db_session.add(row)
        else:
            # None means "leave unchanged" so stop/start don't clobber each other's field
            if status is not None:
                row.status = status
            if depth_level is not None:
                row.depth_level = depth_level
            row.updated_at = now
        db_session.commit()
        return _config_to_dict(row)
    except Exception:
        logger.exception("depth_recorder_db: upsert_config failed")
        db_session.rollback()
        return None


def get_all_configs() -> list[dict]:
    try:
        rows = db_session.query(DepthRecorderConfig).all()
        return [_config_to_dict(r) for r in rows]
    except Exception:
        logger.exception("depth_recorder_db: get_all_configs failed")
        db_session.rollback()
        return []


def get_active_configs() -> list[dict]:
    try:
        rows = db_session.query(DepthRecorderConfig).filter_by(status="active").all()
        return [_config_to_dict(r) for r in rows]
    except Exception:
        logger.exception("depth_recorder_db: get_active_configs failed")
        db_session.rollback()
        return []


def delete_config(symbol: str, exchange: str) -> bool:
    try:
        q = db_session.query(DepthRecorderConfig).filter_by(
            symbol=symbol.upper(), exchange=exchange.upper()
        )
        deleted = q.delete()
        db_session.commit()
        return deleted > 0
    except Exception:
        logger.exception("depth_recorder_db: delete_config failed")
        db_session.rollback()
        return False


def _config_to_dict(row: DepthRecorderConfig) -> dict:
    return {
        "symbol": row.symbol,
        "exchange": row.exchange,
        "status": row.status,
        "depth_level": row.depth_level if row.depth_level is not None else 5,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


# ---------------------------------------------------------------------------
# Query helpers (for the dashboard page)
# ---------------------------------------------------------------------------
def get_recent_ticks(
    symbol: str,
    exchange: str,
    limit: int = 100,
) -> list[dict]:
    """Return the most recent N ticks for a symbol, newest-first."""
    try:
        rows = (
            db_session.query(DepthTick)
            .filter_by(symbol=symbol.upper(), exchange=exchange.upper())
            .order_by(DepthTick.tick_time.desc())
            .limit(limit)
            .all()
        )
        return [_tick_to_dict(r) for r in rows]
    except Exception:
        logger.exception("depth_recorder_db: get_recent_ticks failed")
        db_session.rollback()
        return []


def get_session_ticks(symbol: str, exchange: str, start: datetime, end: datetime) -> list[dict]:
    """Return all ticks for a symbol between start (inclusive) and end (exclusive).

    Ordered old->new by tick_time. This powers the pandas metrics engine, so it
    returns every stored row in the range — use it only for analysis queries,
    not the live dashboard.
    """
    try:
        rows = (
            db_session.query(DepthTick)
            .filter(
                DepthTick.symbol == symbol.upper(),
                DepthTick.exchange == exchange.upper(),
                DepthTick.tick_time >= start,
                DepthTick.tick_time < end,
            )
            .order_by(DepthTick.tick_time.asc())
            .all()
        )
        return [_tick_to_dict(r) for r in rows]
    except Exception:
        logger.exception("depth_recorder_db: get_session_ticks failed")
        db_session.rollback()
        return []


def get_session_ticks_flat(
    symbol: str, exchange: str, start: datetime, end: datetime
) -> list[dict]:
    """Return all ticks in range as flat scalar dicts, old -> new.

    Same rows as :func:`get_session_ticks` but already flattened into the
    bid/ask scalar columns the analytics engine consumes (bid1_p ... ask5_o),
    skipping the nested bids/asks round-trip entirely.
    """
    cols, rows = get_session_rows_flat(symbol, exchange, start, end)
    if not rows:
        return []
    return [dict(zip(cols, row, strict=True)) for row in rows]


def get_session_rows_flat(
    symbol: str, exchange: str, start: datetime, end: datetime
) -> tuple[list[str], list[tuple]]:
    """Return scalar analytics columns and their rows without ORM instances."""
    cols = ["tick_time", "ltp", "volume", "ltt", "total_buy_qty", "total_sell_qty"]
    for i in range(1, 6):
        cols.extend((f"bid{i}_p", f"bid{i}_q", f"bid{i}_o", f"ask{i}_p", f"ask{i}_q", f"ask{i}_o"))
    try:
        entities = [DepthTick.tick_time, DepthTick.ltp, DepthTick.volume, DepthTick.ltt,
                    DepthTick.total_buy_qty, DepthTick.total_sell_qty]
        for i in range(1, 6):
            entities.extend((getattr(DepthTick, f"bid{i}_price"), getattr(DepthTick, f"bid{i}_qty"),
                             getattr(DepthTick, f"bid{i}_orders"), getattr(DepthTick, f"ask{i}_price"),
                             getattr(DepthTick, f"ask{i}_qty"), getattr(DepthTick, f"ask{i}_orders")))
        rows = (
            db_session.query(*entities)
            .filter(
                DepthTick.symbol == symbol.upper(),
                DepthTick.exchange == exchange.upper(),
                DepthTick.tick_time >= start,
                DepthTick.tick_time < end,
            )
            .order_by(DepthTick.tick_time.asc())
            .all()
        )
        return cols, rows
    except Exception:
        logger.exception("depth_recorder_db: get_session_rows_flat failed")
        db_session.rollback()
        return cols, []


def get_session_bounds(symbol: str, exchange: str) -> tuple[datetime, datetime] | None:
    """Return (first_tick_time, last_tick_time) for a symbol, or None if empty."""
    try:
        first = (
            db_session.query(DepthTick)
            .filter_by(symbol=symbol.upper(), exchange=exchange.upper())
            .order_by(DepthTick.tick_time.asc())
            .first()
        )
        last = (
            db_session.query(DepthTick)
            .filter_by(symbol=symbol.upper(), exchange=exchange.upper())
            .order_by(DepthTick.tick_time.desc())
            .first()
        )
        if not first or not last:
            return None
        return first.tick_time, last.tick_time
    except Exception:
        logger.exception("depth_recorder_db: get_session_bounds failed")
        db_session.rollback()
        return None


def get_tick_count(symbol: str, exchange: str) -> int:
    """Return total stored tick count for a symbol."""
    try:
        return (
            db_session.query(func.coalesce(func.sum(DepthSession.n_ticks), 0))
            .filter_by(symbol=symbol.upper(), exchange=exchange.upper()).scalar()
        )
    except Exception:
        db_session.rollback()
        return 0


def get_stats() -> dict:
    """Return overall DB stats for the dashboard."""
    try:
        total = db_session.query(func.coalesce(func.sum(DepthSession.n_ticks), 0)).scalar()
        symbols = db_session.query(DepthSession.symbol, DepthSession.exchange).distinct().all()
        latest = db_session.query(func.max(DepthSession.last_tick)).scalar()
        return {
            "total_ticks": total,
            "symbols": [{"symbol": s, "exchange": e} for s, e in symbols],
            "latest_tick_time": latest.isoformat() if latest else None,
        }
    except Exception:
        logger.exception("depth_recorder_db: get_stats failed")
        db_session.rollback()
        return {"total_ticks": 0, "symbols": [], "latest_tick_time": None}


def prune_old_ticks(days_to_keep: int) -> int:
    """Delete ticks older than ``days_to_keep`` days, one day per transaction.

    Walks forward from the oldest stored tick using range predicates on
    ``tick_time`` (so the time index is used and the write lock is only ever
    held for one day's rows), and drops the matching summary rows with it.
    Returns the number of tick rows deleted.
    """
    try:
        days_to_keep = int(days_to_keep)
    except (TypeError, ValueError):
        return 0
    if days_to_keep < 1:
        return 0

    cutoff_date = datetime.now(IST).date() - timedelta(days=days_to_keep)
    cutoff = datetime.combine(cutoff_date, datetime.min.time())
    deleted = 0
    try:
        while True:
            oldest = db_session.query(func.min(DepthTick.tick_time)).scalar()
            if oldest is None or oldest >= cutoff:
                break
            day_start = datetime.combine(oldest.date(), datetime.min.time())
            day_end = min(day_start + timedelta(days=1), cutoff)
            n = (
                db_session.query(DepthTick)
                .filter(DepthTick.tick_time >= day_start, DepthTick.tick_time < day_end)
                .delete(synchronize_session=False)
            )
            db_session.query(DepthSession).filter(
                DepthSession.day == day_start.date().isoformat()
            ).delete(synchronize_session=False)
            db_session.commit()
            deleted += n
            if n == 0:
                break  # nothing matched: never spin on an unexpected row
        # Summary rows whose ticks are already gone
        db_session.query(DepthSession).filter(
            DepthSession.day < cutoff_date.isoformat()
        ).delete(synchronize_session=False)
        db_session.commit()
        return deleted
    except Exception:
        logger.exception("depth_recorder_db: prune_old_ticks failed")
        db_session.rollback()
        return deleted


def _tick_to_dict(row: DepthTick) -> dict:
    import json
    bids = []
    asks = []
    if row.levels_json:
        try:
            data = json.loads(row.levels_json)
            for b in data.get("b", []):
                bids.append({"price": b[0], "quantity": b[1], "orders": b[2] if len(b) > 2 else None})
            for a in data.get("s", []):
                asks.append({"price": a[0], "quantity": a[1], "orders": a[2] if len(a) > 2 else None})
        except Exception:
            pass

    if not bids:
        bids = [
            {"price": row.bid1_price, "quantity": row.bid1_qty, "orders": row.bid1_orders},
            {"price": row.bid2_price, "quantity": row.bid2_qty, "orders": row.bid2_orders},
            {"price": row.bid3_price, "quantity": row.bid3_qty, "orders": row.bid3_orders},
            {"price": row.bid4_price, "quantity": row.bid4_qty, "orders": row.bid4_orders},
            {"price": row.bid5_price, "quantity": row.bid5_qty, "orders": row.bid5_orders},
        ]
    if not asks:
        asks = [
            {"price": row.ask1_price, "quantity": row.ask1_qty, "orders": row.ask1_orders},
            {"price": row.ask2_price, "quantity": row.ask2_qty, "orders": row.ask2_orders},
            {"price": row.ask3_price, "quantity": row.ask3_qty, "orders": row.ask3_orders},
            {"price": row.ask4_price, "quantity": row.ask4_qty, "orders": row.ask4_orders},
            {"price": row.ask5_price, "quantity": row.ask5_qty, "orders": row.ask5_orders},
        ]

    return {
        "id": row.id,
        "symbol": row.symbol,
        "exchange": row.exchange,
        "tick_time": row.tick_time.isoformat() if row.tick_time else None,
        "ltt": row.ltt,
        "ltp": row.ltp,
        "volume": row.volume,
        "oi": row.oi,
        "total_buy_qty": row.total_buy_qty,
        "total_sell_qty": row.total_sell_qty,
        "depth_level": row.depth_level,
        "levels_json": row.levels_json,
        "bids": bids,
        "asks": asks,
    }
