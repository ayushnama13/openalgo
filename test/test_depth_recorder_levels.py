#!/usr/bin/env python3
"""
Tests for Depth Recorder variable depth levels (5..200), runtime level changes,
levels_json serialization, migration on old schema, and normalization.
"""

import asyncio
import json
import os
import sqlite3
import tempfile
import threading
import time

import pytest

# Set env before importing db/services
os.environ["IGNORE_MARKET_HOURS"] = "1"
os.environ["DEPTH_RECORDER_IGNORE_MARKET_HOURS"] = "1"

import database.depth_recorder_db as drdb
import services.depth_recorder_service as drs


@pytest.fixture(autouse=True)
def setup_db():
    drdb.Base.metadata.drop_all(bind=drdb.engine)
    drdb.Base.metadata.create_all(bind=drdb.engine)
    drdb.init_db()
    yield
    drdb.db_session.remove()
    drdb.Base.metadata.drop_all(bind=drdb.engine)


def test_normalise_depth_payload_small():
    # <= 5 levels should not produce levels_json
    buy_levels = [{"price": 2500 - i * 0.05, "quantity": 100, "orders": 2} for i in range(5)]
    sell_levels = [{"price": 2500 + i * 0.05, "quantity": 100, "orders": 2} for i in range(5)]
    pkt = {
        "type": "market_data",
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "mode": 3,
        "data": {
            "ltp": 2500.0,
            "depth": {"buy": buy_levels, "sell": sell_levels}
        }
    }
    row = drs._normalise_depth_payload(pkt, "RELIANCE", "NSE")
    assert row is not None
    assert row["depth_level"] == 5
    assert row["levels_json"] is None
    assert row["bid1_price"] == 2500.0


def test_normalise_depth_payload_large():
    # > 5 levels (e.g. 20 levels) should produce levels_json
    buy_levels = [{"price": 2500 - i * 0.05, "quantity": 100, "orders": 2} for i in range(20)]
    sell_levels = [{"price": 2500 + i * 0.05, "quantity": 100, "orders": 2} for i in range(20)]
    pkt = {
        "type": "market_data",
        "symbol": "TCS",
        "exchange": "NSE",
        "mode": 3,
        "data": {
            "ltp": 3500.0,
            "depth": {"buy": buy_levels, "sell": sell_levels}
        }
    }
    row = drs._normalise_depth_payload(pkt, "TCS", "NSE")
    assert row is not None
    assert row["depth_level"] == 20
    assert row["levels_json"] is not None

    parsed = json.loads(row["levels_json"])
    assert len(parsed["b"]) == 20
    assert len(parsed["s"]) == 20


