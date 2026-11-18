#!/usr/bin/env python3
"""Create and backfill daily depth recorder session summaries.

Idempotent and safe to re-run. Supports --status to report without changes.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text

from utils.logging import get_logger

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
logger = get_logger(__name__)


def _resolve_db_url() -> str:
    url = os.getenv("DEPTH_RECORDER_DATABASE_URL", "sqlite:///db/depth_recorder.db")
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        url = f"sqlite:///{os.path.join(ROOT, url[len('sqlite:///') :])}"
    return url


def migrate() -> dict:
    engine = create_engine(_resolve_db_url())
    tables = inspect(engine).get_table_names()
    summary = {"created": False, "inserted": 0, "missing_ticks": "depth_ticks" not in tables}
    if summary["missing_ticks"]:
        engine.dispose()
        return summary
    with engine.begin() as conn:
        if "depth_sessions" not in tables:
            conn.execute(text(
                "CREATE TABLE depth_sessions ("
                "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, "
                "symbol VARCHAR(60) NOT NULL, exchange VARCHAR(20) NOT NULL, "
                "day VARCHAR(10) NOT NULL, first_tick DATETIME NOT NULL, "
                "last_tick DATETIME NOT NULL, n_ticks INTEGER NOT NULL, "
                "CONSTRAINT uq_depth_sessions_symbol_exchange_day UNIQUE (symbol, exchange, day))"
            ))
            summary["created"] = True
        result = conn.execute(text(
            "INSERT INTO depth_sessions (symbol, exchange, day, first_tick, last_tick, n_ticks) "
            "SELECT t.symbol, t.exchange, date(t.tick_time), min(t.tick_time), "
            "max(t.tick_time), count(*) FROM depth_ticks AS t "
            "WHERE NOT EXISTS (SELECT 1 FROM depth_sessions AS s WHERE "
            "s.symbol = t.symbol AND s.exchange = t.exchange AND s.day = date(t.tick_time)) "
            "GROUP BY t.symbol, t.exchange, date(t.tick_time)"
        ))
        summary["inserted"] = result.rowcount
    engine.dispose()
    return summary


def main() -> int:
    status_only = "--status" in sys.argv
    engine = create_engine(_resolve_db_url())
    tables = inspect(engine).get_table_names()
    if "depth_ticks" not in tables:
        logger.info("No depth data yet — depth_ticks table is missing.")
        engine.dispose()
        return 0
    if "depth_sessions" not in tables:
        logger.info("depth_sessions: missing")
        if not status_only:
            logger.info(f"Migration created table and backfilled summaries: {migrate()}")
        engine.dispose()
        return 0
    with engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM depth_sessions")).scalar_one()
        missing = conn.execute(text(
            "SELECT count(*) FROM (SELECT symbol, exchange, date(tick_time) AS day "
            "FROM depth_ticks GROUP BY symbol, exchange, date(tick_time)) AS d "
            "WHERE NOT EXISTS (SELECT 1 FROM depth_sessions AS s WHERE "
            "s.symbol=d.symbol AND s.exchange=d.exchange AND s.day=d.day)"
        )).scalar_one()
    logger.info(f"depth_sessions: present ({count} rows); missing summaries: {missing}")
    if not status_only:
        logger.info(f"Migration result: {migrate()}")
    else:
        logger.info("Status only — no changes made.")
    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
