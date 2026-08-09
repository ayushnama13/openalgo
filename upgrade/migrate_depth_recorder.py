#!/usr/bin/env python3
"""
Migration: add order-count and last-trade-time columns to depth_ticks, and
fix the depth_recorder_config uniqueness bug.

Depth analytics (avg order size, churn, Lee-Ready CVD alignment) needs the
number of distinct limit orders behind each level and the exchange last-trade
time. These were not captured when the recorder first shipped.

Adds, if missing:
  ltt            FLOAT                  exchange last-trade epoch
  bid1_orders ... bid5_orders INTEGER   # distinct orders at each bid level
  ask1_orders ... ask5_orders INTEGER   # distinct orders at each ask level

Second part: depth_recorder_config originally declared UNIQUE on symbol
alone, so recording the same base symbol on two exchanges (RELIANCE/NSE +
RELIANCE/BSE) blew up with IntegrityError. SQLite cannot alter a UNIQUE
constraint, so when the old single-column autoindex is detected the table is
rebuilt with UNIQUE (symbol, exchange).

Idempotent, safe to re-run. Supports --status to report without changing.

Usage:
    cd upgrade
    uv run migrate_depth_recorder.py           # apply
    uv run migrate_depth_recorder.py --status  # report only
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

TABLE = "depth_ticks"

# (column_name, sqlite_type)  -- ltt is FLOAT, everything else INTEGER
ADD_COLUMNS = [
    ("ltt", "FLOAT"),
    ("bid1_orders", "INTEGER"),
    ("bid2_orders", "INTEGER"),
    ("bid3_orders", "INTEGER"),
    ("bid4_orders", "INTEGER"),
    ("bid5_orders", "INTEGER"),
    ("ask1_orders", "INTEGER"),
    ("ask2_orders", "INTEGER"),
    ("ask3_orders", "INTEGER"),
    ("ask4_orders", "INTEGER"),
    ("ask5_orders", "INTEGER"),
]

# Recorder configs use (symbol, exchange) as the identity — the same base
# symbol may be recorded on two exchanges at once (RELIANCE on NSE and on
# BSE). The original schema declared unique=True on symbol alone, which made
# the second exchange's upsert_config fail with IntegrityError.
CONFIG_TABLE = "depth_recorder_config"

CONFIG_COLUMNS = ["id", "symbol", "exchange", "status", "created_at", "updated_at"]


def _resolve_db_url() -> str:
    """Resolve the depth recorder database URL, rebased onto the repo root.

    Only DEPTH_RECORDER_DATABASE_URL (or the default) — never DATABASE_URL:
    the running app binds the same default, so a DATABASE_URL fallback here
    silently migrates the main OpenAlgo database instead of the depth one.
    """
    url = os.getenv("DEPTH_RECORDER_DATABASE_URL", "sqlite:///db/depth_recorder.db")
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        rel = url[len("sqlite:///") :]
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        url = f"sqlite:///{os.path.join(root, rel)}"
    return url


def migrate() -> dict:
    """Apply the depth_ticks column additions and the config uniqueness fix.

    Returns summary dict.
    """
    url = _resolve_db_url()
    engine = create_engine(url)
    inspector = inspect(engine)

    summary = {"added": [], "already": [], "missing_table": False, "config_rebuilt": False}

    if TABLE not in inspector.get_table_names():
        logger.info(
            "depth_ticks table does not exist yet — recording not started; nothing to migrate"
        )
        summary["missing_table"] = True
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

        if CONFIG_TABLE in inspector.get_table_names():
            if _config_needs_rebuild(engine):
                _rebuild_config_table(engine)
                summary["config_rebuilt"] = True
                logger.info(
                    "depth_recorder_config rebuilt with composite unique "
                    "(symbol, exchange) — multi-exchange recording now works"
                )
            else:
                logger.info("depth_recorder_config unique constraint already correct")

    engine.dispose()
    return summary


def _config_needs_rebuild(engine) -> bool:
    """True when depth_recorder_config still has the old unique-symbol-only index.

    SQLite cannot drop or alter a UNIQUE constraint in place: the old schema's
    `sqlite_autoindex_depth_recorder_config_1` enforces unique symbol alone,
    so the only fix is a full table rebuild (create+copy+drop+rename).
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT name FROM pragma_index_list(:tbl) "
                "WHERE name LIKE 'sqlite_autoindex%' AND \"unique\" = 1"
            ),
            {"tbl": CONFIG_TABLE},
        ).fetchall()
        for (idx_name,) in rows:
            cols = [
                r[0]
                for r in conn.execute(
                    text("SELECT name FROM pragma_index_info(:idx)"), {"idx": idx_name}
                ).fetchall()
            ]
            if cols == ["symbol"]:
                return True
    return False


def _rebuild_config_table(engine) -> None:
    """Rebuild depth_recorder_config with a composite unique on (symbol, exchange)."""
    with engine.begin() as conn:
        conn.execute(
            text(
                f"CREATE TABLE {CONFIG_TABLE}_new ("
                "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, "
                "symbol VARCHAR(60) NOT NULL, "
                "exchange VARCHAR(20) NOT NULL, "
                "status VARCHAR(10) NOT NULL, "
                "created_at DATETIME NOT NULL, "
                "updated_at DATETIME NOT NULL, "
                f"CONSTRAINT uq_{CONFIG_TABLE}_symbol_exchange UNIQUE (symbol, exchange))"
            )
        )
        cols = ", ".join(CONFIG_COLUMNS)
        conn.execute(
            text(
                f"INSERT INTO {CONFIG_TABLE}_new ({cols}) "
                f"SELECT {cols} FROM {CONFIG_TABLE}"
            )
        )
        conn.execute(text(f"DROP TABLE {CONFIG_TABLE}"))
        conn.execute(text(f"ALTER TABLE {CONFIG_TABLE}_new RENAME TO {CONFIG_TABLE}"))


def main():
    status_only = "--status" in sys.argv
    logger.info("Depth Recorder schema migration (order columns + config uniqueness)")
    logger.info("-" * 60)

    if status_only:
        logger.info("Status only — no changes made.")
        url = _resolve_db_url()
        engine = create_engine(url)
        inspector = inspect(engine)
        if TABLE not in inspector.get_table_names():
            logger.info("No depth data yet — recorder creates depth_ticks on first write.")
            engine.dispose()
            return 0
        existing = {col["name"] for col in inspector.get_columns(TABLE)}
        for c in ADD_COLUMNS:
            marker = "present" if c[0] in existing else "missing"
            logger.info(f"  depth_ticks.{c[0]}: {marker}")
        if CONFIG_TABLE in inspector.get_table_names():
            marker = "REBUILD NEEDED" if _config_needs_rebuild(engine) else "correct"
            logger.info(f"  {CONFIG_TABLE} unique constraint: {marker}")
        else:
            logger.info(f"  {CONFIG_TABLE}: table missing (no recorders configured yet)")
        engine.dispose()
        return 0

    summary = migrate()

    if summary["missing_table"]:
        logger.info("No depth data yet — recorder creates depth_ticks on first write.")
        return 0

    logger.info(f"Columns already present: {len(summary['already'])}")
    logger.info(f"Columns added: {len(summary['added'])}")

    if summary["added"]:
        logger.info("Depth tick order columns added successfully.")
    else:
        logger.info("No columns needed adding — already up to date.")

    if summary["config_rebuilt"]:
        logger.info("depth_recorder_config rebuilt with composite unique (symbol, exchange).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
