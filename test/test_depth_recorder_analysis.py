"""
Services test for the Depth Recorder analytics engine
(``services/depth_analysis.py``).

Writes synthetic per-second depth ticks into the depth-recorder test DB and
verifies the Layer-1 metrics pipeline end to end: session inventory, health,
metrics series, lead-lag, walls and the server-rendered heatmap PNG.
"""

from datetime import datetime, timedelta

import pytest

import database.depth_recorder_db as drdb
import services.depth_analysis as da

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------
SYMBOL = "TEST"
EXCHANGE = "NSE"


def _tick(
    ts: datetime,
    price: float = 2500.0,
    bid_qty: float = 1000.0,
    ask_qty: float = 1000.0,
    volume: float | None = None,
    ltp: float | None = None,
    orders: int | None = None,
) -> dict:
    """Row dict shaped for batch_save_ticks (column-keyed DepthTick attrs)."""
    row = {
        "symbol": SYMBOL,
        "exchange": EXCHANGE,
        "tick_time": ts,
        "ltp": ltp if ltp is not None else price,
        "volume": volume,
        "bid1_price": price - 0.05,
        "bid1_qty": bid_qty,
        "bid2_price": price - 0.10,
        "bid2_qty": bid_qty * 0.5,
        "ask1_price": price + 0.05,
        "ask1_qty": ask_qty,
        "ask2_price": price + 0.10,
        "ask2_qty": ask_qty * 0.5,
    }
    if orders is not None:
        row["bid1_orders"] = orders
        row["ask1_orders"] = orders
    return row


def _seed_session(n: int = 120, start: datetime | None = None, step_s: int = 1, **kw) -> None:
    """Insert n ticks spaced step_s seconds apart, starting at 10:00 IST."""
    if start is None:
        start = datetime(2026, 1, 5, 10, 0, 0)
    ticks = [
        _tick(start + timedelta(seconds=i * step_s), ltp=2500.0 + (i % 5) * 0.05, **kw)
        for i in range(n)
    ]
    drdb.db_session.bulk_insert_mappings(drdb.DepthTick, ticks)
    drdb.db_session.commit()


@pytest.fixture(autouse=True)
def _depth_db():
    drdb.Base.metadata.drop_all(bind=drdb.engine)
    drdb.Base.metadata.create_all(bind=drdb.engine)
    da._session_cache.clear()
    yield
    da._session_cache.clear()
    drdb.db_session.remove()
    drdb.Base.metadata.drop_all(bind=drdb.engine)


# ---------------------------------------------------------------------------
# Session inventory
# ---------------------------------------------------------------------------
def test_list_sessions_newest_first():
    _seed_session(n=5, start=datetime(2026, 1, 5, 10, 0, 0))
    _seed_session(n=5, start=datetime(2026, 1, 6, 10, 0, 0))

    sessions = da.list_sessions(exchange=EXCHANGE, symbol=SYMBOL)
    assert len(sessions) == 2
    assert sessions[0]["day"] > sessions[1]["day"]
    assert sessions[0]["n_ticks"] == 5
    assert sessions[1]["day"] < sessions[0]["day"]


def test_list_sessions_empty():
    assert da.list_sessions(exchange=EXCHANGE, symbol=SYMBOL) == []


# ---------------------------------------------------------------------------
# Metrics series
# ---------------------------------------------------------------------------
def test_metrics_series_shape():
    _seed_session(n=60)
    out = da.metrics_series(SYMBOL, EXCHANGE, "2026-01-05")
    assert out["status"] == "ok"
    assert out["rows"] == 60
    assert len(out["series"]["time"]) == 60
    assert len(out["series"]["ltp"]) == 60
    assert len(out["series"]["ofi_1"]) == 60
    assert len(out["series"]["obi_ln"]) == 60
    assert len(out["ladder"]) == 60
    # 5 bids + 5 asks per ladder row
    assert len(out["ladder"][0]["bids"]) == 5
    assert len(out["ladder"][0]["asks"]) == 5
    assert out["has_orders"] is False


