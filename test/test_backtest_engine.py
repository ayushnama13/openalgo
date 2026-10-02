"""Unit tests for the event-driven multi-symbol engine (in-process, no sandbox)."""

import numpy as np
import pandas as pd
import pytest

from services.backtest_engine import (
    Engine,
    EngineConfig,
    SignalStrategy,
    Strategy,
)


def make_df(opens, highs=None, lows=None, closes=None, start="2024-06-03 09:15:00", freq="5min"):
    n = len(opens)
    closes = closes if closes is not None else [o + 0.5 for o in opens]
    highs = highs if highs is not None else [max(o, c) + 1 for o, c in zip(opens, closes, strict=True)]
    lows = lows if lows is not None else [min(o, c) - 1 for o, c in zip(opens, closes, strict=True)]
    ts = pd.date_range(start, periods=n, freq=freq).strftime("%Y-%m-%d %H:%M:%S")
    return pd.DataFrame(
        {"timestamp": ts, "open": opens, "high": highs, "low": lows, "close": closes,
         "volume": [1000] * n}
    )


def run(bars, strategy, **cfg):
    cfg.setdefault("product_type", "NRML")
    e = Engine(bars, EngineConfig(**cfg), strategy)
    return e.run()


class Scripted(Strategy):
    """Calls scripted actions {bar_index: callable(ctx)}."""

    def __init__(self, script):
        self.script = script

    def on_bar(self, ctx):
        fn = self.script.get(ctx.bar_index)
        if fn:
            fn(ctx)


def test_order_fills_next_bar_open():
    df = make_df([100 + i for i in range(10)])
    res = run({"NSE:A": df}, Scripted({2: lambda c: c.buy("NSE:A"), 5: lambda c: c.close("NSE:A")}))
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t["entry_price"] == pytest.approx(103.0)
    assert t["exit_price"] == pytest.approx(106.0)
    assert t["holding_period_bars"] == 3
    assert t["action"] == "BUY"


def test_multi_symbol_positions_and_max_positions():
    a = make_df([100 + i for i in range(10)])
    b = make_df([50 + i for i in range(10)])
    c = make_df([20 + i for i in range(10)])
    bars = {"NSE:A": a, "NSE:B": b, "NSE:C": c}

    def enter_all(ctx):
        for s in ctx.symbols:
            ctx.buy(s, qty=1)

    res = run(bars, Scripted({1: enter_all}), max_positions=2)
    # only 2 of 3 entries accepted; all closed at end of backtest
    assert len(res.trades) == 2
    assert res.rejections.get("max_positions") == 1
    assert {t["symbol"] for t in res.trades} == {"A", "B"}
    assert any("max_positions" in n for n in res.notes)


def test_equity_matches_trade_pnl():
    df = make_df([100, 101, 103, 102, 108, 107, 105, 110, 111, 112])
    res = run(
        {"NSE:A": df},
        Scripted({1: lambda c: c.buy("NSE:A", qty=10), 6: lambda c: c.close("NSE:A")}),
        brokerage_bps=10, slippage_bps=5,
    )
    total_pnl = sum(t["pnl"] for t in res.trades)
    assert res.equity[-1]["equity"] == pytest.approx(100000 + total_pnl, abs=0.05)
    assert res.trades[0]["costs"] > 0  # entry + exit costs both counted


def test_short_pnl_and_cash():
    df = make_df([100, 100, 100, 90, 90, 90], highs=[101] * 6, lows=[89] * 6)
    res = run(
        {"NSE:A": df},
        Scripted({0: lambda c: c.sell("NSE:A", qty=10), 3: lambda c: c.close("NSE:A")}),
    )
    t = res.trades[0]
    assert t["action"] == "SELL"
    assert t["pnl"] == pytest.approx((100 - 90) * 10)


def test_stop_loss_fills_at_stop_price_not_open():
    opens = [100, 100, 100, 100, 100]
    lows = [99, 99, 95, 99, 99]  # bar 2 dips through a 3% stop (97)
    df = make_df(opens, highs=[101] * 5, lows=lows, closes=[100] * 5)
    res = run({"NSE:A": df}, Scripted({0: lambda c: c.buy("NSE:A", qty=10, sl_pct=3)}))
    t = res.trades[0]
    assert t["exit_reason"] == "Stop Loss"
    assert t["exit_price"] == pytest.approx(97.0)


def test_stop_gap_fills_at_open():
    df = make_df([100, 100, 90, 90], highs=[101, 101, 91, 91], lows=[99, 99, 89, 89],
                 closes=[100, 100, 90, 90])
    res = run({"NSE:A": df}, Scripted({0: lambda c: c.buy("NSE:A", qty=10, sl_pct=3)}))
    assert res.trades[0]["exit_price"] == pytest.approx(90.0)  # gapped through stop


