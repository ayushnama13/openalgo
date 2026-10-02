# services/backtest_reports.py

"""Post-run analytics for the strategy backtester: drawdown periods, rolling
metrics, trade breakdowns (symbol / exit reason / side), MAE and MFE, exposure,
and chart payloads. Pure functions over pandas/numpy so they are easy to test."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

MAX_ROLLING_POINTS = 500


def drawdown_periods(equity: pd.Series, top: int = 10) -> list[dict[str, Any]]:
    """The ``top`` deepest drawdowns, each from a peak to its recovery (or the end).

    Depth is the percentage fall from the prior peak at the trough; durations
    are counted in bars.
    """
    if len(equity) < 2:
        return []
    values = equity.to_numpy(dtype=float)
    peak = np.maximum.accumulate(values)
    periods: list[dict[str, Any]] = []
    i, n = 0, len(values)
    while i < n:
        if values[i] >= peak[i]:
            i += 1
            continue
        start = i - 1  # last bar at the peak
        trough = i
        j = i
        while j < n and values[j] < peak[start]:
            if values[j] < values[trough]:
                trough = j
            j += 1
        recovered = j < n
        depth = (values[trough] - peak[start]) / peak[start] * 100.0 if peak[start] > 0 else 0.0
        periods.append({
            "start": str(equity.index[start]),
            "trough": str(equity.index[trough]),
            "end": str(equity.index[j]) if recovered else None,
            "depth_pct": round(float(depth), 2),
            "bars_to_trough": int(trough - start),
            "bars_to_recover": int(j - trough) if recovered else None,
            "duration_bars": int((j if recovered else n - 1) - start),
            "recovered": bool(recovered),
        })
        i = j
    periods.sort(key=lambda p: p["depth_pct"])
    return periods[:top]


def rolling_metrics(
    equity: pd.Series, periods_per_year: float, window: int | None = None
) -> dict[str, Any]:
    """Rolling annualised Sharpe and rolling return, thinned for charting."""
    n = len(equity)
    if n < 20:
        return {"window": 0, "points": []}
    window = int(window or max(10, min(n // 4, int(periods_per_year))))
    window = max(5, min(window, n - 2))
    rets = equity.pct_change().dropna()
    mean = rets.rolling(window).mean()
    std = rets.rolling(window).std()
    sharpe = (mean / std.replace(0, np.nan)) * math.sqrt(periods_per_year)
    roll_ret = equity.pct_change(window) * 100.0
    frame = pd.DataFrame({"sharpe": sharpe, "return_pct": roll_ret}).dropna(how="all")
    if len(frame) > MAX_ROLLING_POINTS:
        step = len(frame) / MAX_ROLLING_POINTS
        keep = sorted({min(len(frame) - 1, int(i * step)) for i in range(MAX_ROLLING_POINTS)})
        frame = frame.iloc[keep]
    points = [
        {
            "timestamp": str(ts),
            "sharpe": None if pd.isna(r.sharpe) else round(float(r.sharpe), 2),
            "return_pct": None if pd.isna(r.return_pct) else round(float(r.return_pct), 2),
        }
        for ts, r in frame.iterrows()
    ]
    return {"window": window, "points": points}


def _group_stats(trades: pd.DataFrame, key: str) -> list[dict[str, Any]]:
    out = []
    for name, g in trades.groupby(key, dropna=False):
        wins = (g["pnl"] > 0).sum()
        out.append({
            key: str(name),
            "trades": int(len(g)),
            "pnl": round(float(g["pnl"].sum()), 2),
            "win_rate_pct": round(float(wins / len(g) * 100.0), 1),
            "avg_pnl": round(float(g["pnl"].mean()), 2),
            "costs": round(float(g["costs"].sum()), 2) if "costs" in g else 0.0,
        })
    out.sort(key=lambda r: r["pnl"], reverse=True)
    return out


def _streaks(pnl: np.ndarray) -> dict[str, int]:
    best_w = best_l = cur_w = cur_l = 0
    for v in pnl:
        if v > 0:
            cur_w += 1
            cur_l = 0
        elif v < 0:
            cur_l += 1
            cur_w = 0
        else:
            cur_w = cur_l = 0
        best_w, best_l = max(best_w, cur_w), max(best_l, cur_l)
    return {"max_consecutive_wins": best_w, "max_consecutive_losses": best_l}


def trade_breakdown(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-symbol / exit-reason / side tables, streaks and MAE-MFE statistics."""
    if not trades:
        return {"by_symbol": [], "by_exit_reason": [], "by_side": [], "streaks": {}, "excursion": None}
    df = pd.DataFrame(trades)
    df["side"] = np.where(df["action"] == "BUY", "LONG", "SHORT")
    excursion = None
    if {"mae_pct", "mfe_pct"} <= set(df.columns) and df["mae_pct"].notna().any():
        e = df.dropna(subset=["mae_pct", "mfe_pct"])
        win, lose = e[e["pnl"] > 0], e[e["pnl"] <= 0]
        avg_mae = float(e["mae_pct"].mean())
        avg_mfe = float(e["mfe_pct"].mean())
        # how much of the best excursion the trades actually kept
        captured = e.loc[e["mfe_pct"] > 0, "pnl_pct"] / e.loc[e["mfe_pct"] > 0, "mfe_pct"]
        excursion = {
            "avg_mae_pct": round(avg_mae, 2),
            "avg_mfe_pct": round(avg_mfe, 2),
            "avg_mae_winners_pct": round(float(win["mae_pct"].mean()), 2) if len(win) else None,
            "avg_mae_losers_pct": round(float(lose["mae_pct"].mean()), 2) if len(lose) else None,
            "edge_ratio": round(avg_mfe / abs(avg_mae), 2) if avg_mae < 0 else None,
            "avg_profit_capture_pct": round(float(captured.mean() * 100.0), 1) if len(captured) else None,
            "points": [
                {"mae": round(float(a), 2), "mfe": round(float(b), 2), "pnl": round(float(c), 2)}
                for a, b, c in zip(e["mae_pct"], e["mfe_pct"], e["pnl"], strict=True)
            ][:2000],
        }
    return {
        "by_symbol": _group_stats(df, "symbol"),
        "by_exit_reason": _group_stats(df, "exit_reason"),
        "by_side": _group_stats(df, "side"),
        "streaks": _streaks(df["pnl"].to_numpy()),
        "excursion": excursion,
    }


