/**
 * Results of a universe scan: one independent run per constituent on one
 * timeframe. Shows how broad the edge is (breadth stats and a histogram of
 * returns) and a sortable ranking; picking a stock opens its full run below.
 */

import { AlertTriangle, Loader2 } from 'lucide-react'
import type * as PlotlyTypes from 'plotly.js'
import { useMemo, useState } from 'react'
import SingleRunResults, { PLOT_CONFIG, PLOT_LAYOUT } from '@/components/backtest/SingleRunResults'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import Plot from '@/lib/Plot2D'
import { cn } from '@/lib/utils'
import type {
  BatchDetails,
  BatchRunSummary,
  FullJobDetails,
  Metrics,
  ResultsData,
} from '@/stores/strategyBacktestStore'

type SortKey = 'symbol' | keyof Metrics

const COLUMNS: { key: SortKey; label: string; digits?: number }[] = [
  { key: 'symbol', label: 'Symbol' },
  { key: 'total_return_pct', label: 'Return %', digits: 2 },
  { key: 'cagr_pct', label: 'CAGR %', digits: 2 },
  { key: 'sharpe_ratio', label: 'Sharpe', digits: 2 },
  { key: 'max_drawdown_pct', label: 'Max DD %', digits: 2 },
  { key: 'win_rate_pct', label: 'Win %', digits: 1 },
  { key: 'profit_factor', label: 'PF', digits: 2 },
  { key: 'total_trades', label: 'Trades', digits: 0 },
]

export const SURVIVORSHIP_TEXT =
  "Universe runs use the index's current members for every past date. Stocks that were " +
  'dropped from the index are missing (survivorship bias), so results read better than a ' +
  'live run would have.'

export function SurvivorshipBanner({ className }: { className?: string }) {
  return (
    <div
      className={cn(
        'flex gap-2 rounded-md border border-amber-500/30 bg-amber-500/5 p-3 text-xs text-amber-800 dark:text-amber-200/90',
        className
      )}
    >
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
      <span>{SURVIVORSHIP_TEXT}</span>
    </div>
  )
}

export const runSymbol = (r: BatchRunSummary) =>
  (r.symbols?.[0] ?? r.name).split(':').pop() ?? r.name

function median(values: number[]) {
  if (!values.length) return null
  const s = [...values].sort((a, b) => a - b)
  const m = Math.floor(s.length / 2)
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2
}

/** Equal-width bins over the returns, about two per sqrt(n), 8 to 40 of them. */
export function binReturns(values: number[]) {
  if (!values.length) return { centers: [], counts: [], labels: [], width: 1 }
  const lo = Math.min(...values)
  const hi = Math.max(...values)
  const n = Math.min(40, Math.max(8, Math.ceil(Math.sqrt(values.length)) * 2))
  const width = hi > lo ? (hi - lo) / n : 1
  const start = hi > lo ? lo : lo - width / 2
  const counts = new Array<number>(n).fill(0)
  for (const v of values) counts[Math.min(n - 1, Math.floor((v - start) / width))] += 1
  const edges = counts.map((_, i) => start + i * width)
  return {
    centers: edges.map((e) => e + width / 2),
    counts,
    labels: edges.map((e) => `${e.toFixed(1)}% to ${(e + width).toFixed(1)}%`),
    width,
  }
}

const fmt = (v: number | null | undefined, digits = 2) =>
  v == null || !Number.isFinite(v) ? '-' : v.toFixed(digits)

interface Props {
  batch: BatchDetails
  selectedJobId: string | null
  onSelect(run: BatchRunSummary): void
  resultsByJob: Record<string, ResultsData>
  jobDetailsByJob: Record<string, FullJobDetails | null>
  loadingJob: string | null
}

