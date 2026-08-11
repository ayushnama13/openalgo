# test/test_backtest_pipeline.py

"""Tests for the strategy backtesting pipeline (P1).

Covers the shared fill engine, position sizing, analytics metrics and the
full end-to-end job pipeline against a mocked Historify data source, without
touching the real historify.duckdb file.
"""

import json
import math
import os
import time

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("BACKTEST_DATABASE_URL", "sqlite:////tmp/opencode/backtest_tests.db")

import database.backtest_db as bdb
import services.backtest_fill_engine as fill
import services.backtest_service as svc
from database.backtest_db import (
    BacktestEquityCurve,
    BacktestRun,
    BacktestTrade,
    cleanup_old_runs,
    db_session,
)
from services.backtest_analytics import periods_per_year_for_interval


@pytest.fixture(autouse=True)
def _isolated_backtest_db():
    bdb.init_backtest_db()
    session = db_session()
    session.query(BacktestEquityCurve).delete()
    session.query(BacktestTrade).delete()
    session.query(BacktestRun).delete()
    session.commit()
    db_session.remove()
    yield
    session = db_session()
    session.query(BacktestEquityCurve).delete()
    session.query(BacktestTrade).delete()
    session.query(BacktestRun).delete()
    session.commit()
    db_session.remove()


def make_bars(n=300, seed=7):
    """Mirrors the real get_ohlcv() contract: timestamp is raw epoch seconds."""
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 1.0, n))
    epochs = (
        pd.date_range("2024-06-01 09:15:00", periods=n, freq="5min", tz="Asia/Kolkata")
        .tz_convert("UTC")
        .astype("int64")
        // 10**9
    )
    return pd.DataFrame(
        {
            "timestamp": epochs,
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": [100000] * n,
            "oi": [1000] * n,
        }
    )


# ---------------------------------------------------------------------------
# Fill engine
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args,expect_fill",
    [
        ({"action": "BUY", "price_type": "MARKET", "limit_price": 0, "trigger_price": 0}, True),
        ({"action": "SELL", "price_type": "MARKET", "limit_price": 0, "trigger_price": 0}, True),
        ({"action": "BUY", "price_type": "LIMIT", "limit_price": 101, "trigger_price": 0}, True),
        ({"action": "BUY", "price_type": "LIMIT", "limit_price": 99, "trigger_price": 0}, True),
        ({"action": "BUY", "price_type": "LIMIT", "limit_price": 96, "trigger_price": 0}, False),
        ({"action": "SELL", "price_type": "LIMIT", "limit_price": 101, "trigger_price": 0}, True),
        ({"action": "SELL", "price_type": "LIMIT", "limit_price": 106, "trigger_price": 0}, False),
        ({"action": "SELL", "price_type": "SL-M", "limit_price": 0, "trigger_price": 98}, True),
        ({"action": "SELL", "price_type": "SL-M", "limit_price": 0, "trigger_price": 95}, False),
        ({"action": "BUY", "price_type": "SL", "limit_price": 103, "trigger_price": 102}, True),
    ],
)
def test_simulate_order_fill(args, expect_fill):
    filled, price, cost = fill.simulate_order_fill(
        bar_open=100, bar_high=105, bar_low=97, bar_close=103, **args
    )
    assert filled is expect_fill, (args, expect_fill)
    if filled:
        assert price > 0
        assert cost >= 0


def test_fill_slippage_and_brokerage():
    filled, buy_price, cost = fill.simulate_order_fill(
        action="BUY",
        price_type="MARKET",
        limit_price=0,
        trigger_price=0,
        bar_open=100,
        bar_high=105,
        bar_low=97,
        bar_close=103,
        slippage_bps=10,
        brokerage_bps=20,
    )
    assert filled
    assert buy_price > 100  # BUY slips up
    assert abs(cost - buy_price * (20 / 10000.0)) < 1e-9

    filled, sell_price, _ = fill.simulate_order_fill(
        action="SELL",
        price_type="MARKET",
        limit_price=0,
        trigger_price=0,
        bar_open=100,
        bar_high=105,
        bar_low=97,
        bar_close=103,
        slippage_bps=10,
        brokerage_bps=20,
    )
    assert filled
    assert sell_price < 100  # SELL slips down


