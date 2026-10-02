# services/backtest_engine.py

"""Event-driven, multi-symbol backtest engine.

Pure module (numpy/pandas only, no OpenAlgo imports) so it can run inside the
sandboxed child process (see strategy_sandbox.py).

Model
-----
Bars of all symbols are aligned on one timeline. For every timestamp the
engine, in order:

1. enforces the daily loss limit,
2. fills orders the strategy placed on EARLIER bars (market orders fill at this
   bar's open -> no lookahead; limit/stop orders use the bar's range),
3. checks protective exits (stop loss, take profit, trailing stop, bracket
   levels) against the bar's high/low,
4. squares off MIS positions at the exchange cut-off,
5. marks the book to market and records an equity snapshot,
6. calls ``strategy.on_bar(ctx)``, whose orders become pending for the next bar.

Strategy API
------------
Subclass ``Strategy`` (or define top-level ``init(ctx)`` / ``on_bar(ctx)``)::

    class MyStrategy(Strategy):
        params = {"fast": 10, "slow": 30}

        def init(self, ctx):
            self.sma = {s: ta.sma(ctx.data(s)["close"], self.params["fast"])
                        for s in ctx.symbols}

        def on_bar(self, ctx):
            for s in ctx.symbols:
                if ctx.bar(s) is None:
                    continue
                if ctx.close_of(s) > self.sma[s][ctx.i(s)] and not ctx.position(s):
                    ctx.buy(s, pct=20, sl_pct=2, tp_pct=6)

Indicators precomputed in ``init`` via ``ctx.data(sym)`` must be causal (value
at index i may depend only on bars <= i), and must be indexed with
``ctx.i(sym)``. ``ctx.history(sym, n)`` is always lookahead-safe.

The legacy signal DSL (a ``signal`` column of 1/-1/2/-2 or buy_signal /
sell_signal columns) is supported by ``SignalStrategy`` on top of the same
engine, one independent signal series per symbol.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

try:
    from services.backtest_fill_engine import is_mis_square_off_bar, simulate_order_fill
except ImportError:  # child process: services/ itself is on sys.path
    from backtest_fill_engine import is_mis_square_off_bar, simulate_order_fill

BUY = "BUY"
SELL = "SELL"
_EPS = 1e-9


# --------------------------------------------------------------------------
# sizing
# --------------------------------------------------------------------------
def calculate_position_qty(
    sizing_type: str,
    sizing_value: float,
    current_equity: float,
    current_price: float,
    capital_allocation: float,
) -> float:
    """Calculates trade quantity based on position sizing rules."""
    if current_price <= 0:
        return 0.0
    if sizing_type == "fixed_qty":
        return float(sizing_value)
    if sizing_type == "pct_equity":
        return float(math.floor(current_equity * (sizing_value / 100.0) / current_price))
    if sizing_type == "fixed_capital":
        return float(math.floor(sizing_value / current_price))
    if sizing_type == "kelly_vol":
        vol_adj_capital = current_equity * min(0.25, max(0.02, sizing_value))
        return float(math.floor(vol_adj_capital / current_price))
    return 1.0


# --------------------------------------------------------------------------
# data classes
# --------------------------------------------------------------------------
@dataclass
class EngineConfig:
    initial_capital: float = 100000.0
    sizing_type: str = "fixed_qty"
    sizing_value: float = 1.0
    slippage_bps: float = 0.0
    brokerage_bps: float = 0.0
    product_type: str = "MIS"
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    trail_pct: float | None = None
    daily_loss_limit: float | None = None
    max_positions: int = 1
    allow_short: bool = True
    enforce_cash: bool = True
    interval: str = "1m"
    params: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> EngineConfig:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Bar:
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Position:
    symbol: str
    qty: float
    avg_price: float
    entry_time: str
    entry_bar: int
    entry_cost: float = 0.0
    entry_reason: str = "Signal"
    entry_market: bool = True
    sl_pct: float | None = None
    tp_pct: float | None = None
    sl_price: float | None = None
    tp_price: float | None = None
    trail_pct: float | None = None
    extreme: float = 0.0  # high watermark (long) / low watermark (short)
    lo: float = 0.0  # lowest / highest prices seen, for MAE/MFE
    hi: float = 0.0

    @property
    def side(self) -> str:
        return "LONG" if self.qty > 0 else "SHORT"

    def __bool__(self) -> bool:
        return abs(self.qty) > _EPS


@dataclass
class Order:
    id: int
    symbol: str
    side: str
    qty: float | None  # None = size by config at fill time; -1 = "close all"
    pct: float | None
    price_type: str
    limit_price: float
    trigger_price: float
    sl_pct: float | None
    tp_pct: float | None
    sl_price: float | None
    tp_price: float | None
    trail_pct: float | None
    tag: str
    created_bar: int
    valid_bars: int | None = None
    reduce_only: bool = False


_CLOSE_ALL = -1.0


class Strategy:
    """Base class for event-driven strategies."""

    params: dict = {}

    def init(self, ctx: Context) -> None:  # noqa: D401
        """Called once before the first bar."""

    def on_bar(self, ctx: Context) -> None:
        """Called after each aligned bar closes."""


@dataclass
class EngineResult:
    trades: list[dict]
    equity: list[dict]
    notes: list[str]
    logs: list[str]
    rejections: dict
    cancelled: bool = False


# --------------------------------------------------------------------------
# context handed to strategies
# --------------------------------------------------------------------------
class Context:
    def __init__(self, engine: Engine):
        self._e = engine

    # ---- state ----
    @property
    def symbols(self) -> list[str]:
        return self._e.symbols

    @property
    def time(self) -> str:
        return self._e.timeline[self._e.k]

    @property
    def bar_index(self) -> int:
        return self._e.k

    @property
    def equity(self) -> float:
        return self._e.equity_now()

    @property
    def cash(self) -> float:
        return self._e.cash

    @property
    def params(self) -> dict:
        return self._e.cfg.params

    @property
    def positions(self) -> dict[str, Position]:
        return {s: p for s, p in self._e.positions.items() if p}

    def position(self, symbol: str) -> Position | None:
        p = self._e.positions.get(symbol)
        return p if p else None

    def pending(self, symbol: str | None = None) -> list[Order]:
        return [o for o in self._e.pending if symbol is None or o.symbol == symbol]

    # ---- data ----
    def bar(self, symbol: str) -> Bar | None:
        """The symbol's bar at the current timestamp, or None if it has none."""
        return self._e.cur_bar.get(symbol)

    def close_of(self, symbol: str) -> float:
        """Last known close (carried forward over gaps)."""
        return self._e.last_close.get(symbol, float("nan"))

    def i(self, symbol: str) -> int:
        """Index of the symbol's latest bar (<= now) in ctx.data(symbol)."""
        return int(self._e.cnt[symbol][self._e.k]) - 1

    def data(self, symbol: str) -> pd.DataFrame:
        """Full OHLCV frame. Use only causal indicators, indexed by ctx.i()."""
        return self._e.frames[symbol]

    def history(self, symbol: str, n: int | None = None, field: str | None = None):
        """Bars up to and including the current one (lookahead-safe)."""
        end = int(self._e.cnt[symbol][self._e.k])
        start = 0 if n is None else max(0, end - int(n))
        out = self._e.frames[symbol].iloc[start:end]
        return out[field] if field else out

    # ---- orders ----
    def order(
        self,
        symbol: str,
        side: str,
        qty: float | None = None,
        pct: float | None = None,
        price_type: str = "MARKET",
        limit_price: float = 0.0,
        trigger_price: float = 0.0,
        sl_pct: float | None = None,
        tp_pct: float | None = None,
        sl_price: float | None = None,
        tp_price: float | None = None,
        trail_pct: float | None = None,
        tag: str = "",
        valid_bars: int | None = None,
        reduce_only: bool = False,
    ) -> int:
        """Queue an order; it is first eligible to fill on the NEXT bar."""
        if symbol not in self._e.frames:
            raise ValueError(f"unknown symbol {symbol!r}")
        side = side.upper()
        if side not in (BUY, SELL):
            raise ValueError("side must be BUY or SELL")
        return self._e.submit(
            Order(
                id=0, symbol=symbol, side=side, qty=qty, pct=pct,
                price_type=price_type.upper(), limit_price=float(limit_price or 0.0),
                trigger_price=float(trigger_price or 0.0), sl_pct=sl_pct, tp_pct=tp_pct,
                sl_price=sl_price, tp_price=tp_price, trail_pct=trail_pct, tag=tag,
                created_bar=self._e.k, valid_bars=valid_bars, reduce_only=reduce_only,
            )
        )

    def buy(self, symbol: str, **kw) -> int:
        return self.order(symbol, BUY, **kw)

    def sell(self, symbol: str, **kw) -> int:
        """SELL: reduces a long, or opens a short when flat (if allowed)."""
        return self.order(symbol, SELL, **kw)

    def close(self, symbol: str, tag: str = "") -> int | None:
        """Flatten a position at the next bar's open."""
        p = self.position(symbol)
        if not p:
            return None
        return self.order(symbol, SELL if p.qty > 0 else BUY, qty=_CLOSE_ALL, tag=tag)

    def target_pct(self, symbol: str, pct: float, tag: str = "") -> int | None:
        """Rebalance toward ``pct`` % of equity (signed; negative = short)."""
        price = self.close_of(symbol)
        if not price or price <= 0 or math.isnan(price):
            return None
        target_qty = math.floor(abs(self.equity * pct / 100.0) / price) * (1 if pct >= 0 else -1)
        cur = self.position(symbol).qty if self.position(symbol) else 0.0
        delta = target_qty - cur
        if abs(delta) < 1:
            return None
        return self.order(symbol, BUY if delta > 0 else SELL, qty=abs(delta), tag=tag)

    def cancel(self, order_id: int) -> bool:
        before = len(self._e.pending)
        self._e.pending = [o for o in self._e.pending if o.id != order_id]
        return len(self._e.pending) < before

    def cancel_all(self, symbol: str | None = None) -> int:
        before = len(self._e.pending)
        self._e.pending = [o for o in self._e.pending if symbol is not None and o.symbol != symbol]
        return before - len(self._e.pending)

    def log(self, msg: Any) -> None:
        if len(self._e.logs) < 500:
            self._e.logs.append(f"{self.time} {msg}")


