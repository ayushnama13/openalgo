# database/backtest_db.py

import json
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
    create_engine,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

from database.engine_factory import create_db_engine
from utils.logging import get_logger

logger = get_logger(__name__)

BACKTEST_DATABASE_URL = os.getenv("BACKTEST_DATABASE_URL", "sqlite:///db/backtest.db")

# Ensure db directory exists if using default sqlite path
if BACKTEST_DATABASE_URL.startswith("sqlite:///db/"):
    os.makedirs("db", exist_ok=True)

engine = create_db_engine(BACKTEST_DATABASE_URL)
db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    job_id = Column(String(64), primary_key=True)
    name = Column(String(128), nullable=False, default="Untitled Backtest")
    status = Column(String(32), nullable=False, default="pending", index=True)
    strategy_code = Column(Text, nullable=False)
    symbols = Column(Text, nullable=False)  # Stored as JSON string list e.g. ["NSE:SBIN"]
    exchange = Column(String(32), nullable=False, default="NSE")
    interval = Column(String(16), nullable=False, default="1m")
    start_date = Column(String(32), nullable=True)
    end_date = Column(String(32), nullable=True)
    initial_capital = Column(Float, nullable=False, default=100000.0)
    sizing_type = Column(String(32), nullable=False, default="fixed_qty")  # fixed_qty, pct_equity, fixed_capital, kelly_vol
    sizing_value = Column(Float, nullable=False, default=1.0)
    slippage_bps = Column(Float, nullable=False, default=0.0)
    brokerage_bps = Column(Float, nullable=False, default=0.0)
    product_type = Column(String(16), nullable=False, default="MIS")  # MIS, NRML, CNC
    missing_data_policy = Column(String(32), nullable=False, default="skip")  # skip, forward_fill, abort
    stop_loss_pct = Column(Float, nullable=True)
    take_profit_pct = Column(Float, nullable=True)
    daily_loss_limit = Column(Float, nullable=True)
    max_positions = Column(Integer, nullable=True, default=5)
    # Batch grouping: one POST /run fans out into one BacktestRun row per
    # timeframe, all tied by the same batch_id (NULL in old rows until the
    # migration backfills batch_id = job_id, making a legacy run a batch of one).
    batch_id = Column(String(64), nullable=True, index=True)
    batch_name = Column(String(128), nullable=True)
    batch_seq = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(pytz.timezone("Asia/Kolkata")))
    completed_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)
    metrics_json = Column(Text, nullable=True)


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(64), nullable=False, index=True)
    symbol = Column(String(64), nullable=False)
    exchange = Column(String(32), nullable=False, default="NSE")
    action = Column(String(16), nullable=False)  # BUY / SELL
    entry_time = Column(String(64), nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_time = Column(String(64), nullable=False)
    exit_price = Column(Float, nullable=False)
    quantity = Column(Float, nullable=False)
    pnl = Column(Float, nullable=False)
    pnl_pct = Column(Float, nullable=False)
    holding_period_bars = Column(Integer, nullable=False, default=0)
    entry_reason = Column(String(128), nullable=True)
    exit_reason = Column(String(128), nullable=True)
    costs = Column(Float, nullable=False, default=0.0)


class BacktestEquityCurve(Base):
    __tablename__ = "backtest_equity_curve"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(64), nullable=False, index=True)
    timestamp = Column(String(64), nullable=False)
    equity = Column(Float, nullable=False)
    drawdown_pct = Column(Float, nullable=False, default=0.0)
    cash = Column(Float, nullable=False)
    positions_value = Column(Float, nullable=False, default=0.0)


class BacktestStrategy(Base):
    __tablename__ = "backtest_strategies"

    id = Column(String(64), primary_key=True)
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=True)
    strategy_code = Column(Text, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(pytz.timezone("Asia/Kolkata")))
    updated_at = Column(DateTime, default=lambda: datetime.now(pytz.timezone("Asia/Kolkata")), onupdate=lambda: datetime.now(pytz.timezone("Asia/Kolkata")))


# Create index on job_id for performance
Index("idx_backtest_trades_job_id", BacktestTrade.job_id)
Index("idx_backtest_equity_job_id", BacktestEquityCurve.job_id)
Index("idx_backtest_runs_batch_id", BacktestRun.batch_id)


def init_backtest_db():
    """Initialize backtest database tables."""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Initialized backtest database schema successfully.")
    except Exception as e:
        logger.exception(f"Failed to initialize backtest database: {e}")
        raise


init_db = init_backtest_db



def cleanup_old_runs(days_to_keep=30):
    """
    Retention policy cleanup to prevent unbounded database growth.
    Deletes backtest runs, trades, and equity curves older than `days_to_keep`.
    """
    session = db_session()
    try:
        cutoff_date = datetime.now(pytz.timezone("Asia/Kolkata")) - timedelta(days=days_to_keep)
        old_runs = session.query(BacktestRun.job_id).filter(BacktestRun.created_at < cutoff_date).all()
        job_ids = [run.job_id for run in old_runs]
        if job_ids:
            session.query(BacktestEquityCurve).filter(BacktestEquityCurve.job_id.in_(job_ids)).delete(synchronize_session=False)
            session.query(BacktestTrade).filter(BacktestTrade.job_id.in_(job_ids)).delete(synchronize_session=False)
            session.query(BacktestRun).filter(BacktestRun.job_id.in_(job_ids)).delete(synchronize_session=False)
            session.commit()
            logger.info(f"Cleaned up {len(job_ids)} backtest runs older than {days_to_keep} days.")
    except Exception as e:
        session.rollback()
        logger.exception(f"Error cleaning up old backtest runs: {e}")
    finally:
        db_session.remove()
