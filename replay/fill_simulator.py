"""
openalgo/replay/fill_simulator.py
─────────────────────────────────
Walks recorded order book depth levels at a given timestamp to compute
realistic average fill price and slippage for simulated orders.
"""

from datetime import datetime

import pandas as pd

from replay.dal import get_state
from replay.storage import MAX_DEPTH_LEVELS


def _is_positive(value) -> bool:
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _book_levels(state: dict, side: str) -> list[tuple[float, float]]:
    """Extract (price, qty) pairs for one side, best level first."""
    levels = []
    for i in range(1, MAX_DEPTH_LEVELS + 1):
        price = state.get(f"{side}_{i}")
        qty = state.get(f"{side}_qty_{i}")
        if _is_positive(price) and _is_positive(qty):
            levels.append((float(price), float(qty)))
    return levels


def simulate_order_fill(
    symbol: str,
    timestamp: datetime,
    action: str,
    quantity: float,
    price_type: str = "MARKET",
    limit_price: float = 0.0,
) -> dict:
    state = get_state(symbol, timestamp)
    if not state:
        return {
            "status": "rejected",
            "reason": "no_market_data",
            "fill_price": 0.0,
            "filled_qty": 0.0,
            "depth_used": False,
        }

    action = action.upper()
    if action not in ("BUY", "SELL"):
        return {
            "status": "rejected",
            "reason": "invalid_action",
            "fill_price": 0.0,
            "filled_qty": 0.0,
            "depth_used": False,
        }

    # BUY lifts the asks, SELL hits the bids.
    levels = _book_levels(state, "ask" if action == "BUY" else "bid")

    if not levels:
        # No recorded book at this instant. Fall back to LTP, but flag it —
        # a caller that silently trusts this is backtesting against a price
        # that had no liquidity behind it.
        ltp = state.get("ltp")
        ltp_val = float(ltp) if _is_positive(ltp) else 0.0
        if ltp_val <= 0:
            return {
                "status": "rejected",
                "reason": "no_market_data",
                "fill_price": 0.0,
                "filled_qty": 0.0,
                "depth_used": False,
            }
        if price_type == "LIMIT" and (
            (action == "BUY" and ltp_val > limit_price)
            or (action == "SELL" and ltp_val < limit_price)
        ):
            return {
                "status": "unfilled",
                "reason": "limit_not_marketable",
                "fill_price": 0.0,
                "filled_qty": 0.0,
                "depth_used": False,
            }
        return {
            "status": "filled",
            "fill_price": ltp_val,
            "filled_qty": float(quantity),
            "unfilled_qty": 0.0,
            "slippage": 0.0,
            "depth_used": False,
        }

    remaining_qty = float(quantity)
    filled_qty = 0.0
    total_cost = 0.0

    for price, avail_qty in levels:
        if price_type == "LIMIT":
            # Levels are ordered best-first, so the first non-marketable level
            # ends the walk.
            if action == "BUY" and price > limit_price:
                break
            if action == "SELL" and price < limit_price:
                break
        take_qty = min(remaining_qty, avail_qty)
        if take_qty <= 0:
            continue
        total_cost += take_qty * price
        filled_qty += take_qty
        remaining_qty -= take_qty
        if remaining_qty <= 0:
            break

    if filled_qty == 0:
        return {
            "status": "unfilled",
            "reason": "limit_not_marketable" if price_type == "LIMIT" else "no_liquidity",
            "fill_price": 0.0,
            "filled_qty": 0.0,
            "depth_used": True,
        }

    avg_price = total_cost / filled_qty
    best_price = levels[0][0]
    slippage = avg_price - best_price if action == "BUY" else best_price - avg_price

    return {
        "status": "filled" if remaining_qty <= 0 else "partial",
        "fill_price": round(avg_price, 2),
        "filled_qty": filled_qty,
        "unfilled_qty": max(remaining_qty, 0.0),
        "slippage": round(slippage, 2),
        "depth_used": True,
    }
