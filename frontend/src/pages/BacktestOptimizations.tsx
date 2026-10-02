import { Loader2, Square, Trash2 } from 'lucide-react'
import type * as PlotlyTypes from 'plotly.js'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router'
import { fetchCSRFToken } from '@/api/client'
import { PLOT_CONFIG, PLOT_LAYOUT } from '@/components/backtest/plotConfig'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Progress } from '@/components/ui/progress'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import Plot from '@/lib/Plot2D'
import { showToast } from '@/utils/toast'

interface Summary {
  opt_id: string
  name: string
  kind: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'
  progress: number
  error_message: string | null
  created_at: string
}

interface Metrics {
  total_return_pct: number
  sharpe_ratio: number
  max_drawdown_pct: number
  total_trades: number
  win_rate_pct: number
  profit_factor: number
  [key: string]: number
}

interface Row {
  params: Record<string, number | string | boolean>
  score: number | null
  metrics: Metrics | null
  oos_metrics?: Metrics | null
  oos_score?: number | null
  error?: string
}

interface Dsr {
  dsr: number | null
  psr: number | null
  trials: number
}

interface WfWindow {
  window: number
  train: [string, string]
  test: [string, string]
  params?: Record<string, number | string | boolean>
  train_metrics?: Metrics
  test_metrics?: Metrics | null
  skipped?: string
}

interface Result {
  kind: 'sweep' | 'walk_forward'
  objective: string
  trials: number
  warnings: string[]
  notes?: string[]
  deflated_sharpe: Dsr | null
  param_names: string[]
  // sweep
  best?: Row | null
  ranked?: Row[]
  oos_pct?: number
  // walk-forward
  windows?: WfWindow[]
  oos_metrics?: Metrics | null
  oos_equity?: { timestamp: string; equity: number }[]
  walk_forward_efficiency?: number | null
}

interface Detail extends Summary {
  config: { optimize: { objective: string } }
  result: Result | null
}

const fmt = (n: number | null | undefined, d = 2) =>
  n === null || n === undefined ? '-' : n.toLocaleString('en-IN', { maximumFractionDigits: d })
const paramText = (p?: Record<string, unknown>) =>
  p
    ? Object.entries(p)
        .map(([k, v]) => `${k}=${v}`)
        .join(', ')
    : '-'

const STATUS_TONE: Record<string, string> = {
  completed: 'bg-emerald-500/15 text-emerald-300',
  running: 'bg-sky-500/15 text-sky-300',
  pending: 'bg-slate-500/15 text-slate-300',
  failed: 'bg-red-500/15 text-red-300',
  cancelled: 'bg-amber-500/15 text-amber-300',
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-slate-800 bg-slate-900/60 p-3">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="text-base font-semibold">{value}</div>
    </div>
  )
}

