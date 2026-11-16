"""Daily depth summary, retention, cache, and scalar-row tests."""

from datetime import datetime, timedelta

import database.depth_recorder_db as drdb
import services.depth_analysis as da

SYMBOL_A = "SESSIONTESTA"
SYMBOL_B = "SESSIONTESTB"
EXCHANGE = "NSE"


def _tick(symbol, when):
    return {
        "symbol": symbol,
        "exchange": EXCHANGE,
        "tick_time": when,
        "ltp": 100.0,
        "bid1_price": 99.95,
        "bid1_qty": 10.0,
        "ask1_price": 100.05,
        "ask1_qty": 12.0,
        "total_buy_qty": 20.0,
        "total_sell_qty": 25.0,
    }


def setup_function():
    drdb.Base.metadata.drop_all(bind=drdb.engine)
    drdb.Base.metadata.create_all(bind=drdb.engine)
    da.clear_session_cache()


def teardown_function():
    da.clear_session_cache()
    drdb.db_session.remove()
    drdb.Base.metadata.drop_all(bind=drdb.engine)


def test_batch_summaries_and_summary_queries_match_raw_ticks():
    values = [
        _tick(SYMBOL_A, datetime(2026, 1, 5, 10, 0, i)) for i in range(2)
    ] + [
        _tick(SYMBOL_A, datetime(2026, 1, 6, 10, 0, 0)),
        _tick(SYMBOL_B, datetime(2026, 1, 5, 11, 0, 0)),
    ]
    assert drdb.batch_save_ticks(values) == len(values)
    summaries = drdb.db_session.query(drdb.DepthSession).all()
    assert {(r.symbol, r.day, r.n_ticks) for r in summaries} == {
        (SYMBOL_A, "2026-01-05", 2), (SYMBOL_A, "2026-01-06", 1), (SYMBOL_B, "2026-01-05", 1)
    }
    assert da.list_sessions(EXCHANGE, SYMBOL_A) == [
        {"symbol": SYMBOL_A, "exchange": EXCHANGE, "day": "2026-01-06", "n_ticks": 1,
         "first": "2026-01-06T10:00:00", "last": "2026-01-06T10:00:00"},
        {"symbol": SYMBOL_A, "exchange": EXCHANGE, "day": "2026-01-05", "n_ticks": 2,
         "first": "2026-01-05T10:00:00", "last": "2026-01-05T10:00:01"},
    ]
    raw = drdb.db_session.query(drdb.DepthTick).all()
    assert drdb.get_tick_count(SYMBOL_A, EXCHANGE) == sum(r.symbol == SYMBOL_A for r in raw)
    stats = drdb.get_stats()
    assert stats["total_ticks"] == len(raw)
    assert {(row["symbol"], row["exchange"]) for row in stats["symbols"]} == {
        (SYMBOL_A, EXCHANGE), (SYMBOL_B, EXCHANGE)
    }
    assert stats["latest_tick_time"] == max(r.tick_time for r in raw).isoformat()


def test_init_db_backfills_empty_summary_table():
    drdb.db_session.bulk_insert_mappings(drdb.DepthTick, [
        _tick(SYMBOL_A, datetime(2026, 2, 3, 10, 0)),
        _tick(SYMBOL_A, datetime(2026, 2, 3, 10, 1)),
        _tick(SYMBOL_B, datetime(2026, 2, 4, 10, 0)),
    ])
    drdb.db_session.commit()
    drdb.init_db()
    assert drdb.db_session.query(drdb.DepthSession).count() == 2
    assert drdb.get_tick_count(SYMBOL_A, EXCHANGE) == 2


def test_prune_old_days_and_summaries():
    old = datetime.now(drdb.IST).replace(tzinfo=None) - timedelta(days=8)
    recent = datetime.now(drdb.IST).replace(tzinfo=None) - timedelta(days=1)
    drdb.batch_save_ticks([_tick(SYMBOL_A, old), _tick(SYMBOL_A, recent)])
    assert drdb.prune_old_ticks(3) == 1
    assert drdb.db_session.query(drdb.DepthTick).count() == 1
    rows = drdb.db_session.query(drdb.DepthSession).all()
    assert len(rows) == 1 and rows[0].day == recent.date().isoformat()


def test_past_day_cache_is_immutable_and_today_expires(monkeypatch):
    import services.depth_analysis as module

    today = datetime.now(drdb.IST).date().isoformat()
    past = (datetime.now(drdb.IST).date() - timedelta(days=1)).isoformat()
    value = {"built_at": datetime.now() - timedelta(seconds=module.CACHE_TTL_SECONDS + 5)}
    module._cache_set(SYMBOL_A, EXCHANGE, past, value)
    module._cache_set(SYMBOL_B, EXCHANGE, today, value)
    assert module._cache_get(SYMBOL_A, EXCHANGE, past) is value
    assert module._cache_get(SYMBOL_B, EXCHANGE, today) is None


def test_flat_tuple_rows_match_flat_dict_rows():
    start = datetime(2026, 3, 1)
    drdb.batch_save_ticks([_tick(SYMBOL_A, start), _tick(SYMBOL_A, start + timedelta(seconds=1))])
    cols, rows = drdb.get_session_rows_flat(SYMBOL_A, EXCHANGE, start, start + timedelta(days=1))
    dicts = drdb.get_session_ticks_flat(SYMBOL_A, EXCHANGE, start, start + timedelta(days=1))
    assert [dict(zip(cols, row, strict=True)) for row in rows] == dicts