def test_fill_brokerage_scales_with_quantity():
    """Cost must scale with traded value (price * qty), not just price per share."""
    filled, price, cost_1 = fill.simulate_order_fill(
        action="BUY",
        price_type="MARKET",
        limit_price=0,
        trigger_price=0,
        bar_open=100,
        bar_high=105,
        bar_low=97,
        bar_close=103,
        brokerage_bps=20,
        quantity=1,
    )
    assert filled
    _, _, cost_50 = fill.simulate_order_fill(
        action="BUY",
        price_type="MARKET",
        limit_price=0,
        trigger_price=0,
        bar_open=100,
        bar_high=105,
        bar_low=97,
        bar_close=103,
        brokerage_bps=20,
        quantity=50,
    )
    assert abs(cost_50 - cost_1 * 50) < 1e-9


def test_mis_square_off_times():
    assert not fill.is_mis_square_off_bar("2024-03-28 10:00:00", "NSE")
    assert fill.is_mis_square_off_bar("2024-03-28 15:15:00", "NSE")
    assert fill.is_mis_square_off_bar("2024-03-28 16:00:00", "NSE")
    assert not fill.is_mis_square_off_bar("2024-03-28 15:14:59", "NSE")
    assert fill.is_mis_square_off_bar("2024-03-28 23:30:00", "MCX")


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------


def test_position_sizing_rules():
    assert svc.calculate_position_qty("fixed_qty", 10, 100000, 100, 100000) == 10
    assert svc.calculate_position_qty("pct_equity", 10, 100000, 100, 100000) == 100
    assert svc.calculate_position_qty("fixed_capital", 5000, 100000, 100, 100000) == 50
    assert svc.calculate_position_qty("kelly_vol", 0.1, 100000, 100, 100000) > 0
    assert svc.calculate_position_qty("fixed_qty", 5, 100000, 0, 100000) == 0


# ---------------------------------------------------------------------------
# Signal DSL
# ---------------------------------------------------------------------------


def test_signal_dsl_with_indicator_template():
    bars = make_bars(200)
    code = """
def strategy(df):
    df["sma_fast"] = ta.sma(df["close"], period=9)
    df["sma_slow"] = ta.sma(df["close"], period=21)
    df["signal"] = 0
    df.loc[(df["sma_fast"] > df["sma_slow"]) & (df["sma_fast"].shift(1) <= df["sma_slow"].shift(1)), "signal"] = 1
    df.loc[(df["sma_fast"] < df["sma_slow"]) & (df["sma_fast"].shift(1) >= df["sma_slow"].shift(1)), "signal"] = -1
    return df
"""
    out = svc.execute_strategy_dsl(code, bars)
    assert out["signal"].abs().sum() >= 2


def test_signal_dsl_via_buy_sell_columns():
    bars = make_bars(100)
    code = """
def strategy(df):
    df["buy_signal"] = df["close"] > df["close"].shift(1)
    df["sell_signal"] = df["close"] < df["close"].shift(1)
    return df
"""
    out = svc.execute_strategy_dsl(code, bars)
    assert (out["signal"].abs() > 0).sum() >= 2


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


