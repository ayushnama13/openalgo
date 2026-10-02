"""Parameter sweeps, walk-forward, deflated Sharpe and Monte Carlo."""

import json
import os
import time

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("BACKTEST_DATABASE_URL", "sqlite:////tmp/opencode/backtest_tests.db")

import database.backtest_db as bdb
import services.backtest_optimizer as optim
import services.backtest_service as svc
from database.backtest_db import BacktestOptimization, db_session

CODE = """
class Cross(Strategy):
    params = {"fast": 5, "slow": 20}

    def on_bar(self, ctx):
        s = ctx.symbols[0]
        if ctx.bar(s) is None:
            return
        h = ctx.history(s, self.params["slow"] + 1, "close")
        if len(h) <= self.params["slow"]:
            return
        fast = h.iloc[-self.params["fast"]:].mean()
        slow = h.iloc[-self.params["slow"]:].mean()
        p = ctx.position(s)
        if fast > slow and not p:
            ctx.buy(s, pct=50)
        elif fast < slow and p:
            ctx.close(s)
"""

DSL_CODE = """
n = PARAMS.get("n", 5)
df["signal"] = 0
df.loc[df["close"] > df["close"].rolling(n).mean(), "signal"] = 1
df.loc[df["close"] < df["close"].rolling(n).mean(), "signal"] = -1
"""


def make_bars(n=900, seed=3):
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    close = 100 + 8 * np.sin(t / 25.0) + np.cumsum(rng.normal(0, 0.25, n))
    ts = pd.date_range("2024-01-01 09:15:00", periods=n, freq="15min").strftime("%Y-%m-%d %H:%M:%S")
    return pd.DataFrame({"timestamp": ts, "open": close, "high": close + 0.4,
                         "low": close - 0.4, "close": close, "volume": 5000})


BASE = {
    "initial_capital": 100000.0, "sizing_type": "pct_equity", "sizing_value": 50,
    "product_type": "NRML", "max_positions": 1, "interval": "15m",
}


# ---- parameter space ----
def test_grid_expands_ranges_and_lists():
    combos = optim.build_param_combos({"a": [1, 2], "b": {"min": 10, "max": 30, "step": 10}})
    assert len(combos) == 6
    assert {"a": 2, "b": 30} in combos


def test_grid_too_large_is_rejected_with_hint():
    with pytest.raises(ValueError, match="random"):
        optim.build_param_combos({"a": {"min": 1, "max": 50}, "b": {"min": 1, "max": 50}}, max_trials=100)


def test_random_sampling_is_seeded_and_unique():
    space = {"a": {"min": 1, "max": 50}, "b": {"min": 1, "max": 50}}
    one = optim.build_param_combos(space, "random", 30, seed=1)
    two = optim.build_param_combos(space, "random", 30, seed=1)
    assert one == two and len(one) == 30
    assert len({tuple(c.items()) for c in one}) == 30


@pytest.mark.parametrize("bad", [{}, {"a": []}, {"a": {"min": 5, "max": 1}}, {"a": "x"},
                                 {"a": [float("nan")]}, {"a": {"min": 1, "max": 5, "step": 0}}])
def test_bad_param_space_rejected(bad):
    with pytest.raises(ValueError):
        optim.build_param_combos(bad)


# ---- statistics ----
def test_deflated_sharpe_penalises_many_trials():
    rng = np.random.default_rng(0)
    r = rng.normal(0.0008, 0.01, 800)
    sr = float(r.mean() / r.std(ddof=1))
    few = optim.deflated_sharpe(r, [sr, sr * 0.5])
    many = optim.deflated_sharpe(r, list(np.random.default_rng(1).normal(0.02, 0.03, 300)))
    assert 0 <= few["dsr"] <= 1
    assert many["dsr"] < few["dsr"]


def test_deflated_sharpe_handles_degenerate_input():
    assert optim.deflated_sharpe(np.zeros(50), [0.0, 0.0])["dsr"] is None
    assert optim.deflated_sharpe(np.array([0.01, 0.02]), [0.1])["dsr"] is None


def test_monte_carlo_bands_and_reproducibility():
    pnls = [100, -50, 80, -40, 120, -60, 90, -30, 70, -20]
    a = optim.monte_carlo(pnls, 10000, n_sims=500, seed=4)
    b = optim.monte_carlo(pnls, 10000, n_sims=500, seed=4)
    assert a == b
    assert a["final_equity"]["p5"] <= a["final_equity"]["p50"] <= a["final_equity"]["p95"]
    assert a["actual_final_equity"] == 10000 + sum(pnls)
    assert 0 <= a["probability_of_loss"] <= 1
    shuffled = optim.monte_carlo(pnls, 10000, n_sims=300, method="shuffle", seed=4)
    # a pure reordering cannot change the final equity
    assert shuffled["final_equity"]["p5"] == shuffled["final_equity"]["p95"] == a["actual_final_equity"]


def test_monte_carlo_needs_enough_trades():
    with pytest.raises(ValueError, match="at least 5"):
        optim.monte_carlo([1, 2], 1000)


