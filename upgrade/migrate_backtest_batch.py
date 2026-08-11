#!/usr/bin/env python3
"""
Migration: add multi-timeframe batch grouping columns to backtest_runs.

One POST /backtest/api/run fans out across N timeframes; every sibling run
shares a batch_id so the UI can render a comparison matrix plus one full
view per timeframe. Columns added, if missing:

  batch_id    VARCHAR(64)   shared group key; NULL means the row predates batches
  batch_name  VARCHAR(128)  user-facing label, same across siblings
  batch_seq   INTEGER       stable tab order (index into the requested interval list)

Backfill: legacy rows get batch_id = job_id and batch_seq = 0, so every
existing run is a batch of one and the UI needs no special case for them.

Idempotent, safe to re-run. Supports --status to report without changing.

Usage:
    cd upgrade
    uv run migrate_backtest_batch.py           # apply
    uv run migrate_backtest_batch.py --status  # report only
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text

env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(env_path)

from utils.logging import get_logger

logger = get_logger(__name__)

TABLE = "backtest_runs"

ADD_COLUMNS = [
    ("batch_id", "VARCHAR(64)"),
    ("batch_name", "VARCHAR(128)"),
    ("batch_seq", "INTEGER NOT NULL DEFAULT 0"),
]


def _resolve_db_url() -> str:
    """Resolve the backtest database URL, rebased onto the repo root."""
    url = os.getenv("BACKTEST_DATABASE_URL", "sqlite:///db/backtest.db")
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        rel = url[len("sqlite:///") :]
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        url = f"sqlite:///{os.path.join(root, rel)}"
    return url


def migrate() -> dict:
    """Apply the batch grouping columns and backfill legacy rows.

    Returns summary dict.
    """
    url = _resolve_db_url()
    engine = create_engine(url)
    inspector = inspect(engine)

    summary = {"added": [], "already": [], "missing_table": False, "backfilled": 0}

    if TABLE not in inspector.get_table_names():
        logger.info("backtest_runs table does not exist yet — nothing to migrate")
        summary["missing_table"] = True
        engine.dispose()
        return summary

    existing = {col["name"] for col in inspector.get_columns(TABLE)}

    with engine.connect() as conn:
        for column, coltype in ADD_COLUMNS:
            if column in existing:
                summary["already"].append(column)
                continue
            try:
                conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {column} {coltype}"))
                conn.commit()
                summary["added"].append(column)
                logger.info(f"Added column: {column}")
            except Exception as col_err:  # pragma: no cover - defensive
                logger.warning(f"Could not add column {column}: {col_err}")

        # Backfill: every legacy row becomes a batch of one. Guarded on the old
        # value (NULL/empty) so a batch the user already created is left alone.
        result = conn.execute(
            text(
                f"UPDATE {TABLE} SET batch_id = job_id "
                f"WHERE batch_id IS NULL OR batch_id = ''"
            )
        )
        conn.commit()
        summary["backfilled"] = result.rowcount

        # The model declares index=True on batch_id — fresh installs get it
        # from create_all, but ALTER TABLE cannot add an index, so build it
        # here for upgraded databases.
        conn.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS idx_backtest_runs_batch_id "
                f"ON {TABLE} (batch_id)"
            )
        )
        conn.commit()

    engine.dispose()
    return summary


def main():
    status_only = "--status" in sys.argv
    logger.info("Backtest batch grouping migration (multi-timeframe sweeps)")
    logger.info("-" * 60)

    url = _resolve_db_url()
    engine = create_engine(url)
    inspector = inspect(engine)

    if status_only:
        logger.info("Status only — no changes made.")
        if TABLE not in inspector.get_table_names():
            logger.info("No backtest_runs table yet — migrations run on first upgrade.")
            engine.dispose()
            return 0
        existing = {col["name"] for col in inspector.get_columns(TABLE)}
        for c in ADD_COLUMNS:
            marker = "present" if c[0] in existing else "missing"
            logger.info(f"  backtest_runs.{c[0]}: {marker}")
        if "batch_id" in existing:
            with engine.connect() as conn:
                null_rows = conn.execute(
                    text(f"SELECT COUNT(*) FROM {TABLE} WHERE batch_id IS NULL OR batch_id = ''")
                ).scalar()
                logger.info(f"  rows pending batch_id backfill: {null_rows}")
        engine.dispose()
        return 0

    summary = migrate()
    engine.dispose()

    if summary["missing_table"]:
        logger.info("No backtest_runs table yet — nothing to migrate.")
        return 0

    logger.info(f"Columns already present: {len(summary['already'])}")
    logger.info(f"Columns added: {len(summary['added'])}")
    logger.info(f"Rows backfilled as batches of one: {summary['backfilled']}")

    if summary["added"] or summary["backfilled"]:
        logger.info("Backtest batch grouping migration completed.")
    else:
        logger.info("Already up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