def test_backtest_metrics():
    equity = pd.Series(
        [100000, 101000, 100500, 102000, 101500, 104000],
        index=pd.to_datetime(
            ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-02-01"]
        ),
    )
    trades = [
        {"pnl": 1000, "holding_period_bars": 5},
        {"pnl": -500, "holding_period_bars": 3},
        {"pnl": 1500, "holding_period_bars": 2},
        {"pnl": 750, "holding_period_bars": 4},
    ]
    m = svc.calculate_backtest_metrics(equity, trades, 100000)
    assert m["total_return_pct"] == 4.0
    assert m["total_trades"] == 4
    assert m["win_rate_pct"] == 75.0
    assert m["profit_factor"] > 1.0
    assert m["max_drawdown_pct"] > 0
    assert m["sharpe_ratio"] != 0
    assert m["avg_holding_minutes"] == 0.0  # trades carry no timestamps


def test_periods_per_year_for_interval():
    assert periods_per_year_for_interval("1m") == 252.0 * 375.0
    assert periods_per_year_for_interval("5m") == 252.0 * 75.0
    assert periods_per_year_for_interval("15m") == 252.0 * 25.0
    assert periods_per_year_for_interval("1h") == 252.0 * 6.25
    assert periods_per_year_for_interval("D") == 252.0
    assert periods_per_year_for_interval("W") == 52.0
    assert periods_per_year_for_interval("M") == 12.0
    assert periods_per_year_for_interval("Q") == 4.0
    assert periods_per_year_for_interval("Y") == 1.0
    assert periods_per_year_for_interval("not-an-interval") == 252.0
    assert periods_per_year_for_interval(None) == 252.0


def test_sharpe_annualization_interval_aware():
    """Same equity path annualized per-bar: sharpe must scale with sqrt(periods)."""
    dates = pd.date_range("2024-01-01", periods=1000, freq="5min")
    rng = np.random.default_rng(3)
    equity = pd.Series(100000 + np.cumsum(rng.normal(0, 100, 1000)), index=dates)

    m_1m = svc.calculate_backtest_metrics(equity, [], 100000, interval="1m")
    m_5m = svc.calculate_backtest_metrics(equity, [], 100000, interval="5m")
    m_d = svc.calculate_backtest_metrics(equity, [], 100000, interval="D")

    expect_1m = m_5m["sharpe_ratio"] * math.sqrt(
        periods_per_year_for_interval("1m") / periods_per_year_for_interval("5m")
    )
    assert abs(m_1m["sharpe_ratio"] - expect_1m) < 0.005  # metrics round to 2 dp
    expect_d = m_5m["sharpe_ratio"] * math.sqrt(
        periods_per_year_for_interval("D") / periods_per_year_for_interval("5m")
    )
    assert abs(m_d["sharpe_ratio"] - expect_d) < 0.005
    assert m_1m["sortino_ratio"] != m_d["sortino_ratio"]


def test_avg_holding_minutes_from_trade_times():
    equity = pd.Series(
        [100000, 101000, 100500, 102000, 101500, 104000],
        index=pd.to_datetime(
            ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-02-01"]
        ),
    )
    trades = [
        {"pnl": 100, "holding_period_bars": 2, "entry_time": "2024-01-01 09:15:00", "exit_time": "2024-01-01 10:15:00"},
        {"pnl": 200, "holding_period_bars": 1, "entry_time": "2024-01-02 09:15:00", "exit_time": "2024-01-02 10:45:00"},
        {"pnl": -50, "holding_period_bars": 4},  # no timestamps -> skipped
    ]
    m = svc.calculate_backtest_metrics(equity, trades, 100000)
    assert m["avg_holding_bars"] == round((2 + 1 + 4) / 3, 1)
    assert m["avg_holding_minutes"] == (60 + 90) / 2


def test_monthly_heatmap_shape():
    equity = pd.Series(
        [100000, 101000, 100500, 102000, 101500, 104000],
        index=pd.to_datetime(
            ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-02-01"]
        ),
    )
    h = svc.generate_monthly_returns_heatmap(equity)
    assert h["years"] == ["2024"]
    assert h["matrix"]["2024"]["Jan"] is not None
    assert h["matrix"]["2024"]["YTD"] is not None


# ---------------------------------------------------------------------------
# End-to-end pipeline
# ---------------------------------------------------------------------------


