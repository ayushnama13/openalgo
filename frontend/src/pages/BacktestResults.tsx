/**
 * Strategy Backtest Results.
 *
 * Two modes, selected by URL:
 * - ?job_id=...   single run — data from the store (set before navigation) or
 *                 fetched on direct link / reload.
 * - ?batch_id=... multi-timeframe sweep — comparison matrix, overlaid equity
 *                 curves normalized to 100, and one full single-run view per
 *                 timeframe (fetched lazily on first tab open, cached in the
 *                 store). Optional &tf=... deep-links a timeframe tab.
 *                 A universe scan batch (mode "scan", one run per stock on one
 *                 timeframe) gets a breadth summary and a ranking instead.
 */

import { FileChartColumn, Loader2, TrendingUp } from 'lucide-react'
import type * as PlotlyTypes from 'plotly.js'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router'
import { CompareRunsView, MAX_COMPARE } from '@/components/backtest/CompareRuns'
import ScanResults, { SurvivorshipBanner } from '@/components/backtest/ScanResults'
import SingleRunResults, { PLOT_CONFIG, PLOT_LAYOUT } from '@/components/backtest/SingleRunResults'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
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
import {
  type BatchRunSummary,
  type FullJobDetails,
  type Metrics,
  type ResultsData,
  useStrategyBacktestStore,
} from '@/stores/strategyBacktestStore'
import { showToast } from '@/utils/toast'

// ---------------------------------------------------------------------------
// Comparison matrix
// ---------------------------------------------------------------------------

type MatrixColumn = {
  key: keyof Metrics | 'interval' | 'status'
  label: string
  best: 'high' | 'low' | null
  render: (r: BatchRunSummary) => string | number
}

const MATRIX_COLUMNS: MatrixColumn[] = [
  { key: 'interval', label: 'Timeframe', best: null, render: (r) => r.interval },
  {
    key: 'total_return_pct',
    label: 'Return %',
    best: 'high',
    render: (r) => r.metrics?.total_return_pct ?? '—',
  },
  { key: 'cagr_pct', label: 'CAGR %', best: 'high', render: (r) => r.metrics?.cagr_pct ?? '—' },
  {
    key: 'sharpe_ratio',
    label: 'Sharpe',
    best: 'high',
    render: (r) => r.metrics?.sharpe_ratio ?? '—',
  },
  {
    key: 'sortino_ratio',
    label: 'Sortino',
    best: 'high',
    render: (r) => r.metrics?.sortino_ratio ?? '—',
  },
  {
    key: 'max_drawdown_pct',
    label: 'Max DD %',
    best: 'low',
    render: (r) => r.metrics?.max_drawdown_pct ?? '—',
  },
  {
    key: 'win_rate_pct',
    label: 'Win %',
    best: 'high',
    render: (r) => r.metrics?.win_rate_pct ?? '—',
  },
  {
    key: 'profit_factor',
    label: 'PF',
    best: 'high',
    render: (r) => r.metrics?.profit_factor ?? '—',
  },
  { key: 'total_trades', label: 'Trades', best: null, render: (r) => r.metrics?.total_trades ?? 0 },
  {
    key: 'expectancy',
    label: 'Expectancy',
    best: 'high',
    render: (r) => r.metrics?.expectancy ?? '—',
  },
  {
    key: 'avg_holding_minutes',
    label: 'Avg Hold (min)',
    best: null,
    render: (r) =>
      r.metrics?.avg_holding_minutes != null ? Math.round(r.metrics.avg_holding_minutes) : '—',
  },
  { key: 'status', label: 'Status', best: null, render: (r) => r.status },
]

function statusBadgeVariant(status: string) {
  if (status === 'completed') return 'default'
  if (status === 'failed') return 'destructive'
  if (status === 'cancelled') return 'secondary'
  return 'outline'
}

