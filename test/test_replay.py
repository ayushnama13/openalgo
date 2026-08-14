"""
test/test_replay.py
───────────────────
Unit tests for OpenAlgo Replay Mode modules.
"""

import os
from datetime import datetime

import pytest
import pytz

from replay.clock import ReplayClock
from replay.dal import get_range, get_state
from replay.fill_simulator import simulate_order_fill
from replay.patterns import detect_patterns
from replay.storage import load_day_df, save_events

IST = pytz.timezone("Asia/Kolkata")


def test_replay_storage_and_dal():
    symbol = "TESTNIFTY"
    date_str = "2026-08-10"

    events = [
        {
            "timestamp": "2026-08-10T09:15:00+05:30",
            "ltp": 20000.0,
            "bid_1": 19999.0, "bid_qty_1": 100.0,
            "bid_2": 19998.0, "bid_qty_2": 200.0,
            "ask_1": 20001.0, "ask_qty_1": 150.0,
            "ask_2": 20002.0, "ask_qty_2": 250.0,
            "volume": 1000, "oi": 50000,
        },
        {
            "timestamp": "2026-08-10T09:15:01+05:30",
            "ltp": 20005.0,
            "bid_1": 20004.0, "bid_qty_1": 120.0,
            "bid_2": 20003.0, "bid_qty_2": 220.0,
            "ask_1": 20006.0, "ask_qty_1": 130.0,
            "ask_2": 20007.0, "ask_qty_2": 230.0,
            "volume": 1200, "oi": 50100,
        },
    ]

    save_events(symbol, date_str, events)

    df = load_day_df(symbol, date_str)
    assert len(df) == 2

    # Test DAL get_state
    t_query = IST.localize(datetime(2026, 8, 10, 9, 15, 0))
    state = get_state(symbol, t_query)
    assert state is not None
    assert state["ltp"] == 20000.0

    # Test DAL get_range
    t_start = IST.localize(datetime(2026, 8, 10, 9, 15, 0))
    t_end = IST.localize(datetime(2026, 8, 10, 9, 15, 2))
    rng = get_range(symbol, t_start, t_end)
    assert len(rng) == 2


def test_replay_clock():
    clock = ReplayClock()
    clock.configure("2026-08-10", "09:15:00", "15:30:00", ["NIFTY"])
    state = clock.get_state()
    assert state["is_playing"] is False
    assert state["session_date"] == "2026-08-10"

    clock.play()
    assert clock.is_playing is True
    clock.pause()
    assert clock.is_playing is False
    clock.step(5)
    assert clock.current_time.second == 5


def test_fill_simulator():
    symbol = "TESTNIFTY"
    t = IST.localize(datetime(2026, 8, 10, 9, 15, 0))

    # Test BUY fill against asks
    fill_buy = simulate_order_fill(symbol, t, "BUY", 50, "MARKET")
    assert fill_buy["status"] == "filled"
    assert fill_buy["fill_price"] == 20001.0

    # Test SELL fill against bids
    fill_sell = simulate_order_fill(symbol, t, "SELL", 50, "MARKET")
    assert fill_sell["status"] == "filled"
    assert fill_sell["fill_price"] == 19999.0


def test_pattern_detection():
    symbol = "TESTNIFTY"
    t_start = IST.localize(datetime(2026, 8, 10, 9, 15, 0))
    t_end = IST.localize(datetime(2026, 8, 10, 9, 15, 2))
    patterns = detect_patterns(symbol, t_start, t_end)
    assert isinstance(patterns, dict)
    assert "fvgs" in patterns
    assert "msses" in patterns
    assert "sweeps" in patterns


def test_nifty_option_expansion():
    from replay.dal import expand_nifty_options
    expanded = expand_nifty_options("2026-08-10", ["NIFTY", "RELIANCE"])
    # Nifty options within ±500 (inclusive, step 50) means 21 strikes * 2 (CE + PE) = 42 option contracts, plus NIFTY & RELIANCE
    assert len(expanded) > 40
    # Make sure we got both CE and PE symbols
    assert any(x.endswith("CE") for x in expanded)
    assert any(x.endswith("PE") for x in expanded)


def test_live_nifty_option_expansion():
    from replay.recorder import expand_live_nifty_options
    expanded = expand_live_nifty_options([{"symbol": "NIFTY", "exchange": "NSE_INDEX"}], "2026-08-10")
    assert len(expanded) > 40
    assert any(item["symbol"].endswith("CE") for item in expanded)
    assert any(item["symbol"].endswith("PE") for item in expanded)