def test_end_to_end_pipeline(monkeypatch):
    bars = make_bars(200)
    monkeypatch.setattr(svc, "get_ohlcv", lambda *a, **kw: bars.reset_index(drop=True))
    monkeypatch.setattr(
        svc,
        "get_data_range",
        lambda *a, **kw: {"first_timestamp": 0, "last_timestamp": 0, "record_count": 1},
    )

    code = """
def strategy(df):
    df["rsi"] = ta.rsi(df["close"], period=14)
    df["signal"] = 0
    df.loc[(df["rsi"] < 30), "signal"] = 1
    df.loc[(df["rsi"] > 70), "signal"] = -1
    return df
"""
    job_id = svc.start_backtest_job(
        name="test run",
        strategy_code=code,
        symbols=["NSE:TEST"],
        exchange="NSE",
        interval="5m",
        start_date="2024-06-01",
        end_date="2024-06-02",
        initial_capital=100000.0,
        sizing_type="pct_equity",
        sizing_value=10.0,
        slippage_bps=5.0,
        brokerage_bps=20.0,
        product_type="MIS",
        missing_data_policy="skip",
        stop_loss_pct=2.0,
        take_profit_pct=5.0,
        daily_loss_limit=5000.0,
        max_positions=3,
    )
    assert job_id.startswith("bt_")

    status = "pending"
    for _ in range(40):
        time.sleep(0.25)
        run = db_session().query(BacktestRun).filter_by(job_id=job_id).first()
        status = run.status if run else "missing"
        if status in ("completed", "failed", "cancelled"):
            break
        db_session.remove()

    assert status == "completed", f"status={status}"
    trades = db_session().query(BacktestTrade).filter_by(job_id=job_id).count()
    curve = db_session().query(BacktestEquityCurve).filter_by(job_id=job_id).count()
    metrics = json.loads(
        db_session().query(BacktestRun).filter_by(job_id=job_id).first().metrics_json or "{}"
    )
    db_session.remove()

    assert trades > 0
    assert curve == 200
    assert metrics["total_trades"] == trades
    assert "monthly_heatmap" in metrics


def test_cancel_job_flag():
    job_id = "test_cancel"
    ev = svc.register_active_job(job_id)
    assert svc.cancel_job(job_id) is True
    assert ev.is_set()
    svc.unregister_active_job(job_id)
    assert svc.cancel_job(job_id) is False


def test_cleanup_old_runs():
    from datetime import timedelta

    bdb.BacktestRun.created_at.property.columns[0].default.arg(None)
    session = db_session()
    session.add(
        BacktestRun(
            job_id="oldrun",
            name="old",
            strategy_code="x",
            symbols="[]",
            created_at=pd.Timestamp.now(tz="Asia/Kolkata") - timedelta(days=60),
        )
    )
    session.add(BacktestRun(job_id="newrun", name="new", strategy_code="x", symbols="[]"))
    session.commit()
    cleanup_old_runs(days_to_keep=30)
    session.expire_all()
    assert session.query(BacktestRun).filter_by(job_id="oldrun").count() == 0
    assert session.query(BacktestRun).filter_by(job_id="newrun").count() == 1
    session.commit()
    db_session.remove()


# ---------------------------------------------------------------------------
# Multi-timeframe batch fan-out
# ---------------------------------------------------------------------------

SIMPLE_CODE = """
def strategy(df):
    df["signal"] = 0
    df.loc[df["close"] > df["close"].shift(1), "signal"] = 1
    df.loc[df["close"] < df["close"].shift(1), "signal"] = -1
    return df
"""


