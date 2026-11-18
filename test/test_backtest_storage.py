import os
import subprocess
import sys

import numpy as np
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import database.backtest_db as bdb
from database.backtest_db import BacktestEquityBlob, BacktestEquityCurve


def _session(engine):
    return sessionmaker(bind=engine)()


def test_blob_round_trip_exact_timestamps_and_values(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'roundtrip.db'}")
    bdb.Base.metadata.create_all(engine)
    session = _session(engine)
    timestamps = ["2024-01-02 09:15:00", "2024-01-02 09:20:00"]
    vals = np.array([100.25, 101.5])
    bdb.save_equity_curve(session, "job", timestamps, vals, [-1.25, 0], [90, 91], [10.25, 10.5])
    session.commit()
    got = bdb.load_equity_curve(session, "job")
    assert got["timestamp"] == timestamps
    np.testing.assert_array_equal(got["equity"], vals)
    np.testing.assert_array_equal(got["drawdown_pct"], [-1.25, 0])
    np.testing.assert_array_equal(got["cash"], [90, 91])
    np.testing.assert_array_equal(got["positions_value"], [10.25, 10.5])
    assert session.query(BacktestEquityBlob).filter_by(job_id="job").one().n_points == 2
    session.close()
    engine.dispose()


def test_legacy_row_fallback_and_delete(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    bdb.Base.metadata.create_all(engine)
    session = _session(engine)
    session.add_all([
        BacktestEquityCurve(job_id="old", timestamp="2024-01-01 09:15:00", equity=10,
                            drawdown_pct=0, cash=8, positions_value=2),
        BacktestEquityCurve(job_id="old", timestamp="2024-01-01 09:20:00", equity=11,
                            drawdown_pct=0, cash=8, positions_value=3),
    ])
    session.commit()
    result = bdb.load_equity_curve(session, "old")
    assert result["timestamp"] == ["2024-01-01 09:15:00", "2024-01-01 09:20:00"]
    np.testing.assert_array_equal(result["equity"], [10, 11])
    bdb.save_equity_curve(session, "new", result["timestamp"], result["equity"],
                          result["drawdown_pct"], result["cash"], result["positions_value"])
    session.commit()
    bdb.delete_equity_curves(session, ["old", "new"])
    session.commit()
    assert bdb.load_equity_curve(session, "old") is None
    assert bdb.load_equity_curve(session, "new") is None
    session.close()
    engine.dispose()


def test_fresh_database_uses_incremental_vacuum_and_reclaims(tmp_path, monkeypatch):
    db_path = tmp_path / "fresh.db"
    engine = create_engine(f"sqlite:///{db_path}")
    monkeypatch.setattr(bdb, "engine", engine)
    monkeypatch.setattr(bdb, "BACKTEST_DATABASE_URL", f"sqlite:///{db_path}")
    bdb.init_backtest_db()
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA auto_vacuum").scalar() == 2
    session = _session(engine)
    session.add_all([
        BacktestEquityCurve(job_id="large", timestamp=f"2024-01-01 09:{i % 60:02d}:00",
                            equity=float(i), drawdown_pct=0, cash=0, positions_value=0)
        for i in range(30000)
    ])
    session.commit()
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    before = db_path.stat().st_size
    session.query(BacktestEquityCurve).delete()
    session.commit()
    session.close()
    with engine.connect() as conn:
        free_before = conn.exec_driver_sql("PRAGMA freelist_count").scalar()
    assert free_before > 100
    bdb.reclaim_space()
    with engine.connect() as conn:
        # One call must release every freed page (well under the 4000-page cap),
        # not one page per call.
        assert conn.exec_driver_sql("PRAGMA freelist_count").scalar() == 0
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    assert db_path.stat().st_size < before / 4
    engine.dispose()


def test_migration_converts_drops_duplicates_and_is_idempotent(tmp_path):
    db_path = tmp_path / "migration.db"
    engine = create_engine(f"sqlite:///{db_path}")
    bdb.Base.metadata.create_all(
        engine,
        tables=[table for table in bdb.Base.metadata.sorted_tables if table.name != "backtest_equity_blob"],
    )
    with engine.begin() as conn:
        conn.execute(text("CREATE INDEX idx_backtest_trades_job_id ON backtest_trades(job_id)"))
        conn.execute(text("CREATE INDEX idx_backtest_equity_job_id ON backtest_equity_curve(job_id)"))
        conn.execute(text("INSERT INTO backtest_equity_curve (job_id,timestamp,equity,drawdown_pct,cash,positions_value) VALUES ('legacy','2024-01-01 09:15:00',100,0,90,10)"))
    engine.dispose()
    env = {**os.environ, "BACKTEST_DATABASE_URL": f"sqlite:///{db_path}"}
    script = os.path.join(os.path.dirname(os.path.dirname(__file__)), "upgrade", "migrate_backtest_storage.py")
    subprocess.run([sys.executable, script], env=env, check=True, capture_output=True, text=True)
    engine = create_engine(f"sqlite:///{db_path}")
    session = _session(engine)
    assert bdb.load_equity_curve(session, "legacy")["equity"].tolist() == [100.0]
    assert session.query(BacktestEquityCurve).count() == 0
    session.close()
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA auto_vacuum").scalar() == 2
        trades_idx = {r[1] for r in conn.exec_driver_sql("PRAGMA index_list('backtest_trades')")}
        curve_idx = {r[1] for r in conn.exec_driver_sql("PRAGMA index_list('backtest_equity_curve')")}
        assert "idx_backtest_trades_job_id" not in trades_idx
        assert "idx_backtest_equity_job_id" not in curve_idx
    engine.dispose()
    second = subprocess.run([sys.executable, script], env=env, check=True, capture_output=True, text=True)
    assert "'converted': 0" in second.stdout


def test_even_downsample_helper_keeps_last_point():
    from blueprints.backtest import _downsample_curve

    points = list(range(10001))
    sampled = _downsample_curve(points, 5000)
    assert len(sampled) <= 5000
    assert sampled[-1] == points[-1]
