#!/usr/bin/env python3
"""
Migration script for Replay Mode storage hardening.

Applies to data/replay/replay_metadata.db:

1. Creates the replay_auto_record_symbols table if it is missing (installs that
   predate the always-record list).
2. De-duplicates replay_recording_targets, replay_auto_record_symbols and
   replay_symbol_date_index, then adds the unique indexes the ORM now declares.
   Without them, two processes arming the same symbol race into duplicate rows.
3. Adds date indexes. The tick path reads replay_recording_targets by date every
   few seconds and the table had no index at all.
4. Prunes replay_recording_targets rows older than the retention window - they
   are re-armed daily and otherwise accumulate without bound.
5. Repoints replay_symbol_date_index.file_path at the per-day directory used by
   the append-only writer, for days already stored that way.

Idempotent. Safe to re-run.

Usage:
    python migrate_replay_storage.py
    python migrate_replay_storage.py --status
"""

import os
import sys
from datetime import datetime, timedelta

from sqlalchemy import create_engine, inspect, text

# Set UTF-8 encoding for output to handle Unicode characters on Windows
if sys.platform == "win32":
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.logging import get_logger  # noqa: E402

logger = get_logger(__name__)

RETENTION_DAYS = 7

# (index name, table, columns, unique)
INDEXES = [
    ("uq_replay_target", "replay_recording_targets", ("symbol", "exchange", "date"), True),
    ("ix_replay_target_date", "replay_recording_targets", ("date",), False),
    ("uq_replay_auto_record", "replay_auto_record_symbols", ("symbol", "exchange"), True),
    ("uq_replay_index_symbol_date", "replay_symbol_date_index", ("symbol", "date"), True),
    ("ix_replay_index_date", "replay_symbol_date_index", ("date",), False),
]

AUTO_RECORD_DDL = """
CREATE TABLE IF NOT EXISTS replay_auto_record_symbols (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    symbol VARCHAR(50) NOT NULL,
    exchange VARCHAR(20) NOT NULL,
    created_at DATETIME
)
"""


def get_project_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_replay_db_path():
    return os.path.join(get_project_root(), "data", "replay", "replay_metadata.db")


def get_engine():
    path = get_replay_db_path()
    if not os.path.exists(path):
        return None
    return create_engine(f"sqlite:///{path}")


def table_exists(engine, table_name):
    return table_name in inspect(engine).get_table_names()


def index_exists(engine, table_name, index_name):
    if not table_exists(engine, table_name):
        return False
    return any(idx["name"] == index_name for idx in inspect(engine).get_indexes(table_name))


def dedupe(conn, table, columns):
    """Keep the lowest-id row per column tuple. Required before a unique index."""
    col_list = ", ".join(columns)
    result = conn.execute(
        text(
            f"DELETE FROM {table} WHERE id NOT IN "  # noqa: S608 - names are module constants
            f"(SELECT MIN(id) FROM {table} GROUP BY {col_list})"
        )
    )
    return result.rowcount or 0


def report_status(engine):
    print("  Replay storage migration status:")
    if engine is None:
        print("    [PENDING] replay_metadata.db not created yet (runs on first app start)")
        return True

    if not table_exists(engine, "replay_auto_record_symbols"):
        print("    [PENDING] replay_auto_record_symbols table missing")
    else:
        print("    [OK] replay_auto_record_symbols table present")

    for name, table, _cols, _unique in INDEXES:
        if not table_exists(engine, table):
            print(f"    [SKIP] {table} does not exist")
        elif index_exists(engine, table, name):
            print(f"    [OK] {name}")
        else:
            print(f"    [PENDING] {name}")

    if table_exists(engine, "replay_recording_targets"):
        cutoff = (datetime.now().date() - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
        with engine.connect() as conn:
            stale = conn.execute(
                text("SELECT COUNT(*) FROM replay_recording_targets WHERE date < :cutoff"),
                {"cutoff": cutoff},
            ).scalar()
        print(f"    [INFO] {stale} recording target row(s) older than {RETENTION_DAYS} days")

    return True


def migrate():
    engine = get_engine()
    if engine is None:
        print("  [SKIP] replay_metadata.db not found - nothing to migrate")
        print("         It is created automatically the first time Replay Mode runs.")
        return True

    if "--status" in sys.argv:
        return report_status(engine)

    try:
        with engine.begin() as conn:
            # 1. Auto-record table
            if not table_exists(engine, "replay_auto_record_symbols"):
                conn.execute(text(AUTO_RECORD_DDL))
                print("  [OK] Created replay_auto_record_symbols")
            else:
                print("  [SKIP] replay_auto_record_symbols already exists")

            # 2 + 3. De-duplicate, then index
            for name, table, cols, unique in INDEXES:
                if not table_exists(engine, table):
                    print(f"  [SKIP] {table} does not exist")
                    continue
                if index_exists(engine, table, name):
                    print(f"  [SKIP] {name} already present")
                    continue

                if unique:
                    removed = dedupe(conn, table, cols)
                    if removed:
                        print(f"  [OK] Removed {removed} duplicate row(s) from {table}")

                unique_sql = "UNIQUE " if unique else ""
                conn.execute(
                    text(
                        f"CREATE {unique_sql}INDEX IF NOT EXISTS {name} "  # noqa: S608
                        f"ON {table} ({', '.join(cols)})"
                    )
                )
                print(f"  [OK] Created index {name}")

            # 4. Prune stale recording targets
            if table_exists(engine, "replay_recording_targets"):
                cutoff = (datetime.now().date() - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
                result = conn.execute(
                    text("DELETE FROM replay_recording_targets WHERE date < :cutoff"),
                    {"cutoff": cutoff},
                )
                if result.rowcount:
                    print(f"  [OK] Pruned {result.rowcount} stale recording target row(s)")
                else:
                    print("  [SKIP] No stale recording targets")

            # 5. Repoint file_path at the per-day directory where one exists.
            #    Old rows point at data/replay/SYM/DATE.parquet, which the reader
            #    still honours, so this is cosmetic and only applied when the
            #    directory layout is already in use.
            if table_exists(engine, "replay_symbol_date_index"):
                rows = conn.execute(
                    text("SELECT id, symbol, date, file_path FROM replay_symbol_date_index")
                ).fetchall()
                repointed = 0
                for row_id, symbol, date_str, file_path in rows:
                    day_dir = os.path.join(
                        get_project_root(), "data", "replay", str(symbol).upper(), str(date_str)
                    )
                    if os.path.isdir(day_dir) and file_path != day_dir:
                        conn.execute(
                            text("UPDATE replay_symbol_date_index SET file_path = :p WHERE id = :i"),
                            {"p": day_dir, "i": row_id},
                        )
                        repointed += 1
                if repointed:
                    print(f"  [OK] Repointed {repointed} index row(s) at their day directory")
                else:
                    print("  [SKIP] No index rows needed repointing")

        return True
    except Exception as exc:
        print(f"  [FAIL] Replay storage migration error: {exc}")
        logger.exception("Replay storage migration failed")
        return False
    finally:
        engine.dispose()


if __name__ == "__main__":
    print("Replay Mode storage migration")
    ok = migrate()
    sys.exit(0 if ok else 1)
