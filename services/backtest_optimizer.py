# services/backtest_optimizer.py

"""Parameter sweeps, walk-forward analysis and Monte Carlo for the strategy backtester.

Strategies expose tunable values through ``params`` (event strategies:
``self.params["fast"]``; signal DSL: the injected ``PARAMS`` dict). The
optimizer evaluates many parameter sets against ONE loaded dataset inside a
single sandboxed child process, then ranks them here.

Overfitting guards built in:
  * minimum trade count: a parameter set with too few trades can not win,
  * out-of-sample evaluation of every candidate (``oos_pct``),
  * walk-forward: parameters are chosen on each training window only and
    scored on the unseen window that follows, with the OOS curve stitched,
  * deflated Sharpe ratio: the best of N trials is discounted for selection bias.
"""

from __future__ import annotations

import itertools
import json
import math
import random
import uuid
from datetime import datetime
from statistics import NormalDist
from typing import Any

import numpy as np
import pandas as pd
import pytz

from database.backtest_db import BacktestOptimization, db_session
from services.backtest_analytics import calculate_backtest_metrics
from services.strategy_sandbox import BacktestCancelled, run_tasks_sandboxed
from utils.logging import get_logger

logger = get_logger(__name__)

MAX_TRIALS = 300
MAX_VALUES_PER_PARAM = 100
_OBJECTIVES = {
    "sharpe_ratio", "sortino_ratio", "total_return_pct", "cagr_pct", "profit_factor",
    "expectancy", "calmar", "win_rate_pct",
}
_EULER_GAMMA = 0.5772156649015329
_N = NormalDist()


# --------------------------------------------------------------------------
# parameter space
# --------------------------------------------------------------------------
def _expand_values(name: str, spec: Any) -> list:
    if isinstance(spec, list):
        values = spec
    elif isinstance(spec, dict) and {"min", "max"} <= set(spec):
        lo, hi = spec["min"], spec["max"]
        step = spec.get("step", 1)
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lo, hi, step)):
            raise ValueError(f"param {name!r}: min/max/step must be numbers")
        if step <= 0 or hi < lo:
            raise ValueError(f"param {name!r}: need step > 0 and max >= min")
        n = int(math.floor((hi - lo) / step + 1e-9)) + 1
        if n > MAX_VALUES_PER_PARAM:
            raise ValueError(f"param {name!r}: {n} values exceeds the limit of {MAX_VALUES_PER_PARAM}")
        ints = all(isinstance(v, int) for v in (lo, hi, step))
        values = [lo + i * step if ints else round(lo + i * step, 10) for i in range(n)]
    else:
        raise ValueError(f"param {name!r}: give a list of values or {{min, max, step}}")
    if not values:
        raise ValueError(f"param {name!r} has no values")
    if len(values) > MAX_VALUES_PER_PARAM:
        raise ValueError(f"param {name!r}: more than {MAX_VALUES_PER_PARAM} values")
    for v in values:
        if not isinstance(v, (int, float, str, bool)) or (isinstance(v, float) and not math.isfinite(v)):
            raise ValueError(f"param {name!r}: values must be finite numbers, strings or booleans")
    return values


def build_param_combos(
    space: dict, method: str = "grid", max_trials: int = 100, seed: int = 0
) -> list[dict]:
    """Expand a parameter space into at most ``max_trials`` parameter dicts."""
    if not isinstance(space, dict) or not space:
        raise ValueError("param_space must be a non-empty object")
    if method not in ("grid", "random"):
        raise ValueError("method must be 'grid' or 'random'")
    max_trials = int(max(1, min(max_trials, MAX_TRIALS)))
    names = list(space)
    axes = [_expand_values(n, space[n]) for n in names]
    total = math.prod(len(a) for a in axes)

    if method == "grid":
        if total > max_trials:
            raise ValueError(
                f"grid has {total} combinations (limit {max_trials}); "
                "narrow the ranges or use method='random'"
            )
        return [dict(zip(names, combo, strict=True)) for combo in itertools.product(*axes)]

    rng = random.Random(seed)
    if total <= max_trials:
        combos = [dict(zip(names, c, strict=True)) for c in itertools.product(*axes)]
        rng.shuffle(combos)
        return combos
    seen: set[tuple] = set()
    out: list[dict] = []
    attempts = 0
    while len(out) < max_trials and attempts < max_trials * 50:
        attempts += 1
        pick = tuple(rng.choice(a) for a in axes)
        if pick in seen:
            continue
        seen.add(pick)
        out.append(dict(zip(names, pick, strict=True)))
    return out


