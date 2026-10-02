import {
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  type SeriesMarker,
  type UTCTimestamp,
} from 'lightweight-charts'
import { Dices, Fingerprint, Loader2, Microscope, TrendingDown } from 'lucide-react'
import type * as PlotlyTypes from 'plotly.js'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { fetchCSRFToken } from '@/api/client'
import { PLOT_CONFIG, PLOT_LAYOUT } from '@/components/backtest/plotConfig'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import Plot from '@/lib/Plot2D'
import type { FullJobDetails } from '@/stores/strategyBacktestStore'
import { showToast } from '@/utils/toast'

interface DrawdownPeriod {
  start: string
  trough: string
  end: string | null
  depth_pct: number
  bars_to_trough: number
  bars_to_recover: number | null
  duration_bars: number
  recovered: boolean
}

interface GroupRow {
  trades: number
  pnl: number
  win_rate_pct: number
  avg_pnl: number
  costs: number
  [key: string]: string | number
}

interface Excursion {
  avg_mae_pct: number
  avg_mfe_pct: number
  avg_mae_winners_pct: number | null
  avg_mae_losers_pct: number | null
  edge_ratio: number | null
  avg_profit_capture_pct: number | null
  points: { mae: number; mfe: number; pnl: number }[]
}

interface Analytics {
  drawdown_periods: DrawdownPeriod[]
  rolling: {
    window: number
    points: { timestamp: string; sharpe: number | null; return_pct: number | null }[]
  }
  trades: {
    by_symbol: GroupRow[]
    by_exit_reason: GroupRow[]
    by_side: GroupRow[]
    streaks: { max_consecutive_wins?: number; max_consecutive_losses?: number }
    excursion: Excursion | null
  }
  exposure: { time_in_market_pct: number }
}

interface ChartData {
  symbol: string
  symbols: string[]
  candles: { time: string; open: number; high: number; low: number; close: number }[]
  markers: {
    time: string
    kind: 'entry' | 'exit'
    side: 'LONG' | 'SHORT'
    price: number
    pnl?: number
    reason?: string
  }[]
  total_trades: number
  shown_trades: number
}

interface MonteCarlo {
  method: string
  simulations: number
  trades: number
  actual_final_equity: number
  final_equity: Record<string, number>
  max_drawdown_pct: Record<string, number>
  probability_of_loss: number
  probability_of_ruin: number
  ruin_drawdown_pct: number
  actual_percentile: number
  fan: { steps: number[]; p5: number[]; p25: number[]; p50: number[]; p75: number[]; p95: number[] }
}

const toEpoch = (s: string) => (Date.parse(`${s.replace(' ', 'T')}Z`) / 1000) as UTCTimestamp
const fmt = (n: number | null | undefined, d = 2) =>
  n === null || n === undefined ? '-' : n.toLocaleString('en-IN', { maximumFractionDigits: d })

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: 'include' })
  const body = await res.json()
  if (body.status !== 'success') throw new Error(body.message || 'Request failed')
  return body as T
}