def exposure_stats(positions_value: pd.Series) -> dict[str, float]:
    """Share of bars with an open position."""
    if len(positions_value) == 0:
        return {"time_in_market_pct": 0.0}
    held = (positions_value.abs() > 1e-9).mean()
    return {"time_in_market_pct": round(float(held * 100.0), 1)}


def chart_payload(
    bars: pd.DataFrame, trades: list[dict[str, Any]], symbol: str, max_bars: int = 5000
) -> dict[str, Any]:
    """OHLC bars (windowed to ``max_bars``) plus entry/exit markers for ``symbol``."""
    bare = symbol.split(":")[-1]
    mine = [t for t in trades if t["symbol"] == bare]
    bars = bars.reset_index(drop=True)
    if len(bars) > max_bars:
        # keep the window that holds the most trades rather than thinning candles
        times = pd.to_datetime(bars["timestamp"])
        best_start, best_hits = 0, -1
        entry_ts = pd.to_datetime([t["entry_time"] for t in mine]) if mine else pd.DatetimeIndex([])
        for start in range(0, len(bars) - max_bars + 1, max(1, max_bars // 4)):
            lo, hi = times.iloc[start], times.iloc[start + max_bars - 1]
            hits = int(((entry_ts >= lo) & (entry_ts <= hi)).sum())
            if hits > best_hits:
                best_start, best_hits = start, hits
        bars = bars.iloc[best_start : best_start + max_bars]
    first, last = str(bars["timestamp"].iloc[0]), str(bars["timestamp"].iloc[-1])
    markers = []
    for t in mine:
        long = t["action"] == "BUY"
        if first <= str(t["entry_time"]) <= last:
            markers.append({"time": str(t["entry_time"]), "kind": "entry",
                            "side": "LONG" if long else "SHORT", "price": t["entry_price"],
                            "quantity": t["quantity"]})
        if first <= str(t["exit_time"]) <= last:
            markers.append({"time": str(t["exit_time"]), "kind": "exit",
                            "side": "LONG" if long else "SHORT", "price": t["exit_price"],
                            "pnl": t["pnl"], "reason": t.get("exit_reason")})
    candles = [
        {"time": str(r.timestamp), "open": float(r.open), "high": float(r.high),
         "low": float(r.low), "close": float(r.close)}
        for r in bars.itertuples()
    ]
    return {"symbol": symbol, "candles": candles, "markers": markers,
            "total_trades": len(mine), "shown_trades": sum(1 for m in markers if m["kind"] == "entry")}