# ---- sandboxed studies ----
def test_sweep_ranks_candidates_and_reports_oos():
    spec = {"param_space": {"fast": [3, 5, 8], "slow": [15, 30]}, "objective": "total_return_pct",
            "min_trades": 3, "oos_pct": 25}
    res = optim.run_sweep(CODE, {"NSE:X": make_bars()}, BASE, spec, 100000.0, "15m")
    assert res["trials"] == 6 and len(res["ranked"]) == 6
    scores = [r["score"] for r in res["ranked"] if r["score"] is not None]
    assert scores == sorted(scores, reverse=True)
    assert res["best"]["oos_metrics"] is not None
    assert res["deflated_sharpe"]["trials"] >= 2
    assert set(res["best"]["params"]) == {"fast", "slow"}


def test_sweep_with_signal_dsl_uses_params():
    spec = {"param_space": {"n": [3, 10, 30]}, "objective": "total_return_pct", "min_trades": 1}
    res = optim.run_sweep(DSL_CODE, {"NSE:X": make_bars()}, BASE, spec, 100000.0, "15m")
    totals = {r["params"]["n"]: r["metrics"]["total_trades"] for r in res["ranked"]}
    assert len(set(totals.values())) > 1  # the parameter changed behaviour


def test_min_trades_blocks_degenerate_winner():
    spec = {"param_space": {"fast": [5], "slow": [800]}, "min_trades": 5}
    res = optim.run_sweep(CODE, {"NSE:X": make_bars()}, BASE, spec, 100000.0, "15m")
    assert res["best"] is None
    assert any("minimum" in w for w in res["warnings"])


def test_sweep_failing_strategy_surfaces_error():
    spec = {"param_space": {"x": [1, 2]}}
    with pytest.raises(ValueError, match="boom"):
        optim.run_sweep("raise RuntimeError('boom')", {"NSE:X": make_bars()}, BASE, spec, 1e5, "15m")


def test_walk_forward_stitches_out_of_sample():
    spec = {"param_space": {"fast": [3, 6], "slow": [15, 30]}, "objective": "total_return_pct",
            "train_pct": 50, "test_pct": 15, "n_windows": 3, "min_trades": 1}
    res = optim.run_walk_forward(CODE, {"NSE:X": make_bars()}, BASE, spec, 100000.0, "15m")
    assert len(res["windows"]) == 3
    for w in res["windows"]:
        assert w["train"][1] < w["test"][0]  # test strictly after train: no leakage
    assert res["oos_metrics"] is not None
    assert res["oos_equity"][0]["equity"] > 0
    assert res["walk_forward_efficiency"] is None or isinstance(res["walk_forward_efficiency"], float)


def test_walk_forward_rejects_bad_windows():
    with pytest.raises(ValueError):
        optim.run_walk_forward(CODE, {"NSE:X": make_bars()}, BASE,
                               {"param_space": {"fast": [3]}, "train_pct": 90, "test_pct": 50},
                               1e5, "15m")


# ---- background job through the service ----
def _clear():
    s = db_session()
    s.query(BacktestOptimization).delete()
    s.commit()
    db_session.remove()


@pytest.fixture
def clean_db():
    bdb.init_backtest_db()
    _clear()
    yield
    _clear()


def _wait(opt_id, timeout=60):
    for _ in range(int(timeout / 0.25)):
        time.sleep(0.25)
        db_session.remove()
        rec = db_session().query(BacktestOptimization).filter_by(opt_id=opt_id).first()
        if rec.status in ("completed", "failed", "cancelled"):
            return rec
    raise AssertionError("optimization did not finish")


def test_optimization_job_persists_result(monkeypatch, clean_db):
    bars = make_bars()
    monkeypatch.setattr(svc, "load_run_bars", lambda *a, **k: ({"NSE:X": bars}, ["note"]))
    opt_id = svc.start_optimization(
        name="t", strategy_code=CODE, symbols=["NSE:X"], interval="15m", product_type="NRML",
        sizing_type="pct_equity", sizing_value=50, max_positions=1,
        optimize={"kind": "sweep", "param_space": {"fast": [3, 6], "slow": [20]},
                  "objective": "total_return_pct", "min_trades": 1},
    )
    rec = _wait(opt_id)
    assert rec.status == "completed", rec.error_message
    result = json.loads(rec.result_json)
    assert result["trials"] == 2 and result["notes"] == ["note"]
    assert rec.progress == 100.0
    db_session.remove()


def test_optimization_bad_spec_rejected_at_submit(clean_db):
    with pytest.raises(ValueError, match="objective"):
        svc.start_optimization(
            name="t", strategy_code=CODE, symbols=["NSE:X"],
            optimize={"param_space": {"a": [1]}, "objective": "bogus"},
        )
    assert db_session().query(BacktestOptimization).count() == 0
    db_session.remove()


def test_optimization_failure_is_recorded(monkeypatch, clean_db):
    monkeypatch.setattr(svc, "load_run_bars", lambda *a, **k: ({"NSE:X": make_bars()}, []))
    opt_id = svc.start_optimization(
        name="t", strategy_code="raise RuntimeError('kaput')", symbols=["NSE:X"],
        optimize={"param_space": {"a": [1, 2]}},
    )
    rec = _wait(opt_id)
    assert rec.status == "failed" and "kaput" in rec.error_message
    db_session.remove()
