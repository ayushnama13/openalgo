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

    fvgs = []
    msses = []
    sweeps = []

    clean_candles = []
    for c in candles:
        p = c.get("ltp")
        if p is not None and not pd.isna(p) and float(p) > 0:
            clean_candles.append((float(p), c.get("timestamp")))

    if not clean_candles:
        return {"fvgs": [], "msses": [], "sweeps": []}

    prices = [item[0] for item in clean_candles]
    timestamps = [item[1] for item in clean_candles]

    if show_fvg and len(prices) >= 3:
        for i in range(2, len(prices)):
            p0, p1, p2 = prices[i-2], prices[i-1], prices[i]
            # Bullish FVG gap
            if p2 > p0 and (p2 - p0) > (abs(p1 - p0) * 1.5):
                fvgs.append({
                    "type": "bullish_fvg",
                    "timestamp": timestamps[i],
                    "top": p2,
                    "bottom": p0,
                })
            # Bearish FVG gap
            elif p2 < p0 and (p0 - p2) > (abs(p1 - p0) * 1.5):
                fvgs.append({
                    "type": "bearish_fvg",
                    "timestamp": timestamps[i],
                    "top": p0,
                    "bottom": p2,
                })

    if show_mss and len(prices) >= 5:
        # Detect market structure shift on local extremes break
        for i in range(4, len(prices)):
            segment = prices[i-4:i+1]
            peak = max(segment)
            trough = min(segment)
            if prices[i] > peak * 0.9999 and prices[i-1] <= peak:
                msses.append({
                    "type": "bullish_mss",
                    "timestamp": timestamps[i],
                    "level": peak,
                })
            elif prices[i] < trough * 1.0001 and prices[i-1] >= trough:
                msses.append({
                    "type": "bearish_mss",
                    "timestamp": timestamps[i],
                    "level": trough,
                })

    if show_sweep and len(prices) >= 10:
        # Liquidity sweep: brief break of recent high/low followed by reversal
        recent_high = max(prices[-10:-2]) if len(prices) >= 10 else max(prices)
        if prices[-1] > recent_high and prices[-2] <= recent_high:
            sweeps.append({
                "type": "buy_side_liquidity_sweep",
                "timestamp": timestamps[-1],
                "level": recent_high,
            })

    return {
        "fvgs": fvgs if show_fvg else [],
        "msses": msses if show_mss else [],
        "sweeps": sweeps if show_sweep else [],
    }
