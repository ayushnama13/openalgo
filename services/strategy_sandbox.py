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
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

_RUNNER = str(Path(__file__).with_name("_strategy_runner.py"))
_VALID_SIGNALS = {-2, -1, 0, 1, 2}


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
    """Spawn the runner in ``tmp`` and wait for it; raise ValueError on failure."""
    Path(tmp, "job.json").write_text(json.dumps(job), encoding="utf-8")
    Path(tmp, "code.py").write_text(code, encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        "OMP_NUM_THREADS": "1",
    }
    cmd = [*_unshare_prefix(), sys.executable, "-I", _RUNNER, tmp]
    err_path = os.path.join(tmp, "stderr.log")
    with open(err_path, "wb") as err_fh, open(os.devnull, "wb") as out_fh:
        proc = subprocess.Popen(  # noqa: S603
            cmd, cwd=tmp, env=env, stdin=subprocess.DEVNULL,
            stdout=out_fh, stderr=err_fh, start_new_session=True,
        )
        deadline = time.monotonic() + timeout_s
        last_progress = None
        prog_path = os.path.join(tmp, "progress.json")
        try:
            while proc.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    _kill_group(proc)
                    raise BacktestCancelled()
                if time.monotonic() > deadline:
                    _kill_group(proc)
                    raise ValueError(f"Strategy timed out after {timeout_s:g}s")
                if on_progress is not None and os.path.exists(prog_path):
                    try:
                        raw = Path(prog_path).read_text().strip()
                        if raw and raw != last_progress:
                            last_progress = raw
                            on_progress(json.loads(raw))
                    except (ValueError, OSError):
                        pass
                time.sleep(0.05 if timeout_s < 10 else 0.2)
        except BaseException:
            if proc.poll() is None:
                _kill_group(proc)
            raise
    if proc.returncode != 0:
        tail = "\n".join(Path(err_path).read_text(errors="replace").strip().splitlines()[-8:])
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
        equity = json.loads(Path(tmp, "equity.json").read_text())
    if not equity:
        raise ValueError("Backtest produced no equity curve")
    return {"trades": trades, "equity": equity, **meta}


def run_tasks_sandboxed(
    strategy_code: str,
    bars: dict[str, pd.DataFrame],
    config: dict,
    tasks: list[dict],
    timeout_s: float = 3600,
    mem_mb: int = 4096,
    cancel_event=None,
    progress_cb: Callable[[float], None] | None = None,
) -> dict:
    """Evaluate many (params, window) tasks against one loaded dataset.

    ``tasks`` items: ``{"id": int, "params": {...}, "start": ts|None, "end": ts|None}``.
    Returns ``{"timeline": [...], "tasks": {id: {"equity": ndarray, "start": int,
    "trades": [...], "notes": [...]} | {"error": str}}}``.
    """
    fmt = _fmt()
    symbols = list(bars)
    with tempfile.TemporaryDirectory(prefix="oa-tasks-") as tmp:
        for i, sym in enumerate(symbols):
            _write_frame(bars[sym], os.path.join(tmp, f"bars_{i}.{fmt}"), fmt)
        job = {
            "mode": "tasks", "fmt": fmt, "symbols": symbols, "config": config, "tasks": tasks,
            "mem_mb": mem_mb, "cpu_s": int(timeout_s) + 5,
        }

        def _on_progress(p: dict) -> None:
            if progress_cb is not None:
                progress_cb(float(p["pct"]))

        _run_child(tmp, job, strategy_code, timeout_s, cancel_event, _on_progress)
        timeline = json.loads(Path(tmp, "timeline.json").read_text())
        recs = json.loads(Path(tmp, "tasks.json").read_text())
        out: dict = {}
        for rec in recs:
            if "error" in rec:
                out[rec["id"]] = {"error": rec["error"]}
                continue
            eq = np.load(os.path.join(tmp, f"eq_{rec['id']}.npy"), allow_pickle=False)
            out[rec["id"]] = {
                "equity": eq, "start": rec["start"], "trades": rec["trades"], "notes": rec["notes"],
            }
    return {"timeline": timeline, "tasks": out}
