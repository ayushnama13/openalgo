"""
openalgo/replay/metadata_db.py
──────────────────────────────
Metadata database for Replay mode, completely isolated from core OpenAlgo DB.
Uses database.engine_factory.create_db_engine with NullPool.
"""

import os
from datetime import datetime

import pytz
from sqlalchemy import Column, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import declarative_base, sessionmaker

from database.engine_factory import create_db_engine

IST = pytz.timezone("Asia/Kolkata")

REPLAY_DB_URL = "sqlite:///data/replay/replay_metadata.db"
engine = create_db_engine(REPLAY_DB_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class ReplaySession(Base):
    __tablename__ = "replay_sessions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    session_name = Column(String(100), unique=True, nullable=False)
    date = Column(String(20), nullable=False)  # YYYY-MM-DD
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(IST))


class WatchlistSymbol(Base):
    __tablename__ = "replay_watchlist_symbols"
    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, nullable=False)
    symbol = Column(String(50), nullable=False)
    exchange = Column(String(20), nullable=False)
    full_depth = Column(Integer, default=1)  # 1 for full depth, 0 for 1s snapshot


class SymbolDateIndex(Base):
    __tablename__ = "replay_symbol_date_index"
    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(50), nullable=False)
    date = Column(String(20), nullable=False)  # YYYY-MM-DD
    file_path = Column(String(255), nullable=False)
    row_count = Column(Integer, default=0)


class RecordingTarget(Base):
    """
    Cross-process coordination row: written by the Flask blueprint, polled by
    the (possibly out-of-process) websocket_proxy tick loop. SQLite/NullPool
    makes this safe to read/write from either process.
    """
    __tablename__ = "replay_recording_targets"
    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(50), nullable=False)
    exchange = Column(String(20), nullable=False)
    date = Column(String(20), nullable=False)  # YYYY-MM-DD
    created_at = Column(DateTime, default=lambda: datetime.now(IST))


def init_replay_db():
    os.makedirs("data/replay", exist_ok=True)
    Base.metadata.create_all(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


init_replay_db()
