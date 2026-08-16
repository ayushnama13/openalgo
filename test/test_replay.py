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
from replay.dal import get_range, get_state, invalidate_day
from replay.fill_simulator import simulate_order_fill
from replay.patterns import detect_patterns
from replay.storage import compact_day, get_day_dir, load_day_df, save_events

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


def test_storage_is_append_only():
    """Each flush drops a new part file; nothing existing is rewritten."""
    symbol = "TESTAPPEND"
    date_str = "2026-08-11"

    save_events(symbol, date_str, [{"timestamp": "2026-08-11T09:15:00+05:30", "ltp": 100.0}])
    day_dir = get_day_dir(symbol, date_str)
    first_parts = sorted(os.listdir(day_dir))
    assert len(first_parts) == 1

    save_events(symbol, date_str, [{"timestamp": "2026-08-11T09:15:01+05:30", "ltp": 101.0}])
    second_parts = sorted(os.listdir(day_dir))
    assert len(second_parts) == 2
    # The original part is untouched, not rewritten.
    assert first_parts[0] in second_parts

    df = load_day_df(symbol, date_str)
    assert len(df) == 2
    assert list(df["ltp"]) == [100.0, 101.0]


def test_ticks_sharing_a_timestamp_are_not_dropped():
    """
    Broker timestamps are often second-resolution. De-duplicating on timestamp
    alone silently discarded genuine ticks.
    """
    symbol = "TESTDEDUP"
    date_str = "2026-08-11"
    ts = "2026-08-11T09:15:00+05:30"

    save_events(symbol, date_str, [
        {"timestamp": ts, "ltp": 100.0, "volume": 10},
        {"timestamp": ts, "ltp": 100.5, "volume": 20},
        # An exact repeat of the first row is a real duplicate and does collapse.
        {"timestamp": ts, "ltp": 100.0, "volume": 10},
    ])

    df = load_day_df(symbol, date_str)
    assert len(df) == 2


def test_parts_with_different_depth_levels_merge():
    """A 5-level part and a 20-level part read back as one frame."""
    symbol = "TESTDEPTH"
    date_str = "2026-08-11"

    save_events(symbol, date_str, [
        {"timestamp": "2026-08-11T09:15:00+05:30", "ltp": 50.0, "bid_1": 49.0, "bid_qty_1": 10},
    ])
    save_events(symbol, date_str, [
        {
            "timestamp": "2026-08-11T09:15:01+05:30",
            "ltp": 51.0,
            "bid_1": 50.0, "bid_qty_1": 10,
            "bid_6": 45.0, "bid_qty_6": 60,
        },
    ])

    df = load_day_df(symbol, date_str)
    assert len(df) == 2
    assert "bid_6" in df.columns
    # The 5-level row has no level 6.
    assert df["bid_6"].isna().iloc[0]
    assert df["bid_6"].iloc[1] == 45.0


def test_compact_day_folds_parts_into_one():
    symbol = "TESTCOMPACT"
    date_str = "2026-08-11"

    for i in range(4):
        save_events(symbol, date_str, [
            {"timestamp": f"2026-08-11T09:15:0{i}+05:30", "ltp": 100.0 + i},
        ])

    day_dir = get_day_dir(symbol, date_str)
    assert len(os.listdir(day_dir)) == 4

    rows = compact_day(symbol, date_str)
    assert rows == 4
    assert len(os.listdir(day_dir)) == 1

    invalidate_day(symbol, date_str)
    df = load_day_df(symbol, date_str)
    assert len(df) == 4
    assert list(df["ltp"]) == [100.0, 101.0, 102.0, 103.0]


def test_normalize_tick_emits_only_available_levels():
    from replay.recorder import _normalize_tick_event

    event = _normalize_tick_event({
        "ltp": 100.0,
        "timestamp": 1_776_000_000_000,
        "depth": {
            "buy": [{"price": 99.0, "quantity": 10}, {"price": 98.0, "quantity": 20}],
            "sell": [{"price": 101.0, "quantity": 15}],
        },
    })

    assert event is not None
    assert event["bid_1"] == 99.0
    assert event["bid_2"] == 98.0
    assert event["ask_1"] == 101.0
    # Only two levels were sent, so no columns beyond level 2 are written.
    assert "bid_3" not in event
    assert "ask_20" not in event


