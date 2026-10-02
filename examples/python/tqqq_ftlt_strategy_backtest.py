"""
===============================================================================
    TQQQ FTLT MULTI-ASSET ROTATION & VOLATILITY-SCALED BACKTEST STRATEGY
                            OpenAlgo Platform
===============================================================================

Converted from QuantConnect Lean (QCAlgorithm) to OpenAlgo Python Strategy & Backtest.

Strategy Features:
- Multi-asset universe (13 tickers: BIL, UVXY, TQQQ, TECL, UPRO, SQQQ, SOXL, SPY, SPXL, XLK, DBC, TLT, NVDA)
- Technical Indicators: Wilder's RSI (2, 5, 10, 14), SMA (20, 200), NVDA 1-period ROC & 5-period StDev
- Block Allocations: Weighted combination of BIL Replace and TQQQ FTLT Reddit logic
- Risk & Drawdown Scaling: Volatility-adjusted leverage, hard drawdown stop at 10%
- Backtest Engine: Daily portfolio rebalancing, equity curve tracking, performance metrics & Plotly charts
- OpenAlgo Integration: Compatible with OpenAlgo API (`client.history`) and /python strategy runner
"""

import logging
import math
import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from openalgo import api

# ===============================================================================
# STRATEGY CONFIGURATION & PARAMETERS
# ===============================================================================

API_KEY = os.getenv("OPENALGO_API_KEY", "openalgo-apikey")
API_HOST = os.getenv("HOST_SERVER", "http://127.0.0.1:5000")
WS_URL = os.getenv("WEBSOCKET_URL", "ws://127.0.0.1:8765")
EXCHANGE = os.getenv("OPENALGO_STRATEGY_EXCHANGE", os.getenv("EXCHANGE", "US"))

TICKERS = [
    "BIL", "UVXY", "TQQQ", "TECL", "UPRO", "SQQQ", "SOXL",
    "SPY", "SPXL", "XLK", "DBC", "TLT", "NVDA",
]

BLOCKS = [
    (0.9, 0.1), (0.8, 0.2), (0.7, 0.3), (0.6, 0.4), (0.5, 0.5),
    (0.4, 0.6), (0.3, 0.7), (0.2, 0.8), (0.1, 0.9),
]

BIL_BIAS = 0.4          # Increased cash allocation
MAX_LEVERAGE = 0.55      # Reduced leverage for lower drawdown
VOL_LOOKBACK = 20        # Days to measure volatility
DD_STOP_THRESHOLD = 0.10 # Hard stop threshold for drawdown
INITIAL_CAPITAL = 100000.0
FEES_BPS = 5.0           # 5 bps transaction fee

# ===============================================================================
# INDICATOR FUNCTIONS
# ===============================================================================

