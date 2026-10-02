#!/usr/bin/env python3
"""
Mock WebSocket Proxy for testing Depth Recorder with variable depth levels (5..200).
Run standalone: python test/mock_depth_proxy.py [--port 8766]
"""

import asyncio
import json
import random
import sys

import websockets


async def handler(websocket):
    authenticated = False
    subscriptions = {}  # key: (symbol, exchange), value: depth (int)

    async def stream_task():
        prices = {}
        try:
            while True:
                await asyncio.sleep(0.1)  # ~10 msgs/sec
                if not subscriptions:
                    continue
                for (sym, ex), depth in list(subscriptions.items()):
                    if sym not in prices:
                        prices[sym] = 2500.0
                    prices[sym] += random.choice([-0.05, 0.05, 0.0, -0.1, 0.1])
                    mid = prices[sym]

                    buy_levels = []
                    sell_levels = []
                    for i in range(depth):
                        bid_price = round(mid - (i + 1) * 0.05, 2)
                        ask_price = round(mid + (i + 1) * 0.05, 2)
                        buy_levels.append({"price": bid_price, "quantity": 1000.0 + i * 10, "orders": 5 + i})
                        sell_levels.append({"price": ask_price, "quantity": 1000.0 + i * 10, "orders": 5 + i})

                    pkt = {
                        "type": "market_data",
                        "symbol": sym,
                        "exchange": ex,
                        "mode": 3,
                        "data": {
                            "ltp": mid,
                            "volume": 50000.0,
                            "oi": 100000.0,
                            "total_buy_qty": 50000.0,
                            "total_sell_qty": 50000.0,
                            "ltt": asyncio.get_running_loop().time(),
                            "depth": {
                                "buy": buy_levels,
                                "sell": sell_levels
                            }
                        }
                    }
                    await websocket.send(json.dumps(pkt))
        except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError):
            pass

    task = asyncio.create_task(stream_task())
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                continue

            action = data.get("action")
            if action == "authenticate":
                api_key = data.get("api_key")
                if api_key:
                    authenticated = True
                    await websocket.send(json.dumps({"status": "success", "message": "Authenticated"}))
                else:
                    await websocket.send(json.dumps({"status": "error", "message": "Invalid API key"}))
            elif action == "subscribe":
                if not authenticated:
                    await websocket.send(json.dumps({"type": "error", "code": 401, "message": "Unauthorized"}))
                    continue
                req_id = data.get("request_id")
                depth_req = int(data.get("depth", 5))
                sub_results = []
                for sym_info in data.get("symbols", []):
                    sym = sym_info.get("symbol", "").upper()
                    ex = sym_info.get("exchange", "NSE").upper()
                    subscriptions[(sym, ex)] = depth_req
                    sub_results.append({
                        "symbol": sym,
                        "exchange": ex,
                        "status": "success",
                        "depth": depth_req
                    })
                await websocket.send(json.dumps({
                    "type": "subscribe",
                    "status": "success",
                    "request_id": req_id,
                    "subscriptions": sub_results
                }))
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        task.cancel()

async def main():
    port = 8766
    if "--port" in sys.argv:
        try:
            idx = sys.argv.index("--port")
            port = int(sys.argv[idx + 1])
        except (ValueError, IndexError):
            pass

    async with websockets.serve(handler, "127.0.0.1", port):
        print(f"Mock Depth Proxy running on ws://127.0.0.1:{port}")
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Mock Depth Proxy stopped.")
