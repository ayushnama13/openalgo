"""
openalgo/replay/clock.py
────────────────────────
Global Replay Clock managing standalone time, playback state, speed, and watchlist.
"""

import threading
import time
from datetime import datetime, timedelta

import pytz

from utils.logging import get_logger

logger = get_logger(__name__)
IST = pytz.timezone("Asia/Kolkata")


class ReplayClock:
    def __init__(self):
        self._lock = threading.Lock()
        self.current_time: datetime = IST.localize(datetime(2026, 8, 10, 9, 15, 0))
        self.start_time: datetime = IST.localize(datetime(2026, 8, 10, 9, 15, 0))
        self.end_time: datetime = IST.localize(datetime(2026, 8, 10, 15, 30, 0))
        self.is_playing: bool = False
        self.speed: float = 1.0  # 1x, 5x, 20x, etc.
        self.watchlist: list[str] = []
        self.session_date: str = "2026-08-10"
        self._thread: threading.Thread | None = None
        self._listeners: list = []

    def configure(self, session_date: str, start_time: str, end_time: str, watchlist: list[str]):
        with self._lock:
            self.session_date = session_date
            try:
                dt_base = datetime.strptime(session_date, "%Y-%m-%d")
                st_parts = [int(x) for x in start_time.split(":")]
                et_parts = [int(x) for x in end_time.split(":")]
                self.start_time = IST.localize(datetime(dt_base.year, dt_base.month, dt_base.day, st_parts[0], st_parts[1], st_parts[2] if len(st_parts) > 2 else 0))
                self.end_time = IST.localize(datetime(dt_base.year, dt_base.month, dt_base.day, et_parts[0], et_parts[1], et_parts[2] if len(et_parts) > 2 else 0))
                self.current_time = self.start_time
            except Exception as exc:
                logger.error(f"[ReplayClock] Configure error: {exc}")
            self.watchlist = [w.upper() for w in watchlist]

    def play(self):
        with self._lock:
            if self.is_playing:
                return
            self.is_playing = True
        logger.info(f"[ReplayClock] Playing from {self.current_time} at {self.speed}x")

    def pause(self):
        with self._lock:
            self.is_playing = False
        logger.info(f"[ReplayClock] Paused at {self.current_time}")

    def seek(self, timestamp: datetime | str):
        with self._lock:
            if isinstance(timestamp, str):
                dt = datetime.fromisoformat(timestamp)
            else:
                dt = timestamp
            if dt.tzinfo is None:
                dt = IST.localize(dt)
            self.current_time = dt
        logger.info(f"[ReplayClock] Seeked to {self.current_time}")

    def step(self, seconds: int = 1):
        with self._lock:
            self.current_time += timedelta(seconds=seconds)
            if self.current_time > self.end_time:
                self.current_time = self.end_time

    def set_speed(self, speed: float):
        with self._lock:
            self.speed = max(0.1, float(speed))
        logger.info(f"[ReplayClock] Speed set to {self.speed}x")

    def get_state(self) -> dict:
        with self._lock:
            return {
                "current_time": self.current_time.isoformat(),
                "start_time": self.start_time.isoformat(),
                "end_time": self.end_time.isoformat(),
                "is_playing": self.is_playing,
                "speed": self.speed,
                "session_date": self.session_date,
                "watchlist": self.watchlist,
            }

    def add_listener(self, callback):
        with self._lock:
            self._listeners.append(callback)

    def tick_loop(self):
        last_real = time.time()
        while True:
            time.sleep(0.1)
            now_real = time.time()
            real_delta = now_real - last_real
            last_real = now_real

            playing = False
            speed = 1.0
            with self._lock:
                playing = self.is_playing
                speed = self.speed

            if playing:
                with self._lock:
                    advance_seconds = real_delta * speed
                    self.current_time += timedelta(seconds=advance_seconds)
                    if self.current_time >= self.end_time:
                        self.current_time = self.end_time
                        self.is_playing = False

                    curr = self.current_time

                # Notify listeners
                with self._lock:
                    listeners = list(self._listeners)

                for cb in listeners:
                    try:
                        cb(curr)
                    except Exception as exc:
                        logger.error(f"[ReplayClock] Listener error: {exc}")


# Global clock instance
clock_instance = ReplayClock()

# Start background ticker thread
_ticker_thread = threading.Thread(target=clock_instance.tick_loop, daemon=True, name="ReplayClockTicker")
_ticker_thread.start()
