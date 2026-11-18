# database/backtest_db.py

import io
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
    LargeBinary,
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
    # Reproducibility: engine/cost/parameter options, a hash of the strategy code and a
    # fingerprint of the exact bars used, so a result can be traced back to its inputs.
    engine_options = Column(Text, nullable=True)
    code_hash = Column(String(64), nullable=True)
    data_fingerprint = Column(Text, nullable=True)
    # Run mode: "stock" (hand-picked symbols), "universe" (one portfolio over an
    # index's constituents) or "scan" (one run per constituent, batched). NULL in
    # rows written before the column existed means "stock".
    mode = Column(String(16), nullable=True)
    universe = Column(String(32), nullable=True)
    # Research log: a star and a free-text note per run.
    starred = Column(Integer, nullable=False, default=0)
    notes = Column(Text, nullable=True)
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
    mae_pct = Column(Float, nullable=True)  # worst unrealised move against the position, %
    mfe_pct = Column(Float, nullable=True)  # best unrealised move in favour, %


class BacktestEquityCurve(Base):
    __tablename__ = "backtest_equity_curve"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(64), nullable=False, index=True)
    timestamp = Column(String(64), nullable=False)
    equity = Column(Float, nullable=False)
    drawdown_pct = Column(Float, nullable=False, default=0.0)
    cash = Column(Float, nullable=False)
    positions_value = Column(Float, nullable=False, default=0.0)


class BacktestEquityBlob(Base):
    __tablename__ = "backtest_equity_blob"

    job_id = Column(String(64), primary_key=True)
    n_points = Column(Integer, nullable=False)
    data = Column(LargeBinary, nullable=False)


class BacktestOptimization(Base):
    """A parameter sweep or walk-forward study over one strategy."""

    __tablename__ = "backtest_optimizations"

    opt_id = Column(String(64), primary_key=True)
    name = Column(String(128), nullable=False, default="Untitled Optimization")
    kind = Column(String(32), nullable=False, default="sweep")  # sweep, walk_forward
    status = Column(String(32), nullable=False, default="pending", index=True)
    progress = Column(Float, nullable=False, default=0.0)
    strategy_code = Column(Text, nullable=False)
    config_json = Column(Text, nullable=False)  # run config + optimize spec
    result_json = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(pytz.timezone("Asia/Kolkata")))
    completed_at = Column(DateTime, nullable=True)


class BacktestStrategy(Base):
    __tablename__ = "backtest_strategies"

    id = Column(String(64), primary_key=True)
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=True)
    strategy_code = Column(Text, nullable=False)
    settings_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(pytz.timezone("Asia/Kolkata")))
    updated_at = Column(DateTime, default=lambda: datetime.now(pytz.timezone("Asia/Kolkata")), onupdate=lambda: datetime.now(pytz.timezone("Asia/Kolkata")))


# Create index on batch_id for performance
Index("idx_backtest_runs_batch_id", BacktestRun.batch_id)