def _wait_for_terminal(session, batch_id, expected, timeout=15.0):
    """Poll until every run in a batch has a terminal status.

    The worker threads commit from their own sessions, so the local session's
    identity map must be expired before every refresh.
    """
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        session.expire_all()
        statuses = [
            r.status
            for r in session.query(BacktestRun)
            .filter_by(batch_id=batch_id)
            .order_by(BacktestRun.batch_seq.asc())
            .all()
        ]
        if len(statuses) == expected and all(
            s in ("completed", "failed", "cancelled") for s in statuses
        ):
            return statuses
        time.sleep(0.25)
    return statuses


def test_start_backtest_batch_fan_out(monkeypatch):
    bars = make_bars(150)
    monkeypatch.setattr(svc, "get_ohlcv", lambda *a, **kw: bars.reset_index(drop=True))
    monkeypatch.setattr(
        svc,
        "get_data_range",
        lambda *a, **kw: {"first_timestamp": 0, "last_timestamp": 0, "record_count": 1},
    )

    batch = svc.start_backtest_batch(
        name="mtf sweep",
        strategy_code=SIMPLE_CODE,
        symbols=["NSE:TEST"],
        exchange="NSE",
        intervals=["1m", "5m", "1h"],
        start_date="2024-06-01",
        end_date="2024-06-02",
        initial_capital=100000.0,
    )
    assert batch["batch_id"].startswith("batch_")
    assert [j["interval"] for j in batch["jobs"]] == ["1m", "5m", "1h"]

    session = db_session()
    statuses = _wait_for_terminal(session, batch["batch_id"], 3)
    assert all(s == "completed" for s in statuses)

    runs = (
        session.query(BacktestRun)
        .filter_by(batch_id=batch["batch_id"])
        .order_by(BacktestRun.batch_seq.asc())
        .all()
    )
    assert [r.interval for r in runs] == ["1m", "5m", "1h"]
    assert [r.batch_seq for r in runs] == [0, 1, 2]
    assert {r.batch_name for r in runs} == {"mtf sweep"}
    # sibling names disambiguate the timeframe
    assert [r.name for r in runs] == ["mtf sweep (1m)", "mtf sweep (5m)", "mtf sweep (1h)"]
    assert all(r.metrics_json and "avg_holding_minutes" in r.metrics_json for r in runs)
    session.query(BacktestEquityCurve).filter(
        BacktestEquityCurve.job_id.in_([r.job_id for r in runs])
    ).count()
    assert session.query(BacktestEquityCurve).count() == 150 * 3
    db_session.remove()


def test_single_run_wrapper_is_batch_of_one(monkeypatch):
    bars = make_bars(60)
    monkeypatch.setattr(svc, "get_ohlcv", lambda *a, **kw: bars.reset_index(drop=True))
    monkeypatch.setattr(
        svc,
        "get_data_range",
        lambda *a, **kw: {"first_timestamp": 0, "last_timestamp": 0, "record_count": 1},
    )

    job_id = svc.start_backtest_job(
        name="single",
        strategy_code=SIMPLE_CODE,
        symbols=["NSE:TEST"],
        interval="5m",
        initial_capital=100000.0,
    )
    session = db_session()
    run = session.query(BacktestRun).filter_by(job_id=job_id).first()
    assert run is not None
    assert run.batch_id == job_id  # legacy-style batch of one
    assert run.batch_name == "single"
    assert run.batch_seq == 0
    assert run.name == "single"  # no suffix on the single-run path
    statuses = _wait_for_terminal(session, job_id, 1)
    assert statuses == ["completed"]
    db_session.remove()