function SweepView({ r }: { r: Result }) {
  const ranked = r.ranked ?? []
  const heat = useMemo(() => {
    if (r.param_names.length !== 2) return null
    const [a, b] = r.param_names
    const xs = [...new Set(ranked.map((x) => x.params[a]))].sort((p, q) => Number(p) - Number(q))
    const ys = [...new Set(ranked.map((x) => x.params[b]))].sort((p, q) => Number(p) - Number(q))
    const z = ys.map((y) =>
      xs.map((x) => ranked.find((row) => row.params[a] === x && row.params[b] === y)?.score ?? null)
    )
    return {
      data: [
        {
          x: xs.map(String),
          y: ys.map(String),
          z,
          type: 'heatmap',
          colorscale: 'RdYlGn',
          hoverongaps: false,
          colorbar: { title: { text: r.objective } },
        },
      ] as PlotlyTypes.Data[],
      layout: {
        ...PLOT_LAYOUT,
        height: 340,
        margin: { l: 60, r: 16, t: 30, b: 50 },
        xaxis: { ...PLOT_LAYOUT.xaxis, title: { text: a }, type: 'category' },
        yaxis: { ...PLOT_LAYOUT.yaxis, title: { text: b }, type: 'category' },
      } as Partial<PlotlyTypes.Layout>,
    }
  }, [ranked, r.param_names, r.objective])

  return (
    <div className="space-y-5">
      {r.best ? (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Stat label="Best parameters" value={paramText(r.best.params)} />
          <Stat label={`In-sample ${r.objective}`} value={fmt(r.best.score)} />
          {r.oos_pct ? (
            <Stat label={`Out-of-sample ${r.objective}`} value={fmt(r.best.oos_score)} />
          ) : (
            <Stat label="Return" value={`${fmt(r.best.metrics?.total_return_pct)}%`} />
          )}
          <Stat
            label="Deflated Sharpe"
            value={r.deflated_sharpe?.dsr != null ? fmt(r.deflated_sharpe.dsr, 3) : 'n/a'}
          />
        </div>
      ) : (
        <p className="text-sm text-amber-300">No parameter set qualified.</p>
      )}
      {heat && (
        <Plot
          data={heat.data}
          layout={heat.layout}
          config={PLOT_CONFIG}
          style={{ width: '100%', height: 340 }}
          useResizeHandler
        />
      )}
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>#</TableHead>
            <TableHead>Parameters</TableHead>
            <TableHead className="text-right">{r.objective}</TableHead>
            {r.oos_pct ? <TableHead className="text-right">OOS {r.objective}</TableHead> : null}
            <TableHead className="text-right">Return %</TableHead>
            <TableHead className="text-right">Sharpe</TableHead>
            <TableHead className="text-right">Max DD %</TableHead>
            <TableHead className="text-right">Trades</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {ranked.slice(0, 25).map((row, i) => (
            <TableRow key={paramText(row.params)}>
              <TableCell>{i + 1}</TableCell>
              <TableCell className="font-mono text-xs">{paramText(row.params)}</TableCell>
              <TableCell className="text-right">{fmt(row.score)}</TableCell>
              {r.oos_pct ? (
                <TableCell className="text-right">{fmt(row.oos_score)}</TableCell>
              ) : null}
              <TableCell className="text-right">{fmt(row.metrics?.total_return_pct)}</TableCell>
              <TableCell className="text-right">{fmt(row.metrics?.sharpe_ratio)}</TableCell>
              <TableCell className="text-right">{fmt(row.metrics?.max_drawdown_pct)}</TableCell>
              <TableCell className="text-right">{row.metrics?.total_trades ?? '-'}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      {ranked.length > 25 && (
        <p className="text-xs text-muted-foreground">Showing the top 25 of {ranked.length}.</p>
      )}
    </div>
  )
}

function WalkForwardView({ r }: { r: Result }) {
  const curve = useMemo(() => {
    if (!r.oos_equity?.length) return null
    return {
      data: [
        {
          x: r.oos_equity.map((p) => p.timestamp),
          y: r.oos_equity.map((p) => p.equity),
          type: 'scatter',
          mode: 'lines',
          line: { color: '#38bdf8', width: 1.5 },
        },
      ] as PlotlyTypes.Data[],
      layout: { ...PLOT_LAYOUT, height: 280 } as Partial<PlotlyTypes.Layout>,
    }
  }, [r.oos_equity])
  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Out-of-sample return" value={`${fmt(r.oos_metrics?.total_return_pct)}%`} />
        <Stat label="Out-of-sample Sharpe" value={fmt(r.oos_metrics?.sharpe_ratio)} />
        <Stat label="Out-of-sample max DD" value={`${fmt(r.oos_metrics?.max_drawdown_pct)}%`} />
        <Stat
          label="Walk-forward efficiency"
          value={r.walk_forward_efficiency != null ? fmt(r.walk_forward_efficiency, 2) : 'n/a'}
        />
      </div>
      {curve && (
        <Plot
          data={curve.data}
          layout={curve.layout}
          config={PLOT_CONFIG}
          style={{ width: '100%', height: 280 }}
          useResizeHandler
        />
      )}
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Window</TableHead>
            <TableHead>Train</TableHead>
            <TableHead>Test</TableHead>
            <TableHead>Chosen parameters</TableHead>
            <TableHead className="text-right">Train {r.objective}</TableHead>
            <TableHead className="text-right">Test {r.objective}</TableHead>
            <TableHead className="text-right">Test return %</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {(r.windows ?? []).map((w) => (
            <TableRow key={w.window}>
              <TableCell>{w.window + 1}</TableCell>
              <TableCell className="text-xs">
                {w.train[0].slice(0, 10)} to {w.train[1].slice(0, 10)}
              </TableCell>
              <TableCell className="text-xs">
                {w.test[0].slice(0, 10)} to {w.test[1].slice(0, 10)}
              </TableCell>
              <TableCell className="font-mono text-xs">
                {w.skipped ?? paramText(w.params)}
              </TableCell>
              <TableCell className="text-right">{fmt(w.train_metrics?.[r.objective])}</TableCell>
              <TableCell className="text-right">{fmt(w.test_metrics?.[r.objective])}</TableCell>
              <TableCell className="text-right">{fmt(w.test_metrics?.total_return_pct)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <p className="text-xs text-muted-foreground">
        Efficiency is average out-of-sample score divided by average in-sample score. Below about
        0.5, the in-sample edge mostly did not survive.
      </p>
    </div>
  )
}

export default function BacktestOptimizations() {
  const { optId } = useParams()
  const [list, setList] = useState<Summary[]>([])
  const [detail, setDetail] = useState<Detail | null>(null)
  const [error, setError] = useState<string | null>(null)

  const loadList = useCallback(async () => {
    try {
      const res = await fetch('/backtest/api/optimizations', { credentials: 'include' })
      const body = await res.json()
      if (body.status === 'success') setList(body.optimizations)
    } catch {
      /* the list is a convenience; the detail view reports its own errors */
    }
  }, [])

  const loadDetail = useCallback(async () => {
    if (!optId) return null
    try {
      const res = await fetch(`/backtest/api/optimizations/${optId}`, { credentials: 'include' })
      const body = await res.json()
      if (body.status !== 'success') throw new Error(body.message || 'Not found')
      setDetail(body.optimization as Detail)
      setError(null)
      return body.optimization as Detail
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load')
      return null
    }
  }, [optId])

  useEffect(() => {
    loadList()
  }, [loadList])

  useEffect(() => {
    setDetail(null)
    let stop = false
    let timer: ReturnType<typeof setTimeout>
    const tick = async () => {
      const d = await loadDetail()
      if (!stop && d && (d.status === 'pending' || d.status === 'running')) {
        timer = setTimeout(tick, 2000)
      } else {
        loadList()
      }
    }
    if (optId) tick()
    return () => {
      stop = true
      clearTimeout(timer)
    }
  }, [optId, loadDetail, loadList])

  const act = async (id: string, action: 'cancel' | 'delete') => {
    try {
      const csrf = await fetchCSRFToken()
      const res = await fetch(
        action === 'cancel'
          ? `/backtest/api/optimizations/${id}/cancel`
          : `/backtest/api/optimizations/${id}`,
        {
          method: action === 'cancel' ? 'POST' : 'DELETE',
          credentials: 'include',
          headers: { 'X-CSRFToken': csrf },
        }
      )
      const body = await res.json()
      if (body.status !== 'success') throw new Error(body.message)
      loadList()
      if (id === optId) loadDetail()
    } catch (e) {
      showToast.error(e instanceof Error ? e.message : 'Action failed')
    }
  }

  const r = detail?.result

  return (
    <div className="container mx-auto max-w-6xl space-y-6 p-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Optimizations</h1>
        <Button asChild variant="outline" size="sm">
          <Link to="/backtest">Back to backtester</Link>
        </Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">History</CardTitle>
        </CardHeader>
        <CardContent>
          {list.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              None yet. Use "Optimize" on the backtester page.
            </p>
          ) : (
            <Table>
              <TableBody>
                {list.map((o) => (
                  <TableRow key={o.opt_id} className={o.opt_id === optId ? 'bg-slate-800/40' : ''}>
                    <TableCell>
                      <Link
                        className="text-primary hover:underline"
                        to={`/backtest/optimizations/${o.opt_id}`}
                      >
                        {o.name}
                      </Link>
                    </TableCell>
                    <TableCell>{o.kind === 'walk_forward' ? 'Walk-forward' : 'Sweep'}</TableCell>
                    <TableCell>
                      <Badge className={STATUS_TONE[o.status]}>{o.status}</Badge>
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {o.created_at.slice(0, 16).replace('T', ' ')}
                    </TableCell>
                    <TableCell className="text-right">
                      {o.status === 'running' || o.status === 'pending' ? (
                        <Button size="sm" variant="ghost" onClick={() => act(o.opt_id, 'cancel')}>
                          <Square className="h-3.5 w-3.5" />
                        </Button>
                      ) : (
                        <Button size="sm" variant="ghost" onClick={() => act(o.opt_id, 'delete')}>
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {optId && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-3 text-base">
              {detail?.name ?? optId}
              {detail && <Badge className={STATUS_TONE[detail.status]}>{detail.status}</Badge>}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {error && <p className="text-sm text-red-400">{error}</p>}
            {detail && (detail.status === 'running' || detail.status === 'pending') && (
              <div className="space-y-1">
                <Progress value={detail.progress} />
                <p className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Loader2 className="h-3 w-3 animate-spin" /> {fmt(detail.progress, 0)}% complete
                </p>
              </div>
            )}
            {detail?.error_message && (
              <pre className="whitespace-pre-wrap rounded-md border border-red-900 bg-red-950/40 p-3 text-xs text-red-300">
                {detail.error_message}
              </pre>
            )}
            {r?.warnings?.map((w) => (
              <p
                key={w}
                className="rounded-md border border-amber-800 bg-amber-950/30 p-2 text-sm text-amber-300"
              >
                {w}
              </p>
            ))}
            {r && (r.kind === 'sweep' ? <SweepView r={r} /> : <WalkForwardView r={r} />)}
            {r?.notes?.map((n) => (
              <p key={n} className="text-xs text-muted-foreground">
                {n}
              </p>
            ))}
          </CardContent>
        </Card>
      )}
    </div>
  )
}