def test_market_hours_gate():
    from replay.recorder import is_market_hours

    # Monday 10:00 IST
    assert is_market_hours(IST.localize(datetime(2026, 8, 10, 10, 0)))
    # Monday 03:00 IST - outside the session
    assert not is_market_hours(IST.localize(datetime(2026, 8, 10, 3, 0)))
    # Sunday
    assert not is_market_hours(IST.localize(datetime(2026, 8, 16, 10, 0)))


def test_fill_simulator_flags_missing_depth():
    symbol = "TESTNODEPTH"
    date_str = "2026-08-11"
    save_events(symbol, date_str, [{"timestamp": "2026-08-11T09:15:00+05:30", "ltp": 250.0}])

    t = IST.localize(datetime(2026, 8, 11, 9, 15, 0))
    fill = simulate_order_fill(symbol, t, "BUY", 10, "MARKET")
    assert fill["status"] == "filled"
    assert fill["fill_price"] == 250.0
    # The tape had no order book here, so the caller must be able to tell.
    assert fill["depth_used"] is False


def test_fill_simulator_reports_partial():
    symbol = "TESTPARTIAL"
    date_str = "2026-08-11"
    save_events(symbol, date_str, [{
        "timestamp": "2026-08-11T09:15:00+05:30",
        "ltp": 100.0,
        "ask_1": 101.0, "ask_qty_1": 5,
    }])

    t = IST.localize(datetime(2026, 8, 11, 9, 15, 0))
    fill = simulate_order_fill(symbol, t, "BUY", 50, "MARKET")
    assert fill["status"] == "partial"
    assert fill["filled_qty"] == 5
    assert fill["unfilled_qty"] == 45
    assert fill["depth_used"] is True


def test_get_range_downsamples_past_limit():
    symbol = "TESTRANGE"
    date_str = "2026-08-11"
    events = [
        {"timestamp": f"2026-08-11T09:15:00.{i:03d}+05:30", "ltp": float(i)}
        for i in range(200)
    ]
    save_events(symbol, date_str, events)

    t_start = IST.localize(datetime(2026, 8, 11, 9, 15, 0))
    t_end = IST.localize(datetime(2026, 8, 11, 9, 16, 0))

    full = get_range(symbol, t_start, t_end, limit=None)
    assert len(full) == 200

    capped = get_range(symbol, t_start, t_end, limit=20)
    assert len(capped) <= 21  # limit plus the always-retained final row
    # First and last rows of the window survive downsampling.
    assert capped[0]["ltp"] == 0.0
    assert capped[-1]["ltp"] == 199.0


def test_get_state_returns_none_before_first_tick():
    symbol = "TESTNIFTY"
    t_before = IST.localize(datetime(2026, 8, 10, 8, 0, 0))
    state = get_state(symbol, t_before)
    assert state is None


def test_clock_configure_raises_on_invalid_date():
    clock = ReplayClock()
    with pytest.raises(ValueError):
        clock.configure("invalid-date", "09:15:00", "15:30:00", ["NIFTY"])


@pytest.fixture(scope="module", autouse=True)
def _cleanup_test_tapes():
    """
    Remove tapes written by this module so reruns start clean. Module-scoped:
    several tests deliberately read a tape an earlier test wrote.
    """
    yield
    import shutil

    from replay.dal import clear_dal_cache
    from replay.metadata_db import SessionLocal, SymbolDateIndex

    clear_dal_cache()
    db = SessionLocal()
    try:
        rows = db.query(SymbolDateIndex).filter(SymbolDateIndex.symbol.like("TEST%")).all()
        for row in rows:
            shutil.rmtree(os.path.join("data", "replay", row.symbol), ignore_errors=True)
            db.delete(row)
        db.commit()
    finally:
        db.close()