def test_stop_beats_target_when_both_touched_in_one_bar():
    df = make_df([100, 100, 100, 100], highs=[101, 101, 120, 101], lows=[99, 99, 80, 99],
                 closes=[100] * 4)
    res = run(
        {"NSE:A": df},
        Scripted({0: lambda c: c.buy("NSE:A", qty=1, sl_pct=5, tp_pct=10)}),
    )
    assert res.trades[0]["exit_reason"] == "Stop Loss"


def test_take_profit():
    df = make_df([100, 100, 100, 100], highs=[101, 101, 112, 101], lows=[99] * 4, closes=[100] * 4)
    res = run({"NSE:A": df}, Scripted({0: lambda c: c.buy("NSE:A", qty=1, tp_pct=10)}))
    assert res.trades[0]["exit_reason"] == "Take Profit"
    assert res.trades[0]["exit_price"] == pytest.approx(110.0)


def test_trailing_stop():
    highs = [101, 101, 111, 121, 121, 121]
    lows = [99, 99, 100, 112, 100, 99]
    df = make_df([100, 100, 105, 115, 118, 100], highs=highs, lows=lows,
                 closes=[100, 100, 110, 120, 105, 100])
    res = run({"NSE:A": df}, Scripted({0: lambda c: c.buy("NSE:A", qty=1, trail_pct=10)}))
    t = res.trades[0]
    assert t["exit_reason"] == "Trailing Stop"
    assert t["exit_price"] == pytest.approx(121 * 0.9)  # high watermark 121 -> stop 108.9


def test_scale_in_and_scale_out_records_partial_trades():
    df = make_df([100, 100, 110, 110, 120, 120, 120])
    res = run(
        {"NSE:A": df},
        Scripted({
            0: lambda c: c.buy("NSE:A", qty=10),
            1: lambda c: c.buy("NSE:A", qty=10),
            3: lambda c: c.sell("NSE:A", qty=5),
            4: lambda c: c.close("NSE:A"),
        }),
    )
    # avg entry = 105, partial exit 5 @ 120, final exit 15 @ 120
    assert [t["quantity"] for t in res.trades] == [5, 15]
    assert all(t["entry_price"] == pytest.approx(105.0) for t in res.trades)
    assert sum(t["pnl"] for t in res.trades) == pytest.approx(20 * 15)


def test_flip_long_to_short_in_one_order():
    df = make_df([100, 100, 100, 100, 90, 90])
    res = run(
        {"NSE:A": df},
        Scripted({0: lambda c: c.buy("NSE:A", qty=10), 2: lambda c: c.sell("NSE:A", qty=20)}),
    )
    assert res.trades[0]["action"] == "BUY"
    assert res.trades[0]["quantity"] == 10
    assert res.trades[1]["action"] == "SELL"  # the flipped short, closed at the end
    assert res.trades[1]["pnl"] == pytest.approx(9.5 * 10, abs=0.05)  # exits at last close 90.5


def test_limit_order_waits_until_touched():
    df = make_df([100, 100, 100, 100], highs=[101] * 4, lows=[99, 99, 94, 99], closes=[100] * 4)
    res = run(
        {"NSE:A": df},
        Scripted({0: lambda c: c.buy("NSE:A", qty=1, price_type="LIMIT", limit_price=95)}),
    )
    assert res.trades[0]["entry_price"] == pytest.approx(95.0)
    assert res.trades[0]["entry_time"].endswith("09:25:00")


def test_cash_not_overspent():
    df = make_df([1000] * 6)
    res = run({"NSE:A": df}, Scripted({0: lambda c: c.buy("NSE:A", qty=500)}), initial_capital=10000)
    assert res.trades == []
    assert res.rejections["insufficient_cash"] == 1


def test_daily_loss_limit_halts_the_day():
    opens = [100, 100, 90, 90, 90, 90]
    df = make_df(opens, highs=[101] * 6, lows=[89] * 6, closes=opens)
    res = run(
        {"NSE:A": df},
        Scripted({0: lambda c: c.buy("NSE:A", qty=100), 3: lambda c: c.buy("NSE:A", qty=1)}),
        daily_loss_limit=500,
    )
    assert res.trades[0]["exit_reason"] == "Daily Loss Limit"
    assert len(res.trades) == 1  # no new entry after the halt, same day


