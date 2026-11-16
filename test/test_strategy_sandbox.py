import os
import time

import numpy as np
import pandas as pd
import pytest

from services.strategy_sandbox import run_strategy_sandboxed, run_tasks_sandboxed


@pytest.fixture
def df():
    n = 50
    close = 100 + np.cumsum(np.random.default_rng(0).normal(size=n))
    return pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1000}
    )


def test_normal_strategy_with_numpy(df):
    code = "def strategy(df):\n    df['signal'] = np.where(df['close'] > df['close'].mean(), 1, -1)\n    return df\n"
    out = run_strategy_sandboxed(code, df)
    assert len(out) == len(df)
    assert set(out["signal"].unique()) <= {-1, 1}


def test_strategy_using_ta(df):
    code = "def strategy(df):\n    df['signal'] = (ta.sma(df['close'].to_numpy(), 5) > df['close'].to_numpy()).astype(int)\n    return df\n"
    out = run_strategy_sandboxed(code, df)
    assert len(out) == len(df)


def test_buy_sell_signal_mapping(df):
    code = "df['buy_signal'] = df['close'] > 100\ndf['sell_signal'] = df['close'] <= 100\n"
    out = run_strategy_sandboxed(code, df)
    expected = np.where(df["close"] > 100, 1, -1)
    assert (out["signal"].to_numpy() == expected).all()


def test_no_signal_defaults_to_zero(df):
    out = run_strategy_sandboxed("x = 1", df)
    assert (out["signal"] == 0).all()


def test_infinite_loop_times_out(df):
    t0 = time.time()
    with pytest.raises(ValueError, match="timed out"):
        run_strategy_sandboxed("while True:\n    pass\n", df, timeout_s=2)
    assert time.time() - t0 < 10


def test_env_secrets_not_visible(df, monkeypatch):
    monkeypatch.setenv("API_KEY_PEPPER", "x" * 40)
    code = "import os\ndf['signal'] = 1 if os.environ.get('API_KEY_PEPPER') else 0\n"
    with pytest.raises(ValueError, match="not allowed"):
        run_strategy_sandboxed(code, df)


@pytest.mark.parametrize(
    "code",
    [
        "open('/etc/passwd').read()",
        "import subprocess",
        "import os",
        "import socket",
        "__import__('os').system('true')",
        "eval('1')",
    ],
)
def test_dangerous_code_rejected(df, code):
    with pytest.raises(ValueError):
        run_strategy_sandboxed(code, df)


def test_bad_signal_values_rejected(df):
    with pytest.raises(ValueError, match="signal values"):
        run_strategy_sandboxed("df['signal'] = 7", df)


def test_dropping_rows_rejected(df):
    with pytest.raises(ValueError, match="rows"):
        run_strategy_sandboxed("def strategy(df):\n    df = df.iloc[:10].copy()\n    df['signal']=0\n    return df\n", df)


def test_tasks_sandbox_runs_with_one_worker(df):
    frame = df.copy()
    frame.insert(0, "timestamp", pd.date_range("2024-01-01", periods=len(frame), freq="15min")
                 .strftime("%Y-%m-%d %H:%M:%S"))
    config = {
        "initial_capital": 100000.0, "sizing_type": "pct_equity", "sizing_value": 50,
        "product_type": "NRML", "max_positions": 1, "interval": "15m",
    }
    task = {"id": 7, "params": {}, "start": None, "end": None}
    result = run_tasks_sandboxed(
        "df['signal'] = 0", {"NSE:X": frame}, config, [task], workers=1,
    )
    assert list(result["tasks"]) == [7]
    assert "equity" in result["tasks"][7]
