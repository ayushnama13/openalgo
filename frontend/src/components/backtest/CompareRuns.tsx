/**
 * Side-by-side comparison of any backtest runs (POST /backtest/api/compare),
 * plus the small single-run preview shown beside the History table.
 */

import { Loader2 } from 'lucide-react'
import type * as PlotlyTypes from 'plotly.js'
import { useEffect, useMemo, useState } from 'react'
import { fetchCSRFToken } from '@/api/client'
import { PLOT_CONFIG, PLOT_LAYOUT } from '@/components/backtest/plotConfig'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import Plot from '@/lib/Plot2D'

export interface ComparedRun {
  job_id: string
  name: string
  symbols: string[]
  interval: string
  metrics: Record<string, unknown>
  equity_curve: { timestamp: string; equity: number }[]
}

export const MAX_COMPARE = 8

const PALETTE = [
  '#38bdf8',
  '#a78bfa',
  '#34d399',
  '#fbbf24',
  '#f472b6',
  '#fb7185',
  '#4ade80',
  '#f97316',
]

const COLUMNS: { key: string; label: string; best: 'high' | 'low'; suffix?: string }[] = [
  { key: 'total_return_pct', label: 'Return', best: 'high', suffix: '%' },
  { key: 'cagr_pct', label: 'CAGR', best: 'high', suffix: '%' },
  { key: 'sharpe_ratio', label: 'Sharpe', best: 'high' },
  { key: 'sortino_ratio', label: 'Sortino', best: 'high' },
  { key: 'max_drawdown_pct', label: 'Max DD', best: 'low', suffix: '%' },
  { key: 'win_rate_pct', label: 'Win rate', best: 'high', suffix: '%' },
  { key: 'profit_factor', label: 'Profit factor', best: 'high' },
  { key: 'total_trades', label: 'Trades', best: 'high' },
]

const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const fmt = (v: unknown, suffix = '') => {
  const n = num(v)
  return n === null ? '-' : `${n.toLocaleString('en-IN', { maximumFractionDigits: 2 })}${suffix}`
}
const symbolText = (r: ComparedRun) => {
  const s = r.symbols.map((x) => x.split(':').pop())
  return s.length > 2 ? `${s.slice(0, 2).join(', ')} +${s.length - 2}` : s.join(', ')
}

/** Fetch runs for comparison; resolves to the runs found, in request order. */
export async function fetchCompared(jobIds: string[]): Promise<ComparedRun[]> {
  const csrf = await fetchCSRFToken()
  const res = await fetch('/backtest/api/compare', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
    body: JSON.stringify({ job_ids: jobIds }),
  })
  const body = await res.json()
  if (body.status !== 'success') throw new Error(body.message || 'Could not load runs')
  return body.comparison as ComparedRun[]
}

function curveTraces(runs: ComparedRun[], showLegend: boolean): PlotlyTypes.Data[] {
  return runs
    .filter((r) => r.equity_curve.length > 1)
    .map((r, i) => {
      const base = r.equity_curve[0].equity || 1
      return {
        x: r.equity_curve.map((p) => p.timestamp),
        y: r.equity_curve.map((p) => (p.equity / base) * 100),
        type: 'scatter',
        mode: 'lines',
        name: `${r.name} (${r.interval})`,
        showlegend: showLegend,
        line: { color: PALETTE[i % PALETTE.length], width: 1.8 },
      } as PlotlyTypes.Data
    })
}