def test_db_migration_old_schema():
    # Test upgrading an existing old-schema SQLite database in-place
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        # Create table with old schema (without depth_level / levels_json)
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE depth_ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                tick_time DATETIME NOT NULL,
                ltt REAL,
                ltp REAL,
                volume REAL,
                oi REAL,
                total_buy_qty REAL,
                total_sell_qty REAL,
                bid1_price REAL, bid1_qty REAL, bid1_orders INTEGER,
                bid2_price REAL, bid2_qty REAL, bid2_orders INTEGER,
                bid3_price REAL, bid3_qty REAL, bid3_orders INTEGER,
                bid4_price REAL, bid4_qty REAL, bid4_orders INTEGER,
                bid5_price REAL, bid5_qty REAL, bid5_orders INTEGER,
                ask1_price REAL, ask1_qty REAL, ask1_orders INTEGER,
                ask2_price REAL, ask2_qty REAL, ask2_orders INTEGER,
                ask3_price REAL, ask3_qty REAL, ask3_orders INTEGER,
                ask4_price REAL, ask4_qty REAL, ask4_orders INTEGER,
                ask5_price REAL, ask5_qty REAL, ask5_orders INTEGER,
                raw_json TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE depth_recorder_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        """)
        conn.commit()
        conn.close()

        # Now point engine to this temp file and run init_db()
        old_url = drdb.DEPTH_RECORDER_DATABASE_URL
        drdb.DEPTH_RECORDER_DATABASE_URL = f"sqlite:///{path}"
        # Recreate engine / session for temp db
        from sqlalchemy.orm import scoped_session, sessionmaker

        from database.engine_factory import create_db_engine
        drdb.engine = create_db_engine(drdb.DEPTH_RECORDER_DATABASE_URL)
        drdb.db_session = scoped_session(sessionmaker(bind=drdb.engine))
        drdb.Base.query = drdb.db_session.query_property()

        drdb.init_db()

        # Verify columns exist via table_info
        with drdb.engine.connect() as conn:
            from sqlalchemy import text
            ticks_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(depth_ticks)"))}
            config_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(depth_recorder_config)"))}

        assert "depth_level" in ticks_cols
        assert "levels_json" in ticks_cols
        assert "depth_level" in config_cols

        drdb.DEPTH_RECORDER_DATABASE_URL = old_url
    finally:
        if os.path.exists(path):
            os.remove(path)


# Helper to run mock depth proxy server for integration test
import websockets


async def _mock_proxy_handler(websocket):
    subscriptions = {}
    async def stream_task():
        try:
            while True:
                await asyncio.sleep(0.05)
                for (sym, ex), depth in list(subscriptions.items()):
                    buy_levels = [{"price": 2500 - i * 0.05, "quantity": 100, "orders": 5} for i in range(depth)]
                    sell_levels = [{"price": 2500 + i * 0.05, "quantity": 100, "orders": 5} for i in range(depth)]
                    pkt = {
                        "type": "market_data",
                        "symbol": sym,
                        "exchange": ex,
                        "mode": 3,
                        "data": {
                            "ltp": 2500.0,
                            "volume": 1000.0,
                            "oi": 5000.0,
                            "total_buy_qty": 10000.0,
                            "total_sell_qty": 10000.0,
                            "depth": {"buy": buy_levels, "sell": sell_levels}
                        }
                    }
                    await websocket.send(json.dumps(pkt))
        except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError):
            pass

    task = asyncio.create_task(stream_task())
    try:
        async for message in websocket:
            data = json.loads(message)
            action = data.get("action")
            if action == "authenticate":
                await websocket.send(json.dumps({"status": "success"}))
            elif action == "subscribe":
                depth_req = int(data.get("depth", 5))
                subs = []
                for s in data.get("symbols", []):
                    sym = s.get("symbol").upper()
                    ex = s.get("exchange", "NSE").upper()
                    subscriptions[(sym, ex)] = depth_req
                    subs.append({"symbol": sym, "exchange": ex, "status": "success", "depth": depth_req})
                await websocket.send(json.dumps({
                    "type": "subscribe",
                    "status": "success",
                    "request_id": data.get("request_id"),
                    "subscriptions": subs
                }))
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        task.cancel()


@pytest.fixture
def mock_proxy_server():
    loop = asyncio.new_event_loop()
    server_ready = threading.Event()
    port_holder = {}

    def run_server():
        async def main_server():
            async with websockets.serve(_mock_proxy_handler, "127.0.0.1", 0) as server:
                port_holder["port"] = server.sockets[0].getsockname()[1]
                server_ready.set()
                await asyncio.Future()
        loop.run_until_complete(main_server())

    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    server_ready.wait(timeout=5)
    port = port_holder["port"]
    os.environ["WEBSOCKET_URL"] = f"ws://127.0.0.1:{port}"
    yield port


def test_integration_recorder_depth_levels(mock_proxy_server):
    # 1. Start at depth 5
    res = drs.start_recorder("RELIANCE", "NSE", "dummy_key", depth_level=5)
    assert res["status"] == "started"
    assert res["depth_level"] == 5

    # Wait for ticks to be recorded
    time.sleep(1.0)
    ticks = drdb.get_recent_ticks("RELIANCE", "NSE", limit=10)
    assert len(ticks) > 0
    assert ticks[0]["depth_level"] == 5
    assert len(ticks[0]["bids"]) == 5

    # 2. Change depth level to 20 at runtime
    drs.set_depth_level("RELIANCE", "NSE", 20)
    time.sleep(1.0)
    ticks = drdb.get_recent_ticks("RELIANCE", "NSE", limit=10)
    assert ticks[0]["depth_level"] == 20
    assert len(ticks[0]["bids"]) == 20
    assert ticks[0]["levels_json"] is not None

    # 3. Change depth level to 200 at runtime
    drs.set_depth_level("RELIANCE", "NSE", 200)
    time.sleep(1.0)
    ticks = drdb.get_recent_ticks("RELIANCE", "NSE", limit=10)
    assert ticks[0]["depth_level"] == 200
    assert len(ticks[0]["bids"]) == 200

    # Clean stop
    drs.stop_recorder("RELIANCE", "NSE")
    time.sleep(0.5)
