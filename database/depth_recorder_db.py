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

import logging
import os
from datetime import datetime

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
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker

from database.engine_factory import create_db_engine

logger = logging.getLogger(__name__)

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

    # Composite indexes for fast time-range queries per symbol
    __table_args__ = (
        Index("ix_depth_ticks_symbol_exchange_time", "symbol", "exchange", "tick_time"),
        Index("ix_depth_ticks_tick_time", "tick_time"),
    )


class DepthRecorderConfig(Base):
    """
    Persists recorder configuration (symbol, exchange, status).
    Survives server restarts — if 'active' the recorder service
    auto-resumes on boot.
    """

    __tablename__ = "depth_recorder_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(60), nullable=False)
    exchange = Column(String(20), nullable=False)
    # 'active' | 'stopped'
    status = Column(String(10), nullable=False, default="stopped")
    created_at = Column(DateTime(timezone=False), nullable=False)
    updated_at = Column(DateTime(timezone=False), nullable=False)

    # One config per (symbol, exchange) — the same base symbol may be recorded
    # on multiple exchanges (RELIANCE on NSE and BSE at once).
    __table_args__ = (UniqueConstraint("symbol", "exchange"),)


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------
def init_db():
    """Create tables if they don't exist (idempotent)."""
    from database.db_init_helper import init_db_with_logging

    init_db_with_logging(Base, engine, "Depth Recorder DB", logger)


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
        db_session.bulk_insert_mappings(DepthTick, ticks)
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
def upsert_config(symbol: str, exchange: str, status: str) -> dict | None:
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
                status=status,
                created_at=now,
                updated_at=now,
            )
            db_session.add(row)
        else:
            row.status = status
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
        out = []
        for r in rows:
            d = {
                "tick_time": r.tick_time,
                "ltp": r.ltp,
                "volume": r.volume,
                "ltt": r.ltt,
                "total_buy_qty": r.total_buy_qty,
                "total_sell_qty": r.total_sell_qty,
            }
            for i in (1, 2, 3, 4, 5):
                d[f"bid{i}_p"] = getattr(r, f"bid{i}_price")
                d[f"bid{i}_q"] = getattr(r, f"bid{i}_qty")
                d[f"bid{i}_o"] = getattr(r, f"bid{i}_orders")
                d[f"ask{i}_p"] = getattr(r, f"ask{i}_price")
                d[f"ask{i}_q"] = getattr(r, f"ask{i}_qty")
                d[f"ask{i}_o"] = getattr(r, f"ask{i}_orders")
            out.append(d)
        return out
    except Exception:
        logger.exception("depth_recorder_db: get_session_ticks_flat failed")
        db_session.rollback()
        return []


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
            db_session.query(DepthTick)
            .filter_by(symbol=symbol.upper(), exchange=exchange.upper())
            .count()
        )
    except Exception:
        db_session.rollback()
        return 0


def get_stats() -> dict:
    """Return overall DB stats for the dashboard."""
    try:
        total = db_session.query(DepthTick).count()
        symbols = db_session.query(DepthTick.symbol, DepthTick.exchange).distinct().all()
        latest = db_session.query(DepthTick).order_by(DepthTick.tick_time.desc()).first()
        return {
            "total_ticks": total,
            "symbols": [{"symbol": s, "exchange": e} for s, e in symbols],
            "latest_tick_time": latest.tick_time.isoformat() if latest else None,
        }
    except Exception:
        logger.exception("depth_recorder_db: get_stats failed")
        db_session.rollback()
        return {"total_ticks": 0, "symbols": [], "latest_tick_time": None}


def _tick_to_dict(row: DepthTick) -> dict:
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
        "bids": [
            {"price": row.bid1_price, "quantity": row.bid1_qty, "orders": row.bid1_orders},
            {"price": row.bid2_price, "quantity": row.bid2_qty, "orders": row.bid2_orders},
            {"price": row.bid3_price, "quantity": row.bid3_qty, "orders": row.bid3_orders},
            {"price": row.bid4_price, "quantity": row.bid4_qty, "orders": row.bid4_orders},
            {"price": row.bid5_price, "quantity": row.bid5_qty, "orders": row.bid5_orders},
        ],
        "asks": [
            {"price": row.ask1_price, "quantity": row.ask1_qty, "orders": row.ask1_orders},
            {"price": row.ask2_price, "quantity": row.ask2_qty, "orders": row.ask2_orders},
            {"price": row.ask3_price, "quantity": row.ask3_qty, "orders": row.ask3_orders},
            {"price": row.ask4_price, "quantity": row.ask4_qty, "orders": row.ask4_orders},
            {"price": row.ask5_price, "quantity": row.ask5_qty, "orders": row.ask5_orders},
        ],
    }
