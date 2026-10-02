"""Run user strategy code in a locked-down child process.

Defence in depth, NOT a security boundary. Layers: minimal environment (no
secrets from .env), temp cwd, rlimits (memory/CPU/file size), wall-clock kill,
a network-less namespace via ``unshare`` when available, and in-process
restrictions in the child (import whitelist, no open/eval/exec, blocked
sockets and process spawning). Determined Python-level escapes (e.g. via
object introspection) are possible; run OpenAlgo itself as an unprivileged
user and treat strategy code from untrusted sources accordingly.

Data crosses the process boundary as parquet/CSV, never pickle, so a
compromised child cannot execute code in the parent on deserialisation.
"""

from __future__ import annotations

import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

_RUNNER = str(Path(__file__).with_name("_strategy_runner.py"))
_VALID_SIGNALS = {-2, -1, 0, 1, 2}


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
        r = subprocess.run([*cmd, "true"], capture_output=True, timeout=5)
        return cmd if r.returncode == 0 else ()
    except Exception:
        return ()


def _limiter(timeout_s: float, mem_mb: int):
    def apply():
        os.setsid()
        resource.setrlimit(resource.RLIMIT_AS, (mem_mb * 1024 * 1024,) * 2)
        cpu = int(timeout_s) + 5
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return apply


def run_strategy_sandboxed(
    strategy_code: str,
    df: pd.DataFrame,
    timeout_s: float = 60,
    mem_mb: int = 1024,
) -> pd.DataFrame:
    """Execute strategy code against ``df``; return a frame with signal columns.

    The returned frame has the same row count as ``df`` and a numeric
    ``signal`` column with values in {-2,-1,0,1,2}.
    """
    fmt = _fmt()
    with tempfile.TemporaryDirectory(prefix="oa-strategy-") as tmp:
        in_path = os.path.join(tmp, "in." + fmt)
        out_path = os.path.join(tmp, "out." + fmt)
        if fmt == "parquet":
            df.reset_index(drop=True).to_parquet(in_path)
        else:
            df.to_csv(in_path, index=False)
        Path(in_path + ".code").write_text(strategy_code, encoding="utf-8")

        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "PYTHONHASHSEED": "0",
            "OMP_NUM_THREADS": "1",
        }
        cmd = [*_unshare_prefix(), sys.executable, "-I", _RUNNER, in_path, out_path, fmt]
        proc = subprocess.Popen(
            cmd,
            cwd=tmp,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=_limiter(timeout_s, mem_mb),  # noqa: PLW1509
            text=True,
        )
        try:
            _, err = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            raise ValueError(f"Strategy timed out after {timeout_s:g}s") from None

        if proc.returncode != 0:
            tail = "\n".join((err or "").strip().splitlines()[-8:])
            if "MemoryError" in tail:
                raise ValueError(f"Strategy exceeded memory limit ({mem_mb} MB)")
            raise ValueError(f"Strategy failed:\n{tail or 'exit code ' + str(proc.returncode)}")

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