export function CompareRunsView({ jobIds }: { jobIds: string[] }) {
  const [runs, setRuns] = useState<ComparedRun[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const key = jobIds.join(',')

  // biome-ignore lint/correctness/useExhaustiveDependencies: key is the stable form of jobIds
  useEffect(() => {
    let cancelled = false
    setRuns(null)
    setError(null)
    fetchCompared(jobIds)
      .then((r) => !cancelled && setRuns(r))
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : 'Failed to load'))
    return () => {
      cancelled = true
    }
  }, [key])

  const best = useMemo(() => {
    const out: Record<string, number> = {}
    if (!runs || runs.length < 2) return out
    for (const c of COLUMNS) {
      const vals = runs.map((r) => num(r.metrics[c.key])).filter((v): v is number => v !== null)
      if (vals.length) out[c.key] = c.best === 'low' ? Math.min(...vals) : Math.max(...vals)
    }
    return out
  }, [runs])

  if (error) return <p className="text-sm text-red-400">{error}</p>
  if (!runs)
    return (
      <div className="flex items-center gap-2 p-10 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading runs...
      </div>
    )
  if (!runs.length)
    return <p className="text-sm text-muted-foreground">None of these runs exist any more.</p>

  const traces = curveTraces(runs, true)
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Comparing {runs.length} runs</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto rounded-md border border-border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Run</TableHead>
                  <TableHead>Symbols</TableHead>
                  <TableHead>TF</TableHead>
                  {COLUMNS.map((c) => (
                    <TableHead key={c.key} className="text-right">
                      {c.label}
                    </TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {runs.map((r, i) => (
                  <TableRow key={r.job_id}>
                    <TableCell>
                      <a
                        className="flex items-center gap-2 font-medium text-primary hover:underline"
                        href={`/backtest/results?job_id=${r.job_id}`}
                        target="_blank"
                        rel="noreferrer"
                      >
                        <span
                          className="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
                          style={{ backgroundColor: PALETTE[i % PALETTE.length] }}
                        />
                        {r.name}
                      </a>
                    </TableCell>
                    <TableCell className="text-xs">{symbolText(r)}</TableCell>
                    <TableCell>{r.interval}</TableCell>
                    {COLUMNS.map((c) => {
                      const v = num(r.metrics[c.key])
                      const isBest = v !== null && best[c.key] === v
                      return (
                        <TableCell
                          key={c.key}
                          className={`text-right tabular-nums ${isBest ? 'font-bold text-emerald-400' : ''}`}
                        >
                          {fmt(v, c.suffix)}
                        </TableCell>
                      )
                    })}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
          <p className="mt-2 text-xs text-muted-foreground">
            The best value in each column is highlighted. Runs with different symbols, dates or
            costs are not directly comparable; the table does not adjust for that.
          </p>
        </CardContent>
      </Card>
      {traces.length > 0 && (
        <Card>
          <CardContent className="p-4">
            <Plot
              data={traces}
              layout={
                {
                  ...PLOT_LAYOUT,
                  title: {
                    text: 'Equity curves (normalized to 100)',
                    font: { color: '#e2e8f0', size: 14 },
                  },
                  legend: { orientation: 'h', y: -0.15, font: { color: '#94a3b8' } },
                  yaxis: { ...PLOT_LAYOUT.yaxis, title: { text: 'Index (start = 100)' } },
                } as Partial<PlotlyTypes.Layout>
              }
              config={PLOT_CONFIG}
              style={{ width: '100%', height: 420 }}
              useResizeHandler
            />
          </CardContent>
        </Card>
      )}
    </div>
  )
}

/** Compact preview of one run: headline stats and a small equity curve. */
export function RunPreview({ jobId, onClose }: { jobId: string; onClose: () => void }) {
  const [run, setRun] = useState<ComparedRun | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setRun(null)
    setError(null)
    fetchCompared([jobId])
      .then((r) => {
        if (cancelled) return
        if (r.length) setRun(r[0])
        else setError('Run not found')
      })
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : 'Failed to load'))
    return () => {
      cancelled = true
    }
  }, [jobId])

  const m = run?.metrics ?? {}
  const stats: [string, string, number | null][] = [
    ['Return', fmt(m.total_return_pct, '%'), num(m.total_return_pct)],
    ['Sharpe', fmt(m.sharpe_ratio), null],
    ['Max DD', fmt(m.max_drawdown_pct, '%'), null],
    ['Trades', fmt(m.total_trades), null],
  ]

  return (
    <div className="flex h-full flex-col rounded-md border border-border bg-background/60">
      <div className="flex items-start justify-between gap-2 border-b px-3 py-2">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium">{run?.name ?? 'Loading...'}</div>
          {run && (
            <div className="truncate text-xs text-muted-foreground">
              {symbolText(run)} | {run.interval}
            </div>
          )}
        </div>
        <button
          type="button"
          className="shrink-0 text-xs text-muted-foreground hover:text-foreground"
          onClick={onClose}
        >
          Close
        </button>
      </div>
      {error && <p className="p-3 text-xs text-red-400">{error}</p>}
      {!run && !error && (
        <div className="flex flex-1 items-center justify-center p-6">
          <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
        </div>
      )}
      {run && (
        <div className="space-y-2 p-3">
          <div className="grid grid-cols-2 gap-2">
            {stats.map(([label, value, signed]) => (
              <div key={label} className="rounded border border-border bg-muted/50 px-2 py-1.5">
                <div className="text-[10px] text-muted-foreground">{label}</div>
                <div
                  className={`font-mono text-sm ${signed === null ? '' : signed >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}
                >
                  {value}
                </div>
              </div>
            ))}
          </div>
          {run.equity_curve.length > 1 ? (
            <Plot
              data={curveTraces([run], false)}
              layout={
                {
                  ...PLOT_LAYOUT,
                  margin: { l: 36, r: 8, t: 8, b: 24 },
                  height: 150,
                  xaxis: { ...PLOT_LAYOUT.xaxis, showticklabels: false },
                } as Partial<PlotlyTypes.Layout>
              }
              config={{ ...PLOT_CONFIG, displayModeBar: false }}
              style={{ width: '100%', height: 150 }}
              useResizeHandler
            />
          ) : (
            <p className="text-xs text-muted-foreground">No equity curve for this run.</p>
          )}
          <a
            className="block text-xs text-primary hover:underline"
            href={`/backtest/results?job_id=${run.job_id}`}
            target="_blank"
            rel="noreferrer"
          >
            Open full results
          </a>
        </div>
      )}
    </div>
  )
}