# --------------------------------------------------------------------------
# metrics / ranking
# --------------------------------------------------------------------------
def _metrics_for(task: dict, timeline: pd.DatetimeIndex, capital: float, interval: str) -> dict | None:
    if "equity" not in task:
        return None
    eq = task["equity"]
    idx = timeline[task["start"] : task["start"] + len(eq)]
    series = pd.Series(eq, index=idx)
    m = calculate_backtest_metrics(series, task["trades"], capital, interval=interval)
    m["calmar"] = round(m["cagr_pct"] / m["max_drawdown_pct"], 2) if m["max_drawdown_pct"] > 0 else 0.0
    return m


def _score(metrics: dict | None, objective: str, min_trades: int) -> float:
    if metrics is None or metrics["total_trades"] < min_trades:
        return float("-inf")
    value = metrics.get(objective, 0.0)
    return float(value) if value is not None and math.isfinite(value) else float("-inf")


def deflated_sharpe(best_returns: np.ndarray, trial_sharpes: list[float]) -> dict:
    """Deflated Sharpe ratio (Bailey & Lopez de Prado, 2014).

    ``best_returns`` are the per-bar returns of the selected configuration;
    ``trial_sharpes`` are the per-bar (NOT annualised) Sharpe ratios of every
    trial. Returns the probability that the selected Sharpe is real after
    accounting for how many trials were run.
    """
    r = np.asarray(best_returns, dtype=float)
    r = r[np.isfinite(r)]
    t = len(r)
    if t < 10 or r.std(ddof=1) == 0:
        return {"dsr": None, "psr": None, "sr0": None, "trials": len(trial_sharpes)}
    sr = float(r.mean() / r.std(ddof=1))
    z = (r - r.mean()) / r.std(ddof=1)
    skew = float((z**3).mean())
    kurt = float((z**4).mean())
    denom = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr**2
    if denom <= 0:
        return {"dsr": None, "psr": None, "sr0": None, "trials": len(trial_sharpes)}
    scale = math.sqrt(t - 1) / math.sqrt(denom)
    psr = _N.cdf(sr * scale)
    n = len(trial_sharpes)
    sr0 = 0.0
    if n > 1:
        v = float(np.var(trial_sharpes, ddof=1))
        sr0 = math.sqrt(max(v, 0.0)) * (
            (1 - _EULER_GAMMA) * _N.inv_cdf(1 - 1.0 / n)
            + _EULER_GAMMA * _N.inv_cdf(1 - 1.0 / (n * math.e))
        )
    dsr = _N.cdf((sr - sr0) * scale)
    return {"dsr": round(dsr, 4), "psr": round(psr, 4), "sr0": round(sr0, 6), "trials": n}


def _period_sharpe(eq: np.ndarray) -> float:
    if len(eq) < 3:
        return 0.0
    r = np.diff(eq) / eq[:-1]
    sd = r.std(ddof=1)
    return float(r.mean() / sd) if sd > 0 else 0.0


# --------------------------------------------------------------------------
# studies
# --------------------------------------------------------------------------
def _timeline(bars: dict[str, pd.DataFrame]) -> pd.DatetimeIndex:
    union = None
    for df in bars.values():
        idx = pd.DatetimeIndex(pd.to_datetime(df["timestamp"]))
        union = idx if union is None else union.union(idx)
    return union.sort_values()


def _ts(timeline: pd.DatetimeIndex, i: int) -> str:
    return timeline[i].strftime("%Y-%m-%d %H:%M:%S")


def _summary_row(params: dict, m: dict | None, score: float) -> dict:
    return {
        "params": params,
        "score": None if not math.isfinite(score) else round(score, 4),
        "metrics": m,
    }


