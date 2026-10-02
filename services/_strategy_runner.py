"""Child-process runner for user strategy code. Invoked by strategy_sandbox only.

argv: <workdir>
Reads <workdir>/job.json; modes:
  signals - run the signal DSL once, write signal columns (out.<fmt>)
  engine  - run the event-driven multi-symbol engine, write trades/equity
"""

import builtins
import json
import os
import sys
import traceback

_SERVICES_DIR = os.path.dirname(os.path.abspath(__file__))

_ALLOWED_IMPORTS = {
    "math", "statistics", "numpy", "pandas", "ta", "openalgo", "datetime",
    "itertools", "functools", "collections", "typing", "numbers", "decimal",
    "fractions", "operator", "random", "warnings", "time", "dataclasses",
    "enum", "abc", "copy", "heapq", "bisect",
}
_SIGNAL_COLS = ("signal", "buy_signal", "sell_signal")


def _read(path, fmt, pd):
    return pd.read_parquet(path) if fmt == "parquet" else pd.read_csv(path)


def _write(df, path, fmt):
    df.to_parquet(path) if fmt == "parquet" else df.to_csv(path, index=False)


def _apply_limits(job):
    import resource

    mem = int(job.get("mem_mb", 1024)) * 1024 * 1024
    cpu = int(job.get("cpu_s", 65))
    resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _lock_down():
    import os as _os
    import socket

    def _denied(*_a, **_k):
        raise PermissionError("not permitted in strategy sandbox")

    for name in ("socket", "create_connection", "getaddrinfo", "gethostbyname", "socketpair"):
        if hasattr(socket, name):
            setattr(socket, name, _denied)
    for name in (
        "system", "popen", "fork", "forkpty", "kill", "killpg", "putenv", "unsetenv",
        "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe",
        "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp",
        "spawnvpe", "posix_spawn", "posix_spawnp", "remove", "unlink", "rmdir", "rename",
    ):
        if hasattr(_os, name):
            setattr(_os, name, _denied)
    sub = sys.modules.get("subprocess")
    if sub is not None:
        for name in ("Popen", "run", "call", "check_call", "check_output", "getoutput"):
            if hasattr(sub, name):
                setattr(sub, name, _denied)


def _user_namespace(extra):
    real_import = builtins.__import__

    def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and name.split(".")[0] not in _ALLOWED_IMPORTS:
            raise ImportError(f"import of '{name}' is not allowed in strategy code")
        return real_import(name, globals, locals, fromlist, level)

    safe = {
        k: v for k, v in vars(builtins).items()
        if k not in ("open", "eval", "exec", "compile", "input", "breakpoint",
                     "help", "exit", "quit", "__loader__", "__spec__")
    }
    safe["__import__"] = _guarded_import
    return {"__builtins__": safe, **extra}


def _import_ta():
    try:
        from openalgo import ta
    except ImportError:
        try:
            import ta
        except ImportError:
            ta = None
    return ta


def _run_user_code(code, extra):
    env = _user_namespace(extra)
    exec(compile(code, "<strategy>", "exec"), env)  # noqa: S102 - sandboxed child
    return env


def mode_signals(job, workdir, code, pd, np, ta):
    fmt = job["fmt"]
    df = _read(os.path.join(workdir, "in." + fmt), fmt, pd)
    _lock_down()
    env = _run_user_code(code, {"pd": pd, "np": np, "ta": ta, "df": df})
    fn = env.get("strategy")
    if callable(fn):
        res = fn(df.copy())
        if isinstance(res, pd.DataFrame):
            df = res
    if "buy_signal" in df.columns and "sell_signal" in df.columns:
        df["signal"] = 0
        df.loc[df["buy_signal"].astype(bool), "signal"] = 1
        df.loc[df["sell_signal"].astype(bool), "signal"] = -1
    elif "signal" not in df.columns:
        df["signal"] = 0
    out = df[[c for c in _SIGNAL_COLS if c in df.columns]].reset_index(drop=True)
    for c in out.columns:
        out[c] = pd.to_numeric(out[c], errors="coerce") if c == "signal" else out[c].astype(bool)
    _write(out, os.path.join(workdir, "out." + fmt), fmt)



def _build_strategy(code, bars, cfg, symbols, pd, np, ta, be):
    """Exec the user code and return a Strategy instance (event class, functions, or signal DSL)."""
    first = bars[symbols[0]]
    extra = {"pd": pd, "np": np, "ta": ta, "df": first.copy(), "Strategy": be.Strategy,
             "SYMBOLS": list(symbols), "PARAMS": dict(cfg.params)}
    env = _run_user_code(code, extra)

    strat = None
    for v in env.values():
        if isinstance(v, type) and issubclass(v, be.Strategy) and v is not be.Strategy:
            strat = v()
            base = dict(getattr(v, "params", {}) or {})
            strat.params = {**base, **cfg.params}
            cfg.params = strat.params
    if strat is None and callable(env.get("on_bar")):
        init_fn, bar_fn = env.get("init"), env["on_bar"]

        class FnStrategy(be.Strategy):
            def init(self, ctx):
                if callable(init_fn):
                    init_fn(ctx)

            def on_bar(self, ctx):
                bar_fn(ctx)

        strat = FnStrategy()
    if strat is None:  # legacy signal DSL, one independent series per symbol
        signals = {}
        for sym, frame in bars.items():
            def run_code(frame_copy):
                e = _run_user_code(code, {"pd": pd, "np": np, "ta": ta, "df": frame_copy,
                                          "PARAMS": dict(cfg.params)})
                fn = e.get("strategy")
                if callable(fn):
                    res = fn(frame_copy.copy())
                    if isinstance(res, pd.DataFrame):
                        return res
                return frame_copy

            signals[sym] = be.compute_signals(run_code, frame)
        strat = be.SignalStrategy(signals)
    return strat


