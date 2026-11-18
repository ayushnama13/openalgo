// Starter strategies for the backtest editor's "Template" picker.

export const STRATEGY_TEMPLATES: { label: string; code: string }[] = [
  {
    label: 'SMA Cross',
    code: `# SMA Crossover strategy
# Computes signals on the bars DataFrame and returns it.
# df has columns: timestamp, open, high, low, close, volume, oi
def strategy(df):
    df["sma_fast"] = ta.sma(df["close"], period=9)
    df["sma_slow"] = ta.sma(df["close"], period=21)

    df["signal"] = 0
    df.loc[(df["sma_fast"] > df["sma_slow"]) & (df["sma_fast"].shift(1) <= df["sma_slow"].shift(1)), "signal"] = 1
    df.loc[(df["sma_fast"] < df["sma_slow"]) & (df["sma_fast"].shift(1) >= df["sma_slow"].shift(1)), "signal"] = -1
    return df
`,
  },
  {
    label: 'RSI Mean Reversion',
    code: `# RSI mean reversion - buy oversold, exit overbought
def strategy(df):
    df["rsi"] = ta.rsi(df["close"], period=14)

    df["signal"] = 0
    df.loc[(df["rsi"] < 30) & (df["rsi"].shift(1) >= 30), "signal"] = 1
    df.loc[(df["rsi"] > 70) & (df["rsi"].shift(1) <= 70), "signal"] = -1
    return df
`,
  },
  {
    label: 'Supertrend',
    code: `# Supertrend trend-following strategy
# ta.supertrend returns (st_levels, direction); direction 1 = uptrend, -1 = downtrend
def strategy(df):
    st_levels, st_direction = ta.supertrend(df["high"], df["low"], df["close"], period=10, multiplier=3.0)

    df["signal"] = 0
    df.loc[(st_direction == 1) & (st_direction.shift(1) == -1), "signal"] = 1
    df.loc[(st_direction == -1) & (st_direction.shift(1) == 1), "signal"] = -1
    return df
`,
  },
  {
    label: 'Bollinger Band Bounce',
    code: `# Bollinger Band mean reversion
# ta.bbands returns (upper, middle, lower) bands
def strategy(df):
    bb_upper, bb_middle, bb_lower = ta.bbands(df["close"], period=20, std_dev=2.0)

    df["signal"] = 0
    df.loc[(df["close"] < bb_lower) & (df["close"].shift(1) >= bb_lower.shift(1)), "signal"] = 1
    df.loc[(df["close"] > bb_upper) & (df["close"].shift(1) <= bb_upper.shift(1)), "signal"] = -1
    return df
`,
  },
  {
    label: 'MACD Crossover',
    code: `# MACD signal line crossover strategy
# ta.macd returns (macd, signal, histogram)
def strategy(df):
    macd_line, signal_line, hist = ta.macd(df["close"], fast=12, slow=26, signal=9)

    df["signal"] = 0
    df.loc[(macd_line > signal_line) & (macd_line.shift(1) <= signal_line.shift(1)), "signal"] = 1
    df.loc[(macd_line < signal_line) & (macd_line.shift(1) >= signal_line.shift(1)), "signal"] = -1
    return df
`,
  },
  {
    label: 'Opening Range Breakout',
    code: `# Opening Range Breakout (ORB) strategy
def strategy(df):
    # Simple rolling 30-period high/low breakout
    df["orb_high"] = df["high"].rolling(30).max().shift(1)
    df["orb_low"] = df["low"].rolling(30).min().shift(1)

    df["signal"] = 0
    df.loc[(df["close"] > df["orb_high"]) & (df["close"].shift(1) <= df["orb_high"].shift(1)), "signal"] = 1
    df.loc[(df["close"] < df["orb_low"]) & (df["close"].shift(1) >= df["orb_low"].shift(1)), "signal"] = -1
    return df
`,
  },
]

STRATEGY_TEMPLATES.push(
  {
    label: 'Event: multi-symbol momentum',
    code: `# Event-driven strategy: runs on every selected symbol, shares one capital pool.
# Orders placed in on_bar fill at the NEXT bar's open (no lookahead).
class Momentum(Strategy):
    params = {"lookback": 20, "stop_pct": 2.0}

    def on_bar(self, ctx):
        n = self.params["lookback"]
        for sym in ctx.symbols:
            if ctx.bar(sym) is None:
                continue
            closes = ctx.history(sym, n + 1, "close")
            if len(closes) <= n:
                continue
            rising = closes.iloc[-1] > closes.iloc[0]
            pos = ctx.position(sym)
            if rising and not pos and not ctx.pending(sym):
                ctx.buy(sym, pct=20, sl_pct=self.params["stop_pct"], trail_pct=3)
            elif not rising and pos:
                ctx.close(sym, tag="Momentum lost")
`,
  },
  {
    label: 'Event: SMA cross with indicators in init',
    code: `# Precompute causal indicators once in init, then index them with ctx.i(sym).
class SmaCross(Strategy):
    params = {"fast": 9, "slow": 21}

    def init(self, ctx):
        self.fast, self.slow = {}, {}
        for sym in ctx.symbols:
            close = ctx.data(sym)["close"]
            self.fast[sym] = close.rolling(self.params["fast"]).mean().to_numpy()
            self.slow[sym] = close.rolling(self.params["slow"]).mean().to_numpy()

    def on_bar(self, ctx):
        for sym in ctx.symbols:
            if ctx.bar(sym) is None:
                continue
            i = ctx.i(sym)
            if i < 1 or i >= len(self.slow[sym]) or self.slow[sym][i] != self.slow[sym][i]:
                continue
            up = self.fast[sym][i] > self.slow[sym][i] and self.fast[sym][i - 1] <= self.slow[sym][i - 1]
            down = self.fast[sym][i] < self.slow[sym][i] and self.fast[sym][i - 1] >= self.slow[sym][i - 1]
            pos = ctx.position(sym)
            if up and not pos:
                ctx.buy(sym, pct=25)
            elif down and pos:
                ctx.close(sym)
`,
  },
  {
    label: 'Signal: parameterised RSI',
    code: `# Signal-style strategy that reads tunable values from PARAMS (use with Optimize).
def strategy(df):
    period = PARAMS.get("period", 14)
    low = PARAMS.get("low", 30)
    high = PARAMS.get("high", 70)
    df["rsi"] = ta.rsi(df["close"], period=period)

    df["signal"] = 0
    df.loc[(df["rsi"] < low) & (df["rsi"].shift(1) >= low), "signal"] = 1
    df.loc[(df["rsi"] > high) & (df["rsi"].shift(1) <= high), "signal"] = -1
    return df
`,
  }
)

export const DEFAULT_STRATEGY_CODE = STRATEGY_TEMPLATES[0].code