// ---------------------------------------------------------------------------
// Trade chart
// ---------------------------------------------------------------------------
function TradeChart({ jobId, symbols }: { jobId: string; symbols: string[] }) {
  const [symbol, setSymbol] = useState(symbols[0] ?? '')
  const [data, setData] = useState<ChartData | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    getJson<ChartData>(
      `/backtest/api/chart/${jobId}?symbol=${encodeURIComponent(symbol)}&max_bars=5000`
    )
      .then((d) => !cancelled && setData(d))
      .catch((e: Error) => !cancelled && setError(e.message))
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [jobId, symbol])

  useEffect(() => {
    const container = containerRef.current
    if (!container || !data) return
    const chart = createChart(container, {
      width: container.clientWidth,
      height: 420,
      layout: { background: { type: ColorType.Solid, color: 'transparent' }, textColor: '#94a3b8' },
      grid: { vertLines: { color: '#1e293b' }, horzLines: { color: '#1e293b' } },
      timeScale: { timeVisible: true, secondsVisible: false },
    })
    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#22c55e',
      downColor: '#ef4444',
      wickUpColor: '#22c55e',
      wickDownColor: '#ef4444',
      borderVisible: false,
    })
    series.setData(
      data.candles.map((c) => ({
        time: toEpoch(c.time),
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
      }))
    )
    const markers: SeriesMarker<UTCTimestamp>[] = data.markers
      .map((m) => {
        const long = m.side === 'LONG'
        const entry = m.kind === 'entry'
        const below = entry ? long : !long
        return {
          time: toEpoch(m.time),
          position: below ? ('belowBar' as const) : ('aboveBar' as const),
          shape: below ? ('arrowUp' as const) : ('arrowDown' as const),
          color: entry ? (long ? '#38bdf8' : '#f59e0b') : (m.pnl ?? 0) >= 0 ? '#22c55e' : '#ef4444',
          text: entry ? (long ? 'B' : 'S') : `${(m.pnl ?? 0) >= 0 ? '+' : ''}${fmt(m.pnl, 0)}`,
        }
      })
      .sort((a, b) => a.time - b.time)
    createSeriesMarkers(series, markers)
    chart.timeScale().fitContent()
    const onResize = () => chart.applyOptions({ width: container.clientWidth })
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      chart.remove()
    }
  }, [data])

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        {symbols.length > 1 && (
          <div className="flex items-center gap-2">
            <Label className="text-xs">Symbol</Label>
            <Select value={symbol} onValueChange={setSymbol}>
              <SelectTrigger className="w-52">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {symbols.map((s) => (
                  <SelectItem key={s} value={s}>
                    {s}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )}
        {data && (
          <span className="text-xs text-muted-foreground">
            {data.shown_trades} of {data.total_trades} trades in view (the window follows your
            trades when the run has more than 5,000 bars)
          </span>
        )}
        {loading && <Loader2 className="h-4 w-4 animate-spin" />}
      </div>
      {error ? <p className="text-sm text-red-400">{error}</p> : <div ref={containerRef} />}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Analytics
// ---------------------------------------------------------------------------
function GroupTable({
  title,
  rows,
  keyName,
}: {
  title: string
  rows: GroupRow[]
  keyName: string
}) {
  if (!rows.length) return null
  return (
    <div className="space-y-1">
      <h4 className="text-sm font-medium">{title}</h4>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>{keyName}</TableHead>
            <TableHead className="text-right">Trades</TableHead>
            <TableHead className="text-right">Net P&L</TableHead>
            <TableHead className="text-right">Win %</TableHead>
            <TableHead className="text-right">Avg P&L</TableHead>
            <TableHead className="text-right">Costs</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((r) => (
            <TableRow key={String(r[keyName])}>
              <TableCell>{String(r[keyName])}</TableCell>
              <TableCell className="text-right">{r.trades}</TableCell>
              <TableCell
                className={`text-right ${r.pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}
              >
                {fmt(r.pnl)}
              </TableCell>
              <TableCell className="text-right">{fmt(r.win_rate_pct, 1)}</TableCell>
              <TableCell className="text-right">{fmt(r.avg_pnl)}</TableCell>
              <TableCell className="text-right">{fmt(r.costs)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

function AnalyticsPanel({ jobId }: { jobId: string }) {
  const [data, setData] = useState<Analytics | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    getJson<Analytics>(`/backtest/api/analytics/${jobId}`)
      .then(setData)
      .catch((e: Error) => setError(e.message))
  }, [jobId])

  const rollingPlot = useMemo(() => {
    if (!data?.rolling.points.length) return null
    const pts = data.rolling.points
    return {
      data: [
        {
          x: pts.map((p) => p.timestamp),
          y: pts.map((p) => p.sharpe),
          type: 'scatter',
          mode: 'lines',
          name: 'Rolling Sharpe',
          line: { color: '#38bdf8', width: 1.5 },
        },
      ] as PlotlyTypes.Data[],
      layout: {
        ...PLOT_LAYOUT,
        height: 260,
        title: { text: `Rolling Sharpe (${data.rolling.window} bars)`, font: { size: 13 } },
        shapes: [
          {
            type: 'line',
            xref: 'paper',
            x0: 0,
            x1: 1,
            y0: 0,
            y1: 0,
            line: { color: '#475569', dash: 'dot' },
          },
        ],
      } as Partial<PlotlyTypes.Layout>,
    }
  }, [data])

  const excursionPlot = useMemo(() => {
    const ex = data?.trades.excursion
    if (!ex || !ex.points.length) return null
    return {
      data: [
        {
          x: ex.points.map((p) => p.mae),
          y: ex.points.map((p) => p.mfe),
          text: ex.points.map((p) => `P&L ${fmt(p.pnl)}`),
          type: 'scatter',
          mode: 'markers',
          marker: {
            size: 7,
            color: ex.points.map((p) => (p.pnl >= 0 ? '#22c55e' : '#ef4444')),
            opacity: 0.75,
          },
        },
      ] as PlotlyTypes.Data[],
      layout: {
        ...PLOT_LAYOUT,
        height: 300,
        title: { text: 'Excursion per trade', font: { size: 13 } },
        xaxis: { ...PLOT_LAYOUT.xaxis, title: { text: 'MAE % (worst against you)' } },
        yaxis: { ...PLOT_LAYOUT.yaxis, title: { text: 'MFE % (best in your favour)' } },
      } as Partial<PlotlyTypes.Layout>,
    }
  }, [data])

  if (error) return <p className="text-sm text-red-400">{error}</p>
  if (!data) return <Loader2 className="h-5 w-5 animate-spin" />
  const ex = data.trades.excursion

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Time in market" value={`${fmt(data.exposure.time_in_market_pct, 1)}%`} />
        <Stat
          label="Max consecutive wins"
          value={String(data.trades.streaks.max_consecutive_wins ?? 0)}
        />
        <Stat
          label="Max consecutive losses"
          value={String(data.trades.streaks.max_consecutive_losses ?? 0)}
        />
        {ex && <Stat label="Edge ratio (MFE / MAE)" value={fmt(ex.edge_ratio)} />}
      </div>

      <div className="space-y-1">
        <h4 className="flex items-center gap-2 text-sm font-medium">
          <TrendingDown className="h-4 w-4" /> Worst drawdowns
        </h4>
        {data.drawdown_periods.length ? (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Peak</TableHead>
                <TableHead>Trough</TableHead>
                <TableHead>Recovered</TableHead>
                <TableHead className="text-right">Depth</TableHead>
                <TableHead className="text-right">Bars down</TableHead>
                <TableHead className="text-right">Bars to recover</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.drawdown_periods.map((d) => (
                <TableRow key={`${d.start}-${d.trough}`}>
                  <TableCell>{d.start}</TableCell>
                  <TableCell>{d.trough}</TableCell>
                  <TableCell>{d.end ?? <Badge variant="outline">still open</Badge>}</TableCell>
                  <TableCell className="text-right text-red-400">{fmt(d.depth_pct)}%</TableCell>
                  <TableCell className="text-right">{d.bars_to_trough}</TableCell>
                  <TableCell className="text-right">{d.bars_to_recover ?? '-'}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          <p className="text-xs text-muted-foreground">No drawdowns.</p>
        )}
      </div>

      {rollingPlot && (
        <Plot
          data={rollingPlot.data}
          layout={rollingPlot.layout}
          config={PLOT_CONFIG}
          style={{ width: '100%', height: 260 }}
          useResizeHandler
        />
      )}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <GroupTable title="By symbol" rows={data.trades.by_symbol} keyName="symbol" />
        <GroupTable
          title="By exit reason"
          rows={data.trades.by_exit_reason}
          keyName="exit_reason"
        />
        <GroupTable title="Long vs short" rows={data.trades.by_side} keyName="side" />
      </div>

      {ex && (
        <div className="space-y-2">
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="Avg MAE" value={`${fmt(ex.avg_mae_pct)}%`} />
            <Stat label="Avg MFE" value={`${fmt(ex.avg_mfe_pct)}%`} />
            <Stat label="Avg MAE of winners" value={`${fmt(ex.avg_mae_winners_pct)}%`} />
            <Stat label="Profit captured" value={`${fmt(ex.avg_profit_capture_pct, 1)}%`} />
          </div>
          <p className="text-xs text-muted-foreground">
            Winners rarely dip far against you, so a stop wider than the typical winner MAE cuts
            losses without cutting winners. Profit captured is how much of each trade's best open
            profit was kept at exit.
          </p>
          {excursionPlot && (
            <Plot
              data={excursionPlot.data}
              layout={excursionPlot.layout}
              config={PLOT_CONFIG}
              style={{ width: '100%', height: 300 }}
              useResizeHandler
            />
          )}
        </div>
      )}
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-slate-800 bg-slate-900/60 p-3">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="text-base font-semibold">{value}</div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Monte Carlo
// ---------------------------------------------------------------------------
function MonteCarloPanel({ jobId, tradeCount }: { jobId: string; tradeCount: number }) {
  const [method, setMethod] = useState('bootstrap')
  const [result, setResult] = useState<MonteCarlo | null>(null)
  const [running, setRunning] = useState(false)

  const run = useCallback(async () => {
    setRunning(true)
    try {
      const csrf = await fetchCSRFToken()
      const res = await fetch(`/backtest/api/monte-carlo/${jobId}`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
        body: JSON.stringify({ method, simulations: 2000 }),
      })
      const body = await res.json()
      if (body.status !== 'success') throw new Error(body.message)
      setResult(body.monte_carlo as MonteCarlo)
    } catch (e) {
      showToast.error(e instanceof Error ? e.message : 'Monte Carlo failed')
    } finally {
      setRunning(false)
    }
  }, [jobId, method])

  const fan = useMemo(() => {
    if (!result) return null
    const f = result.fan
    const line = (y: number[], name: string, color: string, dash?: string) => ({
      x: f.steps,
      y,
      type: 'scatter' as const,
      mode: 'lines' as const,
      name,
      line: { color, width: 1.5, dash },
    })
    return {
      data: [
        line(f.p95, '95th pct', '#334155'),
        {
          ...line(f.p5, '5th pct', '#334155'),
          fill: 'tonexty' as const,
          fillcolor: 'rgba(56,189,248,0.10)',
        },
        line(f.p75, '75th pct', '#475569'),
        {
          ...line(f.p25, '25th pct', '#475569'),
          fill: 'tonexty' as const,
          fillcolor: 'rgba(56,189,248,0.18)',
        },
        line(f.p50, 'Median', '#38bdf8'),
      ] as PlotlyTypes.Data[],
      layout: {
        ...PLOT_LAYOUT,
        height: 320,
        xaxis: { ...PLOT_LAYOUT.xaxis, title: { text: 'Trade number' } },
        yaxis: { ...PLOT_LAYOUT.yaxis, title: { text: 'Equity' } },
      } as Partial<PlotlyTypes.Layout>,
    }
  }, [result])

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1">
          <Label className="text-xs">Resampling</Label>
          <Select value={method} onValueChange={setMethod}>
            <SelectTrigger className="w-56">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="bootstrap">Bootstrap (draw with replacement)</SelectItem>
              <SelectItem value="shuffle">Shuffle (reorder same trades)</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <Button onClick={run} disabled={running || tradeCount < 5} className="gap-2">
          {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Dices className="h-4 w-4" />}
          Run 2,000 simulations
        </Button>
        {tradeCount < 5 && (
          <span className="text-xs text-muted-foreground">Needs at least 5 trades.</span>
        )}
      </div>
      {result && fan && (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="Median final equity" value={fmt(result.final_equity.p50, 0)} />
            <Stat
              label="5th to 95th percentile"
              value={`${fmt(result.final_equity.p5, 0)} to ${fmt(result.final_equity.p95, 0)}`}
            />
            <Stat
              label="Probability of loss"
              value={`${fmt(result.probability_of_loss * 100, 1)}%`}
            />
            <Stat
              label={`Chance of ${result.ruin_drawdown_pct}% drawdown`}
              value={`${fmt(result.probability_of_ruin * 100, 1)}%`}
            />
            <Stat label="Median max drawdown" value={`${fmt(result.max_drawdown_pct.p50, 1)}%`} />
            <Stat label="95th pct max drawdown" value={`${fmt(result.max_drawdown_pct.p95, 1)}%`} />
            <Stat label="Actual result" value={fmt(result.actual_final_equity, 0)} />
            <Stat
              label="Actual vs simulations"
              value={`${fmt(result.actual_percentile, 0)}th pct`}
            />
          </div>
          <Plot
            data={fan.data}
            layout={fan.layout}
            config={PLOT_CONFIG}
            style={{ width: '100%', height: 320 }}
            useResizeHandler
          />
          <p className="text-xs text-muted-foreground">
            An actual result near the top of the simulated range means the realised trade order was
            luckier than typical; plan around the median and the 95th percentile drawdown.
          </p>
        </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Reproducibility
// ---------------------------------------------------------------------------
function ReproPanel({ job }: { job: FullJobDetails }) {
  const fp = job.data_fingerprint
  return (
    <div className="space-y-4 text-sm">
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <Stat
          label="Strategy code hash (sha256)"
          value={job.code_hash ? `${job.code_hash.slice(0, 16)}...` : 'not recorded'}
        />
        <Stat
          label="Interval / range"
          value={`${job.interval}  ${job.start_date ?? 'start'} to ${job.end_date ?? 'end'}`}
        />
      </div>
      {job.engine_options && Object.keys(job.engine_options).length > 0 && (
        <div className="space-y-1">
          <h4 className="font-medium">Engine options</h4>
          <pre className="overflow-auto rounded-md border border-slate-800 bg-slate-950 p-2 text-xs">
            {JSON.stringify(job.engine_options, null, 2)}
          </pre>
        </div>
      )}
      {fp ? (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Symbol</TableHead>
              <TableHead className="text-right">Bars</TableHead>
              <TableHead>First</TableHead>
              <TableHead>Last</TableHead>
              <TableHead>Contract</TableHead>
              <TableHead>Data hash</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {Object.entries(fp).map(([sym, f]) => (
              <TableRow key={sym}>
                <TableCell>{sym}</TableCell>
                <TableCell className="text-right">{f.bars.toLocaleString()}</TableCell>
                <TableCell>{f.first}</TableCell>
                <TableCell>{f.last}</TableCell>
                <TableCell>
                  {f.instrument ? `${f.instrument.kind}, lot ${f.instrument.lot_size}` : 'EQ'}
                </TableCell>
                <TableCell className="font-mono text-xs">{f.sha256.slice(0, 12)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      ) : (
        <p className="text-xs text-muted-foreground">
          This run predates input recording, so its data fingerprint is not available.
        </p>
      )}
      <p className="text-xs text-muted-foreground">
        Re-running with the same code hash, options and data hashes reproduces these numbers. If a
        data hash differs, Historify has since been re-downloaded or extended.
      </p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Container
// ---------------------------------------------------------------------------
export default function RunInsights({
  jobId,
  jobDetails,
  tradeCount,
}: {
  jobId: string
  jobDetails?: FullJobDetails | null
  tradeCount: number
}) {
  const symbols = jobDetails?.symbols ?? []
  return (
    <Card>
      <CardContent className="p-4">
        <Tabs defaultValue="chart">
          <TabsList>
            <TabsTrigger value="chart">
              <Microscope className="h-4 w-4" /> Trade chart
            </TabsTrigger>
            <TabsTrigger value="analytics">
              <TrendingDown className="h-4 w-4" /> Analytics
            </TabsTrigger>
            <TabsTrigger value="mc">
              <Dices className="h-4 w-4" /> Monte Carlo
            </TabsTrigger>
            {jobDetails && (
              <TabsTrigger value="repro">
                <Fingerprint className="h-4 w-4" /> Reproducibility
              </TabsTrigger>
            )}
          </TabsList>
          <TabsContent value="chart" className="mt-4">
            {tradeCount > 0 || symbols.length > 0 ? (
              <TradeChart jobId={jobId} symbols={symbols.length ? symbols : ['']} />
            ) : (
              <p className="text-sm text-muted-foreground">No trades to chart.</p>
            )}
          </TabsContent>
          <TabsContent value="analytics" className="mt-4">
            <AnalyticsPanel jobId={jobId} />
          </TabsContent>
          <TabsContent value="mc" className="mt-4">
            <MonteCarloPanel jobId={jobId} tradeCount={tradeCount} />
          </TabsContent>
          {jobDetails && (
            <TabsContent value="repro" className="mt-4">
              <ReproPanel job={jobDetails} />
            </TabsContent>
          )}
        </Tabs>
      </CardContent>
    </Card>
  )
}
