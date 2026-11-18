"""Run user strategy code in a locked-down child process.

Defence in depth, NOT a security boundary. Layers: minimal environment (no
secrets from .env), temp cwd, rlimits (memory/CPU/file size, applied by the
runner before user code), wall-clock kill of the whole process group, a
network-less namespace via ``unshare`` when available, and in-process
restrictions in the child (import whitelist, no open/eval/exec, blocked
sockets and process spawning). Determined Python-level escapes (e.g. via
object introspection) are possible; run OpenAlgo as an unprivileged user and
treat strategy code from untrusted sources accordingly.

Data crosses the process boundary as parquet/CSV/JSON, never pickle, so a
compromised child cannot execute code in the parent on deserialisation.

Two entry points:
  run_strategy_sandboxed  - signal DSL only (one frame in, signal columns out)
  run_backtest_sandboxed  - full event-driven multi-symbol engine run
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import ExitStack
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from utils.logging import get_logger

_RUNNER = str(Path(__file__).with_name("_strategy_runner.py"))
_VALID_SIGNALS = {-2, -1, 0, 1, 2}
logger = get_logger(__name__)


class BacktestCancelled(Exception):
    """Raised when the caller's cancel event fires during a sandboxed run."""


def _fmt() -> str:
    try:
        import pyarrow  # noqa: F401

        return "parquet"
    except ImportError:
        return "csv"


@lru_cache(maxsize=1)
def _unshare_prefix() -> tuple[str, ...]:
    exe = shutil.which("unshare")
    if not exe:
        return ()
    cmd = (exe, "--net", "--map-root-user")
    try:
        r = subprocess.run([*cmd, "true"], capture_output=True, timeout=5)  # noqa: S603
        return cmd if r.returncode == 0 else ()
    except Exception:
        return ()


def _write_frame(df: pd.DataFrame, path: str, fmt: str) -> None:
    df = df.reset_index(drop=True)
    df.to_parquet(path) if fmt == "parquet" else df.to_csv(path, index=False)


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()


def _run_child(
    tmp: str,
    job: dict,
    code: str,
    timeout_s: float,
    cancel_event=None,
    on_progress: Callable[[dict], None] | None = None,
) -> None:
    """Spawn one runner through the shared child supervisor."""
    callback = (lambda report, _index: on_progress(report)) if on_progress else None
    _supervise_children([(tmp, job, code)], timeout_s, cancel_event, callback)


