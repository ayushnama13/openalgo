"""
openalgo/replay/fill_simulator.py
─────────────────────────────────
Walks recorded order book depth levels at a given timestamp to compute
realistic average fill price and slippage for simulated orders.
"""

from datetime import datetime

import pandas as pd

from replay.dal import get_state


def simulate_order_fill(symbol: str, timestamp: datetime, action: str, quantity: float, price_type: str = "MARKET", limit_price: float = 0.0) -> dict:
    state = get_state(symbol, timestamp)
    if not state:
        return {"status": "rejected", "reason": "no_market_data", "fill_price": 0.0, "filled_qty": 0.0}

    action = action.upper()
    remaining_qty = float(quantity)
    filled_qty = 0.0
    total_cost = 0.0

    if action == "BUY":
        # Walk ask levels (ask_1 to ask_20, ask_qty_1 to ask_qty_20)
        levels = []
        for i in range(1, 21):
            p = state.get(f"ask_{i}")
            q = state.get(f"ask_qty_{i}")
            if p is not None and not pd.isna(p) and float(p) > 0 and q is not None and not pd.isna(q) and float(q) > 0:
                levels.append((float(p), float(q)))

        if not levels:
            ltp = state.get("ltp", 0.0)
            ltp_val = float(ltp) if ltp is not None and not pd.isna(ltp) else 0.0
            return {"status": "filled", "fill_price": ltp_val, "filled_qty": remaining_qty, "slippage": 0.0}

        for p, q in levels:
            if price_type == "LIMIT" and p > limit_price:
                break
            take_q = min(remaining_qty, q)
            if take_q <= 0:
                continue
            total_cost += take_q * p
            filled_qty += take_q
            remaining_qty -= take_q
            if remaining_qty <= 0:
                break

        if filled_qty == 0:
            return {"status": "unfilled", "fill_price": 0.0, "filled_qty": 0.0}

        avg_price = total_cost / filled_qty
        base_price = levels[0][0]
        slippage = avg_price - base_price
        return {
            "status": "filled",
            "fill_price": round(avg_price, 2),
            "filled_qty": filled_qty,
            "unfilled_qty": remaining_qty,
            "slippage": round(slippage, 2),
        }

    elif action == "SELL":
        # Walk bid levels (bid_1 to bid_20, bid_qty_1 to bid_qty_20)
        levels = []
        for i in range(1, 21):
            p = state.get(f"bid_{i}")
            q = state.get(f"bid_qty_{i}")
            if p is not None and not pd.isna(p) and float(p) > 0 and q is not None and not pd.isna(q) and float(q) > 0:
                levels.append((float(p), float(q)))

        if not levels:
            ltp = state.get("ltp", 0.0)
            ltp_val = float(ltp) if ltp is not None and not pd.isna(ltp) else 0.0
            return {"status": "filled", "fill_price": ltp_val, "filled_qty": remaining_qty, "slippage": 0.0}

        for p, q in levels:
            if price_type == "LIMIT" and p < limit_price:
                break
            take_q = min(remaining_qty, q)
            if take_q <= 0:
                continue
            total_cost += take_q * p
            filled_qty += take_q
            remaining_qty -= take_q
            if remaining_qty <= 0:
                break

        if filled_qty == 0:
            return {"status": "unfilled", "fill_price": 0.0, "filled_qty": 0.0}

        avg_price = total_cost / filled_qty
        base_price = levels[0][0]
        slippage = base_price - avg_price
        return {
            "status": "filled",
            "fill_price": round(avg_price, 2),
            "filled_qty": filled_qty,
            "unfilled_qty": remaining_qty,
            "slippage": round(slippage, 2),
        }

    return {"status": "rejected", "reason": "invalid_action", "fill_price": 0.0, "filled_qty": 0.0}