def test_mis_square_off():
    ts = "2024-06-03 15:05:00"
    df = make_df([100] * 8, start=ts)
    res = run(
        {"NSE:A": df},
        Scripted({0: lambda c: c.buy("NSE:A", qty=1)}),
        product_type="MIS",
    )
    assert res.trades[0]["exit_reason"] == "MIS Auto Squareoff"


def test_sparse_symbol_uses_last_close_for_mark_to_market():
    a = make_df([100] * 6)
    b = make_df([50, 50, 50], closes=[50, 50, 50])
    res = run(
        {"NSE:A": a, "NSE:B": b},
        Scripted({0: lambda c: c.buy("NSE:B", qty=10)}),
    )
    assert len(res.equity) == 6
    assert res.trades[0]["symbol"] == "B"


def test_signal_strategy_matches_semantics():
    df = make_df([100 + i for i in range(12)])
    sig = np.zeros(12, dtype=int)
    sig[3], sig[7] = 1, -1
    res = run({"NSE:A": df}, SignalStrategy({"NSE:A": sig}), sizing_type="fixed_qty", sizing_value=1)
    t = res.trades[0]
    assert t["entry_price"] == pytest.approx(104.0)  # signal bar 3 -> bar 4 open
    assert t["exit_price"] == pytest.approx(108.0)


def test_history_has_no_lookahead():
    df = make_df([100 + i for i in range(8)])
    seen = []

    class S(Strategy):
        def on_bar(self, ctx):
            h = ctx.history("NSE:A")
            seen.append((ctx.bar_index, len(h), h["close"].iloc[-1]))

    run({"NSE:A": df}, S())
    for k, n, last in seen:
        assert n == k + 1
        assert last == df["close"].iloc[k]


def test_mae_mfe_recorded():
    df = make_df([100, 100, 100, 100], highs=[101, 101, 108, 101], lows=[99, 99, 96, 99],
                 closes=[100] * 4)
    res = run({"NSE:A": df}, Scripted({0: lambda c: c.buy("NSE:A", qty=1), 2: lambda c: c.close("NSE:A")}))
    t = res.trades[0]
    assert t["mfe_pct"] == pytest.approx(8.0)
    assert t["mae_pct"] == pytest.approx(-4.0)


def test_cost_preset_charges_statutory_costs():
    df = make_df([100] * 6)
    res = run(
        {"NSE:A": df},
        Scripted({0: lambda c: c.buy("NSE:A", qty=100), 3: lambda c: c.close("NSE:A")}),
        cost_preset="india_intraday_nse",
    )
    t = res.trades[0]
    # brokerage 2 x Rs 3 (0.03% of 10k) + STT 0.025% of sell (2.5) + others: well above 0, below 1% of value
    assert 5 < t["costs"] < 100
    assert res.equity[-1]["equity"] == pytest.approx(100000 + t["pnl"], abs=0.05)


def test_auto_preset_picks_by_product():
    df = make_df([100] * 6)
    script = {0: lambda c: c.buy("NSE:A", qty=100), 3: lambda c: c.close("NSE:A")}
    cnc = run({"NSE:A": df}, Scripted(script), cost_preset="auto", product_type="CNC")
    mis = run({"NSE:A": df}, Scripted(script), cost_preset="auto", product_type="MIS")
    # delivery charges 0.1% STT on both legs, so it costs more than intraday
    assert cnc.trades[0]["costs"] > mis.trades[0]["costs"]


def test_volume_cap_creates_partial_fills():
    df = make_df([100] * 8)
    df["volume"] = 100
    res = run(
        {"NSE:A": df},
        Scripted({0: lambda c: c.buy("NSE:A", qty=100)}),
        max_volume_pct=30, initial_capital=1_000_000,
    )
    # 30 shares per bar -> position builds over several bars, then closes at the end
    assert sum(t["quantity"] for t in res.trades) == pytest.approx(100)


def test_spread_and_impact_worsen_fills():
    df = make_df([100] * 6)
    script = {0: lambda c: c.buy("NSE:A", qty=10), 3: lambda c: c.close("NSE:A")}
    base = run({"NSE:A": df}, Scripted(script))
    wide = run({"NSE:A": df}, Scripted(script), spread_bps=20, impact_bps=50)
    assert wide.trades[0]["pnl"] < base.trades[0]["pnl"]


# ---- F&O: lots, margin, futures accounting, contract end ----
FUT = "NFO:NIFTYFUT"
FUT_INFO = {FUT: {"lot_size": 75, "kind": "FUT"}}


