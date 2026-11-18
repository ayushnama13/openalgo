#!/usr/bin/env python3
"""
Migration: universe-mode backtests and the run research log.

backtest_runs  mode      VARCHAR(16)  "stock" | "universe" | "scan"
               universe  VARCHAR(32)  index universe id (e.g. NIFTY50), NULL for stock runs
               starred   INTEGER      0/1 star from the runs list (NOT NULL DEFAULT 0)
               notes     TEXT         free-text research note

No backfill: every existing row predates universe mode, so NULL mode already
reads as "stock" and NULL universe as "none"; starred starts at 0 because no run
has ever been starred. Idempotent, safe to re-run. Supports --status.

Usage:
    cd upgrade
    uv run migrate_backtest_universe.py           # apply
    uv run migrate_backtest_universe.py --status  # report only
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
NEW_COLUMNS = [
    ("mode", "VARCHAR(16)"),
    ("universe", "VARCHAR(32)"),
    ("starred", "INTEGER NOT NULL DEFAULT 0"),
    ("notes", "TEXT"),
]


def _resolve_db_url() -> str:
    """Resolve the backtest database URL, rebased onto the repo root."""
    url = os.getenv("BACKTEST_DATABASE_URL", "sqlite:///db/backtest.db")
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        rel = url[len("sqlite:///") :]
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        url = f"sqlite:///{os.path.join(root, rel)}"
    return url


def _existing_columns(engine) -> set[str] | None:
    inspector = inspect(engine)
    if TABLE not in set(inspector.get_table_names()):
        return None
    return {c["name"] for c in inspector.get_columns(TABLE)}


def migrate() -> dict:
    """Add any missing columns. Returns {"added": [...], "already": [...], "missing_table": bool}."""
    engine = create_engine(_resolve_db_url())
    summary = {"added": [], "already": [], "missing_table": False}
    try:
        existing = _existing_columns(engine)
        if existing is None:
            summary["missing_table"] = True
            return summary
        with engine.connect() as conn:
            for column, coltype in NEW_COLUMNS:
                if column in existing:
                    summary["already"].append(column)
                    continue
                conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {column} {coltype}"))
                conn.commit()
                summary["added"].append(column)
                logger.info(f"Added column: {TABLE}.{column}")
            conn.execute(
                text(f"CREATE INDEX IF NOT EXISTS ix_backtest_runs_starred ON {TABLE} (starred)")
            )
            conn.commit()
    finally:
        engine.dispose()
    return summary


def main():
    logger.info("Backtest universe mode migration (mode, universe, starred, notes)")
    logger.info("-" * 60)

    if "--status" in sys.argv:
        engine = create_engine(_resolve_db_url())
        try:
            existing = _existing_columns(engine)
        finally:
            engine.dispose()
        if existing is None:
            logger.info(f"  {TABLE}: table not created yet (created on first start)")
            return 0
        for column, _ in NEW_COLUMNS:
            logger.info(f"  {TABLE}.{column}: {'present' if column in existing else 'missing'}")
        return 0

    summary = migrate()
    if summary["missing_table"]:
        logger.info(f"{TABLE} not created yet; it is created with these columns on first start")
        return 0
    logger.info(f"Columns added: {len(summary['added'])}, already present: {len(summary['already'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