def run_sweep(
    strategy_code: str,
    bars: dict[str, pd.DataFrame],
    base_config: dict,
    spec: dict,
    capital: float,
    interval: str,
    cancel_event=None,
    progress_cb=None,
) -> dict:
    combos = build_param_combos(
        spec["param_space"], spec.get("method", "grid"), spec.get("max_trials", 100), spec.get("seed", 0)
    )
    objective = spec.get("objective", "sharpe_ratio")
    min_trades = int(spec.get("min_trades", 5))
    oos_pct = float(spec.get("oos_pct") or 0.0)
    timeline = _timeline(bars)
    n = len(timeline)
    if n < 20:
        raise ValueError("Not enough bars to optimise")

    split = int(n * (1 - oos_pct / 100.0)) if oos_pct else n
    in_end = _ts(timeline, split - 1)
    tasks = [{"id": i, "params": p, "start": None, "end": in_end if oos_pct else None}
             for i, p in enumerate(combos)]
    if oos_pct:
        oos_start = _ts(timeline, split)
        tasks += [{"id": len(combos) + i, "params": p, "start": oos_start, "end": None}
                  for i, p in enumerate(combos)]

    out = run_tasks_sandboxed(
        strategy_code, bars, base_config, tasks, cancel_event=cancel_event,
        progress_cb=progress_cb,
    )
    tl = pd.DatetimeIndex(pd.to_datetime(out["timeline"]))
    results = out["tasks"]

    errors = [r["error"] for r in results.values() if "error" in r]
    if errors and len(errors) == len(results):
        raise ValueError(f"Every parameter set failed:\n{errors[0]}")

    rows = []
    trial_sharpes = []
    for i, p in enumerate(combos):
        m_in = _metrics_for(results[i], tl, capital, interval)
        row = _summary_row(p, m_in, _score(m_in, objective, min_trades))
        if "error" in results[i]:
            row["error"] = results[i]["error"]
        else:
            trial_sharpes.append(_period_sharpe(results[i]["equity"]))
        if oos_pct:
            m_out = _metrics_for(results[len(combos) + i], tl, capital, interval)
            row["oos_metrics"] = m_out
            row["oos_score"] = None if m_out is None else round(_score(m_out, objective, 0), 4)
        rows.append(row)

    ranked = sorted(rows, key=lambda r: (r["score"] is None, -(r["score"] or 0)))
    best = ranked[0] if ranked and ranked[0]["score"] is not None else None
    dsr = None
    if best is not None:
        idx = rows.index(best)
        eq = results[idx]["equity"]
        dsr = deflated_sharpe(np.diff(eq) / eq[:-1], trial_sharpes)

    warnings = []
    if best is None:
        warnings.append(f"No parameter set reached the minimum of {min_trades} trades.")
    if dsr and dsr["dsr"] is not None and dsr["dsr"] < 0.95:
        warnings.append(
            f"Deflated Sharpe {dsr['dsr']:.2f} is below 0.95: the best result may be selection luck "
            f"after {dsr['trials']} trials."
        )
    if oos_pct and best is not None and best.get("oos_score") is not None and best["score"]:
        if best["oos_score"] < 0.5 * best["score"]:
            warnings.append("Out-of-sample score is under half the in-sample score: likely overfit.")

    return {
        "kind": "sweep",
        "objective": objective,
        "trials": len(combos),
        "oos_pct": oos_pct,
        "best": best,
        "deflated_sharpe": dsr,
        "ranked": ranked,
        "param_names": list(spec["param_space"]),
        "warnings": warnings,
    }


