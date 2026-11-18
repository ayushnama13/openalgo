# test/test_backtest_universe.py

"""Universe-mode backtests: bundled constituents, coverage, run planning and the scan fan-out.

Historify is mocked throughout (catalog ranges and OHLCV), so nothing touches
the real historify.duckdb file.
"""

import json
import os
import time

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("BACKTEST_DATABASE_URL", "sqlite:////tmp/opencode/backtest_tests.db")

import database.backtest_db as bdb
import services.backtest_service as svc
import services.backtest_universes as uni
from database.backtest_db import (
    BacktestEquityBlob,
    BacktestEquityCurve,
    BacktestRun,
    BacktestTrade,
    db_session,
)

SIMPLE_CODE = """
def strategy(df):
    df["signal"] = 0
    df.loc[df["close"] > df["close"].shift(1), "signal"] = 1
    df.loc[df["close"] < df["close"].shift(1), "signal"] = -1
    return df
"""


def _clear():
    session = db_session()
    for model in (BacktestEquityCurve, BacktestEquityBlob, BacktestTrade, BacktestRun):
        session.query(model).delete()
    session.commit()
    db_session.remove()


@pytest.fixture(autouse=True)
def _isolated_db():
    bdb.init_backtest_db()
    _clear()
    yield
    _clear()


def _daily_bars(n=120, seed=3):
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 1.0, n))
    epochs = (
        pd.date_range("2024-01-01 09:15:00", periods=n, freq="D", tz="Asia/Kolkata")
        .tz_convert("UTC")
        .astype("int64")
        // 10**9
    )
    return pd.DataFrame({
        "timestamp": epochs, "open": close, "high": close + 1, "low": close - 1,
        "close": close, "volume": [100000] * n,
    })


def _ranges_for(symbols):
    return {s: {"first_timestamp": 1704080700, "last_timestamp": 1714000000, "record_count": 100} for s in symbols}


def test_bundled_universes_are_well_formed():
    listing = uni.list_universes()
    ids = {u["id"]: u for u in listing["universes"]}
    assert listing["as_of"]
    for wanted, size in (("NIFTY50", 50), ("NIFTYNEXT50", 50), ("NIFTY100", 100), ("NIFTY200", 200)):
        assert ids[wanted]["count"] == size
    assert ids["NIFTY500"]["count"] >= 500
    for u in listing["universes"]:
        detail = uni.get_universe(u["id"])
        symbols = [c["symbol"] for c in detail["constituents"]]
        assert len(symbols) == len(set(symbols)), f"duplicate constituent in {u['id']}"
        assert all(s == s.upper() and s for s in symbols)
    # NIFTY 100 is NIFTY 50 plus NIFTY NEXT 50
    n50 = {c["symbol"] for c in uni.get_universe("NIFTY50")["constituents"]}
    nn50 = {c["symbol"] for c in uni.get_universe("NIFTYNEXT50")["constituents"]}
    n100 = {c["symbol"] for c in uni.get_universe("NIFTY100")["constituents"]}
    assert n50 | nn50 == n100


def test_universe_lookup_is_case_insensitive_and_unknown_raises():
    assert uni.get_universe("nifty50")["id"] == "NIFTY50"
    assert uni.get_universe("NOPE") is None
    with pytest.raises(ValueError, match="Unknown universe"):
        uni.universe_symbols("NOPE")


def test_coverage_reports_symbols_with_data_and_common_window(monkeypatch):
    members = [c["symbol"] for c in uni.get_universe("NIFTYIT")["constituents"]]
    have = members[:3]
    ranges = _ranges_for(have)
    ranges[have[0]] = {"first_timestamp": 1706745600, "last_timestamp": 1714000000, "record_count": 80}
    asked = []

    def fake_ranges(exchange, interval):
        asked.append((exchange, interval))
        return ranges

    monkeypatch.setattr(uni, "get_catalog_ranges", fake_ranges)
    cov = uni.universe_coverage("NIFTYIT", "1h")
    assert asked == [("NSE", "1m")]  # 1h is aggregated from stored 1m bars
    assert cov["count"] == len(members)
    assert cov["with_data"] == 3
    assert cov["common_first"] == "2024-02-01"  # the latest first bar among covered symbols
    assert {r["symbol"] for r in cov["constituents"] if r["has_data"]} == set(have)
    assert "survivorship" in cov["survivorship_note"]