function BatchMatrix({
  runs,
  onSelectTf,
}: {
  runs: BatchRunSummary[]
  onSelectTf: (tf: string) => void
}) {
  const [sortKey, setSortKey] = useState<string>('total_return_pct')
  const [sortDesc, setSortDesc] = useState(true)

  const metricCols = MATRIX_COLUMNS.filter((c) => c.key !== 'interval' && c.key !== 'status')
  const ordered = [...runs].sort((a, b) => {
    if (sortKey === 'interval') return a.interval.localeCompare(b.interval) * (sortDesc ? 1 : -1)
    const col = MATRIX_COLUMNS.find((c) => c.key === sortKey)
    if (!col) return 0
    const av = a.metrics ? Number(a.metrics[sortKey as keyof Metrics] ?? -Infinity) : -Infinity
    const bv = b.metrics ? Number(b.metrics[sortKey as keyof Metrics] ?? -Infinity) : -Infinity
    return (av - bv) * (sortDesc ? -1 : 1)
  })

  const bestValues: Record<string, number> = {}
  for (const col of metricCols) {
    const valid = runs
      .filter((r) => r.metrics && !Number.isNaN(Number(r.metrics[col.key as keyof Metrics])))
      .map((r) => Number(r.metrics![col.key as keyof Metrics]))
      .filter((v) => Number.isFinite(v))
    if (!valid.length) continue
    bestValues[col.key] = col.best === 'low' ? Math.min(...valid) : Math.max(...valid)
  }

  const setSort = (key: string) => {
    if (key === sortKey) setSortDesc((d) => !d)
    else {
      setSortKey(key)
      setSortDesc(true)
    }
  }

  return (
    <div className="overflow-x-auto rounded-md border border-slate-800">
      <Table>
        <TableHeader>
          <TableRow>
            {MATRIX_COLUMNS.map((col) => (
              <TableHead
                key={col.key}
                className={
                  col.key === 'interval'
                    ? 'cursor-pointer select-none'
                    : 'cursor-pointer select-none text-right'
                }
                onClick={() => setSort(col.key)}
                title="Click to sort"
              >
                {col.label}
                {sortKey === col.key ? (sortDesc ? ' ↓' : ' ↑') : ''}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {ordered.map((r) => (
            <TableRow key={r.job_id}>
              <TableCell>
                <button
                  type="button"
                  className="font-semibold text-primary hover:underline"
                  onClick={() => onSelectTf(r.interval)}
                  title="Open full results for this timeframe"
                >
                  {r.interval}
                </button>
              </TableCell>
              {metricCols.map((col) => {
                const raw = r.metrics ? Number(r.metrics[col.key as keyof Metrics]) : NaN
                const isBest =
                  col.best != null &&
                  r.metrics &&
                  Number.isFinite(raw) &&
                  Number.isFinite(bestValues[col.key]) &&
                  raw === bestValues[col.key]
                return (
                  <TableCell
                    key={col.key}
                    className={`text-right tabular-nums ${isBest ? 'font-bold text-emerald-400' : ''}`}
                  >
                    {col.render(r)}
                  </TableCell>
                )
              })}
              <TableCell className="text-right">
                <Badge variant={statusBadgeVariant(r.status)}>{r.status}</Badge>
                {r.status === 'failed' && r.error_message ? (
                  <span
                    className="mt-1 block max-w-56 truncate text-xs text-rose-400"
                    title={r.error_message}
                  >
                    {r.error_message}
                  </span>
                ) : null}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Overlaid equity curves (normalized to 100)
// ---------------------------------------------------------------------------

const CURVE_PALETTE = ['#38bdf8', '#a78bfa', '#34d399', '#fbbf24', '#f472b6', '#fb7185', '#4ade80']

export function BatchOverlay({ batchId }: { batchId: string }) {
  const batchCurves = useStrategyBacktestStore((s) => s.batchCurves)
  const batchCurvesFor = useStrategyBacktestStore((s) => s.batchCurvesFor)
  const [loading, setLoading] = useState(batchCurvesFor !== batchId)
  const curves = batchCurvesFor === batchId ? batchCurves : []

  useEffect(() => {
    if (batchCurvesFor === batchId) {
      setLoading(false)
      return
    }
    let cancelled = false
    setLoading(true)
    const fetchCurves = async () => {
      try {
        const res = await fetch(`/backtest/api/batches/${batchId}/curves`, {
          credentials: 'include',
        }).then((r) => r.json())
        if (cancelled) return
        if (res.status === 'success') {
          useStrategyBacktestStore.getState().setBatchCurves(batchId, res.curves ?? [])
        }
      } catch {
        // overlay stays empty on failure
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    fetchCurves()
    return () => {
      cancelled = true
    }
  }, [batchId, batchCurvesFor])

  if (loading) {
    return (
      <Card>
        <CardContent className="p-10 text-center text-sm text-muted-foreground">
          <Loader2 className="mx-auto h-6 w-6 animate-spin text-primary" />
          <p className="mt-2">Loading equity curves...</p>
        </CardContent>
      </Card>
    )
  }

  const withData = curves.filter((c) => c.points.length > 1)
  if (!withData.length) {
    return (
      <Card>
        <CardContent className="p-8 text-center text-sm text-muted-foreground">
          No equity curve data for this batch
        </CardContent>
      </Card>
    )
  }

  const data: PlotlyTypes.Data[] = withData.map((c, i) => {
    const base = c.points[0].equity || 1
    return {
      x: c.points.map((p) => p.timestamp),
      y: c.points.map((p) => (p.equity / base) * 100),
      type: 'scatter',
      mode: 'lines',
      name: c.interval,
      line: { color: CURVE_PALETTE[i % CURVE_PALETTE.length], width: 1.8 },
    }
  })

  const layout: Partial<PlotlyTypes.Layout> = {
    ...PLOT_LAYOUT,
    title: { text: 'Equity Curves (normalized to 100)', font: { color: '#e2e8f0', size: 14 } },
    legend: { orientation: 'h', y: 1.08, font: { color: '#94a3b8' } },
    yaxis: {
      ...PLOT_LAYOUT.yaxis,
      title: { text: 'Index (start = 100)', font: { color: '#94a3b8' } },
    },
  }

  return (
    <Card>
      <CardContent className="p-4">
        <Plot
          data={data}
          layout={layout}
          config={PLOT_CONFIG}
          style={{ width: '100%', height: 400 }}
          useResizeHandler
        />
      </CardContent>
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function BacktestResults() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const jobId = searchParams.get('job_id')
  const batchId = searchParams.get('batch_id')
  const tfParam = searchParams.get('tf')
  const compareParam = searchParams.get('compare')

  if (compareParam) {
    const ids = [...new Set(compareParam.split(',').filter(Boolean))].slice(0, MAX_COMPARE)
    return (
      <div className="space-y-6">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <h1 className="text-2xl font-bold text-foreground">Compare Runs</h1>
            <p className="text-sm text-muted-foreground">Selected from the backtest history</p>
          </div>
          <Button size="sm" onClick={() => navigate('/backtest')}>
            Back to Backtest
          </Button>
        </div>
        <CompareRunsView jobIds={ids} />
      </div>
    )
  }

  if (batchId) return <BatchResultsView batchId={batchId} initialTf={tfParam} />

  return <SingleResultsView jobId={jobId} navigate={navigate} />
}

// ---------------------------------------------------------------------------
// Single-run mode
// ---------------------------------------------------------------------------

function SingleResultsView({
  jobId,
  navigate,
}: {
  jobId: string | null
  navigate: ReturnType<typeof useNavigate>
}) {
  const result = useStrategyBacktestStore((s) => s.result)
  const jobDetails = useStrategyBacktestStore((s) => s.jobDetails)
  const setResult = useStrategyBacktestStore((s) => s.setResult)

  const [loading, setLoading] = useState(false)

  const hasStoredResult = !!result && (!jobId || result.job_id === jobId)

  useEffect(() => {
    if (!jobId) return
    if (hasStoredResult) return
    let cancelled = false
    setLoading(true)
    ;(async () => {
      try {
        const res = await fetch(`/backtest/api/results/${jobId}`, { credentials: 'include' }).then(
          (r) => r.json()
        )
        if (cancelled) return
        if (res.status === 'success') {
          let job: FullJobDetails | null = null
          try {
            const j = await fetch(`/backtest/api/jobs/${jobId}`, { credentials: 'include' }).then(
              (r) => r.json()
            )
            if (!cancelled && j.status === 'success') job = j.job as FullJobDetails
          } catch {
            // parameters card just stays hidden if this fails
          }
          if (!cancelled) setResult(res as ResultsData, job)
        } else {
          if (!cancelled) showToast.error(res.message || 'Failed to load results')
        }
      } catch {
        if (!cancelled) showToast.error('Failed to load results')
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [jobId, hasStoredResult, setResult])

  if (loading) {
    return (
      <div className="space-y-6">
        <Card className="p-12 text-center">
          <Loader2 className="mx-auto h-8 w-8 animate-spin text-primary" />
          <p className="mt-3 text-sm text-muted-foreground">Loading results...</p>
        </Card>
      </div>
    )
  }

  if (!result) {
    return (
      <div className="space-y-6">
        <Card className="p-12 text-center text-muted-foreground">
          <TrendingUp className="mx-auto h-12 w-12 text-slate-700" />
          <h2 className="mt-4 text-lg font-semibold">No backtest results yet</h2>
          <p className="text-sm mt-1">Run a backtest on the setup page; the result opens here.</p>
          <Button className="mt-4" onClick={() => navigate('/backtest')}>
            Go to Strategy Backtesting
          </Button>
        </Card>
      </div>
    )
  }

  // The chart curve is downsampled server-side; the bar count comes from the full run.
  const tradingDays = result.equity_points_total ?? result.equity_curve.length ?? 0

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-foreground">Backtest Results</h1>
          <p className="text-sm text-muted-foreground">{result.name}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="outline">{result.job_id}</Badge>
          <Badge variant="secondary">{tradingDays.toLocaleString()} bars</Badge>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              window.open(`/backtest/api/tearsheet/${result.job_id}`, '_blank')
            }}
          >
            <FileChartColumn className="h-4 w-4" /> Tearsheet
          </Button>
          <Button size="sm" onClick={() => navigate('/backtest')}>
            New Backtest
          </Button>
        </div>
      </div>

      {(jobDetails?.mode === 'universe' || jobDetails?.mode === 'scan') && <SurvivorshipBanner />}

      <SingleRunResults result={result} jobDetails={jobDetails} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Batch mode
// ---------------------------------------------------------------------------

function BatchResultsView({ batchId, initialTf }: { batchId: string; initialTf: string | null }) {
  const navigate = useNavigate()
  const batch = useStrategyBacktestStore((s) => s.batch)
  const setBatch = useStrategyBacktestStore((s) => s.setBatch)
  const resultsByJob = useStrategyBacktestStore((s) => s.resultsByJob)
  const setResultsByJob = useStrategyBacktestStore((s) => s.setResultsByJob)

  const [loading, setLoading] = useState(!batch?.batch_id || batch.batch_id !== batchId)
  const [jobDetailsByJob, setJobDetailsByJob] = useState<Record<string, FullJobDetails | null>>({})
  const [loadingJob, setLoadingJob] = useState<string | null>(null)
  const [activeTf, setActiveTf] = useState<string | null>(initialTf)
  const [scanJobId, setScanJobId] = useState<string | null>(null)

  useEffect(() => {
    if (batch?.batch_id === batchId) {
      setLoading(false)
      return
    }
    let cancelled = false
    setLoading(true)
    ;(async () => {
      try {
        const res = await fetch(`/backtest/api/batches/${batchId}`, {
          credentials: 'include',
        }).then((r) => r.json())
        if (cancelled) return
        if (res.status === 'success') {
          setBatch({
            batch_id: res.batch_id,
            name: res.name,
            mode: res.mode,
            universe: res.universe,
            runs: res.runs,
          })
        } else {
          showToast.error(res.message || 'Failed to load batch')
        }
      } catch {
        showToast.error('Failed to load batch')
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [batchId, batch?.batch_id, setBatch])

  const loadingJobs = useRef(new Set<string>())
  const ensureRunLoaded = useCallback(
    async (run: BatchRunSummary) => {
      if (resultsByJob[run.job_id]) return
      if (loadingJobs.current.has(run.job_id)) return
      loadingJobs.current.add(run.job_id)
      setLoadingJob(run.job_id)
      try {
        const [resRes, jobRes] = await Promise.all([
          fetch(`/backtest/api/results/${run.job_id}`, { credentials: 'include' }).then((r) =>
            r.json()
          ),
          fetch(`/backtest/api/jobs/${run.job_id}`, { credentials: 'include' }).then((r) =>
            r.json()
          ),
        ])
        if (resRes.status === 'success') {
          setResultsByJob(run.job_id, resRes as ResultsData)
          if (jobRes.status === 'success') {
            setJobDetailsByJob((prev) => ({ ...prev, [run.job_id]: jobRes.job as FullJobDetails }))
          }
        } else {
          showToast.error(resRes.message || `Failed to load results for ${run.interval}`)
        }
      } catch {
        showToast.error(`Failed to load results for ${run.interval}`)
      } finally {
        loadingJobs.current.delete(run.job_id)
        setLoadingJob((current) => (current === run.job_id ? null : current))
      }
    },
    [resultsByJob, setResultsByJob]
  )

  // Fetch full results for a timeframe the first time its tab is opened.
  useEffect(() => {
    if (batch?.mode === 'scan') return
    const run = batch?.runs.find((r) => r.interval === activeTf)
    if (!run) return
    ensureRunLoaded(run)
  }, [activeTf, batch?.mode, batch?.runs, ensureRunLoaded])

  const selectScanRun = (run: BatchRunSummary) => {
    setScanJobId(run.job_id)
    ensureRunLoaded(run)
  }

  if (loading || !batch) {
    return (
      <div className="space-y-6">
        <Card className="p-12 text-center">
          <Loader2 className="mx-auto h-8 w-8 animate-spin text-primary" />
          <p className="mt-3 text-sm text-muted-foreground">Loading batch results...</p>
        </Card>
      </div>
    )
  }

  const completedCount = batch.runs.filter((r) => r.status === 'completed').length
  const isScan = batch.mode === 'scan'
  const isUniverse = isScan || batch.mode === 'universe'
  const heading = isScan
    ? 'Universe Scan'
    : batch.mode === 'universe'
      ? 'Universe Portfolio Backtest'
      : 'Multi-Timeframe Backtest'
  const unit = isScan ? 'stocks' : 'timeframes'

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-foreground">{heading}</h1>
          <p className="text-sm text-muted-foreground">
            {batch.name} - {batch.runs.length} {unit}
            {isScan ? ` on ${batch.runs[0]?.interval ?? '-'}` : ''} - {completedCount} completed
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="outline">{batch.batch_id}</Badge>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              window.open(`/backtest/api/tearsheet/${batch.batch_id}`, '_blank')
            }}
          >
            <FileChartColumn className="h-4 w-4" /> Tearsheet
          </Button>
          <Button size="sm" onClick={() => navigate('/backtest')}>
            New Backtest
          </Button>
        </div>
      </div>

      {isUniverse && <SurvivorshipBanner />}

      {isScan ? (
        <ScanResults
          batch={batch}
          selectedJobId={scanJobId}
          onSelect={selectScanRun}
          resultsByJob={resultsByJob}
          jobDetailsByJob={jobDetailsByJob}
          loadingJob={loadingJob}
        />
      ) : (
        <>
          {/* Comparison matrix */}
          <BatchMatrix runs={batch.runs} onSelectTf={setActiveTf} />

          {/* Overlaid equity curves */}
          <BatchOverlay batchId={batch.batch_id} />

          {/* Per-timeframe full views, lazy-loaded on first open */}
          <Tabs value={activeTf ?? batch.runs[0].interval} onValueChange={setActiveTf}>
            <TabsList className="w-full justify-start overflow-x-auto">
              {batch.runs.map((r) => (
                <TabsTrigger key={r.job_id} value={r.interval}>
                  {r.interval}
                </TabsTrigger>
              ))}
            </TabsList>
            {batch.runs.map((r) => (
              <TabsContent key={r.job_id} value={r.interval} className="mt-4">
                {activeTf === r.interval && !resultsByJob[r.job_id] && (
                  <Card>
                    <CardContent className="p-10 text-center text-sm text-muted-foreground">
                      <Loader2 className="mx-auto h-6 w-6 animate-spin text-primary" />
                      <p className="mt-2">
                        {loadingJob === r.job_id
                          ? 'Loading full results...'
                          : 'Results not loaded yet'}
                      </p>
                      {loadingJob !== r.job_id && (
                        <Button
                          className="mt-3"
                          size="sm"
                          variant="outline"
                          onClick={() => ensureRunLoaded(r)}
                        >
                          Load {r.interval} results
                        </Button>
                      )}
                    </CardContent>
                  </Card>
                )}
                {activeTf === r.interval && resultsByJob[r.job_id] && (
                  <SingleRunResults
                    result={resultsByJob[r.job_id]}
                    jobDetails={jobDetailsByJob[r.job_id]}
                  />
                )}
              </TabsContent>
            ))}
          </Tabs>
        </>
      )}
    </div>
  )
}