def run_walk_forward(
    strategy_code: str,
    bars: dict[str, pd.DataFrame],
    base_config: dict,
    spec: dict,
    capital: float,
    interval: str,
    cancel_event=None,
    progress_cb=None,
) -> dict:
    combos = build_param_combos(
        spec["param_space"], spec.get("method", "grid"), spec.get("max_trials", 50), spec.get("seed", 0)
    )
    objective = spec.get("objective", "sharpe_ratio")
    min_trades = int(spec.get("min_trades", 3))
    train_pct = float(spec.get("train_pct", 60))
    test_pct = float(spec.get("test_pct", 20))
    max_windows = int(spec.get("n_windows", 5))
    if not (10 <= train_pct <= 90 and 5 <= test_pct <= 50 and train_pct + test_pct <= 100):
        raise ValueError("need 10<=train_pct<=90, 5<=test_pct<=50 and train_pct+test_pct<=100")
    if not 1 <= max_windows <= 20:
        raise ValueError("n_windows must be between 1 and 20")

    timeline = _timeline(bars)
    n = len(timeline)
    train_len = int(n * train_pct / 100.0)
    test_len = int(n * test_pct / 100.0)
    if train_len < 10 or test_len < 5:
        raise ValueError("Not enough bars for the requested windows")
    windows = []
    start = 0
    while start + train_len + test_len <= n and len(windows) < max_windows:
        windows.append((start, start + train_len - 1, start + train_len, start + train_len + test_len - 1))
        start += test_len
    if not windows:
        raise ValueError("The window settings leave no complete train+test window")

    # Round 1: every candidate on every training window.
    tasks1 = []
    tid = 0
    for w, (a, b, _, _) in enumerate(windows):
        for p in combos:
            tasks1.append({"id": tid, "params": p, "start": _ts(timeline, a), "end": _ts(timeline, b), "w": w})
            tid += 1
    half = (lambda pct: progress_cb(pct * 0.8)) if progress_cb else None  # noqa: E731
    out1 = run_tasks_sandboxed(strategy_code, bars, base_config, tasks1,
                               cancel_event=cancel_event, progress_cb=half)
    tl = pd.DatetimeIndex(pd.to_datetime(out1["timeline"]))

    chosen = []
    trial_sharpes: list[float] = []
    for w in range(len(windows)):
        best = None
        for t in tasks1:
            if t["w"] != w:
                continue
            res = out1["tasks"][t["id"]]
            m = _metrics_for(res, tl, capital, interval)
            sc = _score(m, objective, min_trades)
            if "equity" in res:
                trial_sharpes.append(_period_sharpe(res["equity"]))
            if best is None or sc > best[0]:
                best = (sc, t["params"], m)
        chosen.append(best if best and math.isfinite(best[0]) else None)

    # Round 2: each window's winner on the unseen test window that follows.
    tasks2 = []
    for w, ch in enumerate(chosen):
        if ch is None:
            continue
        _, _, c, d = windows[w]
        tasks2.append({"id": w, "params": ch[1], "start": _ts(timeline, c), "end": _ts(timeline, d)})
    if not tasks2:
        raise ValueError(f"No window had a parameter set with at least {min_trades} trades")
    out2 = run_tasks_sandboxed(strategy_code, bars, base_config, tasks2,
                               cancel_event=cancel_event,
                               progress_cb=(lambda pct: progress_cb(80 + pct * 0.2)) if progress_cb else None)
    tl2 = pd.DatetimeIndex(pd.to_datetime(out2["timeline"]))

    rows = []
    stitched = [capital]
    stitched_ts = []
    for w, ch in enumerate(chosen):
        a, b, c, d = windows[w]
        row = {
            "window": w,
            "train": [_ts(timeline, a), _ts(timeline, b)],
            "test": [_ts(timeline, c), _ts(timeline, d)],
        }
        if ch is None:
            row["skipped"] = f"no parameter set had {min_trades}+ trades in the training window"
            rows.append(row)
            continue
        res = out2["tasks"][w]
        m_test = _metrics_for(res, tl2, capital, interval)
        row.update({"params": ch[1], "train_metrics": ch[2], "test_metrics": m_test})
        rows.append(row)
        if "equity" in res and len(res["equity"]) > 1:
            eq = res["equity"]
            window_ts = tl2[res["start"] : res["start"] + len(eq)]
            rets = eq / eq[0]
            base = stitched[-1]
            stitched.extend((base * rets[1:]).tolist())
            stitched_ts.extend(window_ts[1:])

    oos_metrics = None
    if len(stitched_ts) > 2:
        series = pd.Series(stitched[1:], index=pd.DatetimeIndex(stitched_ts))
        oos_metrics = calculate_backtest_metrics(series, [], capital, interval=interval)
        oos_metrics["total_trades"] = sum(
            (r["test_metrics"] or {}).get("total_trades", 0) for r in rows if r.get("test_metrics")
        )

    is_scores = [r["train_metrics"].get(objective, 0) for r in rows if r.get("train_metrics")]
    oos_scores = [r["test_metrics"].get(objective, 0) for r in rows if r.get("test_metrics")]
    efficiency = None
    if is_scores and oos_scores and np.mean(is_scores) not in (0, 0.0):
        efficiency = round(float(np.mean(oos_scores) / np.mean(is_scores)), 3)

    dsr = None
    if len(stitched) > 12:
        dsr = deflated_sharpe(np.diff(stitched) / np.array(stitched[:-1]), trial_sharpes)

    warnings = []
    if efficiency is not None and efficiency < 0.5:
        warnings.append(
            f"Walk-forward efficiency {efficiency:.2f}: out-of-sample {objective} is less than half "
            "of in-sample, a sign of overfitting."
        )
    losing = sum(1 for r in rows if r.get("test_metrics") and r["test_metrics"]["total_return_pct"] < 0)
    if rows and losing > len(rows) / 2:
        warnings.append(f"{losing} of {len(rows)} test windows lost money.")

    return {
        "kind": "walk_forward",
        "objective": objective,
        "trials": len(combos),
        "windows": rows,
        "oos_metrics": oos_metrics,
        "oos_equity": [
            {"timestamp": str(t), "equity": round(float(e), 2)}
            for t, e in zip(stitched_ts, stitched[1:], strict=True)
        ][:5000],
        "walk_forward_efficiency": efficiency,
        "deflated_sharpe": dsr,
        "param_names": list(spec["param_space"]),
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# Monte Carlo on realised trades
# --------------------------------------------------------------------------
def monte_carlo(
    pnls: list[float],
    initial_capital: float,
    n_sims: int = 2000,
    method: str = "bootstrap",
    seed: int = 0,
    ruin_drawdown_pct: float = 50.0,
) -> dict:
    """Resample the trade sequence to show how much of the result was path luck.

    ``bootstrap`` draws trades with replacement; ``shuffle`` reorders the same
    trades. Equity is the running sum of trade P&L from ``initial_capital``.
    """
    if method not in ("bootstrap", "shuffle"):
        raise ValueError("method must be 'bootstrap' or 'shuffle'")
    n_sims = int(max(100, min(n_sims, 20000)))
    arr = np.asarray(pnls, dtype=float)
    if len(arr) < 5:
        raise ValueError("Need at least 5 trades for a Monte Carlo analysis")
    if len(arr) > 50000:
        raise ValueError("Too many trades for Monte Carlo (limit 50000)")
    rng = np.random.default_rng(seed)
    if method == "bootstrap":
        draws = rng.choice(arr, size=(n_sims, len(arr)), replace=True)
    else:
        draws = np.stack([rng.permutation(arr) for _ in range(n_sims)])
    paths = initial_capital + np.cumsum(draws, axis=1)
    full = np.concatenate([np.full((n_sims, 1), initial_capital), paths], axis=1)
    peaks = np.maximum.accumulate(full, axis=1)
    dd = np.where(peaks > 0, (peaks - full) / peaks, 0.0).max(axis=1) * 100.0
    final = full[:, -1]

    def pct(a, qs=(5, 25, 50, 75, 95)):
        return {f"p{q}": round(float(np.percentile(a, q)), 2) for q in qs}

    steps = np.unique(np.linspace(0, full.shape[1] - 1, num=min(60, full.shape[1])).astype(int))
    bands = {f"p{q}": [round(float(v), 2) for v in np.percentile(full[:, steps], q, axis=0)]
             for q in (5, 25, 50, 75, 95)}
    actual_final = initial_capital + float(arr.sum())
    return {
        "method": method,
        "simulations": n_sims,
        "trades": int(len(arr)),
        "actual_final_equity": round(actual_final, 2),
        "final_equity": pct(final),
        "max_drawdown_pct": pct(dd),
        "probability_of_loss": round(float((final < initial_capital).mean()), 4),
        "probability_of_ruin": round(float((dd >= ruin_drawdown_pct).mean()), 4),
        "ruin_drawdown_pct": ruin_drawdown_pct,
        "actual_percentile": round(float((final < actual_final).mean() * 100.0), 1),
        "fan": {"steps": steps.tolist(), **bands},
    }


# --------------------------------------------------------------------------
# persistence + background execution
# --------------------------------------------------------------------------
def validate_spec(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise ValueError("optimize must be an object")
    kind = spec.get("kind", "sweep")
    if kind not in ("sweep", "walk_forward"):
        raise ValueError("optimize.kind must be 'sweep' or 'walk_forward'")
    if spec.get("objective", "sharpe_ratio") not in _OBJECTIVES:
        raise ValueError(f"optimize.objective must be one of {sorted(_OBJECTIVES)}")
    build_param_combos(  # validates the space eagerly so bad input fails at submit time
        spec.get("param_space"), spec.get("method", "grid"),
        spec.get("max_trials", 100 if kind == "sweep" else 50), spec.get("seed", 0),
    )
    oos = spec.get("oos_pct")
    if oos is not None and not (isinstance(oos, (int, float)) and 5 <= oos <= 50):
        raise ValueError("optimize.oos_pct must be between 5 and 50")
    return {**spec, "kind": kind}


def _now():
    return datetime.now(pytz.timezone("Asia/Kolkata"))


def create_optimization_record(name: str, strategy_code: str, config: dict) -> str:
    spec = validate_spec(config["optimize"])
    config = {**config, "optimize": spec}
    opt_id = f"opt_{uuid.uuid4().hex[:12]}"
    session = db_session()
    try:
        session.add(BacktestOptimization(
            opt_id=opt_id, name=name, kind=spec["kind"], status="pending",
            strategy_code=strategy_code, config_json=json.dumps(config),
        ))
        session.commit()
    finally:
        db_session.remove()
    return opt_id


def run_optimization_job(opt_id: str, cancel_event, loader) -> None:
    """Background worker: load data, run the study, persist the result."""
    session = db_session()
    try:
        rec = session.query(BacktestOptimization).filter_by(opt_id=opt_id).first()
        if rec is None:
            return
        rec.status = "running"
        session.commit()
        cfg = json.loads(rec.config_json)
        spec = cfg["optimize"]
        bars, notes = loader(cfg)

        def progress(pct: float) -> None:
            s2 = db_session()
            try:
                s2.query(BacktestOptimization).filter_by(opt_id=opt_id).update({"progress": float(pct)})
                s2.commit()
            finally:
                db_session.remove()

        base = cfg["engine_config"]
        capital = float(base["initial_capital"])
        runner = run_walk_forward if spec["kind"] == "walk_forward" else run_sweep
        result = runner(rec.strategy_code, bars, base, spec, capital, cfg["interval"],
                        cancel_event=cancel_event, progress_cb=progress)
        result["notes"] = notes
        session = db_session()
        rec = session.query(BacktestOptimization).filter_by(opt_id=opt_id).first()
        rec.status = "completed"
        rec.progress = 100.0
        rec.result_json = json.dumps(result, default=_json_default)
        rec.completed_at = _now()
        session.commit()
    except BacktestCancelled:
        _finish(opt_id, "cancelled", None)
    except Exception as exc:
        logger.exception(f"Optimization {opt_id} failed: {exc}")
        _finish(opt_id, "failed", str(exc))
    finally:
        db_session.remove()


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(f"not JSON serialisable: {type(o)}")


def _finish(opt_id: str, status: str, error: str | None) -> None:
    session = db_session()
    try:
        session.rollback()
        rec = session.query(BacktestOptimization).filter_by(opt_id=opt_id).first()
        if rec is not None:
            rec.status = status
            rec.error_message = error
            rec.completed_at = _now()
            session.commit()
    finally:
        db_session.remove()