def _supervise_children(children, timeout_s, cancel_event=None, on_progress=None) -> None:
    """Run child specifications concurrently, killing and reaping all on failure."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        "OMP_NUM_THREADS": "1",
    }
    procs = []
    deadline = time.monotonic() + timeout_s
    last_progress = [None] * len(children)
    try:
        with ExitStack() as stack:
            out_fh = stack.enter_context(open(os.devnull, "wb"))
            for workdir, job, code in children:
                Path(workdir, "job.json").write_text(json.dumps(job), encoding="utf-8")
                Path(workdir, "code.py").write_text(code, encoding="utf-8")
                err_fh = stack.enter_context(open(os.path.join(workdir, "stderr.log"), "wb"))
                cmd = [*_unshare_prefix(), sys.executable, "-I", _RUNNER, workdir]
                proc = subprocess.Popen(  # noqa: S603
                    cmd, cwd=workdir, env=env, stdin=subprocess.DEVNULL,
                    stdout=out_fh, stderr=err_fh, start_new_session=True,
                )
                procs.append((proc, workdir, job))

            while any(proc.poll() is None for proc, _, _ in procs):
                if cancel_event is not None and cancel_event.is_set():
                    raise BacktestCancelled()
                if time.monotonic() > deadline:
                    raise ValueError(f"Strategy timed out after {timeout_s:g}s")
                for i, (_proc, workdir, _) in enumerate(procs):
                    # Read finished children too: the last write of a child that
                    # exited between two polls would otherwise be lost.
                    if on_progress is not None:
                        prog_path = os.path.join(workdir, "progress.json")
                        try:
                            raw = Path(prog_path).read_text().strip()
                            if raw and raw != last_progress[i]:
                                last_progress[i] = raw
                                on_progress(json.loads(raw), i)
                        except (ValueError, OSError):
                            pass
                failed = next((x for x in procs if x[0].poll() not in (None, 0)), None)
                if failed:
                    proc, workdir, job = failed
                    _kill_children(procs)
                    _raise_child_failure(proc, workdir, job)
                time.sleep(0.05 if timeout_s < 10 else 0.2)
        failed = next((x for x in procs if x[0].returncode != 0), None)
        if failed:
            _raise_child_failure(*failed)
    except BaseException as exc:
        _kill_children(procs)
        if not isinstance(exc, (BacktestCancelled, ValueError)):
            logger.exception("Unexpected error supervising strategy child processes")
        raise


def _kill_children(procs) -> None:
    for proc, _, _ in procs:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    for proc, _, _ in procs:
        proc.wait()


def _raise_child_failure(proc, workdir, job) -> None:
    tail = "\n".join(Path(workdir, "stderr.log").read_text(errors="replace").strip().splitlines()[-8:])
    if proc.returncode in (-signal.SIGKILL, -signal.SIGXCPU):
        raise ValueError("Strategy exceeded its CPU or memory limit")
    if "MemoryError" in tail:
        raise ValueError(f"Strategy exceeded memory limit ({job.get('mem_mb')} MB)")
    raise ValueError(f"Strategy failed:\n{tail or 'exit code ' + str(proc.returncode)}")


def run_strategy_sandboxed(
    strategy_code: str,
    df: pd.DataFrame,
    timeout_s: float = 60,
    mem_mb: int = 1024,
) -> pd.DataFrame:
    """Execute signal-DSL code against ``df``; return a frame with signal columns."""
    fmt = _fmt()
    with tempfile.TemporaryDirectory(prefix="oa-strategy-") as tmp:
        _write_frame(df, os.path.join(tmp, "in." + fmt), fmt)
        job = {"mode": "signals", "fmt": fmt, "mem_mb": mem_mb, "cpu_s": int(timeout_s) + 5}
        _run_child(tmp, job, strategy_code, timeout_s)
        out_path = os.path.join(tmp, "out." + fmt)
        out = pd.read_parquet(out_path) if fmt == "parquet" else pd.read_csv(out_path)

    if len(out) != len(df):
        raise ValueError(
            f"Strategy returned {len(out)} rows but input has {len(df)}; do not drop or add rows"
        )
    sig = pd.to_numeric(out["signal"], errors="coerce").fillna(0)
    if not set(np.unique(sig.to_numpy()).astype(int)).issubset(_VALID_SIGNALS) or (sig % 1 != 0).any():
        raise ValueError("Strategy signal values must be in {-2,-1,0,1,2}")
    out["signal"] = sig.astype(int).to_numpy()
    out.index = df.index
    return out


def run_backtest_sandboxed(
    strategy_code: str,
    bars: dict[str, pd.DataFrame],
    config: dict,
    timeout_s: float = 3600,
    mem_mb: int = 4096,
    cancel_event=None,
    progress_cb: Callable[[float, str, float, int], None] | None = None,
    equity_as: str = "records",
) -> dict:
    """Run the multi-symbol engine on ``bars`` ({'NSE:SBIN': ohlcv frame}) in the sandbox.

    Returns ``{"trades": [...], "equity": [...], "notes": [...], "logs": [...],
    "rejections": {...}}``. Raises ``BacktestCancelled`` if ``cancel_event`` fires.
    """
    fmt = _fmt()
    symbols = list(bars)
    with tempfile.TemporaryDirectory(prefix="oa-backtest-") as tmp:
        for i, sym in enumerate(symbols):
            _write_frame(bars[sym], os.path.join(tmp, f"bars_{i}.{fmt}"), fmt)
        job = {
            "mode": "engine", "fmt": fmt, "symbols": symbols, "config": config,
            "mem_mb": mem_mb, "cpu_s": int(timeout_s) + 5,
        }

        def _on_progress(p: dict) -> None:
            if progress_cb is not None:
                progress_cb(float(p["pct"]), str(p["ts"]), float(p["equity"]), int(p["trades"]))

        _run_child(tmp, job, strategy_code, timeout_s, cancel_event, _on_progress)
        meta = json.loads(Path(tmp, "meta.json").read_text())
        trades = json.loads(Path(tmp, "trades.json").read_text())
        equity_path = Path(tmp, "equity." + fmt)
        equity = pd.read_parquet(equity_path) if fmt == "parquet" else pd.read_csv(equity_path)
    if equity.empty:
        raise ValueError("Backtest produced no equity curve")
    if equity_as == "records":
        equity = equity.to_dict(orient="records")
    elif equity_as != "frame":
        raise ValueError("equity_as must be 'records' or 'frame'")
    return {"trades": trades, "equity": equity, **meta}


_MAX_TASK_WORKERS = 16


def _resolve_workers(workers: int | None) -> int:
    """Child processes for a task batch: argument, else BACKTEST_OPT_WORKERS, else cores - 1 (max 4).

    Every child loads its own copy of the bars, so the default stays modest.
    A bad or non-positive setting falls back to the default instead of failing
    the whole optimisation.
    """
    default = max(1, min(4, (os.cpu_count() or 2) - 1))
    if workers is None:
        raw = os.environ.get("BACKTEST_OPT_WORKERS", "").strip()
        if not raw:
            return default
        try:
            workers = int(raw)
        except ValueError:
            logger.warning(f"Ignoring invalid BACKTEST_OPT_WORKERS={raw!r}; using {default}")
            return default
    if workers < 1:
        return default
    return min(int(workers), _MAX_TASK_WORKERS)


def run_tasks_sandboxed(
    strategy_code: str,
    bars: dict[str, pd.DataFrame],
    config: dict,
    tasks: list[dict],
    timeout_s: float = 3600,
    mem_mb: int = 4096,
    cancel_event=None,
    progress_cb: Callable[[float], None] | None = None,
    workers: int | None = None,
) -> dict:
    """Evaluate many (params, window) tasks against one loaded dataset.

    ``tasks`` items: ``{"id": int, "params": {...}, "start": ts|None, "end": ts|None}``.
    Returns ``{"timeline": [...], "tasks": {id: {"equity": ndarray, "start": int,
    "trades": [...], "notes": [...]} | {"error": str}}}``.
    """
    fmt = _fmt()
    symbols = list(bars)
    workers = _resolve_workers(workers)
    if not tasks:
        union = None
        for frame in bars.values():
            idx = pd.DatetimeIndex(pd.to_datetime(frame["timestamp"]))
            union = idx if union is None else union.union(idx)
        timeline = list(union.sort_values().strftime("%Y-%m-%d %H:%M:%S")) if union is not None else []
        if progress_cb is not None:
            progress_cb(100.0)
        return {"timeline": timeline, "tasks": {}}

    worker_count = min(len(tasks), workers)
    with tempfile.TemporaryDirectory(prefix="oa-tasks-") as tmp:
        for i, sym in enumerate(symbols):
            bar_path = os.path.join(tmp, f"bars_{i}.{fmt}")
            _write_frame(bars[sym], bar_path, fmt)
            os.chmod(bar_path, 0o444)
        chunks = [tasks[i::worker_count] for i in range(worker_count)]
        children = []
        progress = [0.0] * worker_count
        last_pct = 0.0

        def _on_progress(report: dict, index: int) -> None:
            nonlocal last_pct
            progress[index] = max(progress[index], min(100.0, float(report["pct"])))
            pct = sum(p * len(chunks[i]) for i, p in enumerate(progress)) / len(tasks)
            pct = max(last_pct, pct)
            last_pct = pct
            if progress_cb is not None:
                progress_cb(pct)

        for i, chunk in enumerate(chunks):
            workdir = os.path.join(tmp, f"child_{i}")
            os.mkdir(workdir)
            job = {
                "mode": "tasks", "fmt": fmt, "symbols": symbols, "config": config,
                "tasks": chunk, "bars_dir": tmp, "mem_mb": mem_mb,
                "cpu_s": int(timeout_s) + 5,
            }
            children.append((workdir, job, strategy_code))

        _supervise_children(children, timeout_s, cancel_event, _on_progress)
        timeline = json.loads(Path(children[0][0], "timeline.json").read_text())
        out: dict = {}
        for workdir, _, _ in children:
            recs = json.loads(Path(workdir, "tasks.json").read_text())
            for rec in recs:
                if "error" in rec:
                    out[rec["id"]] = {"error": rec["error"]}
                    continue
                eq = np.load(os.path.join(workdir, f"eq_{rec['id']}.npy"), allow_pickle=False)
                out[rec["id"]] = {
                    "equity": eq, "start": rec["start"], "trades": rec["trades"], "notes": rec["notes"],
                }
        if progress_cb is not None:
            progress_cb(100.0)
    return {"timeline": timeline, "tasks": {task["id"]: out[task["id"]] for task in tasks}}
