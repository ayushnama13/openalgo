# services/backtest_fill_engine.py

"""
Shared Fill Simulation Engine.
Extracts order matching logic from sandbox/execution_engine.py:345-473
and integrates cost models (Costs from portfolio.engine) and exchange MIS square-off rules.
"""

from portfolio.engine import Costs
from utils.logging import get_logger

logger = get_logger(__name__)

# Exchange-aligned MIS auto square-off times (HH:MM string / minute of day)
MIS_SQUARE_OFF_TIMES = {
    "NSE": "15:15",
    "BSE": "15:15",
    "NFO": "15:15",
    "BFO": "15:15",
    "CDS": "16:45",
    "BCD": "16:45",
    "MCX": "23:30",
    "NCDEX": "17:00",
}


def get_square_off_time(exchange: str) -> str:
    """Get MIS square-off time string for exchange."""
    return MIS_SQUARE_OFF_TIMES.get(exchange.upper(), "15:15")


def is_mis_square_off_bar(timestamp_str: str, exchange: str) -> bool:
    """
    Check if a given bar timestamp string (e.g., '2024-03-28 15:15:00') hits
    or passes the MIS square-off time for the specified exchange.
    """
    try:
        sq_time_str = get_square_off_time(exchange)
        sq_hour, sq_minute = map(int, sq_time_str.split(":"))

        if " " in timestamp_str:
            time_part = timestamp_str.split(" ")[1]
        elif "T" in timestamp_str:
            time_part = timestamp_str.split("T")[1]
        else:
            time_part = timestamp_str

        time_components = time_part.split(":")
        bar_hour = int(time_components[0])
        bar_minute = int(time_components[1])

        bar_minutes_total = bar_hour * 60 + bar_minute
        sq_minutes_total = sq_hour * 60 + sq_minute

        return bar_minutes_total >= sq_minutes_total
    except Exception:
        return False


def simulate_order_fill(
    action: str,  # "BUY" or "SELL"
    price_type: str,  # "MARKET", "LIMIT", "SL", "SL-M"
    limit_price: float,
    trigger_price: float,
    bar_open: float,
    bar_high: float,
    bar_low: float,
    bar_close: float,
    volume: float = 0.0,
    slippage_bps: float = 0.0,
    brokerage_bps: float = 0.0,
    quantity: float = 1.0,
) -> tuple[bool, float, float]:
    """
    Simulates fill matching against a single OHLCV bar using Sandbox execution engine rules.

    `quantity` scales the brokerage cost to the traded value (price * qty), not
    just the per-share price — a 100-share trade costs 100x a 1-share trade.

    Returns:
        (filled, raw_execution_price, total_cost_amount)
    """
    action = action.upper()
    price_type = price_type.upper()

    should_execute = False
    raw_fill_price = 0.0

    if price_type == "MARKET":
        # MARKET orders fill at bar open (or close depending on bar simulation model)
        should_execute = True
        raw_fill_price = bar_open if bar_open > 0 else bar_close

    elif price_type == "LIMIT":
        # Limit BUY: Execute if bar_low <= limit_price. Fill at limit_price or open if gapped down.
        # Limit SELL: Execute if bar_high >= limit_price. Fill at limit_price or open if gapped up.
        if action == "BUY":
            if bar_low <= limit_price:
                should_execute = True
                raw_fill_price = min(bar_open, limit_price) if bar_open <= limit_price else limit_price
        elif action == "SELL":
            if bar_high >= limit_price:
                should_execute = True
                raw_fill_price = max(bar_open, limit_price) if bar_open >= limit_price else limit_price

    elif price_type == "SL":
        # Stop-Loss Limit Order:
        # BUY: Triggers when price >= trigger_price, fills if limit_price satisfiable
        # SELL: Triggers when price <= trigger_price, fills if limit_price satisfiable
        if action == "BUY":
            if bar_high >= trigger_price and bar_low <= limit_price:
                should_execute = True
                raw_fill_price = limit_price
        elif action == "SELL":
            if bar_low <= trigger_price and bar_high >= limit_price:
                should_execute = True
                raw_fill_price = limit_price

    elif price_type == "SL-M":
        # Stop-Loss Market Order:
        # BUY: Triggers when price >= trigger_price
        # SELL: Triggers when price <= trigger_price
        if action == "BUY":
            if bar_high >= trigger_price:
                should_execute = True
                raw_fill_price = max(trigger_price, bar_open)
        elif action == "SELL":
            if bar_low <= trigger_price:
                should_execute = True
                raw_fill_price = min(trigger_price, bar_open)

    if not should_execute or raw_fill_price <= 0:
        return False, 0.0, 0.0

    # Apply Slippage Model (bps) - shifts the fill price; BUY pays higher, SELL receives lower
    slippage_adj = raw_fill_price * (slippage_bps / 10000.0)

    if action == "BUY":
        effective_price = raw_fill_price + slippage_adj
    else:  # SELL
        effective_price = max(0.0, raw_fill_price - slippage_adj)

    # Brokerage / tax cost amount - reuses portfolio.engine.Costs (bps fraction of traded value)
    cost_model = Costs(bps=brokerage_bps)
    brokerage_cost = effective_price * quantity * cost_model.total

    return True, effective_price, brokerage_cost
