# services/backtest_analytics.py

import math
from typing import Any, Optional

import numpy as np
import pandas as pd

from database.historify_db import get_ohlcv, parse_interval
from utils.logging import get_logger

logger = get_logger(__name__)

# NSE equity sessions run 09:15-15:30 IST = 6.25 hours = 375 minutes, 252
# trading days per year. Per-bar returns are annualized with sqrt(periods),
# so the period count MUST match the bar size or Sharpe/Sortino are wrong:
# annualizing 1m bars with sqrt(252) inflates the ratio ~19x vs daily bars,
# which makes cross-timeframe comparison meaningless.
_TRADING_MINUTES_PER_DAY = 375.0
_TRADING_DAYS_PER_YEAR = 252.0


def periods_per_year_for_interval(interval: str | None) -> float:
    """Annualized-period count (Sharpe/Sortino scaling factor) for an interval.

    Bar-size tuned for the NSE equity session (6h15m/day, 252 days/yr):
    - intraday: 252 * 375 / minutes  (1m -> 94500, 5m -> 18900, 15m -> 6300, 1h -> 1575)
    - daily    -> 252, weekly -> 52, monthly -> 12, quarterly -> 4, yearly -> 1
    Falls back to 252 for anything unparseable.
    """
    parsed = parse_interval(interval) if interval else None
    if not parsed:
        return _TRADING_DAYS_PER_YEAR

    ptype = parsed["type"]
    if ptype == "intraday":
        minutes = float(parsed["minutes"])
        if minutes <= 0:
            return _TRADING_DAYS_PER_YEAR
        return _TRADING_DAYS_PER_YEAR * _TRADING_MINUTES_PER_DAY / minutes
    if ptype == "daily":
        return _TRADING_DAYS_PER_YEAR / float(max(1, parsed["days"]))
    if ptype == "weekly":
        return 52.0 / float(max(1, parsed["value"]))
    if ptype in ("monthly", "quarterly", "yearly"):
        return 12.0 / float(max(1, parsed["months"]))
    return _TRADING_DAYS_PER_YEAR


