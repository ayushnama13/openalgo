"""
openalgo/replay/patterns.py
───────────────────────────
Detects Fair Value Gaps (FVG), Market Structure Shifts (MSS), and Liquidity Sweeps
on historical range data.
"""

import pandas as pd

from replay.dal import get_range


def detect_patterns(symbol: str, t_start, t_end, show_fvg: bool = True, show_mss: bool = True, show_sweep: bool = True) -> dict:
    candles = get_range(symbol, t_start, t_end)
    if not candles:
        return {"fvgs": [], "msses": [], "sweeps": []}

    df_raw = pd.DataFrame(candles)
    if df_raw.empty or "ltp" not in df_raw.columns or "timestamp" not in df_raw.columns:
        return {"fvgs": [], "msses": [], "sweeps": []}

    df_raw["ltp"] = pd.to_numeric(df_raw["ltp"], errors="coerce")
    df_raw = df_raw.dropna(subset=["ltp"])
    df_raw["dt"] = pd.to_datetime(df_raw["timestamp"], errors="coerce")
    df_raw = df_raw.dropna(subset=["dt"]).sort_values("dt")

    if df_raw.empty:
        return {"fvgs": [], "msses": [], "sweeps": []}

    # Resample raw ticks into 1-minute OHLC bars
    df_raw.set_index("dt", inplace=True)
    ohlc = df_raw["ltp"].resample("1min").ohlc().dropna()

    if ohlc.empty:
        return {"fvgs": [], "msses": [], "sweeps": []}

    fvgs = []
    msses = []
    sweeps = []

    highs = ohlc["high"].to_numpy()
    lows = ohlc["low"].to_numpy()
    closes = ohlc["close"].to_numpy()
    timestamps = [ts.isoformat() for ts in ohlc.index]
    n = len(ohlc)

    # Bullish / Bearish Fair Value Gaps (3-bar pattern on 1m OHLC)
    if show_fvg and n >= 3:
        for i in range(2, n):
            # Bullish FVG: Low of candle 3 is strictly greater than High of candle 1
            if lows[i] > highs[i - 2]:
                fvgs.append({
                    "type": "bullish_fvg",
                    "timestamp": timestamps[i],
                    "top": float(lows[i]),
                    "bottom": float(highs[i - 2]),
                })
            # Bearish FVG: High of candle 3 is strictly lower than Low of candle 1
            elif highs[i] < lows[i - 2]:
                fvgs.append({
                    "type": "bearish_fvg",
                    "timestamp": timestamps[i],
                    "top": float(lows[i - 2]),
                    "bottom": float(highs[i]),
                })

    # Market Structure Shifts (MSS)
    if show_mss and n >= 5:
        for i in range(4, n):
            prior_highs = highs[i - 4:i]
            prior_lows = lows[i - 4:i]
            peak = max(prior_highs)
            trough = min(prior_lows)
            if closes[i] > peak and closes[i - 1] <= peak:
                msses.append({
                    "type": "bullish_mss",
                    "timestamp": timestamps[i],
                    "level": float(peak),
                })
            elif closes[i] < trough and closes[i - 1] >= trough:
                msses.append({
                    "type": "bearish_mss",
                    "timestamp": timestamps[i],
                    "level": float(trough),
                })

    # Liquidity Sweeps
    if show_sweep and n >= 10:
        for i in range(10, n):
            lookback_high = max(highs[i - 10:i - 1])
            lookback_low = min(lows[i - 10:i - 1])
            if highs[i] > lookback_high and closes[i] <= lookback_high:
                sweeps.append({
                    "type": "buy_side_liquidity_sweep",
                    "timestamp": timestamps[i],
                    "level": float(lookback_high),
                })
            elif lows[i] < lookback_low and closes[i] >= lookback_low:
                sweeps.append({
                    "type": "sell_side_liquidity_sweep",
                    "timestamp": timestamps[i],
                    "level": float(lookback_low),
                })

    return {
        "fvgs": fvgs if show_fvg else [],
        "msses": msses if show_mss else [],
        "sweeps": sweeps if show_sweep else [],
    }
