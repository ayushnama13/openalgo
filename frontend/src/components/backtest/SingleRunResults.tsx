/**
 * SingleRunResults — presentational body of one backtest run.
 *
 * Rendered by the BacktestResults page for the single-job mode, and once per
 * timeframe inside batch mode tabs. Data comes in from props; the page owns
 * fetching and caching.
 */

import { BarChart3, ChevronDown, Download, Table2 } from 'lucide-react'
import type * as PlotlyTypes from 'plotly.js'
import { useMemo } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { ScrollArea } from '@/components/ui/scroll-area'
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
import type { FullJobDetails, ResultsData } from '@/stores/strategyBacktestStore'

export const PLOT_LAYOUT: Partial<PlotlyTypes.Layout> = {
  paper_bgcolor: 'transparent',
  plot_bgcolor: 'transparent',
  font: { color: '#94a3b8', family: 'Inter, system-ui, sans-serif', size: 11 },
  margin: { l: 50, r: 16, t: 30, b: 40 },
  xaxis: {
    showgrid: true,
    gridcolor: 'rgba(148,163,184,0.15)',
    zeroline: false,
  },
  yaxis: {
    showgrid: true,
    gridcolor: 'rgba(148,163,184,0.15)',
    zeroline: false,
  },
  hoverlabel: { bgcolor: '#1e293b', bordercolor: '#334155', font: { color: '#f8fafc' } },
}

export const PLOT_CONFIG: Partial<PlotlyTypes.Config> = {
  displaylogo: false,
  responsive: true,
  modeBarButtonsToRemove: ['lasso2d', 'select2d', 'autoScale2d'],
}

// ---------------------------------------------------------------------------
// Metrics card
// ---------------------------------------------------------------------------