def calculate_backtest_metrics(
    equity_series: pd.Series,
    trades: list[dict[str, Any]],
    initial_capital: float,
    trading_days_per_year: int = 252,
    interval: str | None = None,
) -> dict[str, Any]:
    """
    Computes comprehensive performance metrics for a backtest run.

    ``interval`` (e.g. "5m", "D") drives Sharpe/Sortino annualization so the
    ratios stay comparable across timeframes. When omitted, falls back to the
    legacy ``trading_days_per_year`` behavior (daily bar assumption).
    """
    if equity_series.empty or len(equity_series) < 2:
        return {
            "total_return_pct": 0.0,
            "ending_equity": initial_capital,
            "cagr_pct": 0.0,
            "sharpe_ratio": 0.0,
            "sortino_ratio": 0.0,
            "max_drawdown_pct": 0.0,
            "total_trades": len(trades),
            "win_rate_pct": 0.0,
            "profit_factor": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "win_loss_ratio": 0.0,
            "expectancy": 0.0,
            "avg_holding_bars": 0.0,
            "avg_holding_minutes": 0.0,
        }

    ending_equity = float(equity_series.iloc[-1])
    total_pnl = ending_equity - initial_capital
    total_return_pct = (total_pnl / initial_capital) * 100.0

    # Daily returns (or bar returns)
    pct_changes = equity_series.pct_change().dropna()

    # CAGR calculation
    total_days = max(1, (equity_series.index[-1] - equity_series.index[0]).days if isinstance(equity_series.index, pd.DatetimeIndex) else len(equity_series))
    years = total_days / 365.25 if isinstance(equity_series.index, pd.DatetimeIndex) else len(equity_series) / trading_days_per_year
    years = max(years, 0.001)

    if ending_equity > 0:
        cagr_pct = ((ending_equity / initial_capital) ** (1.0 / years) - 1.0) * 100.0
    else:
        cagr_pct = -100.0

    # Sharpe Ratio (annualized)
    # sqrt(periods_per_year) scales the per-bar ratio to annual: 1m bars get
    # far more periods per year than daily bars, keeping the ratio comparable
    # across timeframes of the same strategy.
    periods_per_year = (
        periods_per_year_for_interval(interval) if interval else float(trading_days_per_year)
    )
    mean_return = pct_changes.mean()
    std_return = pct_changes.std()
    if std_return > 0:
        sharpe_ratio = (mean_return / std_return) * math.sqrt(periods_per_year)
    else:
        sharpe_ratio = 0.0

    # Sortino Ratio (downside risk)
    downside_returns = pct_changes[pct_changes < 0]
    downside_std = downside_returns.std()
    if downside_std > 0:
        sortino_ratio = (mean_return / downside_std) * math.sqrt(periods_per_year)
    else:
        sortino_ratio = 0.0

    # Max Drawdown
    cummax = equity_series.cummax()
    drawdown = (equity_series - cummax) / cummax
    max_drawdown_pct = float(abs(drawdown.min())) * 100.0

    # Trade statistics
    total_trades = len(trades)
    if total_trades > 0:
        pnls = [t.get("pnl", 0.0) for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        win_count = len(wins)
        loss_count = len(losses)
        win_rate_pct = (win_count / total_trades) * 100.0

        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))

        if gross_loss > 0:
            profit_factor = gross_profit / gross_loss
        else:
            profit_factor = float("inf") if gross_profit > 0 else 0.0

        avg_win = float(np.mean(wins)) if wins else 0.0
        avg_loss = float(np.mean(losses)) if losses else 0.0
        win_loss_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else (float("inf") if avg_win > 0 else 0.0)

        # Expectancy = (Win Rate * Avg Win) - (Loss Rate * Avg Loss)
        win_prob = win_count / total_trades
        loss_prob = loss_count / total_trades
        expectancy = (win_prob * avg_win) - (loss_prob * abs(avg_loss))

        holding_bars = [t.get("holding_period_bars", 0) for t in trades]
        avg_holding_bars = float(np.mean(holding_bars)) if holding_bars else 0.0

        # Wall-clock holding time — 10 bars on 1m (10 min) is a very different
        # trade than 10 bars on 1h (10 hours), so the matrix compares minutes.
        holding_minutes: list[float] = []
        for t in trades:
            et = t.get("entry_time")
            xt = t.get("exit_time")
            if not et or not xt:
                continue
            try:
                holding_minutes.append((pd.to_datetime(xt) - pd.to_datetime(et)).total_seconds() / 60.0)
            except (TypeError, ValueError):
                continue
        avg_holding_minutes = float(np.mean(holding_minutes)) if holding_minutes else 0.0
    else:
        win_rate_pct = 0.0
        profit_factor = 0.0
        avg_win = 0.0
        avg_loss = 0.0
        win_loss_ratio = 0.0
        expectancy = 0.0
        avg_holding_bars = 0.0
        avg_holding_minutes = 0.0

    # Ensure JSON serializable infinity values
    if math.isinf(profit_factor):
        profit_factor = 999.99
    if math.isinf(win_loss_ratio):
        win_loss_ratio = 999.99

    return {
        "initial_capital": round(initial_capital, 2),
        "ending_equity": round(ending_equity, 2),
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": round(total_return_pct, 2),
        "cagr_pct": round(cagr_pct, 2),
        "sharpe_ratio": round(sharpe_ratio, 2),
        "sortino_ratio": round(sortino_ratio, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "total_trades": total_trades,
        "win_rate_pct": round(win_rate_pct, 2),
        "profit_factor": round(profit_factor, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "win_loss_ratio": round(win_loss_ratio, 2),
        "expectancy": round(expectancy, 2),
        "avg_holding_bars": round(avg_holding_bars, 1),
        "avg_holding_minutes": round(avg_holding_minutes, 1),
    }


def generate_monthly_returns_heatmap(equity_series: pd.Series) -> dict[str, Any]:
    """
    Computes monthly returns heatmap matrix from an equity series indexed by DatetimeIndex or datetime strings.
    """
    if equity_series.empty:
        return {"years": [], "months": [], "matrix": {}}

    try:
        if not isinstance(equity_series.index, pd.DatetimeIndex):
            equity_series.index = pd.to_datetime(equity_series.index)

        # Resample to end of month equity
        monthly_equity = equity_series.resample("ME").last().dropna()
        if monthly_equity.empty:
            return {"years": [], "months": [], "matrix": {}}

        # Add initial point for first month return computation if needed
        first_eq = equity_series.iloc[0]
        monthly_returns = monthly_equity.pct_change()
        if len(monthly_equity) > 0:
            monthly_returns.iloc[0] = (monthly_equity.iloc[0] - first_eq) / first_eq

        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        matrix = {}
        years = sorted(set(monthly_returns.index.year))

        for yr in years:
            yr_str = str(yr)
            matrix[yr_str] = {}
            yr_series = monthly_returns[monthly_returns.index.year == yr]

            # Monthly returns in %
            for m_idx in range(1, 13):
                m_name = months[m_idx - 1]
                m_data = yr_series[yr_series.index.month == m_idx]
                if not m_data.empty:
                    matrix[yr_str][m_name] = round(float(m_data.iloc[0]) * 100.0, 2)
                else:
                    matrix[yr_str][m_name] = None

            # YTD calculation for the year
            yr_equity = equity_series[equity_series.index.year == yr]
            if not yr_equity.empty:
                start_val = yr_equity.iloc[0]
                end_val = yr_equity.iloc[-1]
                ytd = ((end_val - start_val) / start_val) * 100.0 if start_val > 0 else 0.0
                matrix[yr_str]["YTD"] = round(float(ytd), 2)
            else:
                matrix[yr_str]["YTD"] = 0.0

        return {"years": [str(y) for y in years], "months": months, "matrix": matrix}

    except Exception as e:
        logger.exception(f"Error generating monthly returns heatmap: {e}")
        return {"years": [], "months": [], "matrix": {}}


def get_benchmark_comparison(
    start_time_epoch: int | None,
    end_time_epoch: int | None,
    interval: str,
    initial_capital: float,
    benchmark_symbol: str = "NIFTY",
    benchmark_exchange: str = "NSE_INDEX",
) -> list[dict[str, Any]]:
    """
    Retrieves benchmark OHLCV and returns benchmark equity curve aligned with initial capital.
    """
    try:
        bench_df = get_ohlcv(
            symbol=benchmark_symbol,
            exchange=benchmark_exchange,
            interval=interval,
            start_timestamp=start_time_epoch,
            end_timestamp=end_time_epoch,
        )

        if bench_df.empty or "close" not in bench_df.columns or len(bench_df) < 2:
            return []

        first_close = bench_df["close"].iloc[0]
        if first_close <= 0:
            return []

        benchmark_equity = (bench_df["close"] / first_close) * initial_capital

        # get_ohlcv() returns raw epoch seconds - convert to the same
        # "YYYY-MM-DD HH:MM:SS" IST string format the strategy's own equity
        # curve uses, so the two series share a common x-axis on the chart.
        timestamps = (
            pd.to_datetime(bench_df["timestamp"], unit="s", utc=True)
            .dt.tz_convert("Asia/Kolkata")
            .dt.tz_localize(None)
            .astype(str)
        )

        results = []
        for ts, eq in zip(timestamps, benchmark_equity, strict=False):
            results.append({
                "timestamp": ts,
                "benchmark_equity": round(float(eq), 2),
            })
        return results
    except Exception as e:
        logger.exception(f"Error fetching benchmark comparison: {e}")
        return []