# --------------------------------------------------------------------------
# legacy signal DSL on top of the engine
# --------------------------------------------------------------------------
class SignalStrategy(Strategy):
    """signal: 1 buy, -1 exit long, 2 short, -2 cover. One series per symbol."""

    def __init__(self, signals: dict[str, np.ndarray]):
        self.signals = signals

    def on_bar(self, ctx: Context) -> None:
        for s in ctx.symbols:
            if ctx.bar(s) is None or ctx.pending(s):
                continue
            j = ctx.i(s)
            sig = int(self.signals[s][j])
            p = ctx.position(s)
            if sig == 1 and not p:
                ctx.buy(s)
            elif sig == -1 and p and p.qty > 0:
                ctx.close(s, tag="Signal Exit")
            elif sig == 2 and not p:
                ctx.sell(s)
            elif sig == -2 and p and p.qty < 0:
                ctx.close(s, tag="Signal Exit")


# --------------------------------------------------------------------------
# engine
# --------------------------------------------------------------------------
class Engine:
    def __init__(
        self,
        bars: dict[str, pd.DataFrame],
        cfg: EngineConfig,
        strategy: Strategy,
        progress_cb: Callable[[float, str, float, int], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ):
        if not bars:
            raise ValueError("no symbols to backtest")
        self.cfg = cfg
        self.strategy = strategy
        self.progress_cb = progress_cb
        self.cancel_check = cancel_check
        self.symbols = list(bars)
        self.exchange = {s: (s.split(":")[0] if ":" in s else "NSE") for s in self.symbols}

        self.frames: dict[str, pd.DataFrame] = {}
        ts_dt: dict[str, pd.DatetimeIndex] = {}
        for s, df in bars.items():
            df = df.reset_index(drop=True)
            ts_dt[s] = pd.DatetimeIndex(pd.to_datetime(df["timestamp"]))
            self.frames[s] = df
        union = ts_dt[self.symbols[0]]
        for s in self.symbols[1:]:
            union = union.union(ts_dt[s])
        union = union.sort_values()
        self.timeline = list(union.strftime("%Y-%m-%d %H:%M:%S"))
        self.T = len(self.timeline)

        self.o, self.h, self.l, self.c, self.v = {}, {}, {}, {}, {}
        self.own = {}
        self.cnt = {}
        for s in self.symbols:
            df = self.frames[s]
            self.o[s] = df["open"].to_numpy(float)
            self.h[s] = df["high"].to_numpy(float)
            self.l[s] = df["low"].to_numpy(float)
            self.c[s] = df["close"].to_numpy(float)
            self.v[s] = df["volume"].to_numpy(float) if "volume" in df.columns else np.zeros(len(df))
            idx = ts_dt[s]
            right = idx.searchsorted(union, side="right")
            left = idx.searchsorted(union, side="left")
            self.cnt[s] = right
            self.own[s] = np.where(right > left, left, -1)

        self.cash = float(cfg.initial_capital)
        self.positions: dict[str, Position] = {}
        self.pending: list[Order] = []
        self.last_close: dict[str, float] = {}
        self.cur_bar: dict[str, Bar | None] = {}
        self.trades: list[dict] = []
        self.equity_rows: list[dict] = []
        self.notes: list[str] = []
        self.logs: list[str] = []
        self.rejections: dict[str, int] = {}
        self.k = 0
        self._oid = 0

    # ---- helpers ----
    def equity_now(self) -> float:
        return self.cash + sum(
            p.qty * self.last_close.get(s, p.avg_price) for s, p in self.positions.items() if p
        )

    def submit(self, order: Order) -> int:
        self._oid += 1
        order.id = self._oid
        self.pending.append(order)
        return order.id

    def _reject(self, reason: str) -> None:
        self.rejections[reason] = self.rejections.get(reason, 0) + 1

    def _fill_price(self, s, action, price_type, limit, trigger, b: Bar, qty):
        return simulate_order_fill(
            action=action, price_type=price_type, limit_price=limit, trigger_price=trigger,
            bar_open=b.open, bar_high=b.high, bar_low=b.low, bar_close=b.close,
            volume=b.volume, slippage_bps=self.cfg.slippage_bps,
            brokerage_bps=self.cfg.brokerage_bps, quantity=qty,
        )

    # ---- book keeping ----
    def _apply_fill(self, s: str, side: str, qty: float, price: float, cost: float,
                    order: Order | None, reason: str, market: bool) -> None:
        sign = 1.0 if side == BUY else -1.0
        self.cash -= sign * qty * price + cost
        p = self.positions.get(s)
        t = self.timeline[self.k]
        if not p:
            self._open(s, sign * qty, price, cost, order, market)
            return
        if p.qty * sign > 0:  # scale in
            total = abs(p.qty) + qty
            p.avg_price = (abs(p.qty) * p.avg_price + qty * price) / total
            p.qty += sign * qty
            p.entry_cost += cost
            if order is not None:
                self._attach_brackets(p, order)
            return
        closed = min(qty, abs(p.qty))
        part_cost = cost * closed / qty
        alloc_entry = p.entry_cost * closed / abs(p.qty)
        direction = 1.0 if p.qty > 0 else -1.0
        gross = (price - p.avg_price) * closed * direction
        total_cost = part_cost + alloc_entry
        pnl = gross - total_cost
        base = p.avg_price * closed
        mfe = ((p.hi - p.avg_price) if direction > 0 else (p.avg_price - p.lo)) / p.avg_price * 100.0
        mae = ((p.lo - p.avg_price) if direction > 0 else (p.avg_price - p.hi)) / p.avg_price * 100.0
        self.trades.append({
            "symbol": s.split(":")[-1],
            "exchange": self.exchange[s],
            "action": BUY if direction > 0 else SELL,
            "entry_time": p.entry_time,
            "entry_price": round(p.avg_price, 2),
            "exit_time": t,
            "exit_price": round(price, 2),
            "quantity": closed,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / base * 100.0, 2) if base > 0 else 0.0,
            "holding_period_bars": self.k - p.entry_bar,
            "entry_reason": p.entry_reason,
            "exit_reason": reason,
            "costs": round(total_cost, 2),
            "mae_pct": round(min(mae, 0.0), 2),
            "mfe_pct": round(max(mfe, 0.0), 2),
        })
        p.entry_cost -= alloc_entry
        p.qty -= direction * closed
        remaining = qty - closed
        if abs(p.qty) < _EPS:
            del self.positions[s]
        if remaining > _EPS:
            self._open(s, sign * remaining, price, cost * remaining / qty, order, market)

    def _open(self, s, signed_qty, price, cost, order, market):
        p = Position(
            symbol=s, qty=signed_qty, avg_price=price, entry_time=self.timeline[self.k],
            entry_bar=self.k, entry_cost=cost,
            entry_reason=(order.tag if order and order.tag else "Signal"),
            entry_market=market, extreme=price, lo=price, hi=price,
        )
        if order is not None:
            self._attach_brackets(p, order)
        self.positions[s] = p

    @staticmethod
    def _attach_brackets(p: Position, o: Order) -> None:
        for name in ("sl_pct", "tp_pct", "sl_price", "tp_price", "trail_pct"):
            v = getattr(o, name)
            if v is not None:
                setattr(p, name, v)

    # ---- order processing ----
    def _resolve_qty(self, o: Order, price: float, p: Position | None) -> float:
        if o.qty == _CLOSE_ALL:
            return abs(p.qty) if p else 0.0
        if o.qty is not None:
            return float(o.qty)
        if o.pct is not None:
            return float(math.floor(self.equity_now() * (o.pct / 100.0) / price)) if price > 0 else 0.0
        return calculate_position_qty(
            self.cfg.sizing_type, self.cfg.sizing_value, self.equity_now(), price,
            self.cfg.initial_capital,
        )

    def _process_pending(self) -> None:
        keep: list[Order] = []
        for o in self.pending:
            if o.created_bar >= self.k:
                keep.append(o)
                continue
            if o.valid_bars is not None and self.k - o.created_bar > o.valid_bars:
                continue
            b = self.cur_bar.get(o.symbol)
            if b is None:
                keep.append(o)
                continue
            p = self.positions.get(o.symbol)
            qty = self._resolve_qty(o, b.close, p)
            if qty <= 0:
                continue
            closing_dir = bool(p) and ((p.qty > 0) == (o.side == SELL))
            if o.reduce_only or o.qty == _CLOSE_ALL:
                if not closing_dir:
                    continue
                qty = min(qty, abs(p.qty))
            elif closing_dir and not self.cfg.allow_short:
                qty = min(qty, abs(p.qty))
            elif not p and o.side == SELL and not self.cfg.allow_short:
                self._reject("short_disabled")
                continue
            filled, price, cost = self._fill_price(
                o.symbol, o.side, o.price_type, o.limit_price, o.trigger_price, b, qty
            )
            if not filled:
                keep.append(o)
                continue
            opens_qty = qty - (min(qty, abs(p.qty)) if closing_dir else 0.0)
            if opens_qty > _EPS:
                fresh = not closing_dir and not p
                flip = closing_dir
                if (fresh or flip) and len([x for x in self.positions.values() if x]) - (
                    1 if flip else 0
                ) >= max(1, int(self.cfg.max_positions or 1)):
                    self._reject("max_positions")
                    continue
                if (
                    o.side == BUY
                    and self.cfg.enforce_cash
                    and not (p and p.qty < 0)  # cash includes short proceeds; skip when covering
                    and qty * price + cost > self.cash + _EPS
                ):
                    self._reject("insufficient_cash")
                    continue
            reason = o.tag or ("Signal Exit" if closing_dir else "Signal")
            self._apply_fill(o.symbol, o.side, qty, price, cost, o, reason, o.price_type == "MARKET")
        self.pending = keep

    def _levels(self, p: Position):
        long = p.qty > 0
        sl = p.sl_price
        if sl is None:
            pct = p.sl_pct if p.sl_pct is not None else self.cfg.stop_loss_pct
            if pct:
                sl = p.avg_price * (1 - abs(pct) / 100.0) if long else p.avg_price * (1 + abs(pct) / 100.0)
        reason_sl = "Stop Loss"
        tr = p.trail_pct if p.trail_pct is not None else self.cfg.trail_pct
        if tr:
            tstop = p.extreme * (1 - abs(tr) / 100.0) if long else p.extreme * (1 + abs(tr) / 100.0)
            if sl is None or (long and tstop > sl) or (not long and tstop < sl):
                sl, reason_sl = tstop, "Trailing Stop"
        tp = p.tp_price
        if tp is None:
            pct = p.tp_pct if p.tp_pct is not None else self.cfg.take_profit_pct
            if pct:
                tp = p.avg_price * (1 + abs(pct) / 100.0) if long else p.avg_price * (1 - abs(pct) / 100.0)
        return sl, reason_sl, tp

    def _protective(self) -> None:
        for s, p in list(self.positions.items()):
            b = self.cur_bar.get(s)
            if not p or b is None:
                continue
            if p.entry_bar == self.k and not p.entry_market:
                continue  # intrabar fill time unknown: skip stop check on entry bar
            long = p.qty > 0
            p.hi = max(p.hi, b.high)
            p.lo = min(p.lo, b.low)
            sl, reason_sl, tp = self._levels(p)
            exit_side = SELL if long else BUY
            sl_hit = sl is not None and ((b.low <= sl) if long else (b.high >= sl))
            tp_hit = tp is not None and ((b.high >= tp) if long else (b.low <= tp))
            gap_sl = sl is not None and ((b.open <= sl) if long else (b.open >= sl))
            gap_tp = tp is not None and ((b.open >= tp) if long else (b.open <= tp))
            pick = None
            if gap_tp and not gap_sl:
                pick = "tp"
            elif sl_hit:  # stop wins when both are touched inside one bar (pessimistic)
                pick = "sl"
            elif tp_hit:
                pick = "tp"
            if pick == "sl":
                filled, price, cost = self._fill_price(s, exit_side, "SL-M", 0.0, sl, b, abs(p.qty))
                reason = reason_sl
            elif pick == "tp":
                filled, price, cost = self._fill_price(s, exit_side, "LIMIT", tp, 0.0, b, abs(p.qty))
                reason = "Take Profit"
            else:
                filled = False
            if filled:
                self._apply_fill(s, exit_side, abs(p.qty), price, cost, None, reason, True)
                self.pending = [o for o in self.pending if o.symbol != s or not o.reduce_only]
            else:
                p.extreme = max(p.extreme, b.high) if long else min(p.extreme, b.low)

    def _force_close(self, s: str, reason: str, at_close: bool = False) -> None:
        p = self.positions.get(s)
        b = self.cur_bar.get(s)
        if not p:
            return
        if b is None:
            b = Bar(self.timeline[self.k], *(self.last_close.get(s, p.avg_price),) * 4, 0.0)
        elif at_close:
            b = Bar(b.time, b.close, b.high, b.low, b.close, b.volume)
        side = SELL if p.qty > 0 else BUY
        filled, price, cost = self._fill_price(s, side, "MARKET", 0.0, 0.0, b, abs(p.qty))
        if filled:
            self._apply_fill(s, side, abs(p.qty), price, cost, None, reason, True)

    # ---- main loop ----
    def run(self) -> EngineResult:
        ctx = Context(self)
        self.k = 0
        self.strategy.init(ctx)
        cur_day = ""
        day_start_equity = self.cfg.initial_capital
        halted = False
        step = max(1, self.T // 50)
        cancelled = False
        mis = self.cfg.product_type == "MIS"

        for k in range(self.T):
            self.k = k
            if self.cancel_check is not None and self.cancel_check():
                cancelled = True
                break
            t = self.timeline[k]
            day = t[:10]
            if day != cur_day:  # previous bar's closes still in last_close
                cur_day = day
                day_start_equity = self.equity_now()
                halted = False
                if mis:
                    self.pending = []

            self.cur_bar = {}
            for s in self.symbols:
                j = self.own[s][k]
                if j >= 0:
                    self.cur_bar[s] = Bar(t, self.o[s][j], self.h[s][j], self.l[s][j],
                                          self.c[s][j], self.v[s][j])
                    self.last_close[s] = self.c[s][j]

            eq = self.equity_now()
            lim = self.cfg.daily_loss_limit
            if lim and lim > 0 and day_start_equity - eq >= lim:
                if not halted:
                    for s in list(self.positions):
                        self._force_close(s, "Daily Loss Limit")
                    self.pending = []
                halted = True
            if not halted:
                self._process_pending()
                self._protective()
                if mis:
                    for s, p in list(self.positions.items()):
                        if p and s in self.cur_bar and is_mis_square_off_bar(t, self.exchange[s]):
                            self._force_close(s, "MIS Auto Squareoff")
                    if any(is_mis_square_off_bar(t, self.exchange[s]) for s in self.cur_bar):
                        self.pending = []

            if k == self.T - 1:
                for s in list(self.positions):
                    self._force_close(s, "End of Backtest", at_close=True)

            eq = self.equity_now()
            self.equity_rows.append({
                "timestamp": t,
                "equity": round(eq, 2),
                "cash": round(self.cash, 2),
                "positions_value": round(eq - self.cash, 2),
            })

            if not halted and k < self.T - 1:
                self.strategy.on_bar(ctx)

            if self.progress_cb is not None and (k % step == 0 or k == self.T - 1):
                self.progress_cb((k + 1) / self.T * 100.0, t, eq, len(self.trades))

        if self.rejections:
            labels = {
                "max_positions": "orders rejected: max_positions limit reached",
                "insufficient_cash": "orders rejected: insufficient cash",
                "short_disabled": "orders rejected: shorting disabled",
            }
            for key, n in self.rejections.items():
                self.notes.append(f"{n} {labels.get(key, key)}")
        return EngineResult(self.trades, self.equity_rows, self.notes, self.logs,
                            self.rejections, cancelled)


# --------------------------------------------------------------------------
# strategy discovery (used by the sandbox runner)
# --------------------------------------------------------------------------
def compute_signals(run_code: Callable[[pd.DataFrame], pd.DataFrame], df: pd.DataFrame) -> np.ndarray:
    """Apply user DSL to one symbol's frame and return the signal array."""
    out = run_code(df.copy())
    if "buy_signal" in out.columns and "sell_signal" in out.columns:
        sig = np.zeros(len(out), dtype=int)
        sig[out["buy_signal"].astype(bool).to_numpy()] = 1
        sig[out["sell_signal"].astype(bool).to_numpy()] = -1
        return sig
    if "signal" not in out.columns:
        return np.zeros(len(out), dtype=int)
    sig = pd.to_numeric(out["signal"], errors="coerce").fillna(0).to_numpy()
    if len(sig) != len(df):
        raise ValueError(f"strategy returned {len(sig)} rows but input has {len(df)}")
    if (sig % 1 != 0).any() or not set(np.unique(sig).astype(int)) <= {-2, -1, 0, 1, 2}:
        raise ValueError("Strategy signal values must be in {-2,-1,0,1,2}")
    return sig.astype(int)
