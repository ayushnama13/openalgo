#!/usr/bin/env python3
"""Add saved run settings to backtest strategy templates.

No backfill: NULL settings_json means the template has no saved run settings.
Idempotent, safe to re-run. Supports --status.
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

TABLE = "backtest_strategies"
COLUMN = "settings_json"


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
    return {column["name"] for column in inspector.get_columns(TABLE)}


def migrate() -> dict:
    engine = create_engine(_resolve_db_url())
    summary = {"added": False, "already": False, "missing_table": False}
    try:
        existing = _existing_columns(engine)
        if existing is None:
            summary["missing_table"] = True
            return summary
        if COLUMN in existing:
            summary["already"] = True
            return summary
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} TEXT"))
        summary["added"] = True
        logger.info(f"Added column: {TABLE}.{COLUMN}")
    finally:
        engine.dispose()
    return summary


def main():
    logger.info("Backtest strategy template settings migration")
    logger.info("-" * 60)
    if "--status" in sys.argv:
        engine = create_engine(_resolve_db_url())
        try:
            existing = _existing_columns(engine)
        finally:
            engine.dispose()
        if existing is None:
            logger.info(f"  {TABLE}: table not created yet (created on first start)")
        else:
            logger.info(f"  {TABLE}.{COLUMN}: {'present' if COLUMN in existing else 'missing'}")
        return 0
    summary = migrate()
    if summary["missing_table"]:
        logger.info(f"{TABLE} not created yet; it is created with this column on first start")
    else:
        logger.info(f"Column added: {summary['added']}, already present: {summary['already']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
