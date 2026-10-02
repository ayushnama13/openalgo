"""End-to-end tests: multi-symbol and event-driven strategies through the sandboxed pipeline."""

import json
import os
import time

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("BACKTEST_DATABASE_URL", "sqlite:////tmp/opencode/backtest_tests.db")

import database.backtest_db as bdb
import services.backtest_service as svc
from database.backtest_db import (
    BacktestEquityCurve,
    BacktestRun,
    BacktestTrade,
    db_session,
)


def _clear():
    s = db_session()
    s.query(BacktestEquityCurve).delete()
    s.query(BacktestTrade).delete()
    s.query(BacktestRun).delete()
    s.commit()
    db_session.remove()


@pytest.fixture(autouse=True)
def _isolated_db():
    bdb.init_backtest_db()
    _clear()
    yield
    _clear()


def bars_for(seed, n=120):
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 1.0, n))
    epochs = (
        pd.date_range("2024-06-03 09:15:00", periods=n, freq="5min", tz="Asia/Kolkata")
        .tz_convert("UTC").astype("int64") // 10**9
    )
    return pd.DataFrame({
        "timestamp": epochs, "open": close, "high": close + 1, "low": close - 1,
        "close": close, "volume": 1000, "oi": 0,
    })


@pytest.fixture
def three_symbols(monkeypatch):
    data = {"AAA": bars_for(1), "BBB": bars_for(2), "CCC": bars_for(3)}
    monkeypatch.setattr(svc, "get_ohlcv", lambda symbol, **kw: data[symbol].copy())
    monkeypatch.setattr(
        svc, "get_data_range",
        lambda *a, **kw: {"first_timestamp": 0, "last_timestamp": 0, "record_count": 1},
    )
    return data


def run_job(**kw):
    kw.setdefault("name", "t")
    kw.setdefault("exchange", "NSE")
    kw.setdefault("interval", "5m")
    kw.setdefault("product_type", "NRML")
    job_id = svc.start_backtest_job(**kw)
    status = "pending"
    for _ in range(120):
        time.sleep(0.25)
        db_session.remove()
        run = db_session().query(BacktestRun).filter_by(job_id=job_id).first()
        status = run.status
        if status in ("completed", "failed", "cancelled"):
            break
    return job_id, status, run


DSL_ALWAYS_LONG = "df['signal'] = 0\ndf.loc[df.index[2], 'signal'] = 1\n"

EVENT_STRATEGY = """
class Momo(Strategy):
    params = {"lookback": 5}

    def on_bar(self, ctx):
        for s in ctx.symbols:
            if ctx.bar(s) is None:
                continue
            h = ctx.history(s, self.params["lookback"] + 1, "close")
            if len(h) <= self.params["lookback"]:
                continue
            up = h.iloc[-1] > h.iloc[0]
            if up and not ctx.position(s):
                ctx.buy(s, pct=10, sl_pct=3)
            elif not up and ctx.position(s):
                ctx.close(s)
"""


def test_dsl_runs_on_every_symbol(three_symbols):
    job_id, status, run = run_job(
        strategy_code=DSL_ALWAYS_LONG, symbols=["NSE:AAA", "NSE:BBB", "NSE:CCC"],
        sizing_type="fixed_qty", sizing_value=1.0, max_positions=3,
    )
    assert status == "completed", run.error_message
    trades = db_session().query(BacktestTrade).filter_by(job_id=job_id).all()
    assert {t.symbol for t in trades} == {"AAA", "BBB", "CCC"}
    db_session.remove()


def test_max_positions_is_enforced_and_noted(three_symbols):
    job_id, status, run = run_job(
        strategy_code=DSL_ALWAYS_LONG, symbols=["NSE:AAA", "NSE:BBB", "NSE:CCC"],
        sizing_type="fixed_qty", sizing_value=1.0, max_positions=2,
    )
    assert status == "completed", run.error_message
    trades = db_session().query(BacktestTrade).filter_by(job_id=job_id).all()
    assert len({t.symbol for t in trades}) == 2
    notes = json.loads(run.metrics_json)["notes"]
    assert any("max_positions" in n for n in notes)
    db_session.remove()


def test_event_strategy_class_multi_symbol(three_symbols):
    job_id, status, run = run_job(
        strategy_code=EVENT_STRATEGY, symbols=["NSE:AAA", "NSE:BBB", "NSE:CCC"],
        sizing_type="pct_equity", sizing_value=10, max_positions=3,
    )
    assert status == "completed", run.error_message
    s = db_session()
    n_trades = s.query(BacktestTrade).filter_by(job_id=job_id).count()
    curve = s.query(BacktestEquityCurve).filter_by(job_id=job_id).count()
    assert n_trades > 0
    assert curve == 120
    metrics = json.loads(run.metrics_json)
    assert metrics["total_trades"] == n_trades
    db_session.remove()


def test_strategy_error_is_reported(three_symbols):
    job_id, status, run = run_job(
        strategy_code="raise RuntimeError('boom')", symbols=["NSE:AAA"],
    )
    assert status == "failed"
    assert "boom" in run.error_message
    db_session.remove()


def test_strategy_cannot_read_secrets_or_files(three_symbols):
    job_id, status, run = run_job(
        strategy_code="import os\n", symbols=["NSE:AAA"],
    )
    assert status == "failed"
    assert "not allowed" in run.error_message
    db_session.remove()


def test_missing_symbol_is_skipped_with_note(monkeypatch, three_symbols):
    real = svc.get_ohlcv

    def fake(symbol, **kw):
        return pd.DataFrame() if symbol == "CCC" else real(symbol=symbol, **kw)

    monkeypatch.setattr(svc, "get_ohlcv", fake)
    job_id, status, run = run_job(
        strategy_code=DSL_ALWAYS_LONG, symbols=["NSE:AAA", "NSE:CCC"], max_positions=2,
    )
    assert status == "completed", run.error_message
    assert any("CCC" in n for n in json.loads(run.metrics_json)["notes"])
    db_session.remove()


def test_engine_options_applied_and_reproducibility_recorded(three_symbols):
    job_id, status, run = run_job(
        strategy_code=DSL_ALWAYS_LONG, symbols=["NSE:AAA"], product_type="CNC",
        sizing_type="fixed_qty", sizing_value=10,
        engine_options={"cost_preset": "auto", "spread_bps": 10},
    )
    assert status == "completed", run.error_message
    assert json.loads(run.engine_options) == {"cost_preset": "auto", "spread_bps": 10}
    assert len(run.code_hash) == 64
    fp = json.loads(run.data_fingerprint)["NSE:AAA"]
    assert fp["bars"] == 120 and len(fp["sha256"]) == 64
    t = db_session().query(BacktestTrade).filter_by(job_id=job_id).first()
    assert t.costs > 0  # statutory charges applied
    assert t.mae_pct is not None and t.mfe_pct is not None
    db_session.remove()


def test_bad_engine_options_rejected():
    with pytest.raises(ValueError, match="Unknown engine option"):
        svc.sanitize_engine_options({"os_system": 1})
    with pytest.raises(ValueError, match="cost_preset"):
        svc.sanitize_engine_options({"cost_preset": "nope"})
    with pytest.raises(ValueError, match="max_volume_pct"):
        svc.sanitize_engine_options({"max_volume_pct": 250})
    with pytest.raises(ValueError, match="non-negative"):
        svc.sanitize_engine_options({"spread_bps": -1})
    with pytest.raises(ValueError, match="wrong type"):
        svc.sanitize_engine_options({"allow_short": 1})
