"""Child-process runner for user strategy code. Invoked by strategy_sandbox only.

argv: <input_file> <output_file> <format: parquet|csv>
"""

import builtins
import sys

_ALLOWED_IMPORTS = {
    "math", "statistics", "numpy", "pandas", "ta", "openalgo", "datetime",
    "itertools", "functools", "collections", "typing", "numbers", "decimal",
    "fractions", "operator", "random", "warnings", "time",
}
_SIGNAL_COLS = ("signal", "buy_signal", "sell_signal")


def _read(path, fmt, pd):
    return pd.read_parquet(path) if fmt == "parquet" else pd.read_csv(path)


def _write(df, path, fmt):
    df.to_parquet(path) if fmt == "parquet" else df.to_csv(path, index=False)


def _lock_down():
    import os
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
        if hasattr(os, name):
            setattr(os, name, _denied)
    sub = sys.modules.get("subprocess")
    if sub is not None:
        for name in ("Popen", "run", "call", "check_call", "check_output", "getoutput"):
            if hasattr(sub, name):
                setattr(sub, name, _denied)


def main():
    in_path, out_path, fmt = sys.argv[1:4]
    with open(in_path + ".code", encoding="utf-8") as fh:
        code = fh.read()

    import numpy as np
    import pandas as pd

    try:
        from openalgo import ta
    except ImportError:
        try:
            import ta
        except ImportError:
            ta = None

    df = _read(in_path, fmt, pd)
    _lock_down()

    real_import = builtins.__import__

    def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and name.split(".")[0] not in _ALLOWED_IMPORTS:
            raise ImportError(f"import of '{name}' is not allowed in strategy code")
        return real_import(name, globals, locals, fromlist, level)

    safe_builtins = {
        k: v for k, v in vars(builtins).items()
        if k not in ("open", "eval", "exec", "compile", "input", "breakpoint",
                     "help", "exit", "quit", "__loader__", "__spec__")
    }
    safe_builtins["__import__"] = _guarded_import
    env = {"__builtins__": safe_builtins, "pd": pd, "np": np, "ta": ta, "df": df}

    exec(compile(code, "<strategy>", "exec"), env)  # noqa: S102 - sandboxed child
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
    _write(out, out_path, fmt)


if __name__ == "__main__":
    main()