function MetricCard({
  label,
  value,
  suffix,
  tone = 'default',
}: {
  label: string
  value: number | string
  suffix?: string
  tone?: 'default' | 'positive' | 'negative' | 'neutral'
}) {
  const toneClass =
    tone === 'positive'
      ? 'text-emerald-400'
      : tone === 'negative'
        ? 'text-rose-400'
        : tone === 'neutral'
          ? 'text-slate-300'
          : 'text-primary'
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/60 p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={`mt-1 text-xl font-bold ${toneClass}`}>
        {typeof value === 'number'
          ? value.toLocaleString(undefined, { maximumFractionDigits: 2 })
          : value}
        {suffix ? (
          <span className="text-sm font-medium text-muted-foreground">{suffix}</span>
        ) : null}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Parameters card
// ---------------------------------------------------------------------------

function ParamRows({ job }: { job: FullJobDetails }) {
  const sizingLabel =
    job.sizing_type === 'fixed_qty'
      ? 'Quantity'
      : job.sizing_type === 'pct_equity'
        ? '% of Equity'
        : 'Amount'
  const rows: [string, string][] = [
    ['Symbol(s)', (job.symbols || []).join(', ') || '-'],
    ['Exchange', job.exchange || '-'],
    ['Interval', job.interval || '-'],
    ['Start Date', job.start_date || '-'],
    ['End Date', job.end_date || '-'],
    ['Initial Capital', `₹${Number(job.initial_capital ?? 0).toLocaleString('en-IN')}`],
    ['Position Sizing', `${sizingLabel}: ${job.sizing_value}`],
    ['Slippage', `${job.slippage_bps} bps`],
    ['Brokerage', `${job.brokerage_bps} bps`],
    ['Product Type', job.product_type || '-'],
    ['Missing Data Policy', job.missing_data_policy || '-'],
    ['Stop Loss', job.stop_loss_pct != null ? `${job.stop_loss_pct}%` : 'Not set'],
    ['Take Profit', job.take_profit_pct != null ? `${job.take_profit_pct}%` : 'Not set'],
    ['Daily Loss Limit', job.daily_loss_limit != null ? `₹${job.daily_loss_limit}` : 'Not set'],
    ['Max Open Positions', String(job.max_positions ?? '-')],
  ]
  return (
    <div className="grid grid-cols-1 gap-x-6 gap-y-2 sm:grid-cols-2 lg:grid-cols-3">
      {rows.map(([k, v]) => (
        <div
          key={k}
          className="flex items-baseline justify-between gap-2 border-b border-slate-800/60 pb-1.5"
        >
          <span className="text-xs text-muted-foreground">{k}</span>
          <span className="text-right text-sm font-medium">{v}</span>
        </div>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function SingleRunResults({
  result,
  jobDetails,
}: {
  result: ResultsData
  jobDetails?: FullJobDetails | null
}) {
  const metrics = result.metrics

  const equityPlot = useMemo(() => {
    if (!result || !result.equity_curve.length) return null
    const data: PlotlyTypes.Data[] = [
      {
        x: result.equity_curve.map((p) => p.timestamp),
        y: result.equity_curve.map((p) => p.equity),
        type: 'scatter',
        mode: 'lines',
        name: 'Strategy',
        line: { color: '#38bdf8', width: 2 },
      },
    ]
    if (result.benchmark_curve.length) {
      data.push({
        x: result.benchmark_curve.map((p) => p.timestamp),
        y: result.benchmark_curve.map((p) => p.benchmark_equity),
        type: 'scatter',
        mode: 'lines',
        name: 'Benchmark (NIFTY 50)',
        line: { color: 'rgba(148,163,184,0.6)', width: 1.5, dash: 'dash' },
      })
    }
    const layout: Partial<PlotlyTypes.Layout> = {
      ...PLOT_LAYOUT,
      title: { text: 'Equity Curve vs Benchmark', font: { color: '#e2e8f0', size: 14 } },
      legend: { orientation: 'h', y: 1.08, font: { color: '#94a3b8' } },
      yaxis: { ...PLOT_LAYOUT.yaxis, title: { text: 'Equity', font: { color: '#94a3b8' } } },
    }
    return { data, layout }
  }, [result])

  const drawdownPlot = useMemo(() => {
    if (!result || !result.equity_curve.length) return null
    const hasDrawdown = result.equity_curve.some((p) => p.drawdown_pct !== 0)
    if (!hasDrawdown) return null
    return {
      data: [
        {
          x: result.equity_curve.map((p) => p.timestamp),
          y: result.equity_curve.map((p) => -Math.abs(p.drawdown_pct)),
          type: 'scatter',
          mode: 'lines',
          name: 'Drawdown %',
          fill: 'tozeroy',
          fillcolor: 'rgba(244,63,94,0.15)',
          line: { color: '#f43f5e', width: 1.5 },
        },
      ] as PlotlyTypes.Data[],
      layout: {
        ...PLOT_LAYOUT,
        title: { text: 'Drawdown (Underwater Curve)', font: { color: '#e2e8f0', size: 14 } },
        yaxis: { ...PLOT_LAYOUT.yaxis, title: { text: 'Drawdown %', font: { color: '#94a3b8' } } },
      } as Partial<PlotlyTypes.Layout>,
    }
  }, [result])

  const heatmapPlot = useMemo(() => {
    const heatmap = metrics?.monthly_heatmap
    if (!heatmap || !heatmap.years.length) return null

    const years = heatmap.years
    const months = heatmap.months
    const z: (number | null)[][] = years.map((yr) =>
      months.map((m) => heatmap.matrix[yr]?.[m] ?? null)
    )

    return {
      data: [
        {
          z,
          x: months,
          y: years,
          type: 'heatmap' as const,
          colorscale: [
            [0, '#7f1d1d'],
            [0.5, '#1e293b'],
            [1, '#14532d'],
          ],
          zmid: 0,
          hoverongaps: false,
          text: z.map((row) => row.map((v) => (v == null ? '' : `${v}%`))),
          texttemplate: '%{text}',
        } as unknown as PlotlyTypes.Data,
      ],
      layout: {
        ...PLOT_LAYOUT,
        title: { text: 'Monthly Returns (%)', font: { color: '#e2e8f0', size: 14 } },
        yaxis: { ...PLOT_LAYOUT.yaxis, autorange: 'reversed' },
        colorbar: {
          title: { text: '%', font: { color: '#94a3b8' } },
          thickness: 10,
          outlinewidth: 0,
        },
      } as Partial<PlotlyTypes.Layout>,
    }
  }, [metrics])

  const pnlHistogram = useMemo(() => {
    if (!result || !result.trades.length) return null
    return {
      data: [
        {
          x: result.trades.map((t) => t.pnl),
          type: 'histogram',
          name: 'Trade P&L',
          marker: { color: 'rgba(56,189,248,0.7)' },
          nbinsx: 30,
        } as unknown as PlotlyTypes.Data,
      ],
      layout: {
        ...PLOT_LAYOUT,
        title: { text: 'Trade P&L Distribution', font: { color: '#e2e8f0', size: 14 } },
        xaxis: { ...PLOT_LAYOUT.xaxis, title: { text: 'P&L', font: { color: '#94a3b8' } } },
      } as Partial<PlotlyTypes.Layout>,
    }
  }, [result])

  const holdingHistogram = useMemo(() => {
    if (!result || !result.trades.length) return null
    return {
      data: [
        {
          x: result.trades.map((t) => t.holding_period_bars),
          type: 'histogram',
          name: 'Holding Period',
          marker: { color: 'rgba(52,211,153,0.7)' },
          nbinsx: 20,
        } as unknown as PlotlyTypes.Data,
      ],
      layout: {
        ...PLOT_LAYOUT,
        title: { text: 'Holding Period Distribution (bars)', font: { color: '#e2e8f0', size: 14 } },
        xaxis: { ...PLOT_LAYOUT.xaxis, title: { text: 'Bars held', font: { color: '#94a3b8' } } },
      } as Partial<PlotlyTypes.Layout>,
    }
  }, [result])

  const showParams = jobDetails != null && jobDetails.job_id === result.job_id

  const handleExportCSV = () => {
    if (!result || !result.trades || !result.trades.length) return
    const headers = [
      '#',
      'Action',
      'Entry Time',
      'Entry Price',
      'Exit Time',
      'Exit Price',
      'Quantity',
      'PnL',
      'PnL %',
      'Exit Reason',
    ]
    const rows = result.trades.map((t, idx) => [
      idx + 1,
      t.action,
      t.entry_time,
      t.entry_price,
      t.exit_time,
      t.exit_price,
      t.quantity,
      t.pnl,
      `${t.pnl_pct}%`,
      t.exit_reason || 'Signal Exit',
    ])
    const csvContent = [headers.join(','), ...rows.map((r) => r.map((c) => `"${c}"`).join(','))].join('\n')
    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.setAttribute('download', `backtest_trades_${result.job_id || 'run'}.csv`)
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
  }

  return (
    <div className="space-y-6">
      {/* Parameters used */}
      {showParams && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Parameters Used</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <ParamRows job={jobDetails} />
            {jobDetails.strategy_code && (
              <details className="group mt-2 rounded-md border border-slate-800 bg-slate-900/60">
                <summary className="flex cursor-pointer items-center gap-2 p-2 text-sm text-muted-foreground">
                  <ChevronDown className="h-4 w-4 transition-transform group-open:rotate-180" />
                  Strategy code
                </summary>
                <ScrollArea className="h-64 rounded-b-md border-t border-slate-800">
                  <pre className="whitespace-pre-wrap p-3 text-xs text-slate-300">
                    {jobDetails.strategy_code}
                  </pre>
                </ScrollArea>
              </details>
            )}
          </CardContent>
        </Card>
      )}

      {/* Metrics */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <MetricCard
          label="Total Return"
          value={metrics.total_return_pct}
          suffix="%"
          tone={metrics.total_return_pct >= 0 ? 'positive' : 'negative'}
        />
        <MetricCard
          label="CAGR"
          value={metrics.cagr_pct}
          suffix="%"
          tone={metrics.cagr_pct >= 0 ? 'positive' : 'negative'}
        />
        <MetricCard label="Sharpe Ratio" value={metrics.sharpe_ratio} />
        <MetricCard label="Sortino Ratio" value={metrics.sortino_ratio} />
        <MetricCard
          label="Max Drawdown"
          value={metrics.max_drawdown_pct}
          suffix="%"
          tone="negative"
        />
        <MetricCard label="Win Rate" value={metrics.win_rate_pct} suffix="%" />
        <MetricCard label="Profit Factor" value={metrics.profit_factor} />
        <MetricCard
          label="Expectancy"
          value={metrics.expectancy}
          tone={metrics.expectancy >= 0 ? 'positive' : 'negative'}
        />
      </div>

      <div className="grid grid-cols-3 gap-3 sm:grid-cols-6">
        <MetricCard label="Total Trades" value={metrics.total_trades} tone="neutral" />
        <MetricCard label="Avg Win" value={metrics.avg_win} tone="positive" />
        <MetricCard label="Avg Loss" value={metrics.avg_loss} tone="negative" />
        <MetricCard label="W/L Ratio" value={metrics.win_loss_ratio} />
        <MetricCard
          label="Ending Equity"
          value={metrics.ending_equity}
          tone={metrics.ending_equity >= metrics.initial_capital ? 'positive' : 'negative'}
        />
        <MetricCard
          label="Avg Holding"
          value={
            metrics.avg_holding_minutes
              ? `${metrics.avg_holding_bars} bars / ${Math.round(metrics.avg_holding_minutes)} min`
              : metrics.avg_holding_bars
          }
          suffix={metrics.avg_holding_minutes ? undefined : ' bars'}
          tone="neutral"
        />
      </div>

      {/* Equity curve */}
      <Card>
        <CardContent className="p-4">
          {equityPlot ? (
            <Plot
              data={equityPlot.data}
              layout={equityPlot.layout}
              config={PLOT_CONFIG}
              style={{ width: '100%', height: 380 }}
              useResizeHandler
            />
          ) : (
            <p className="text-center text-sm text-muted-foreground py-10">No equity curve data</p>
          )}
        </CardContent>
      </Card>

      <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
        {drawdownPlot && (
          <Card>
            <CardContent className="p-4">
              <Plot
                data={drawdownPlot.data}
                layout={drawdownPlot.layout}
                config={PLOT_CONFIG}
                style={{ width: '100%', height: 280 }}
                useResizeHandler
              />
            </CardContent>
          </Card>
        )}
        {heatmapPlot && (
          <Card>
            <CardContent className="p-4">
              <Plot
                data={heatmapPlot.data}
                layout={heatmapPlot.layout}
                config={PLOT_CONFIG}
                style={{ width: '100%', height: 280 }}
                useResizeHandler
              />
            </CardContent>
          </Card>
        )}
      </div>

      {/* Detailed tabs */}
      <Card>
        <CardContent className="p-4">
          <Tabs defaultValue="trades">
            <div className="flex items-center justify-between">
              <TabsList>
                <TabsTrigger value="trades">
                  <Table2 className="h-4 w-4" /> Trade Log ({result.trades?.length || 0})
                </TabsTrigger>
                <TabsTrigger value="pnl">
                  <BarChart3 className="h-4 w-4" /> Distributions
                </TabsTrigger>
              </TabsList>

              {result.trades && result.trades.length > 0 && (
                <Button variant="outline" size="sm" className="h-8 gap-1.5 text-xs" onClick={handleExportCSV}>
                  <Download className="h-3.5 w-3.5" /> Export CSV
                </Button>
              )}
            </div>
            <TabsContent value="trades" className="mt-4">
              <ScrollArea className="h-96 rounded-md border border-slate-800">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>#</TableHead>
                      <TableHead>Action</TableHead>
                      <TableHead>Entry</TableHead>
                      <TableHead className="text-right">Entry Price</TableHead>
                      <TableHead>Exit</TableHead>
                      <TableHead className="text-right">Exit Price</TableHead>
                      <TableHead className="text-right">Qty</TableHead>
                      <TableHead className="text-right">P&L</TableHead>
                      <TableHead className="text-right">P&L %</TableHead>
                      <TableHead>Exit Reason</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {result.trades.length === 0 && (
                      <TableRow>
                        <TableCell colSpan={10} className="text-center text-muted-foreground">
                          No trades recorded
                        </TableCell>
                      </TableRow>
                    )}
                    {result.trades.map((t, idx) => (
                      <TableRow key={t.id}>
                        <TableCell className="text-muted-foreground">{idx + 1}</TableCell>
                        <TableCell>
                          <Badge variant={t.action === 'BUY' ? 'default' : 'destructive'}>
                            {t.action}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-xs">{t.entry_time}</TableCell>
                        <TableCell className="text-right">{t.entry_price}</TableCell>
                        <TableCell className="text-xs">{t.exit_time}</TableCell>
                        <TableCell className="text-right">{t.exit_price}</TableCell>
                        <TableCell className="text-right">{t.quantity}</TableCell>
                        <TableCell
                          className={`text-right font-medium ${
                            t.pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'
                          }`}
                        >
                          {t.pnl >= 0 ? '+' : ''}
                          {t.pnl}
                        </TableCell>
                        <TableCell
                          className={`text-right ${
                            t.pnl_pct >= 0 ? 'text-emerald-400' : 'text-rose-400'
                          }`}
                        >
                          {t.pnl_pct}%
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          {t.exit_reason || 'Signal Exit'}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </ScrollArea>
            </TabsContent>
            <TabsContent value="pnl" className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2">
              {pnlHistogram && (
                <Plot
                  data={pnlHistogram.data}
                  layout={pnlHistogram.layout}
                  config={PLOT_CONFIG}
                  style={{ width: '100%', height: 300 }}
                  useResizeHandler
                />
              )}
              {holdingHistogram && (
                <Plot
                  data={holdingHistogram.data}
                  layout={holdingHistogram.layout}
                  config={PLOT_CONFIG}
                  style={{ width: '100%', height: 300 }}
                  useResizeHandler
                />
              )}
            </TabsContent>
          </Tabs>
        </CardContent>
      </Card>
    </div>
  )
}