def test_plan_runs_modes(monkeypatch):
    members = [c["symbol"] for c in uni.get_universe("NIFTYIT")["constituents"]]
    monkeypatch.setattr(svc, "get_catalog_ranges", lambda exch, iv: _ranges_for(members[:4]))

    runs, skipped, exch = svc._plan_runs("stock", "s", ["NSE:SBIN"], "NSE", ["D", "W"], None)
    assert [r["name"] for r in runs] == ["s (D)", "s (W)"] and not skipped and exch == "NSE"

    runs, skipped, _ = svc._plan_runs("universe", "u", [], "NSE", ["D"], "niftyit")
    assert len(runs) == 1 and runs[0]["name"] == "u"
    assert runs[0]["symbols"] == [f"NSE:{s}" for s in members[:4]]
    assert skipped == members[4:]

    runs, _, _ = svc._plan_runs("scan", "sc", [], "NSE", ["D"], "NIFTYIT")
    assert [r["symbols"] for r in runs] == [[f"NSE:{s}"] for s in members[:4]]
    assert runs[0]["name"] == f"sc ({members[0]})"

    with pytest.raises(ValueError, match="single timeframe"):
        svc._plan_runs("scan", "sc", [], "NSE", ["D", "W"], "NIFTYIT")
    with pytest.raises(ValueError, match="universe is required"):
        svc._plan_runs("universe", "u", [], "NSE", ["D"], None)
    with pytest.raises(ValueError, match="mode must be"):
        svc._plan_runs("bogus", "u", [], "NSE", ["D"], None)

    monkeypatch.setattr(svc, "get_catalog_ranges", lambda exch, iv: {})
    with pytest.raises(ValueError, match="No NIFTY IT constituent"):
        svc._plan_runs("universe", "u", [], "NSE", ["D"], "NIFTYIT")


def test_load_run_bars_enforces_symbol_cap_and_bar_budget(monkeypatch):
    bars = _daily_bars(50)
    monkeypatch.setattr(svc, "get_ohlcv", lambda *a, **kw: bars.copy())
    many = [f"NSE:S{i}" for i in range(svc._MAX_SYMBOLS + 1)]
    with pytest.raises(ValueError, match="Too many symbols"):
        svc.load_run_bars(many, "NSE", "D", None, None, "skip")
    loaded, _ = svc.load_run_bars(many, "NSE", "D", None, None, "skip", max_symbols=len(many))
    assert len(loaded) == len(many)

    monkeypatch.setattr(svc, "_MAX_TOTAL_BARS", 120)
    with pytest.raises(ValueError, match="Run is too large"):
        svc.load_run_bars(["NSE:A", "NSE:B", "NSE:C"], "NSE", "D", None, None, "skip")


def _wait(batch_id, expected, timeout=30.0):
    deadline = time.time() + timeout
    session = db_session()
    try:
        while time.time() < deadline:
            session.expire_all()
            rows = session.query(BacktestRun).filter_by(batch_id=batch_id).order_by(BacktestRun.batch_seq).all()
            if len(rows) == expected and all(r.status in ("completed", "failed", "cancelled") for r in rows):
                return [(r.status, r.mode, r.universe, r.symbols, r.error_message) for r in rows]
            time.sleep(0.25)
        return [(r.status, r.mode, r.universe, r.symbols, r.error_message) for r in rows]
    finally:
        db_session.remove()


def test_scan_batch_runs_one_job_per_covered_constituent(monkeypatch):
    members = [c["symbol"] for c in uni.get_universe("NIFTYIT")["constituents"]]
    covered = members[:3]
    monkeypatch.setattr(svc, "get_catalog_ranges", lambda exch, iv: _ranges_for(covered))
    monkeypatch.setattr(svc, "get_ohlcv", lambda *a, **kw: _daily_bars())

    batch = svc.start_backtest_batch(
        name="it scan", strategy_code=SIMPLE_CODE, symbols=[], intervals=["D"],
        product_type="CNC", mode="scan", universe="NIFTYIT",
    )
    assert batch["batch_id"].startswith("batch_")
    assert [j["symbol"] for j in batch["jobs"]] == covered
    assert batch["skipped"] == members[3:]

    rows = _wait(batch["batch_id"], 3)
    assert [r[0] for r in rows] == ["completed"] * 3, rows
    assert {r[1] for r in rows} == {"scan"} and {r[2] for r in rows} == {"NIFTYIT"}
    assert [r[3] for r in rows] == [f'["NSE:{s}"]' for s in covered]


def test_universe_portfolio_run_holds_every_covered_constituent(monkeypatch):
    members = [c["symbol"] for c in uni.get_universe("NIFTYIT")["constituents"]]
    monkeypatch.setattr(svc, "get_catalog_ranges", lambda exch, iv: _ranges_for(members))
    monkeypatch.setattr(svc, "get_ohlcv", lambda *a, **kw: _daily_bars())

    batch = svc.start_backtest_batch(
        name="it portfolio", strategy_code=SIMPLE_CODE, symbols=[], intervals=["D"],
        product_type="CNC", mode="universe", universe="NIFTYIT", max_positions=3,
    )
    assert len(batch["jobs"]) == 1 and batch["batch_id"] == batch["jobs"][0]["job_id"]
    rows = _wait(batch["batch_id"], 1)
    assert rows[0][0] == "completed", rows
    assert rows[0][1] == "universe" and rows[0][2] == "NIFTYIT"
    assert len(json.loads(rows[0][3])) == len(members)