def _load_bars(job, workdir, pd):
    fmt = job["fmt"]
    return {sym: _read(os.path.join(workdir, f"bars_{i}.{fmt}"), fmt, pd)
            for i, sym in enumerate(job["symbols"])}


def mode_engine(job, workdir, code, pd, np, ta):
    sys.path.insert(0, _SERVICES_DIR)
    import backtest_engine as be

    bars = _load_bars(job, workdir, pd)
    cfg = be.EngineConfig.from_dict(job["config"])

    prog = open(os.path.join(workdir, "progress.json"), "w")  # noqa: SIM115 - held for the run

    def progress(pct, ts, equity, ntrades):
        prog.seek(0)
        prog.write(json.dumps({"pct": pct, "ts": ts, "equity": equity, "trades": ntrades}).ljust(200))
        prog.flush()

    _lock_down()
    strat = _build_strategy(code, bars, cfg, job["symbols"], pd, np, ta, be)
    result = be.Engine(bars, cfg, strat, progress_cb=progress).run()

    pd.DataFrame(result.trades, columns=_TRADE_COLS).to_json(
        os.path.join(workdir, "trades.json"), orient="records")
    pd.DataFrame(result.equity).to_json(os.path.join(workdir, "equity.json"), orient="records")
    with open(os.path.join(workdir, "meta.json"), "w") as fh:
        json.dump({"notes": result.notes, "logs": result.logs,
                   "rejections": result.rejections, "cancelled": result.cancelled}, fh)


def mode_tasks(job, workdir, code, pd, np, ta):
    """Run many (params, time-window) engine evaluations over one loaded dataset.

    Each task: {"id", "params", "start", "end"} where start/end are inclusive
    timestamp strings or null. Writes eq_<id>.npy (equity floats) plus
    tasks.json (offsets into timeline.json and slim trade rows).
    """
    sys.path.insert(0, _SERVICES_DIR)
    import backtest_engine as be

    bars = _load_bars(job, workdir, pd)
    symbols = job["symbols"]
    base_cfg = dict(job["config"])
    union = None
    for frame in bars.values():
        idx = pd.DatetimeIndex(pd.to_datetime(frame["timestamp"]))
        union = idx if union is None else union.union(idx)
    timeline = list(union.sort_values().strftime("%Y-%m-%d %H:%M:%S"))
    timeline_arr = np.array(timeline)
    with open(os.path.join(workdir, "timeline.json"), "w") as fh:
        json.dump(timeline, fh)

    prog = open(os.path.join(workdir, "progress.json"), "w")  # noqa: SIM115 - held for the run
    _lock_down()

    out = []
    tasks = job["tasks"]
    for n, task in enumerate(tasks):
        rec = {"id": task["id"]}
        try:
            sub = {}
            for sym in symbols:
                f = bars[sym]
                keep = np.ones(len(f), dtype=bool)
                if task.get("start"):
                    keep &= (f["timestamp"] >= task["start"]).to_numpy()
                if task.get("end"):
                    keep &= (f["timestamp"] <= task["end"]).to_numpy()
                sub[sym] = f[keep].reset_index(drop=True)
            sub = {k: v for k, v in sub.items() if len(v) >= 2}
            if not sub:
                raise ValueError("no bars in the task window")
            cfg = be.EngineConfig.from_dict({**base_cfg, "params": task.get("params", {})})
            strat = _build_strategy(code, sub, cfg, list(sub), pd, np, ta, be)
            res = be.Engine(sub, cfg, strat).run()
            eq = np.array([r["equity"] for r in res.equity], dtype="float64")
            np.save(os.path.join(workdir, f"eq_{task['id']}.npy"), eq, allow_pickle=False)
            rec.update({
                "start": int(np.searchsorted(timeline_arr, res.equity[0]["timestamp"])),
                "n": int(len(eq)),
                "trades": [{k: t[k] for k in ("pnl", "holding_period_bars", "entry_time", "exit_time")}
                           for t in res.trades],
                "notes": res.notes,
            })
        except Exception:
            lines = traceback.format_exc().strip().splitlines()
            rec["error"] = "\n".join(lines[-4:])
        out.append(rec)
        prog.seek(0)
        prog.write(json.dumps({"pct": (n + 1) / len(tasks) * 100.0, "ts": str(task["id"]),
                               "equity": 0.0, "trades": 0}).ljust(200))
        prog.flush()
    with open(os.path.join(workdir, "tasks.json"), "w") as fh:
        json.dump(out, fh)


_TRADE_COLS = [
    "symbol", "exchange", "action", "entry_time", "entry_price", "exit_time", "exit_price",
    "quantity", "pnl", "pnl_pct", "holding_period_bars", "entry_reason", "exit_reason",
    "costs", "mae_pct", "mfe_pct",
]


def main():
    workdir = sys.argv[1]
    with open(os.path.join(workdir, "job.json"), encoding="utf-8") as fh:
        job = json.load(fh)
    with open(os.path.join(workdir, "code.py"), encoding="utf-8") as fh:
        code = fh.read()

    import numpy as np
    import pandas as pd

    ta = _import_ta()
    _apply_limits(job)
    try:
        {"signals": mode_signals, "engine": mode_engine, "tasks": mode_tasks}[job["mode"]](
            job, workdir, code, pd, np, ta)
    except SystemExit:
        raise
    except BaseException as exc:  # report the user-code failure compactly
        lines = traceback.format_exc().strip().splitlines()
        keep = [ln for ln in lines if "<strategy>" in ln or not ln.startswith("  File")]
        print("\n".join(keep[-8:]), file=sys.stderr)
        if isinstance(exc, MemoryError):
            print("MemoryError", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