def test_preflight_fail_marks_interval_failed(monkeypatch):
    monkeypatch.setattr(svc, "get_ohlcv", lambda *a, **kw: make_bars(60))
    available = {"1m": {"first_timestamp": 0, "last_timestamp": 0, "record_count": 1}}

    def fake_range(symbol, exchange, interval):
        return available.get(interval)

    monkeypatch.setattr(svc, "get_data_range", fake_range)

    batch = svc.start_backtest_batch(
        name="partial sweep",
        strategy_code=SIMPLE_CODE,
        symbols=["NSE:TEST"],
        intervals=["1m", "W"],
    )
    session = db_session()
    runs = (
        session.query(BacktestRun)
        .filter_by(batch_id=batch["batch_id"])
        .order_by(BacktestRun.batch_seq.asc())
        .all()
    )
    assert [r.interval for r in runs] == ["1m", "W"]
    # the healthy interval may already be running by the time we look
    assert runs[0].status != "failed"
    assert runs[1].status == "failed"
    assert "No historical data available" in (runs[1].error_message or "")
    # the healthy interval still runs to completion
    statuses = _wait_for_terminal(session, batch["batch_id"], 2)
    assert statuses[1] == "failed"
    assert statuses[0] in ("completed", "failed", "cancelled")
    db_session.remove()


def test_resolve_base_interval_maps_to_storage_intervals():
    """Only 1m and D are stored; everything else is aggregated from one of them."""
    from database.historify_db import resolve_base_interval

    assert resolve_base_interval("1m") == "1m"
    assert resolve_base_interval("D") == "D"
    for iv in ("5m", "15m", "30m", "1h", "2h", "25m"):
        assert resolve_base_interval(iv) == "1m", iv
    for iv in ("W", "M", "Q", "Y", "2W"):
        assert resolve_base_interval(iv) == "D", iv
    # unparseable falls through untouched so the caller's own error surfaces
    assert resolve_base_interval("banana") == "banana"


def test_preflight_checks_base_interval_not_requested_interval(monkeypatch):
    """A 1m-only catalog must still admit 5m/15m/1h — they aggregate from 1m.

    Regression: the preflight asked data_catalog for '5m' directly, which never
    has a row, so every aggregated timeframe in a sweep was written to failed.
    """
    bars = make_bars(80)
    monkeypatch.setattr(svc, "get_ohlcv", lambda *a, **kw: bars.reset_index(drop=True))
    stored = {"1m": {"first_timestamp": 0, "last_timestamp": 0, "record_count": 1}}
    asked: list[str] = []

    def fake_range(symbol, exchange, interval):
        asked.append(interval)
        return stored.get(interval)

    monkeypatch.setattr(svc, "get_data_range", fake_range)

    batch = svc.start_backtest_batch(
        name="aggregated sweep",
        strategy_code=SIMPLE_CODE,
        symbols=["NSE:TEST"],
        intervals=["5m", "15m", "1h"],
    )
    # the catalog was queried for the base interval, never the requested one
    assert asked == ["1m", "1m", "1m"]

    session = db_session()
    statuses = _wait_for_terminal(session, batch["batch_id"], 3)
    assert all(s == "completed" for s in statuses), statuses
    db_session.remove()


def test_signals_execute_on_next_bar_open(monkeypatch):
    """No lookahead: a signal raised on bar i fills at bar i+1's open.

    Filling on the signal bar itself used the bar's OPEN — a price that printed
    before the signal (derived from that bar's close) could have existed.
    """
    n = 12
    epochs = (
        pd.date_range("2024-06-03 09:15:00", periods=n, freq="5min", tz="Asia/Kolkata")
        .tz_convert("UTC")
        .astype("int64")
        // 10**9
    )
    # Distinct open vs close per bar so the two candidate fill prices differ.
    opens = [100.0 + i for i in range(n)]
    closes = [o + 0.5 for o in opens]
    bars = pd.DataFrame(
        {
            "timestamp": epochs,
            "open": opens,
            "high": [c + 1 for c in closes],
            "low": [o - 1 for o in opens],
            "close": closes,
            "volume": [1000] * n,
            "oi": [0] * n,
        }
    )
    monkeypatch.setattr(svc, "get_ohlcv", lambda *a, **kw: bars.reset_index(drop=True))
    monkeypatch.setattr(
        svc,
        "get_data_range",
        lambda *a, **kw: {"first_timestamp": 0, "last_timestamp": 0, "record_count": 1},
    )

    code = (
        "df['signal'] = 0\n"
        "df.loc[df.index[3], 'signal'] = 1\n"
        "df.loc[df.index[7], 'signal'] = -1\n"
    )
    job_id = svc.start_backtest_job(
        name="lookahead check",
        strategy_code=code,
        symbols=["NSE:TEST"],
        interval="5m",
        product_type="NRML",
        sizing_type="fixed_qty",
        sizing_value=1.0,
    )
    session = db_session()
    assert _wait_for_terminal(session, job_id, 1) == ["completed"]
    trades = session.query(BacktestTrade).filter_by(job_id=job_id).all()
    assert len(trades) == 1
    trade = trades[0]
    # signal on bar 3 -> fill at bar 4's open, not bar 3's
    assert trade.entry_price == pytest.approx(opens[4])
    assert trade.entry_price != pytest.approx(opens[3])
    # signal on bar 7 -> fill at bar 8's open
    assert trade.exit_price == pytest.approx(opens[8])
    assert trade.holding_period_bars == 4
    db_session.remove()