def _wall_clock_seconds(timestamps):
    """Encode naive IST 'YYYY-MM-DD HH:MM:SS' wall-clock stamps as int64 epoch seconds.

    Vectorised: a 1m run over a few years is ~500k points, and a per-element
    conversion costs seconds.
    """
    import pandas as pd

    idx = pd.DatetimeIndex(pd.to_datetime(list(timestamps)))
    if idx.tz is None:
        idx = idx.tz_localize("Asia/Kolkata")
    epoch = pd.Timestamp("1970-01-01", tz="UTC")
    return ((idx - epoch) // pd.Timedelta(seconds=1)).to_numpy(dtype="int64")


def save_equity_curve(session, job_id, timestamps, equity, drawdown_pct, cash, positions_value):
    """Store one run's equity curve as a single compressed row (does not commit)."""
    import numpy as np

    ts = _wall_clock_seconds(timestamps)
    arrays = {
        "ts": ts,
        "equity": np.asarray(equity, dtype=np.float64),
        "drawdown_pct": np.asarray(drawdown_pct, dtype=np.float64),
        "cash": np.asarray(cash, dtype=np.float64),
        "positions_value": np.asarray(positions_value, dtype=np.float64),
    }
    if any(len(a) != len(ts) for a in arrays.values()):
        raise ValueError("equity curve arrays must all have the same length")
    payload = io.BytesIO()
    np.savez_compressed(payload, **arrays)
    session.merge(BacktestEquityBlob(job_id=job_id, n_points=len(ts), data=payload.getvalue()))


def load_equity_curve(session, job_id):
    """Return one run's curve as {"timestamp": [str], "equity"/...: ndarray}, or None.

    Reads the compressed blob; runs stored before the blob format fall back to
    the legacy one-row-per-bar table.
    """
    import numpy as np
    import pandas as pd

    blob = session.query(BacktestEquityBlob).filter_by(job_id=job_id).first()
    if blob:
        with np.load(io.BytesIO(blob.data), allow_pickle=False) as loaded:
            return {
                "timestamp": pd.to_datetime(loaded["ts"], unit="s", utc=True).tz_convert("Asia/Kolkata").tz_localize(None).strftime("%Y-%m-%d %H:%M:%S").tolist(),
                **{key: loaded[key].copy() for key in ("equity", "drawdown_pct", "cash", "positions_value")},
            }
    rows = session.query(
        BacktestEquityCurve.timestamp, BacktestEquityCurve.equity,
        BacktestEquityCurve.drawdown_pct, BacktestEquityCurve.cash,
        BacktestEquityCurve.positions_value,
    ).filter_by(job_id=job_id).order_by(BacktestEquityCurve.id.asc()).all()
    if not rows:
        return None
    return {
        "timestamp": [r.timestamp for r in rows],
        **{key: np.asarray([getattr(r, key) for r in rows], dtype=np.float64)
           for key in ("equity", "drawdown_pct", "cash", "positions_value")},
    }


def delete_equity_curves(session, job_ids):
    if not job_ids:
        return
    session.query(BacktestEquityBlob).filter(BacktestEquityBlob.job_id.in_(job_ids)).delete(synchronize_session=False)
    session.query(BacktestEquityCurve).filter(BacktestEquityCurve.job_id.in_(job_ids)).delete(synchronize_session=False)


def reclaim_space(max_pages: int = 4000):
    """Hand up to ``max_pages`` free pages back to the filesystem (SQLite only).

    Needs ``auto_vacuum = INCREMENTAL`` (set by init_backtest_db on new files
    and by upgrade/migrate_backtest_storage.py on existing ones); otherwise it
    is a harmless no-op. Never raises.
    """
    if not BACKTEST_DATABASE_URL.startswith("sqlite"):
        return
    pages = max(0, int(max_pages))
    if pages == 0:
        return
    try:
        raw = engine.raw_connection()
        try:
            cursor = raw.cursor()
            # The pragma frees ONE page per statement step: it only reaches
            # max_pages if the cursor is driven to completion.
            cursor.execute(f"PRAGMA incremental_vacuum({pages})")
            cursor.fetchall()
            cursor.close()
            raw.commit()
        finally:
            raw.close()
    except Exception:
        logger.exception("Failed to reclaim backtest database space")


def init_backtest_db():
    """Initialize backtest database tables."""
    try:
        if BACKTEST_DATABASE_URL.startswith("sqlite"):
            db_path = engine.url.database
            is_new_file = not db_path or db_path == ":memory:" or not os.path.exists(db_path) or os.path.getsize(db_path) == 0
            with engine.connect() as conn:
                if is_new_file:
                    conn.exec_driver_sql("PRAGMA auto_vacuum=INCREMENTAL")
                    conn.exec_driver_sql("VACUUM")
                conn.commit()
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
            delete_equity_curves(session, job_ids)
            session.query(BacktestTrade).filter(BacktestTrade.job_id.in_(job_ids)).delete(synchronize_session=False)
            session.query(BacktestRun).filter(BacktestRun.job_id.in_(job_ids)).delete(synchronize_session=False)
            session.commit()
            reclaim_space()
            logger.info(f"Cleaned up {len(job_ids)} backtest runs older than {days_to_keep} days.")
    except Exception as e:
        session.rollback()
        logger.exception(f"Error cleaning up old backtest runs: {e}")
    finally:
        db_session.remove()
