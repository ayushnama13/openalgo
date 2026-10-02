import numpy as np
import pandas as pd
import pytest

from services import backtest_reports as rep


def series(vals, freq="D"):
    return pd.Series(vals, index=pd.date_range("2024-01-01", periods=len(vals), freq=freq))


def test_drawdown_periods_depth_and_recovery():
    eq = series([100, 110, 99, 105, 110, 120, 90, 95, 100])
    periods = rep.drawdown_periods(eq)
    assert periods[0]["depth_pct"] == pytest.approx(-25.0)  # 120 -> 90
    assert periods[0]["recovered"] is False and periods[0]["end"] is None
    second = periods[1]
    assert second["depth_pct"] == pytest.approx(-10.0)  # 110 -> 99
    assert second["recovered"] and second["bars_to_recover"] == 2


def test_drawdown_periods_monotonic_curve_has_none():
    assert rep.drawdown_periods(series([1, 2, 3, 4])) == []
    assert rep.drawdown_periods(series([5])) == []


def test_rolling_metrics_shape_and_thinning():
    rng = np.random.default_rng(1)
    eq = series(100 * np.cumprod(1 + rng.normal(0.001, 0.01, 3000)), freq="h")
    out = rep.rolling_metrics(eq, periods_per_year=1575)
    assert 0 < len(out["points"]) <= rep.MAX_ROLLING_POINTS
    assert out["window"] > 0
    assert rep.rolling_metrics(series([1, 2, 3]), 252)["points"] == []


TRADES = [
    {"symbol": "A", "action": "BUY", "pnl": 100.0, "pnl_pct": 2.0, "costs": 5.0, "exit_reason": "Take Profit", "mae_pct": -0.5, "mfe_pct": 3.0},
    {"symbol": "A", "action": "BUY", "pnl": -50.0, "pnl_pct": -1.0, "costs": 5.0, "exit_reason": "Stop Loss", "mae_pct": -1.5, "mfe_pct": 0.2},
    {"symbol": "B", "action": "SELL", "pnl": 30.0, "pnl_pct": 0.6, "costs": 4.0, "exit_reason": "Signal Exit", "mae_pct": -0.2, "mfe_pct": 1.0},
    {"symbol": "B", "action": "SELL", "pnl": -20.0, "pnl_pct": -0.4, "costs": 4.0, "exit_reason": "Signal Exit", "mae_pct": -0.9, "mfe_pct": 0.1},
]


def test_trade_breakdown_groups_and_streaks():
    out = rep.trade_breakdown(TRADES)
    by_symbol = {r["symbol"]: r for r in out["by_symbol"]}
    assert by_symbol["A"]["pnl"] == 50.0 and by_symbol["B"]["trades"] == 2
    assert {r["side"] for r in out["by_side"]} == {"LONG", "SHORT"}
    assert out["streaks"] == {"max_consecutive_wins": 1, "max_consecutive_losses": 1}
    exc = out["excursion"]
    assert exc["avg_mae_pct"] == pytest.approx(-0.78, abs=0.006)
    assert exc["edge_ratio"] == pytest.approx(1.075 / 0.775, abs=0.02)
    assert exc["avg_mae_losers_pct"] < exc["avg_mae_winners_pct"]  # losers go further against you


def test_trade_breakdown_without_excursion_columns_and_empty():
    legacy = [{k: v for k, v in t.items() if k not in ("mae_pct", "mfe_pct")} for t in TRADES]
    assert rep.trade_breakdown(legacy)["excursion"] is None
    assert rep.trade_breakdown([])["by_symbol"] == []


def test_exposure():
    assert rep.exposure_stats(series([0, 0, 5, 5]))["time_in_market_pct"] == 50.0


def test_chart_payload_markers_and_window():
    ts = pd.date_range("2024-01-01", periods=300, freq="5min").strftime("%Y-%m-%d %H:%M:%S")
    bars = pd.DataFrame({"timestamp": ts, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5})
    trades = [{"symbol": "A", "action": "BUY", "entry_time": ts[250], "entry_price": 1.0,
               "exit_time": ts[260], "exit_price": 1.5, "quantity": 1, "pnl": 0.5, "exit_reason": "x"}]
    out = rep.chart_payload(bars, trades, "NSE:A", max_bars=100)
    assert len(out["candles"]) == 100
    assert out["shown_trades"] == 1  # the window moved to where the trade is
    assert {m["kind"] for m in out["markers"]} == {"entry", "exit"}
