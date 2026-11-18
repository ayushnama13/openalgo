import pandas as pd
import pytest

from services.backtest_engine import Context, Engine, EngineConfig, Strategy


def _bars(prices=(100, 100, 100), volumes=(100, 100, 100)):
    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-02 10:00", periods=len(prices), freq="min"),
        "open": prices,
        "high": [p + 2 for p in prices],
        "low": [p - 2 for p in prices],
        "close": prices,
        "volume": volumes,
    })


class _OrderOnFirstBar(Strategy):
    def __init__(self, **order):
        self.order_args = order

    def on_bar(self, ctx):
        if ctx.bar_index == 0:
            ctx.buy("NSE:TEST", **self.order_args)


@pytest.mark.parametrize("field", ["qty", "pct"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0, -2])
def test_context_rejects_invalid_order_size(field, value):
    engine = Engine({"NSE:TEST": _bars()}, EngineConfig(), Strategy())
    ctx = Context(engine)
    with pytest.raises(ValueError):
        ctx.order("NSE:TEST", "BUY", **{field: value})


@pytest.mark.parametrize(
    "field", ["limit_price", "trigger_price", "sl_pct", "tp_pct", "sl_price", "tp_price", "trail_pct"]
)
def test_context_rejects_nonfinite_price_and_bracket_fields(field):
    engine = Engine({"NSE:TEST": _bars()}, EngineConfig(), Strategy())
    with pytest.raises(ValueError):
        Context(engine).order("NSE:TEST", "BUY", **{field: float("nan")})


def test_close_all_sentinel_still_works():
    class CloseStrategy(Strategy):
        def on_bar(self, ctx):
            if ctx.bar_index == 0:
                ctx.buy("NSE:TEST", qty=2)
            elif ctx.bar_index == 1:
                ctx.close("NSE:TEST")

    result = Engine({"NSE:TEST": _bars()}, EngineConfig(product_type="CNC"), CloseStrategy()).run()
    assert result.trades[0]["quantity"] == 2


@pytest.mark.parametrize("order_args", [{"qty": 0.5}, {}])
def test_resolved_fractional_quantity_is_floored(order_args):
    cfg = EngineConfig(product_type="CNC", sizing_type="fixed_qty", sizing_value=0.5)
    engine = Engine({"NSE:TEST": _bars()}, cfg, _OrderOnFirstBar(**order_args))
    result = engine.run()
    assert not result.trades
    assert not engine.positions


def test_limit_order_slippage_respects_limit():
    buy = Engine({"NSE:TEST": _bars((100, 100, 100))},
                 EngineConfig(product_type="CNC", slippage_bps=500),
                 _OrderOnFirstBar(qty=1, price_type="LIMIT", limit_price=100))
    buy.run()
    assert buy.trades[0]["entry_price"] <= 100

    class SellLimit(Strategy):
        def on_bar(self, ctx):
            if ctx.bar_index == 0:
                ctx.sell("NSE:TEST", qty=1, price_type="LIMIT", limit_price=100)

    sell = Engine({"NSE:TEST": _bars()}, EngineConfig(product_type="CNC", allow_short=True,
                                                       slippage_bps=500), SellLimit())
    sell.run()
    assert sell.trades[0]["entry_price"] >= 100


def test_partial_fills_charge_order_basis_once():
    class RecordingSchedule:
        def __init__(self):
            self.orders = []

        def charge(self, buy_value, sell_value, orders):
            self.orders.append(orders)
            return float(orders)

    class PartialOrder(Strategy):
        def on_bar(self, ctx):
            if ctx.bar_index == 0:
                ctx.buy("NSE:TEST", qty=25)

    engine = Engine({"NSE:TEST": _bars(volumes=(100, 100, 100))},
                    EngineConfig(product_type="CNC", max_volume_pct=10), PartialOrder())
    schedule = RecordingSchedule()
    engine.schedules["NSE:TEST"] = schedule
    engine.run()
    assert schedule.orders[:2] == [1, 0]
    assert schedule.orders[-1] == 1