def test_metrics_series_downsampling():
    _seed_session(n=400)
    out = da.metrics_series(SYMBOL, EXCHANGE, "2026-01-05", max_points=100)
    assert out["status"] == "ok"
    assert out["rows"] <= 100


def test_metrics_series_no_data():
    _seed_session(n=5)
    out = da.metrics_series(SYMBOL, EXCHANGE, "2026-01-05")
    out_none = da.metrics_series("MISSING", EXCHANGE, "2026-01-05")
    assert out["status"] == "ok"
    assert out_none["status"] == "no_data"


# ---------------------------------------------------------------------------
# Health report
# ---------------------------------------------------------------------------
def test_report_health_counts_and_gaps():
    start = datetime(2026, 1, 5, 10, 0, 0)
    ticks = [_tick(start + timedelta(seconds=i)) for i in range(10)]
    ticks.append(_tick(start + timedelta(seconds=20)))  # 10s gap
    drdb.db_session.bulk_insert_mappings(drdb.DepthTick, ticks)
    drdb.db_session.commit()

    out = da.report_health(SYMBOL, EXCHANGE, "2026-01-05")
    assert out["status"] == "ok"
    assert out["total_packets"] == 11
    assert any(p["packets"] > 0 for p in out["packets_per_min"])
    # one >3s gap (11 seconds) between tick 9 and tick 10
    assert len(out["gaps"]) == 1
    assert out["gaps"][0]["duration_s"] == pytest.approx(11.0)


def test_report_health_no_data():
    out = da.report_health(SYMBOL, EXCHANGE, "2026-01-05")
    assert out["status"] == "no_data"


# ---------------------------------------------------------------------------
# Lead-lag
# ---------------------------------------------------------------------------
def test_leadlag_report_shape():
    _seed_session(n=300, start=datetime(2026, 1, 5, 9, 30, 0))
    out = da.leadlag_report(SYMBOL, EXCHANGE, "2026-01-05", horizons=[1, 10])
    assert out["status"] == "ok"
    assert out["horizons"] == [1, 10]
    # 2 horizons x 3 time buckets
    assert len(out["results"]) == 6
    assert all(h in ("open", "midday", "close") for r in out["results"] for h in [r["bucket"]])
    assert out["scatter"]


def test_leadlag_insufficient_rows():
    _seed_session(n=5)
    out = da.leadlag_report(SYMBOL, EXCHANGE, "2026-01-05")
    assert out["status"] == "no_data"


# ---------------------------------------------------------------------------
# Walls
# ---------------------------------------------------------------------------
def test_walls_top_level():
    start = datetime(2026, 1, 5, 10, 0, 0)
    ticks = []
    for i in range(30):
        ticks.append(_tick(start + timedelta(seconds=i), price=2500.0, bid_qty=10_000.0))
    for i in range(30, 60):
        ticks.append(_tick(start + timedelta(seconds=i), price=2505.0, bid_qty=100.0))
    drdb.db_session.bulk_insert_mappings(drdb.DepthTick, ticks)
    drdb.db_session.commit()

    out = da.walls_report(SYMBOL, EXCHANGE, "2026-01-05", top=5)
    assert out["status"] == "ok"
    assert out["walls"]
    # the price-level with the largest resting size wins
    assert out["walls"][0]["price"] == 2499.95


# ---------------------------------------------------------------------------
# Heatmap PNG
# ---------------------------------------------------------------------------
def test_render_heatmap_png():
    _seed_session(n=60)
    png_bytes, meta = da.render_heatmap_png(EXCHANGE, SYMBOL, "2026-01-05", step=0.05)
    assert png_bytes
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    assert meta["status"] == "ok"
    assert meta["price_min"] <= meta["price_max"]
    assert len(meta["times"]) >= 1
    assert meta["price_bins"] > 0


def test_render_heatmap_no_data():
    png_bytes, meta = da.render_heatmap_png(EXCHANGE, SYMBOL, "2026-01-05")
    assert png_bytes == b""
    assert meta == {"status": "no_data"}
