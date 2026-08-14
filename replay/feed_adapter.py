"""
openalgo/replay/feed_adapter.py
───────────────────────────────
Replay Feed Adapter mimicking live OpenAlgo WebSocket/ZMQ feed interface.
Allows strategies to run unmodified against historical tape.
"""

from datetime import datetime

from replay.clock import clock_instance
from replay.dal import get_state
from utils.logging import get_logger

logger = get_logger(__name__)


class ReplayFeedAdapter:
    def __init__(self):
        self._tick_callbacks = []
        self._depth_callbacks = []
        clock_instance.add_listener(self._on_clock_tick)

    def on_tick(self, callback):
        """Register a callback for tick updates: callback(tick_dict)"""
        self._tick_callbacks.append(callback)

    def on_depth(self, callback):
        """Register a callback for depth updates: callback(depth_dict)"""
        self._depth_callbacks.append(callback)

    def _on_clock_tick(self, current_time: datetime):
        watchlist = clock_instance.watchlist
        if not watchlist:
            return

        for symbol in watchlist:
            state = get_state(symbol, current_time)
            if state:
                # Fire tick callbacks
                tick_data = {
                    "symbol": symbol,
                    "timestamp": state.get("timestamp"),
                    "ltp": state.get("ltp"),
                    "volume": state.get("volume"),
                    "oi": state.get("oi"),
                }
                for cb in list(self._tick_callbacks):
                    try:
                        cb(tick_data)
                    except Exception as exc:
                        logger.error(f"[ReplayFeedAdapter] Tick callback error: {exc}")

                # Fire depth callbacks
                depth_data = {
                    "symbol": symbol,
                    "timestamp": state.get("timestamp"),
                    "bids": [
                        {"price": state.get(f"bid_{i}"), "quantity": state.get(f"bid_qty_{i}")}
                        for i in range(1, 6)
                    ],
                    "asks": [
                        {"price": state.get(f"ask_{i}"), "quantity": state.get(f"ask_qty_{i}")}
                        for i in range(1, 6)
                    ],
                }
                for cb in list(self._depth_callbacks):
                    try:
                        cb(depth_data)
                    except Exception as exc:
                        logger.error(f"[ReplayFeedAdapter] Depth callback error: {exc}")


# Global adapter instance
feed_adapter_instance = ReplayFeedAdapter()
