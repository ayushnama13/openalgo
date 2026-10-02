#!/usr/bin/env python3
"""
Migration: columns for the event-driven backtest engine.

backtest_runs   engine_options   TEXT         JSON of cost / execution / strategy parameters
                code_hash        VARCHAR(64)  sha256 of the strategy source
                data_fingerprint TEXT         JSON per symbol: bars, first/last timestamp, sha256
backtest_trades mae_pct          FLOAT        max adverse excursion, % of entry price
                mfe_pct          FLOAT        max favourable excursion, % of entry price

Old rows keep NULL in every new column: nothing can be derived for a run that
was executed before the inputs were recorded, and a made-up default would read
as real data. Idempotent, safe to re-run. Supports --status.

Usage:
    cd upgrade
    uv run migrate_backtest_engine_v2.py           # apply
    uv run migrate_backtest_engine_v2.py --status  # report only
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

NEW_COLUMNS = {
    "backtest_runs": [
        ("engine_options", "TEXT"),
        ("code_hash", "VARCHAR(64)"),
        ("data_fingerprint", "TEXT"),
    ],
    "backtest_trades": [
        ("mae_pct", "FLOAT"),
        ("mfe_pct", "FLOAT"),
    ],
}


def _resolve_db_url() -> str:
    """Resolve the backtest database URL, rebased onto the repo root."""
    url = os.getenv("BACKTEST_DATABASE_URL", "sqlite:///db/backtest.db")
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        rel = url[len("sqlite:///") :]
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        url = f"sqlite:///{os.path.join(root, rel)}"
    return url


def migrate() -> dict:
    """Add any missing columns. Returns {"added": [...], "already": [...], "missing_tables": [...]}."""
    engine = create_engine(_resolve_db_url())
    inspector = inspect(engine)
    summary = {"added": [], "already": [], "missing_tables": []}
    tables = set(inspector.get_table_names())

    with engine.connect() as conn:
        for table, columns in NEW_COLUMNS.items():
            if table not in tables:
                summary["missing_tables"].append(table)
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            for column, coltype in columns:
                if column in existing:
                    summary["already"].append(f"{table}.{column}")
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"))
                conn.commit()
                summary["added"].append(f"{table}.{column}")
                logger.info(f"Added column: {table}.{column}")
    engine.dispose()
    return summary


def main():
    status_only = "--status" in sys.argv
    logger.info("Backtest engine v2 migration (reproducibility + MAE/MFE columns)")
    logger.info("-" * 60)

    if status_only:
        engine = create_engine(_resolve_db_url())
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        for table, columns in NEW_COLUMNS.items():
            if table not in tables:
                logger.info(f"  {table}: table not created yet")
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            for column, _ in columns:
                logger.info(f"  {table}.{column}: {'present' if column in existing else 'missing'}")
        engine.dispose()
        return 0

    summary = migrate()
    logger.info(f"Columns added: {len(summary['added'])}, already present: {len(summary['already'])}")
    if summary["missing_tables"]:
        logger.info(f"Tables not created yet (created on first start): {summary['missing_tables']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