export default function ScanResults({
  batch,
  selectedJobId,
  onSelect,
  resultsByJob,
  jobDetailsByJob,
  loadingJob,
}: Props) {
  const [sortKey, setSortKey] = useState<SortKey>('total_return_pct')
  const [sortDesc, setSortDesc] = useState(true)
  const [filter, setFilter] = useState('')

  const completed = useMemo(
    () => batch.runs.filter((r) => r.status === 'completed' && r.metrics),
    [batch.runs]
  )
  const returns = useMemo(
    () =>
      completed.map((r) => Number(r.metrics?.total_return_pct)).filter((v) => Number.isFinite(v)),
    [completed]
  )
  const stats = useMemo(() => {
    const sharpes = completed
      .map((r) => Number(r.metrics?.sharpe_ratio))
      .filter((v) => Number.isFinite(v))
    const profitable = returns.filter((v) => v > 0).length
    return {
      profitablePct: returns.length ? (profitable / returns.length) * 100 : null,
      medianReturn: median(returns),
      meanSharpe: sharpes.length ? sharpes.reduce((a, b) => a + b, 0) / sharpes.length : null,
      best: returns.length ? Math.max(...returns) : null,
      worst: returns.length ? Math.min(...returns) : null,
    }
  }, [completed, returns])

  // Ranked by return regardless of the table sort, so the rank is stable.
  const rankOf = useMemo(() => {
    const ranked = [...completed].sort(
      (a, b) =>
        Number(b.metrics?.total_return_pct ?? -Infinity) -
        Number(a.metrics?.total_return_pct ?? -Infinity)
    )
    return new Map(ranked.map((r, i) => [r.job_id, i + 1]))
  }, [completed])

  const rows = useMemo(() => {
    const q = filter.trim().toUpperCase()
    const list = q ? batch.runs.filter((r) => runSymbol(r).includes(q)) : [...batch.runs]
    const val = (r: BatchRunSummary) => {
      const v = r.metrics ? Number(r.metrics[sortKey as keyof Metrics]) : NaN
      return Number.isFinite(v) ? v : null
    }
    return list.sort((a, b) => {
      if (sortKey === 'symbol') {
        return runSymbol(a).localeCompare(runSymbol(b)) * (sortDesc ? -1 : 1)
      }
      const av = val(a)
      const bv = val(b)
      // Runs without metrics always sink to the bottom.
      if (av == null || bv == null) return av == null ? (bv == null ? 0 : 1) : -1
      return (av - bv) * (sortDesc ? -1 : 1)
    })
  }, [batch.runs, filter, sortKey, sortDesc])

  const setSort = (key: SortKey) => {
    if (key === sortKey) setSortDesc((d) => !d)
    else {
      setSortKey(key)
      setSortDesc(key !== 'symbol' && key !== 'max_drawdown_pct')
    }
  }

  const failed = batch.runs.filter((r) => r.status === 'failed').length
  const selected = batch.runs.find((r) => r.job_id === selectedJobId) ?? null

  // The 2D Plotly build (lib/plotly-2d) registers no histogram trace, so the
  // bins are computed here and drawn as bars.
  const bins = useMemo(() => binReturns(returns), [returns])
  const histogram: PlotlyTypes.Data[] = [
    {
      x: bins.centers,
      y: bins.counts,
      width: bins.width * 0.95,
      customdata: bins.labels,
      type: 'bar',
      marker: { color: '#38bdf8', line: { color: '#0f172a', width: 1 } },
      hovertemplate: '%{customdata}: %{y} stocks<extra></extra>',
    },
  ]
  const histogramLayout: Partial<PlotlyTypes.Layout> = {
    ...PLOT_LAYOUT,
    title: { text: 'Distribution of returns', font: { color: '#e2e8f0', size: 14 } },
    bargap: 0.04,
    shapes: [
      {
        type: 'line',
        x0: 0,
        x1: 0,
        yref: 'paper',
        y0: 0,
        y1: 1,
        line: { color: '#94a3b8', width: 1, dash: 'dot' },
      },
    ],
    xaxis: { ...PLOT_LAYOUT.xaxis, title: { text: 'Return %', font: { color: '#94a3b8' } } },
    yaxis: { ...PLOT_LAYOUT.yaxis, title: { text: 'Stocks', font: { color: '#94a3b8' } } },
  }

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <Stat
          label="Stocks"
          value={`${completed.length} / ${batch.runs.length}`}
          hint="completed"
        />
        <Stat
          label="Profitable"
          value={stats.profitablePct == null ? '-' : `${stats.profitablePct.toFixed(0)}%`}
          tone={
            stats.profitablePct == null ? undefined : stats.profitablePct >= 50 ? 'good' : 'bad'
          }
        />
        <Stat
          label="Median return"
          value={stats.medianReturn == null ? '-' : `${fmt(stats.medianReturn)}%`}
          tone={stats.medianReturn == null ? undefined : stats.medianReturn > 0 ? 'good' : 'bad'}
        />
        <Stat label="Mean Sharpe" value={fmt(stats.meanSharpe)} />
        <Stat label="Best" value={stats.best == null ? '-' : `${fmt(stats.best)}%`} />
        <Stat label="Worst" value={stats.worst == null ? '-' : `${fmt(stats.worst)}%`} />
      </div>
      {failed > 0 && (
        <p className="text-xs text-muted-foreground">
          {failed} run{failed === 1 ? '' : 's'} failed; they are listed at the bottom of the ranking
          with the error.
        </p>
      )}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
        <Card>
          <CardContent className="p-4">
            {returns.length ? (
              <Plot
                data={histogram}
                layout={histogramLayout}
                config={PLOT_CONFIG}
                style={{ width: '100%', height: 380 }}
                useResizeHandler
              />
            ) : (
              <p className="p-8 text-center text-sm text-muted-foreground">No completed runs</p>
            )}
          </CardContent>
        </Card>

        <Card className="gap-2 py-3">
          <div className="flex items-center justify-between gap-3 px-4">
            <span className="text-sm font-medium">Ranking</span>
            <Input
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter symbol"
              aria-label="Filter symbol"
              className="h-8 w-40"
            />
          </div>
          <div className="max-h-[380px] overflow-auto border-t">
            <table className="w-full text-sm">
              <thead className="sticky top-0 z-10 bg-card text-xs text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 text-right font-medium">#</th>
                  {COLUMNS.map((c) => (
                    <th
                      key={c.key}
                      className={cn(
                        'px-3 py-2 font-medium',
                        c.key === 'symbol' ? 'text-left' : 'text-right'
                      )}
                    >
                      <button
                        type="button"
                        className="hover:text-foreground"
                        onClick={() => setSort(c.key)}
                      >
                        {c.label}
                        {sortKey === c.key ? (sortDesc ? ' (desc)' : ' (asc)') : ''}
                      </button>
                    </th>
                  ))}
                  <th className="px-3 py-2 text-right font-medium">Status</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const m = r.metrics
                  const ret = m ? Number(m.total_return_pct) : NaN
                  return (
                    <tr
                      key={r.job_id}
                      onClick={() => r.status === 'completed' && onSelect(r)}
                      className={cn(
                        'border-t border-border/50',
                        r.status === 'completed' && 'cursor-pointer hover:bg-muted/40',
                        r.job_id === selectedJobId && 'bg-primary/10'
                      )}
                    >
                      <td className="px-3 py-1.5 text-right tabular-nums text-muted-foreground">
                        {rankOf.get(r.job_id) ?? '-'}
                      </td>
                      <td className="px-3 py-1.5 font-mono font-medium">{runSymbol(r)}</td>
                      {COLUMNS.slice(1).map((c) => {
                        const v = m ? Number(m[c.key as keyof Metrics]) : NaN
                        return (
                          <td
                            key={c.key}
                            className={cn(
                              'px-3 py-1.5 text-right tabular-nums',
                              c.key === 'total_return_pct' &&
                                Number.isFinite(ret) &&
                                (ret > 0 ? 'text-emerald-400' : ret < 0 ? 'text-rose-400' : '')
                            )}
                          >
                            {m ? fmt(v, c.digits) : '-'}
                          </td>
                        )
                      })}
                      <td className="px-3 py-1.5 text-right">
                        {r.status === 'completed' ? (
                          <span className="text-xs text-muted-foreground">done</span>
                        ) : (
                          <Badge
                            variant={r.status === 'failed' ? 'destructive' : 'secondary'}
                            title={r.error_message ?? undefined}
                          >
                            {r.status}
                          </Badge>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </Card>
      </div>

      {selected ? (
        resultsByJob[selected.job_id] ? (
          <div className="space-y-2">
            <h2 className="text-lg font-semibold">{runSymbol(selected)}</h2>
            <SingleRunResults
              result={resultsByJob[selected.job_id]}
              jobDetails={jobDetailsByJob[selected.job_id]}
            />
          </div>
        ) : (
          <Card>
            <CardContent className="p-10 text-center text-sm text-muted-foreground">
              {loadingJob === selected.job_id ? (
                <>
                  <Loader2 className="mx-auto h-6 w-6 animate-spin text-primary" />
                  <p className="mt-2">Loading {runSymbol(selected)}...</p>
                </>
              ) : (
                <p>Results for {runSymbol(selected)} could not be loaded.</p>
              )}
            </CardContent>
          </Card>
        )
      ) : (
        <p className="text-center text-sm text-muted-foreground">
          Pick a stock in the ranking to open its full results.
        </p>
      )}
    </div>
  )
}

function Stat({
  label,
  value,
  hint,
  tone,
}: {
  label: string
  value: string
  hint?: string
  tone?: 'good' | 'bad'
}) {
  return (
    <div className="rounded-lg border bg-card px-3 py-2">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div
        className={cn(
          'text-lg font-semibold tabular-nums',
          tone === 'good' && 'text-emerald-400',
          tone === 'bad' && 'text-rose-400'
        )}
      >
        {value}
      </div>
      {hint && <div className="text-[11px] text-muted-foreground">{hint}</div>}
    </div>
  )
}