def test_backtest_routes_reject_anonymous_requests():
    """Every /backtest/api route is session-gated.

    The blueprint executes user-supplied strategy code, and app.py's global
    before_request only expires sessions - it does not enforce login - so the
    per-route decorator is the only thing standing in front of it.
    """
    from flask import Flask

    from blueprints.backtest import backtest_bp

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["WTF_CSRF_ENABLED"] = False
    app.register_blueprint(backtest_bp)

    rules = [r for r in app.url_map.iter_rules() if r.rule.startswith("/backtest/api")]
    assert rules, "no backtest routes registered"

    client = app.test_client()
    checked = 0
    for rule in rules:
        path = rule.rule.replace("<job_id>", "x").replace("<batch_id>", "x")
        path = path.replace("<strat_id>", "x").replace("<batch_or_job_id>", "x")
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            resp = client.open(path, method=method, json={})
            assert resp.status_code in (301, 302, 401), f"{method} {path} -> {resp.status_code}"
            checked += 1
    assert checked >= len(rules)


def test_cancel_batch_marks_queued_runs_cancelled(monkeypatch):
    """With 2 workers and 4 tasks, two runs must sit queued; cancel_batch
    must pull them out of the queue and mark them cancelled."""
    import threading as t

    release = t.Event()

    def slow_ohlcv(*a, **kw):
        assert release.wait(timeout=10), "worker never released"
        return make_bars(100)

    monkeypatch.setattr(svc, "get_ohlcv", slow_ohlcv)
    monkeypatch.setattr(
        svc,
        "get_data_range",
        lambda *a, **kw: {"first_timestamp": 0, "last_timestamp": 0, "record_count": 1},
    )

    batch = svc.start_backtest_batch(
        name="cancel test",
        strategy_code=SIMPLE_CODE,
        symbols=["NSE:TEST"],
        intervals=["1m", "5m", "15m", "1h"],
    )
    session = db_session()

    # Wait until the 2 workers are occupied (status running), leaving 2 queued.
    import time

    deadline = time.time() + 10
    while time.time() < deadline:
        running = (
            session.query(BacktestRun).filter_by(batch_id=batch["batch_id"], status="running").count()
        )
        if running >= 2:
            break
        time.sleep(0.05)

    cancelled = svc.cancel_batch(batch["batch_id"])
    assert len(cancelled) >= 1
    # Queued futures are cancelled synchronously; rows must already say so.
    queued_cancelled = (
        session.query(BacktestRun)
        .filter_by(batch_id=batch["batch_id"], status="cancelled")
        .count()
    )
    assert queued_cancelled >= 1

    # Release the workers; their cancel events turn them cancelled too.
    release.set()
    statuses = _wait_for_terminal(session, batch["batch_id"], 4)
    assert all(s == "cancelled" for s in statuses)
    db_session.remove()