def test_futures_do_not_pay_notional_and_settle_pnl():
    df = make_df([20000, 20000, 20100, 20100, 20100, 20100])
    res = run(
        {FUT: df},
        Scripted({0: lambda c: c.buy(FUT, qty=75), 3: lambda c: c.close(FUT)}),
        instruments=FUT_INFO, initial_capital=500000,
    )
    t = res.trades[0]
    assert t["quantity"] == 75
    assert t["pnl"] == pytest.approx(75 * 100)
    # equity never dipped by the 15 lakh notional: cash stayed ~ initial until the close
    assert min(r["equity"] for r in res.equity) > 490000
    assert res.equity[-1]["equity"] == pytest.approx(500000 + t["pnl"], abs=0.05)


def test_fixed_qty_means_lots_for_contracts():
    df = make_df([20000] * 6)
    res = run({FUT: df}, Scripted({0: lambda c: c.buy(FUT)}), instruments=FUT_INFO,
              sizing_type="fixed_qty", sizing_value=2, initial_capital=1_000_000)
    assert res.trades[0]["quantity"] == 150  # 2 lots x 75


def test_explicit_qty_rounds_down_to_whole_lots():
    df = make_df([20000] * 6)
    res = run({FUT: df}, Scripted({0: lambda c: c.buy(FUT, qty=200)}), instruments=FUT_INFO,
              initial_capital=1_000_000)
    assert res.trades[0]["quantity"] == 150  # 200 -> 2 lots


def test_margin_limits_futures_position():
    df = make_df([20000] * 6)
    # one lot needs 75 * 20000 * 12% = 1.8 lakh; 2 lakh capital cannot carry two lots
    res = run({FUT: df}, Scripted({0: lambda c: c.buy(FUT, qty=150)}), instruments=FUT_INFO,
              initial_capital=200000)
    assert res.trades == []
    assert res.rejections["insufficient_margin"] == 1


def test_short_option_blocks_strike_based_margin_but_long_option_pays_premium():
    opt = "NFO:NIFTY24500CE"
    info = {opt: {"lot_size": 75, "kind": "CE", "strike": 24500}}
    df = make_df([100] * 6)
    # short 1 lot: margin = 75 * 24500 * 15% = 2.76 lakh
    short_ok = run({opt: df}, Scripted({0: lambda c: c.sell(opt, qty=75)}), instruments=info,
                   initial_capital=300000)
    assert short_ok.trades and short_ok.trades[0]["action"] == "SELL"
    short_no = run({opt: df}, Scripted({0: lambda c: c.sell(opt, qty=75)}), instruments=info,
                   initial_capital=200000)
    assert short_no.trades == [] and short_no.rejections["insufficient_margin"] == 1
    long_ok = run({opt: df}, Scripted({0: lambda c: c.buy(opt, qty=75)}), instruments=info,
                  initial_capital=10000)
    assert long_ok.trades  # premium 7,500 fits in 10,000 cash


def test_contract_expiry_settles_at_last_bar_of_that_contract():
    short_life = make_df([100] * 4)            # contract ends after 4 bars
    long_life = make_df([50] * 10)             # underlying keeps trading
    res = run(
        {"NSE:EXP": short_life, "NSE:LIVE": long_life},
        Scripted({0: lambda c: c.buy("NSE:EXP", qty=1)}),
        max_positions=2,
    )
    t = res.trades[0]
    assert t["exit_reason"] == "Contract Expiry / Last Bar"
    assert t["exit_time"] == short_life["timestamp"].iloc[-1]


def test_auto_cost_preset_uses_contract_kind():
    df = make_df([20000, 20000, 20000, 20000])
    script = {0: lambda c: c.buy(FUT, qty=75), 2: lambda c: c.close(FUT)}
    fut = run({FUT: df}, Scripted(script), instruments=FUT_INFO, cost_preset="auto",
              product_type="NRML", initial_capital=500000)
    eq_df = make_df([20000, 20000, 20000, 20000])
    eq = run({"NSE:EQ1": eq_df},
             Scripted({0: lambda c: c.buy("NSE:EQ1", qty=75), 2: lambda c: c.close("NSE:EQ1")}),
             cost_preset="auto", product_type="NRML", initial_capital=5_000_000)
    # futures STT (0.02% sell) is far below delivery STT (0.1% both legs)
    assert fut.trades[0]["costs"] < eq.trades[0]["costs"]


def test_mis_leverage_widens_buying_power():
    df = make_df([1000] * 6)
    script = {0: lambda c: c.buy("NSE:A", qty=300)}  # Rs 3 lakh on 1 lakh capital
    no = run({"NSE:A": df}, Scripted(script), product_type="MIS")
    yes = run({"NSE:A": df}, Scripted(script), product_type="MIS", intraday_leverage=5)
    assert no.trades == [] and yes.trades
