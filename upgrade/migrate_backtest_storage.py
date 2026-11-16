#!/usr/bin/env python3
"""
Migration: compact backtest storage.

backtest_equity_blob (new table)  one compressed row per run instead of one row per bar
backtest_equity_curve             legacy rows are converted to a blob per job, then deleted
idx_backtest_trades_job_id        dropped: duplicates ix_backtest_trades_job_id
idx_backtest_equity_job_id        dropped: duplicates ix_backtest_equity_curve_job_id
auto_vacuum                       set to INCREMENTAL, followed by one VACUUM so the file
                                  gives back the pages freed by earlier deletes

Conversion is lossless (timestamps and values round-trip exactly) and runs one
job per transaction. Idempotent, safe to re-run. Supports --status.

Stop the application before applying: VACUUM needs exclusive access.

Usage:
    cd upgrade
    uv run migrate_backtest_storage.py           # apply
    uv run migrate_backtest_storage.py --status  # report only
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT, ".env"))

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from database.backtest_db import (
    BACKTEST_DATABASE_URL,
    BacktestEquityBlob,
    BacktestEquityCurve,
    save_equity_curve,
)

DUPLICATE_INDEXES = (
    ("backtest_trades", "idx_backtest_trades_job_id", "ix_backtest_trades_job_id"),
    ("backtest_equity_curve", "idx_backtest_equity_job_id", "ix_backtest_equity_curve_job_id"),
)


def _resolve_url():
    url = BACKTEST_DATABASE_URL
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        url = "sqlite:///" + os.path.join(ROOT, url[len("sqlite:///"):])
    return url


def _file_size(engine) -> int:
    path = engine.url.database
    return os.path.getsize(path) if path and os.path.exists(path) else 0


def _pending(engine) -> dict:
    """What a real run would still change."""
    tables = set(inspect(engine).get_table_names())
    is_sqlite = engine.dialect.name == "sqlite"
    state = {
        "blob_table": "backtest_equity_blob" in tables,
        "jobs_to_convert": 0,
        "duplicate_indexes": [],
        "auto_vacuum": None,
        "free_pages": None,
        "file_bytes": _file_size(engine),
    }
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        if "backtest_equity_curve" in tables:
            legacy = {row[0] for row in session.query(BacktestEquityCurve.job_id).distinct().all()}
            done = set()
            if state["blob_table"]:
                done = {row[0] for row in session.query(BacktestEquityBlob.job_id).all()}
            state["jobs_to_convert"] = len(legacy - done)
    finally:
        session.close()
    if is_sqlite:
        with engine.connect() as conn:
            for table, name, twin in DUPLICATE_INDEXES:
                present = {row[1] for row in conn.exec_driver_sql(f"PRAGMA index_list('{table}')").all()}
                if name in present and twin in present:
                    state["duplicate_indexes"].append(name)
            state["auto_vacuum"] = conn.exec_driver_sql("PRAGMA auto_vacuum").scalar()
            state["free_pages"] = conn.exec_driver_sql("PRAGMA freelist_count").scalar()
    return state


def migrate(status_only=False):
    engine = create_engine(_resolve_url())
    summary = {"converted": 0, "indexes_dropped": [], "auto_vacuum": None}
    try:
        state = _pending(engine)
        if status_only:
            print(f"backtest_equity_blob: {'present' if state['blob_table'] else 'missing'}")
            print(f"legacy jobs to convert: {state['jobs_to_convert']}")
            print(f"duplicate indexes to drop: {state['duplicate_indexes'] or 'none'}")
            print(f"auto_vacuum: {state['auto_vacuum']} (2 = incremental)")
            print(f"file size: {state['file_bytes']} bytes, free pages: {state['free_pages']}")
            summary["auto_vacuum"] = state["auto_vacuum"]
            return summary

        BacktestEquityBlob.__table__.create(engine, checkfirst=True)
        Session = sessionmaker(bind=engine)
        session = Session()
        try:
            if "backtest_equity_curve" in inspect(engine).get_table_names():
                job_ids = [row[0] for row in session.query(BacktestEquityCurve.job_id).distinct().all()]
                for job_id in job_ids:
                    if session.query(BacktestEquityBlob.job_id).filter_by(job_id=job_id).first():
                        continue  # never overwrite a blob that already exists
                    rows = (
                        session.query(
                            BacktestEquityCurve.timestamp, BacktestEquityCurve.equity,
                            BacktestEquityCurve.drawdown_pct, BacktestEquityCurve.cash,
                            BacktestEquityCurve.positions_value,
                        )
                        .filter_by(job_id=job_id)
                        .order_by(BacktestEquityCurve.id)
                        .all()
                    )
                    save_equity_curve(
                        session, job_id, [r[0] for r in rows], [r[1] for r in rows],
                        [r[2] for r in rows], [r[3] for r in rows], [r[4] for r in rows],
                    )
                    session.query(BacktestEquityCurve).filter_by(job_id=job_id).delete(
                        synchronize_session=False
                    )
                    session.commit()
                    summary["converted"] += 1
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        if engine.dialect.name == "sqlite":
            with engine.connect() as conn:
                for table, name, twin in DUPLICATE_INDEXES:
                    present = {
                        row[1] for row in conn.exec_driver_sql(f"PRAGMA index_list('{table}')").all()
                    }
                    if name in present and twin in present:
                        conn.exec_driver_sql(f"DROP INDEX {name}")
                        summary["indexes_dropped"].append(name)
                conn.commit()
                summary["auto_vacuum"] = conn.exec_driver_sql("PRAGMA auto_vacuum").scalar()
                if summary["auto_vacuum"] != 2:
                    before = _file_size(engine)
                    conn.exec_driver_sql("PRAGMA auto_vacuum=INCREMENTAL")
                    conn.commit()
                    conn.exec_driver_sql("VACUUM")
                    conn.commit()
                    summary["auto_vacuum"] = conn.exec_driver_sql("PRAGMA auto_vacuum").scalar()
                    print(f"Database size: {before} -> {_file_size(engine)} bytes")
        return summary
    finally:
        engine.dispose()


def main():
    status_only = "--status" in sys.argv
    summary = migrate(status_only)
    if status_only:
        print("Status only - no changes made.")
    else:
        print(f"Migration result: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