def calculate_wilders_rsi(series: pd.Series, period: int) -> pd.Series:
    """Calculate Wilder's Relative Strength Index (RSI)."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1.0/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0/period, min_periods=period, adjust=False).mean()

    rs = avg_gain / (avg_loss + 1e-10)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi

def calculate_indicators(price_df: pd.DataFrame) -> dict:
    """
    Calculate required technical indicators for all tickers across all time periods.
    Returns nested dictionary: indicators[ticker][metric_name] = pd.Series
    """
    indicators = {}

    for ticker in TICKERS:
        close = price_df[ticker]
        ind = {
            "close": close,
            "rsi_2": calculate_wilders_rsi(close, 2),
            "rsi_5": calculate_wilders_rsi(close, 5),
            "rsi_10": calculate_wilders_rsi(close, 10),
            "rsi_14": calculate_wilders_rsi(close, 14),
            "ma_20": close.rolling(20, min_periods=20).mean(),
            "ma_200": close.rolling(200, min_periods=200).mean(),
        }

        if ticker == "NVDA":
            roc1 = close.pct_change(1)
            # 5-period standard deviation of ROC (scaled to percentage format)
            ind["stdev5"] = roc1.rolling(5, min_periods=5).std(ddof=1) * 100.0
        else:
            ind["stdev5"] = pd.Series(0.0, index=close.index)

        indicators[ticker] = ind

    # SPY 20-day annualized volatility
    spy_returns = price_df["SPY"].pct_change(1)
    indicators["SPY"]["vol_20d"] = spy_returns.rolling(VOL_LOOKBACK, min_periods=VOL_LOOKBACK).std(ddof=0) * np.sqrt(252)

    return indicators

# ===============================================================================
# CORE STRATEGY SELECTION & ALLOCATION LOGIC
# ===============================================================================

class TQQQFTLTStrategy:
    """
    TQQQ FTLT Multi-Asset Strategy logic converted from QuantConnect QCAlgorithm.
    """
    def __init__(self, tickers=TICKERS, blocks=BLOCKS):
        self.tickers = tickers
        self.blocks = blocks

    def bil_replace(self, idx: int, indicators: dict) -> str:
        """Determines whether to allocate to SOXL or BIL based on NVDA volatility vs RSI."""
        nvda_stdev5 = indicators["NVDA"]["stdev5"].iloc[idx]
        nvda_rsi2 = indicators["NVDA"]["rsi_2"].iloc[idx]

        if pd.isna(nvda_stdev5) or pd.isna(nvda_rsi2):
            return "BIL"

        return "SOXL" if nvda_stdev5 > nvda_rsi2 else "BIL"

    def tqqq_ftlt_reddit(self, idx: int, indicators: dict) -> str:
        """Core TQQQ FTLT strategy decision tree based on regime, RSI, and MA crossovers."""
        spy_close = indicators["SPY"]["close"].iloc[idx]
        spy_ma200 = indicators["SPY"]["ma_200"].iloc[idx]

        if pd.isna(spy_ma200):
            return "BIL"

        spy_above_ma200 = spy_close > spy_ma200

        if spy_above_ma200:
            tqqq_rsi10 = indicators["TQQQ"]["rsi_10"].iloc[idx]
            if tqqq_rsi10 > 79:
                return "UVXY"

            spxl_rsi10 = indicators["SPXL"]["rsi_10"].iloc[idx]
            if spxl_rsi10 > 80:
                return "UVXY"

            xlk_rsi10 = indicators["XLK"]["rsi_10"].iloc[idx]
            dbc_rsi10 = indicators["DBC"]["rsi_10"].iloc[idx]
            if xlk_rsi10 > dbc_rsi10:
                return "TQQQ"
            return "BIL"

        # Below MA200 regime
        tqqq_rsi10 = indicators["TQQQ"]["rsi_10"].iloc[idx]
        if tqqq_rsi10 < 31:
            return "TECL"

        spy_rsi10 = indicators["SPY"]["rsi_10"].iloc[idx]
        if spy_rsi10 < 30:
            return "UPRO"

        tqqq_close = indicators["TQQQ"]["close"].iloc[idx]
        tqqq_ma20 = indicators["TQQQ"]["ma_20"].iloc[idx]

        if not pd.isna(tqqq_ma20) and tqqq_close < tqqq_ma20:
            sqqq_rsi10 = indicators["SQQQ"]["rsi_10"].iloc[idx]
            tlt_rsi10 = indicators["TLT"]["rsi_10"].iloc[idx]
            xlk_rsi10 = indicators["XLK"]["rsi_10"].iloc[idx]
            dbc_rsi10 = indicators["DBC"]["rsi_10"].iloc[idx]

            if sqqq_rsi10 > tlt_rsi10:
                if xlk_rsi10 > dbc_rsi10:
                    return "BIL"
                return "SQQQ"
            return "BIL"

        sqqq_rsi10 = indicators["SQQQ"]["rsi_10"].iloc[idx]
        if sqqq_rsi10 < 31:
            return "SQQQ"

        xlk_rsi10 = indicators["XLK"]["rsi_10"].iloc[idx]
        dbc_rsi10 = indicators["DBC"]["rsi_10"].iloc[idx]
        if xlk_rsi10 > dbc_rsi10:
            dbc_close = indicators["DBC"]["close"].iloc[idx]
            dbc_ma20 = indicators["DBC"]["ma_20"].iloc[idx]
            if not pd.isna(dbc_ma20) and dbc_close < dbc_ma20:
                return "TQQQ"
            return "BIL"

        return "BIL"

    def decide_weights(self, idx: int, indicators: dict) -> dict:
        """Calculates portfolio candidate weights across blocks and sorts by RSI(14)."""
        merged = {}
        bw = 1.0 / len(self.blocks)

        for w_bil, w_ftlt in self.blocks:
            t_bil = self.bil_replace(idx, indicators)
            t_ftlt = self.tqqq_ftlt_reddit(idx, indicators)
            merged[t_bil] = merged.get(t_bil, 0.0) + bw * w_bil
            merged[t_ftlt] = merged.get(t_ftlt, 0.0) + bw * w_ftlt

        candidates = [(t, merged[t]) for t in merged if merged[t] > 0]
        if not candidates:
            return {"BIL": 1.0}

        # Sort candidates by RSI(14) descending and take top 2
        sorted_candidates = sorted(
            candidates,
            key=lambda x: indicators[x[0]]["rsi_14"].iloc[idx] if not pd.isna(indicators[x[0]]["rsi_14"].iloc[idx]) else -1.0,
            reverse=True
        )
        top2 = sorted_candidates[:2]

        out = dict.fromkeys(self.tickers, 0.0)
        if top2:
            equal_w = 1.0 / len(top2)
            for t, _ in top2:
                out[t] = equal_w
        else:
            out["BIL"] = 1.0

        return out

    def get_volatility_scalar(self, spy_vol: float, drawdown: float) -> float:
        """Calculate position sizing scalar based on annualized volatility and drawdown."""
        if pd.isna(spy_vol) or spy_vol is None:
            return 0.4

        # Volatility scaling
        if spy_vol > 0.30:
            vol_scalar = 0.3
        elif spy_vol > 0.25:
            vol_scalar = 0.5
        elif spy_vol > 0.20:
            vol_scalar = 0.7
        else:
            vol_scalar = 1.0

        # Drawdown protection
        if drawdown > DD_STOP_THRESHOLD:
            return 0.0  # Hard stop: liquidate risky positions
        elif drawdown > 0.10:
            dd_scalar = 0.4
        elif drawdown > 0.07:
            dd_scalar = 0.6
        else:
            dd_scalar = 1.0

        return min(vol_scalar, dd_scalar)

    def calculate_target_weights(self, idx: int, indicators: dict, current_equity: float, peak_equity: float) -> dict:
        """Main method to compute target portfolio allocation for bar index idx."""
        weights = self.decide_weights(idx, indicators)

        if "BIL" in weights:
            weights["BIL"] += BIL_BIAS
        else:
            weights["BIL"] = BIL_BIAS

        spy_vol = indicators["SPY"]["vol_20d"].iloc[idx]
        drawdown = max(0.0, (peak_equity - current_equity) / peak_equity) if peak_equity > 0 else 0.0

        vol_scalar = self.get_volatility_scalar(spy_vol, drawdown)

        if vol_scalar == 0.0:
            # 100% Cash / BIL allocation during hard drawdown stop
            return {t: (1.0 if t == "BIL" else 0.0) for t in self.tickers}

        risky_assets = ["TQQQ", "TECL", "UPRO", "SQQQ", "SOXL", "UVXY"]
        for t in risky_assets:
            if t in weights:
                weights[t] *= vol_scalar

        reduction = 1.0 - vol_scalar
        weights["BIL"] = weights.get("BIL", 0.0) + reduction

        total = sum(weights.values())
        if total > MAX_LEVERAGE:
            scale = MAX_LEVERAGE / total
            for t in weights:
                weights[t] *= scale
            weights["BIL"] = weights.get("BIL", 0.0) + (1.0 - MAX_LEVERAGE)

        total = sum(weights.values())
        if total <= 0:
            return {t: (1.0 if t == "BIL" else 0.0) for t in self.tickers}

        # Normalize target weights
        return {t: weights.get(t, 0.0) / total for t in self.tickers}

# ===============================================================================
# BACKTESTING ENGINE & METRICS
# ===============================================================================

def run_backtest(price_df: pd.DataFrame, initial_capital: float = INITIAL_CAPITAL, fees_bps: float = FEES_BPS) -> dict:
    """
    Run daily rebalanced portfolio backtest across historical price DataFrame.

    price_df: DataFrame with DatetimeIndex and columns for each ticker.
    """
    indicators = calculate_indicators(price_df)
    strategy = TQQQFTLTStrategy()

    dates = price_df.index
    total_bars = len(dates)

    cash = initial_capital
    holdings_qty = dict.fromkeys(TICKERS, 0.0)
    peak_equity = initial_capital

    equity_curve = []
    trade_history = []
    weight_history = []

    # Warmup period check (200 bars needed for MA200)
    start_idx = 200
    if total_bars <= start_idx:
        raise ValueError(f"Insufficient data rows ({total_bars}) for warmup period (200).")

    for i in range(start_idx, total_bars):
        current_date = dates[i]

        # Calculate current portfolio value based on current prices
        current_prices = {t: price_df[t].iloc[i] for t in TICKERS}
        portfolio_value = cash + sum(holdings_qty[t] * current_prices[t] for t in TICKERS)

        if portfolio_value > peak_equity:
            peak_equity = portfolio_value

        # Decide target weights
        target_weights = strategy.calculate_target_weights(i, indicators, portfolio_value, peak_equity)
        weight_history.append({"date": current_date, **target_weights})

        # Rebalance portfolio
        target_values = {t: portfolio_value * target_weights.get(t, 0.0) for t in TICKERS}
        target_qtys = {t: (target_values[t] / current_prices[t]) if current_prices[t] > 0 else 0.0 for t in TICKERS}

        # Execute orders and deduct fees
        total_fee = 0.0
        for t in TICKERS:
            qty_diff = target_qtys[t] - holdings_qty[t]
            if abs(qty_diff) > 1e-5:
                trade_value = abs(qty_diff) * current_prices[t]
                fee = trade_value * (fees_bps / 10000.0)
                total_fee += fee

                trade_history.append({
                    "date": current_date,
                    "ticker": t,
                    "action": "BUY" if qty_diff > 0 else "SELL",
                    "qty": abs(qty_diff),
                    "price": current_prices[t],
                    "trade_value": trade_value,
                    "fee": fee
                })

                holdings_qty[t] = target_qtys[t]

        cash = portfolio_value - sum(holdings_qty[t] * current_prices[t] for t in TICKERS) - total_fee
        new_portfolio_value = cash + sum(holdings_qty[t] * current_prices[t] for t in TICKERS)

        equity_curve.append({
            "date": current_date,
            "equity": new_portfolio_value,
            "cash": cash,
            "peak_equity": peak_equity,
            "drawdown": (peak_equity - new_portfolio_value) / peak_equity
        })

    equity_df = pd.DataFrame(equity_curve).set_index("date")
    equity_df["returns"] = equity_df["equity"].pct_change().fillna(0.0)

    # SPY benchmark comparison
    spy_prices = price_df["SPY"].iloc[start_idx:]
    spy_benchmark = (spy_prices / spy_prices.iloc[0]) * initial_capital
    equity_df["spy_benchmark"] = spy_benchmark

    # Calculate performance metrics
    metrics = compute_metrics(equity_df, trade_history, initial_capital)

    return {
        "equity_df": equity_df,
        "metrics": metrics,
        "trades_df": pd.DataFrame(trade_history),
        "weights_df": pd.DataFrame(weight_history).set_index("date")
    }

def compute_metrics(equity_df: pd.DataFrame, trades: list, initial_capital: float) -> dict:
    """Compute detailed quantitative performance metrics."""
    total_days = len(equity_df)
    final_equity = equity_df["equity"].iloc[-1]
    total_return = (final_equity - initial_capital) / initial_capital

    cagr = ((final_equity / initial_capital) ** (252.0 / total_days) - 1.0) if total_days > 0 else 0.0

    daily_rets = equity_df["returns"]
    ann_vol = daily_rets.std() * np.sqrt(252)
    sharpe = (daily_rets.mean() * 252.0) / (ann_vol + 1e-10)

    downside_rets = daily_rets[daily_rets < 0]
    downside_vol = downside_rets.std() * np.sqrt(252)
    sortino = (daily_rets.mean() * 252.0) / (downside_vol + 1e-10)

    max_dd = equity_df["drawdown"].max()

    trades_count = len(trades)

    return {
        "Initial Capital": f"${initial_capital:,.2f}",
        "Final Equity": f"${final_equity:,.2f}",
        "Total Return": f"{total_return * 100.0:.2f}%",
        "CAGR": f"{cagr * 100.0:.2f}%",
        "Annualized Volatility": f"{ann_vol * 100.0:.2f}%",
        "Sharpe Ratio": f"{sharpe:.2f}",
        "Sortino Ratio": f"{sortino:.2f}",
        "Max Drawdown": f"{max_dd * 100.0:.2f}%",
        "Total Trades Executed": trades_count
    }

# ===============================================================================
# DATA FETCHING & SYNTHETIC FALLBACK
# ===============================================================================

def generate_sample_data(start_date="2020-01-01", end_date="2025-12-31") -> pd.DataFrame:
    """Generates synthetic daily price data for all 13 tickers for offline testing."""
    dates = pd.date_range(start=start_date, end=end_date, freq="B")
    np.random.seed(42)

    data = {}
    base_prices = {
        "BIL": 91.0, "UVXY": 15.0, "TQQQ": 40.0, "TECL": 35.0, "UPRO": 45.0,
        "SQQQ": 30.0, "SOXL": 25.0, "SPY": 350.0, "SPXL": 70.0, "XLK": 120.0,
        "DBC": 15.0, "TLT": 130.0, "NVDA": 50.0
    }

    vols = {
        "BIL": 0.001, "UVXY": 0.05, "TQQQ": 0.03, "TECL": 0.032, "UPRO": 0.028,
        "SQQQ": 0.03, "SOXL": 0.035, "SPY": 0.012, "SPXL": 0.025, "XLK": 0.015,
        "DBC": 0.01, "TLT": 0.008, "NVDA": 0.025
    }

    for ticker in TICKERS:
        ret = np.random.normal(0.0003, vols[ticker], len(dates))
        price = base_prices[ticker] * np.cumprod(1.0 + ret)
        data[ticker] = price

    df = pd.DataFrame(data, index=dates)
    return df

def fetch_data_from_openalgo(start_date="2020-01-01", end_date="2025-12-31") -> pd.DataFrame:
    """Fetch multi-ticker history using OpenAlgo Python SDK."""
    client = api(api_key=API_KEY, host=API_HOST, ws_url=WS_URL)
    ticker_dfs = {}

    print(f"Fetching historical data for {len(TICKERS)} tickers from OpenAlgo ({API_HOST})...")
    for t in TICKERS:
        try:
            res = client.history(
                symbol=t,
                exchange=EXCHANGE,
                interval="1d",
                start_date=start_date,
                end_date=end_date,
                source="db"
            )
            if isinstance(res, pd.DataFrame) and not res.empty:
                df_t = res.copy()
            elif isinstance(res, list) and len(res) > 0:
                df_t = pd.DataFrame(res)
            else:
                df_t = pd.DataFrame()

            if not df_t.empty and "close" in df_t.columns:
                if "timestamp" in df_t.columns:
                    df_t["datetime"] = pd.to_datetime(df_t["timestamp"])
                    df_t = df_t.set_index("datetime")
                ticker_dfs[t] = df_t["close"]
        except Exception as e:
            print(f"Failed to fetch {t} via OpenAlgo API: {e}")

    if len(ticker_dfs) == len(TICKERS):
        combined = pd.DataFrame(ticker_dfs).dropna()
        return combined
    else:
        print("Note: Incomplete API data received. Using synthetic dataset for demonstration...")
        return generate_sample_data(start_date, end_date)

# ===============================================================================
# MAIN EXECUTION ENTRY POINT
# ===============================================================================

if __name__ == "__main__":
    print("=" * 70)
    print("   OpenAlgo Strategy Backtester: TQQQ FTLT Multi-Asset Strategy")
    print("=" * 70)

    # 1. Fetch or generate data
    df = fetch_data_from_openalgo(start_date="2020-01-01", end_date="2025-12-31")
    print(f"Dataset Loaded: {len(df)} daily bars from {df.index.min().strftime('%Y-%m-%d')} to {df.index.max().strftime('%Y-%m-%d')}")

    # 2. Run Backtest
    print("\nRunning Backtest simulation...")
    results = run_backtest(df, initial_capital=INITIAL_CAPITAL, fees_bps=FEES_BPS)

    # 3. Print Results
    metrics = results["metrics"]
    print("\n" + "=" * 50)
    print("            BACKTEST PERFORMANCE SUMMARY")
    print("=" * 50)
    for k, v in metrics.items():
        print(f" {k:<25} : {v}")
    print("=" * 50)

    # 4. Display sample recent trades
    trades_df = results["trades_df"]
    if not trades_df.empty:
        print(f"\nTotal Rebalance Transactions: {len(trades_df)}")
        print("\nLast 10 Trades:")
        print(trades_df.tail(10).to_string(index=False))
