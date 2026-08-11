import {
  Activity,
  ChevronDown,
  ChevronRight,
  FlaskConical,
  History,
  LineChart,
  Loader2,
  Play,
  RotateCcw,
  Square,
  Target,
  Trash2,
} from 'lucide-react'
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router'
import { fetchCSRFToken } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Progress } from '@/components/ui/progress'
import { PythonEditor } from '@/components/ui/python-editor'
import { ScrollArea } from '@/components/ui/scroll-area'
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
import { useSocket } from '@/hooks/useSocket'
import {
  type FullJobDetails,
  type ResultsData,
  useStrategyBacktestStore,
} from '@/stores/strategyBacktestStore'
import { showToast } from '@/utils/toast'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface CatalogItem {
  symbol: string
  exchange: string
}

interface RangeInfo {
  first_timestamp: number | null
  last_timestamp: number | null
  start_date: string
  end_date: string
  record_count: number | null
}

interface JobSummary {
  job_id: string
  name: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'
  symbols: string[]
  exchange: string
  interval: string
  start_date?: string
  end_date?: string
  initial_capital: number
  created_at?: string
  completed_at?: string
  total_return_pct: number
  sharpe_ratio: number
  max_drawdown_pct: number
  win_rate_pct: number
  total_trades: number
  batch_id?: string
  batch_name?: string
  batch_seq?: number
  error_message?: string
}

interface BatchJobInfo {
  job_id: string
  interval: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'
}

interface ActiveBatch {
  batch_id: string
  jobs: BatchJobInfo[]
}

// ---------------------------------------------------------------------------
// Helper functions
// ---------------------------------------------------------------------------

function statusBadgeVariant(status: string) {
  if (status === 'completed') return 'default'
  if (status === 'failed') return 'destructive'
  if (status === 'cancelled') return 'secondary'
  return 'outline'
}

// ---------------------------------------------------------------------------
// Strategy templates
// ---------------------------------------------------------------------------

const STRATEGY_TEMPLATES: { label: string; code: string }[] = [
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
]

const DEFAULT_STRATEGY_CODE = STRATEGY_TEMPLATES[0].code

const INTERVALS = ['1m', '5m', '15m', '1h', 'D', 'W', 'M']
const INTERVAL_PRESETS: { label: string; intervals: string[] }[] = [
  { label: 'Intraday', intervals: ['1m', '5m', '15m'] },
  { label: 'Swing', intervals: ['1h', 'D', 'W'] },
  { label: 'All', intervals: [...INTERVALS] },
]
const SIZING_TYPES = [
  { value: 'fixed_qty', label: 'Fixed Quantity' },
  { value: 'pct_equity', label: '% of Equity' },
  { value: 'fixed_capital', label: 'Fixed Capital' },
  { value: 'kelly_vol', label: 'Volatility Adjusted' },
]
const PRODUCT_TYPES = [
  { value: 'MIS', label: 'MIS (Intraday)' },
  { value: 'NRML', label: 'NRML (Carry Forward)' },
  { value: 'CNC', label: 'CNC (Delivery)' },
]
const MISSING_DATA_POLICIES = [
  { value: 'skip', label: 'Skip Missing Bars' },
  { value: 'forward_fill', label: 'Forward Fill' },
  { value: 'abort', label: 'Abort Run' },
]

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function Backtest() {
  const navigate = useNavigate()
  const { socket } = useSocket()
  const setResult = useStrategyBacktestStore((s) => s.setResult)
  const setBatch = useStrategyBacktestStore((s) => s.setBatch)

  // Data source
  const [catalog, setCatalog] = useState<CatalogItem[]>([])
  const [catalogLoading, setCatalogLoading] = useState(true)

  // Form state
  const [symbol, setSymbol] = useState('')
  const [exchange, setExchange] = useState('NSE')
  const [intervals, setIntervals] = useState<string[]>(['1m'])
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')
  const [rangesByInterval, setRangesByInterval] = useState<Record<string, RangeInfo | null>>({})
  const [strategyCode, setStrategyCode] = useState(DEFAULT_STRATEGY_CODE)
  const [initialCapital, setInitialCapital] = useState(100000)
  const [sizingType, setSizingType] = useState('fixed_qty')
  const [sizingValue, setSizingValue] = useState(1)
  const [slippageBps, setSlippageBps] = useState(0)
  const [brokerageBps, setBrokerageBps] = useState(20)
  const [productType, setProductType] = useState('MIS')
  const [missingDataPolicy, setMissingDataPolicy] = useState('skip')
  const [stopLossPct, setStopLossPct] = useState('')
  const [takeProfitPct, setTakeProfitPct] = useState('')
  const [dailyLossLimit, setDailyLossLimit] = useState('')
  const [maxPositions, setMaxPositions] = useState(5)

  // Run state
  const [running, setRunning] = useState(false)
  const [activeJobId, setActiveJobId] = useState<string | null>(null)
  const [activeBatch, setActiveBatch] = useState<ActiveBatch | null>(null)
  const [jobProgress, setJobProgress] = useState<
    Record<string, { percent: number; current_date: string; equity: number; trades_count: number }>
  >({})

  // Jobs
  const [jobs, setJobs] = useState<JobSummary[]>([])
  const [jobsOpen, setJobsOpen] = useState(false)
  const [runningJobIds, setRunningJobIds] = useState<Set<string>>(new Set())

  const socketRef = useRef(socket)
  socketRef.current = socket

  // Mirrors of state read inside stable callbacks below (loadCatalog,
  // loadRanges) - closing over the state directly would force those
  // callbacks' identities to change on every keystroke/selection, which
  // would re-trigger the effects that depend on them and refetch on every
  // symbol pick or date edit instead of once.
  const symbolRef = useRef(symbol)
  symbolRef.current = symbol
  const startDateRef = useRef(startDate)
  startDateRef.current = startDate

  // -------------------------------------------------------------------------
  // Data loading
  // -------------------------------------------------------------------------

  const loadCatalog = useCallback(async () => {
    setCatalogLoading(true)
    try {
      const response = await fetch('/backtest/api/catalog', { credentials: 'include' })
      const data = await response.json()
      if (data.status === 'success') {
        setCatalog(data.catalog || [])
        const first = (data.catalog || [])[0]
        if (first && !symbolRef.current) {
          setSymbol(first.symbol)
          setExchange(first.exchange)
        }
      }
    } catch {
      showToast.error('Failed to load data catalog')
    } finally {
      setCatalogLoading(false)
    }
  }, [])

  const loadRanges = useCallback(async (sym: string, exch: string, ivs: string[]) => {
    const results: Record<string, RangeInfo | null> = {}
    await Promise.all(
      ivs.map(async (iv) => {
        try {
          const params = new URLSearchParams({ symbol: sym, exchange: exch, interval: iv })
          const response = await fetch(`/backtest/api/range?${params}`, {
            credentials: 'include',
          })
          const data = await response.json()
          results[iv] = data.status === 'success' ? (data as RangeInfo) : null
        } catch {
          results[iv] = null
        }
      })
    )
    setRangesByInterval(results)
    // Seed the date window from the first available interval, but never
    // clobber a range the user already picked.
    if (!startDateRef.current) {
      const firstAvail = INTERVALS.find((iv) => results[iv])
      if (firstAvail && results[firstAvail]) {
        setStartDate((results[firstAvail] as RangeInfo).start_date.slice(0, 10))
        setEndDate((results[firstAvail] as RangeInfo).end_date.slice(0, 10))
      }
    }
  }, [])

  const loadJobs = useCallback(async () => {
    try {
      const response = await fetch('/backtest/api/jobs', { credentials: 'include' })
      const data = await response.json()
      if (data.status === 'success') {
        setJobs(data.jobs || [])
        const newRunning = new Set<string>()
        for (const job of data.jobs as JobSummary[]) {
          if (job.status === 'pending' || job.status === 'running') {
            newRunning.add(job.job_id)
            setActiveJobId(job.job_id)
            setRunning(job.status === 'running')
          }
        }
        setRunningJobIds(newRunning)
      }
    } catch {
      // ignore - jobs panel is optional
    }
  }, [])

  useEffect(() => {
    loadCatalog()
    loadJobs()
  }, [loadCatalog, loadJobs])

  useEffect(() => {
    if (symbol && exchange && intervals.length) {
      loadRanges(symbol, exchange, intervals)
    }
  }, [symbol, exchange, intervals, loadRanges])

  // -------------------------------------------------------------------------
  // Socket progress
  // -------------------------------------------------------------------------

  useEffect(() => {
    if (!socket) return

    const handleProgress = (data: {
      job_id: string
      batch_id?: string | null
      batch_seq?: number | null
      interval?: string | null
      percent: number
      current_date: string
      equity: number
      trades_count: number
    }) => {
      if (activeBatch?.jobs.some((j) => j.job_id === data.job_id)) {
        setJobProgress((prev) => ({
          ...prev,
          [data.job_id]: {
            percent: data.percent,
            current_date: data.current_date,
            equity: data.equity,
            trades_count: data.trades_count,
          },
        }))
      } else if (data.job_id === activeJobId) {
        setJobProgress((prev) => ({
          ...prev,
          [data.job_id]: {
            percent: data.percent,
            current_date: data.current_date,
            equity: data.equity,
            trades_count: data.trades_count,
          },
        }))
      }
    }

    socket.on('backtest_progress', handleProgress)
    return () => {
      socket.off('backtest_progress', handleProgress)
    }
  }, [socket, activeJobId, activeBatch])

  // Poll jobs while any run is active to catch terminal state
  useEffect(() => {
    if (!runningJobIds.size) return
    const timer = setInterval(() => {
      loadJobs()
    }, 2500)
    return () => clearInterval(timer)
  }, [runningJobIds, loadJobs])

  // -------------------------------------------------------------------------
  // Actions
  // -------------------------------------------------------------------------

  const handleTemplateChange = (label: string) => {
    const template = STRATEGY_TEMPLATES.find((t) => t.label === label)
    if (template) setStrategyCode(template.code)
  }

  const handleRun = async () => {
    if (!symbol) {
      showToast.error('Please select a symbol')
      return
    }
    if (!intervals.length) {
      showToast.error('Please select at least one timeframe')
      return
    }
    if (!strategyCode.trim()) {
      showToast.error('Strategy code is required')
      return
    }
    const firstRange = rangesByInterval[intervals[0]]
    if (firstRange?.start_date && startDate < firstRange.start_date.slice(0, 10)) {
      showToast.warning('Start date is before the earliest available data')
    }

    setRunning(true)
    setJobProgress({})
    const sweep = intervals.length > 1
    try {
      const csrfToken = await fetchCSRFToken()
      const response = await fetch('/backtest/api/run', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify({
          name: sweep ? `${symbol} MTF backtest` : `${symbol} ${intervals[0]} backtest`,
          strategy_code: strategyCode,
          symbols: [`${exchange}:${symbol}`],
          exchange,
          intervals,
          interval: intervals[0],
          start_date: startDate || null,
          end_date: endDate || null,
          initial_capital: initialCapital,
          sizing_type: sizingType,
          sizing_value: sizingValue,
          slippage_bps: slippageBps,
          brokerage_bps: brokerageBps,
          product_type: productType,
          missing_data_policy: missingDataPolicy,
          stop_loss_pct: stopLossPct ? parseFloat(stopLossPct) : null,
          take_profit_pct: takeProfitPct ? parseFloat(takeProfitPct) : null,
          daily_loss_limit: dailyLossLimit ? parseFloat(dailyLossLimit) : null,
          max_positions: maxPositions,
        }),
      })
      const data = await response.json()
      if (data.status === 'success') {
        setActiveJobId(data.jobs?.[0]?.job_id ?? data.job_id)
        setActiveBatch(sweep ? { batch_id: data.batch_id, jobs: data.jobs ?? [] } : null)
        showToast.success(
          sweep
            ? `Backtest batch ${data.batch_id} submitted (${data.jobs?.length ?? intervals.length} timeframes)`
            : `Backtest job ${data.job_id} submitted`
        )
        loadJobs()
      } else {
        showToast.error(data.message || 'Failed to submit backtest job')
        setRunning(false)
      }
    } catch {
      showToast.error('Failed to submit backtest job')
      setRunning(false)
    }
  }

  const handleCancel = async () => {
    try {
      const csrfToken = await fetchCSRFToken()
      if (activeBatch) {
        await fetch(`/backtest/api/batches/${activeBatch.batch_id}/cancel`, {
          method: 'POST',
          credentials: 'include',
          headers: { 'X-CSRFToken': csrfToken },
        })
        showToast.info('Cancel request sent for the whole batch')
      } else if (activeJobId) {
        await fetch(`/backtest/api/cancel/${activeJobId}`, {
          method: 'POST',
          credentials: 'include',
          headers: { 'X-CSRFToken': csrfToken },
        })
        showToast.info('Cancel request sent')
      }
    } catch {
      showToast.error('Failed to cancel job')
    }
  }

  const checkJobDone = useCallback(
    (jobId: string) => {
      const job = jobs.find((j) => j.job_id === jobId)
      if (!job) return false
      return job.status === 'completed' || job.status === 'failed' || job.status === 'cancelled'
    },
    [jobs]
  )

  const toggleInterval = (iv: string) => {
    setIntervals((prev) => {
      if (prev.includes(iv)) return prev.length > 1 ? prev.filter((x) => x !== iv) : prev
      return [...prev, iv]
    })
  }

  const openResults = useCallback(
    async (jobId: string) => {
      try {
        const [resRes, jobRes] = await Promise.all([
          fetch(`/backtest/api/results/${jobId}`, { credentials: 'include' }).then((r) => r.json()),
          fetch(`/backtest/api/jobs/${jobId}`, { credentials: 'include' }).then((r) => r.json()),
        ])
        if (resRes.status === 'success') {
          const job = jobRes.status === 'success' ? (jobRes.job as FullJobDetails) : null
          setResult(resRes as ResultsData, job)
          navigate(`/backtest/results?job_id=${jobId}`)
          return
        }
        showToast.error(resRes.message || 'Failed to load results')
      } catch {
        showToast.error('Failed to load results')
      }
    },
    [navigate, setResult]
  )

  const openBatchResults = useCallback(
    async (batchId: string) => {
      try {
        const response = await fetch(`/backtest/api/batches/${batchId}`, { credentials: 'include' })
        const data = await response.json()
        if (data.status === 'success') {
          setBatch({
            batch_id: data.batch_id,
            name: data.name,
            runs: data.runs,
          })
          navigate(`/backtest/results?batch_id=${batchId}`)
          return
        }
        showToast.error(data.message || 'Failed to load batch results')
      } catch {
        showToast.error('Failed to load batch results')
      }
    },
    [navigate, setBatch]
  )

  // When all runs of the active batch reach a terminal state, open the
  // batch results page. Single runs keep their existing per-job behavior.
  useEffect(() => {
    if (!running) return
    if (activeBatch?.jobs.length) {
      const batchJobs = jobs.filter((j) => activeBatch.jobs.some((bj) => bj.job_id === j.job_id))
      if (
        batchJobs.length === activeBatch.jobs.length &&
        batchJobs.every((j) => checkJobDone(j.job_id))
      ) {
        setRunning(false)
        setActiveBatch(null)
        const allOk = batchJobs.every((j) => j.status === 'completed')
        if (allOk) {
          openBatchResults(activeBatch.batch_id)
        } else {
          showToast.warning('Batch finished with some failures - see job history')
        }
      }
      return
    }
    if (activeJobId && checkJobDone(activeJobId)) {
      const job = jobs.find((j) => j.job_id === activeJobId)
      if (job) {
        setRunning(false)
        if (job.status === 'completed') {
          openResults(activeJobId)
        }
      }
    }
  }, [jobs, running, activeJobId, activeBatch, checkJobDone, openResults, openBatchResults])

  const applyJobParams = useCallback((job: FullJobDetails) => {
    setStrategyCode(job.strategy_code)
    setSymbol((job.symbols[0] || '').split(':')[1] || job.symbols[0] || '')
    setExchange(job.exchange)
    setIntervals([job.interval])
    setStartDate(job.start_date || '')
    setEndDate(job.end_date || '')
    setInitialCapital(job.initial_capital)
    setSizingType(job.sizing_type)
    setSizingValue(job.sizing_value)
    setSlippageBps(job.slippage_bps)
    setBrokerageBps(job.brokerage_bps)
    setProductType(job.product_type)
    setMissingDataPolicy(job.missing_data_policy)
    setStopLossPct(job.stop_loss_pct != null ? String(job.stop_loss_pct) : '')
    setTakeProfitPct(job.take_profit_pct != null ? String(job.take_profit_pct) : '')
    setDailyLossLimit(job.daily_loss_limit != null ? String(job.daily_loss_limit) : '')
    setMaxPositions(job.max_positions)
  }, [])

  const handleReRun = async (job: JobSummary) => {
    try {
      const response = await fetch(`/backtest/api/jobs/${job.job_id}`, { credentials: 'include' })
      const data = await response.json()
      if (data.status === 'success' && data.job) {
        applyJobParams(data.job as FullJobDetails)
        showToast.success('Job parameters loaded - press Run')
      }
    } catch {
      showToast.error('Failed to load job parameters')
    }
  }

  const handleBatchReRun = async (kids: JobSummary[]) => {
    try {
      const response = await fetch(`/backtest/api/jobs/${kids[0].job_id}`, {
        credentials: 'include',
      })
      const data = await response.json()
      if (data.status === 'success' && data.job) {
        applyJobParams(data.job as FullJobDetails)
        setIntervals(kids.map((k) => k.interval))
        showToast.success(`Batch parameters loaded for ${kids.length} timeframes - press Run`)
      }
    } catch {
      showToast.error('Failed to load batch parameters')
    }
  }

  const handleDeleteJob = async (jobId: string) => {
    try {
      const csrfToken = await fetchCSRFToken()
      const response = await fetch(`/backtest/api/jobs/${jobId}`, {
        method: 'DELETE',
        credentials: 'include',
        headers: { 'X-CSRFToken': csrfToken },
      })
      const data = await response.json()
      if (data.status === 'success') {
        showToast.success('Job deleted')
        loadJobs()
      }
    } catch {
      showToast.error('Failed to delete job')
    }
  }

  const handleDeleteBatch = async (batchId: string) => {
    try {
      const csrfToken = await fetchCSRFToken()
      const response = await fetch(`/backtest/api/batches/${batchId}`, {
        method: 'DELETE',
        credentials: 'include',
        headers: { 'X-CSRFToken': csrfToken },
      })
      const data = await response.json()
      if (data.status === 'success') {
        showToast.success('Batch deleted')
        loadJobs()
      }
    } catch {
      showToast.error('Failed to delete batch')
    }
  }

  // Group jobs into batches for the history table. A legacy/single run is a
  // batch of one (batch_id = job_id) and renders as a flat row.
  const batchGroups = useMemo(() => {
    const map = new Map<string, JobSummary[]>()
    for (const job of jobs) {
      const key = job.batch_id || job.job_id
      const arr = map.get(key) ?? []
      arr.push(job)
      map.set(key, arr)
    }
    return [...map.entries()].map(([batchId, kids]) => ({
      batchId,
      kids: kids.sort((a, b) => (a.batch_seq ?? 0) - (b.batch_seq ?? 0)),
    }))
  }, [jobs])

  const [expandedBatches, setExpandedBatches] = useState<Set<string>>(new Set())

  const toggleBatchRow = (batchId: string) => {
    setExpandedBatches((prev) => {
      const next = new Set(prev)
      if (next.has(batchId)) next.delete(batchId)
      else next.add(batchId)
      return next
    })
  }

  const batchStatusBadge = (kids: JobSummary[]) => {
    const statuses = kids.map((k) => k.status)
    if (statuses.every((s) => s === 'completed')) return 'completed'
    if (statuses.some((s) => s === 'running')) return 'running'
    if (statuses.some((s) => s === 'pending')) return 'pending'
    if (statuses.some((s) => s === 'failed') || statuses.some((s) => s === 'cancelled'))
      return 'partial'
    return 'pending'
  }

  const jobRowActions = (job: JobSummary, batchId?: string) => (
    <div className="flex items-center gap-1">
      {job.status === 'completed' && (
        <Button
          variant="ghost"
          size="icon"
          title="View results in new tab"
          onClick={(e) => {
            e.stopPropagation()
            window.open(`/backtest/results?job_id=${job.job_id}`, '_blank')
          }}
        >
          <LineChart className="h-4 w-4" />
        </Button>
      )}
      <Button
        variant="ghost"
        size="icon"
        title="Rerun with same parameters"
        onClick={(e) => {
          e.stopPropagation()
          if (batchId) {
            setJobsOpen(false)
            const kids = batchGroups.find((g) => g.batchId === batchId)?.kids
            if (kids) handleBatchReRun(kids)
          } else {
            setJobsOpen(false)
            handleReRun(job)
          }
        }}
      >
        <RotateCcw className="h-4 w-4" />
      </Button>
      <Button
        variant="ghost"
        size="icon"
        title="Delete"
        onClick={(e) => {
          e.stopPropagation()
          handleDeleteJob(job.job_id)
        }}
      >
        <Trash2 className="h-4 w-4 text-rose-400" />
      </Button>
    </div>
  )

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-foreground">Strategy Backtesting</h1>
          <p className="text-sm text-muted-foreground">
            Configure a strategy, set the data window and risk parameters, then run the backtest.
            Results open on a dedicated page.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {running && (
            <Button variant="destructive" onClick={handleCancel} disabled={!activeJobId}>
              <Square className="h-4 w-4" /> Cancel
            </Button>
          )}
          <Button variant="outline" onClick={() => setJobsOpen(true)}>
            <History className="h-4 w-4" /> Job History
          </Button>
          <Button onClick={handleRun} disabled={running}>
            {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
            {running ? 'Running...' : 'Run Backtest'}
          </Button>
        </div>
      </div>

      {/* Run progress */}
      {running && activeBatch && (
        <Card>
          <CardContent className="p-4 space-y-3">
            <div className="flex items-center justify-between text-sm">
              <span className="text-muted-foreground">
                Batch {activeBatch.batch_id} - {activeBatch.jobs.length} timeframes
              </span>
              <span className="font-medium">
                Trades: {Object.values(jobProgress).reduce((sum, p) => sum + p.trades_count, 0)}
              </span>
            </div>
            {activeBatch.jobs.map((j) => {
              const p = jobProgress[j.job_id]
              const pendingState = j.status === 'failed' ? 'failed' : 'queued'
              return (
                <div key={j.job_id} className="space-y-1">
                  <div className="flex items-center justify-between text-xs">
                    <span className="text-muted-foreground">
                      {j.interval}
                      {p ? ` - ${p.current_date}` : ` - ${pendingState}`}
                    </span>
                    <span className="tabular-nums">{p ? `${p.percent.toFixed(1)}%` : '0.0%'}</span>
                  </div>
                  <Progress value={p?.percent ?? 0} />
                </div>
              )
            })}
          </CardContent>
        </Card>
      )}
      {running && !activeBatch && activeJobId && jobProgress[activeJobId] && (
        <Card>
          <CardContent className="p-4 space-y-2">
            <div className="flex items-center justify-between text-sm">
              <span className="text-muted-foreground">
                Processing {jobProgress[activeJobId].current_date} - equity{' '}
                {jobProgress[activeJobId].equity.toLocaleString()}
              </span>
              <span className="font-medium">Trades: {jobProgress[activeJobId].trades_count}</span>
            </div>
            <Progress value={jobProgress[activeJobId].percent} />
            <div className="text-right text-xs text-muted-foreground">
              {jobProgress[activeJobId].percent.toFixed(1)}%
            </div>
          </CardContent>
        </Card>
      )}

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
        {/* Data source */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base flex items-center gap-2">
              <Activity className="h-4 w-4 text-primary" /> Data Source
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label>Symbol</Label>
                <Select
                  value={symbol}
                  onValueChange={(v) => {
                    setSymbol(v)
                    const item = catalog.find((c) => c.symbol === v)
                    if (item) setExchange(item.exchange)
                  }}
                >
                  <SelectTrigger disabled={catalogLoading}>
                    <SelectValue placeholder={catalogLoading ? 'Loading...' : 'Select symbol'} />
                  </SelectTrigger>
                  <SelectContent>
                    <ScrollArea className="h-72">
                      {catalog.map((item) => (
                        <SelectItem key={`${item.exchange}:${item.symbol}`} value={item.symbol}>
                          {item.symbol} ({item.exchange})
                        </SelectItem>
                      ))}
                    </ScrollArea>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label>Timeframes</Label>
                <div className="flex flex-wrap gap-1.5">
                  {INTERVALS.map((iv) => {
                    const selected = intervals.includes(iv)
                    const r = rangesByInterval[iv]
                    return (
                      <Button
                        key={iv}
                        size="sm"
                        variant={selected ? 'default' : 'outline'}
                        onClick={() => toggleInterval(iv)}
                        title={
                          r
                            ? `${iv}: ${r.record_count?.toLocaleString() ?? '?'} bars from ${r.start_date}`
                            : `${iv}: no available data`
                        }
                      >
                        {iv}
                      </Button>
                    )
                  })}
                </div>
                <div className="flex flex-wrap items-center gap-1.5">
                  {INTERVAL_PRESETS.map((p) => (
                    <Button
                      key={p.label}
                      size="sm"
                      variant="ghost"
                      className="h-6 px-2 text-xs"
                      onClick={() => setIntervals(p.intervals)}
                    >
                      {p.label}
                    </Button>
                  ))}
                  <span className="text-xs text-muted-foreground">
                    - select one or more timeframes
                  </span>
                </div>
                {intervals.length > 0 && (
                  <div className="flex flex-wrap gap-x-3 gap-y-1">
                    {intervals.map((iv) => {
                      const r = rangesByInterval[iv]
                      return (
                        <span key={iv} className="inline-flex items-center gap-1.5 text-xs">
                          <span
                            className={`h-1.5 w-1.5 rounded-full ${r ? 'bg-emerald-400' : 'bg-slate-600'}`}
                          />
                          {iv}
                          <span className={r ? 'text-muted-foreground' : 'text-slate-500'}>
                            {r ? `${(r.record_count ?? 0).toLocaleString()} bars` : 'no data'}
                          </span>
                        </span>
                      )
                    })}
                  </div>
                )}
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label>Start Date</Label>
                <Input
                  type="date"
                  value={startDate}
                  onChange={(e) => setStartDate(e.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <Label>End Date</Label>
                <Input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
              </div>
            </div>

            <div className="rounded-md border border-slate-800 bg-slate-900/60 p-2 text-xs text-muted-foreground">
              Available data per selected timeframe is shown above (green = has data, grey = none).
              Runs without data are marked failed at submit time; the rest of the batch proceeds.
            </div>
          </CardContent>
        </Card>

        {/* Strategy */}
        <Card>
          <CardHeader className="flex-row items-center justify-between space-y-0">
            <CardTitle className="text-base flex items-center gap-2">
              <FlaskConical className="h-4 w-4 text-primary" /> Strategy
            </CardTitle>
            <div className="flex items-center gap-2">
              <Label className="text-xs text-muted-foreground">Template</Label>
              <Select onValueChange={handleTemplateChange}>
                <SelectTrigger className="w-44">
                  <SelectValue placeholder="Load template" />
                </SelectTrigger>
                <SelectContent>
                  {STRATEGY_TEMPLATES.map((t) => (
                    <SelectItem key={t.label} value={t.label}>
                      {t.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="rounded-lg border border-slate-800 overflow-hidden">
              <PythonEditor value={strategyCode} onChange={setStrategyCode} height="280px" />
            </div>
            <p className="text-xs text-muted-foreground">
              Define a <code className="text-primary">strategy(df)</code> function returning the
              bars DataFrame with a <code className="text-primary">signal</code> column: 1 = BUY, -1
              = SELL/exit. Available: <code className="text-primary">df</code>,{' '}
              <code className="text-primary">ta</code> (openalgo.ta indicators),{' '}
              <code className="text-primary">pd</code>, <code className="text-primary">np</code>.
            </p>
          </CardContent>
        </Card>

        {/* Execution & Risk */}
        <Card className="xl:col-span-2">
          <CardHeader>
            <CardTitle className="text-base flex items-center gap-2">
              <Target className="h-4 w-4 text-primary" /> Execution & Risk
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              <div className="space-y-1.5">
                <Label>Initial Capital</Label>
                <Input
                  type="number"
                  min={0}
                  value={initialCapital}
                  onChange={(e) => setInitialCapital(Number(e.target.value))}
                />
              </div>
              <div className="space-y-1.5">
                <Label>Product Type</Label>
                <Select value={productType} onValueChange={setProductType}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {PRODUCT_TYPES.map((p) => (
                      <SelectItem key={p.value} value={p.value}>
                        {p.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label>Position Sizing</Label>
                <Select value={sizingType} onValueChange={setSizingType}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {SIZING_TYPES.map((s) => (
                      <SelectItem key={s.value} value={s.value}>
                        {s.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label>
                  {sizingType === 'fixed_qty'
                    ? 'Quantity'
                    : sizingType === 'pct_equity'
                      ? 'Percent of Equity'
                      : 'Amount'}
                </Label>
                <Input
                  type="number"
                  min={0}
                  value={sizingValue}
                  onChange={(e) => setSizingValue(Number(e.target.value))}
                />
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              <div className="space-y-1.5">
                <Label>Slippage (bps)</Label>
                <Input
                  type="number"
                  min={0}
                  value={slippageBps}
                  onChange={(e) => setSlippageBps(Number(e.target.value))}
                />
              </div>
              <div className="space-y-1.5">
                <Label>Brokerage (bps)</Label>
                <Input
                  type="number"
                  min={0}
                  value={brokerageBps}
                  onChange={(e) => setBrokerageBps(Number(e.target.value))}
                />
              </div>
              <div className="space-y-1.5">
                <Label>Missing Data Policy</Label>
                <Select value={missingDataPolicy} onValueChange={setMissingDataPolicy}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {MISSING_DATA_POLICIES.map((m) => (
                      <SelectItem key={m.value} value={m.value}>
                        {m.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label>Max Open Positions</Label>
                <Input
                  type="number"
                  min={1}
                  value={maxPositions}
                  onChange={(e) => setMaxPositions(Number(e.target.value))}
                />
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
              <div className="space-y-1.5">
                <Label>Stop Loss %</Label>
                <Input
                  type="number"
                  value={stopLossPct}
                  placeholder="Optional"
                  onChange={(e) => setStopLossPct(e.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <Label>Take Profit %</Label>
                <Input
                  type="number"
                  value={takeProfitPct}
                  placeholder="Optional"
                  onChange={(e) => setTakeProfitPct(e.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <Label>Daily Loss Limit (Rs)</Label>
                <Input
                  type="number"
                  value={dailyLossLimit}
                  placeholder="Optional"
                  onChange={(e) => setDailyLossLimit(e.target.value)}
                />
              </div>
            </div>

            <p className="text-xs text-muted-foreground">
              MIS positions are auto squared-off at the exchange close time (e.g. 15:15 IST for
              NSE). Brokerage & slippage apply per fill. Daily loss limit kills the strategy for the
              day when breached.
            </p>
          </CardContent>
        </Card>
      </div>

      {/* Job history dialog */}
      <Dialog open={jobsOpen} onOpenChange={setJobsOpen}>
        {/* sm:max-w-7xl, not max-w-7xl: DialogContent's own base class is
            sm:max-w-lg, and tailwind-merge keeps responsive and unprefixed
            max-w-* as separate groups - so an unprefixed override loses to it
            above 640px and the dialog collapses back to 32rem. */}
        <DialogContent className="w-[95vw] sm:max-w-7xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <History className="h-5 w-5" /> Backtest Job History
            </DialogTitle>
            <DialogDescription>
              Past runs with status and key metrics. Click a row to open its results page.
            </DialogDescription>
          </DialogHeader>

          {/* Native scroll container rather than ScrollArea: Radix renders its
              viewport as display:table, which shrink-wraps to content instead
              of taking the dialog's width, so a nested overflow-x-auto never
              gets a definite width to scroll against and the table paints
              outside the dialog. */}
          <div className="max-h-[26rem] overflow-auto rounded-md border border-slate-800">
            <div>
              <Table className="min-w-[750px]">
                <TableHeader>
                  <TableRow>
                    <TableHead>Name</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Symbol</TableHead>
                    <TableHead>Interval</TableHead>
                    <TableHead className="text-right">Return %</TableHead>
                    <TableHead className="text-right">Sharpe</TableHead>
                    <TableHead className="text-right">Max DD %</TableHead>
                    <TableHead className="text-right">Trades</TableHead>
                    <TableHead className="text-right">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {batchGroups.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={9} className="text-center py-10 text-muted-foreground">
                        No backtest jobs have been submitted yet
                      </TableCell>
                    </TableRow>
                  )}
                  {batchGroups.map(({ batchId, kids }) => {
                    const isBatch = kids.length > 1
                    if (!isBatch) {
                      const job = kids[0]
                      return (
                        <TableRow key={job.job_id} className="cursor-pointer">
                          <TableCell className="font-medium">{job.name}</TableCell>
                          <TableCell>
                            <Badge variant={statusBadgeVariant(job.status)}>{job.status}</Badge>
                          </TableCell>
                          <TableCell className="text-xs">
                            {(job.symbols || []).join(', ')}
                          </TableCell>
                          <TableCell>{job.interval}</TableCell>
                          <TableCell
                            className={`text-right ${
                              job.total_return_pct >= 0 ? 'text-emerald-400' : 'text-rose-400'
                            }`}
                          >
                            {job.total_return_pct}%
                          </TableCell>
                          <TableCell className="text-right">{job.sharpe_ratio ?? '-'}</TableCell>
                          <TableCell className="text-right">{job.max_drawdown_pct}%</TableCell>
                          <TableCell className="text-right">{job.total_trades}</TableCell>
                          <TableCell>{jobRowActions(job, undefined)}</TableCell>
                        </TableRow>
                      )
                    }
                    const expanded = expandedBatches.has(batchId)
                    const rollup = batchStatusBadge(kids)
                    const bestReturn = Math.max(...kids.map((k) => k.total_return_pct))
                    const worstReturn = Math.min(...kids.map((k) => k.total_return_pct))
                    return (
                      <Fragment key={batchId}>
                        <TableRow
                          className="cursor-pointer bg-slate-900/40"
                          onClick={() => toggleBatchRow(batchId)}
                        >
                          <TableCell className="font-medium">
                            <span className="flex items-center gap-1.5">
                              {expanded ? (
                                <ChevronDown className="h-4 w-4 text-muted-foreground" />
                              ) : (
                                <ChevronRight className="h-4 w-4 text-muted-foreground" />
                              )}
                              {kids[0].batch_name || kids[0].name}
                            </span>
                          </TableCell>
                          <TableCell>
                            <Badge variant={statusBadgeVariant(rollup)}>{rollup}</Badge>
                          </TableCell>
                          <TableCell className="text-xs">
                            {(kids[0].symbols || []).join(', ')}
                          </TableCell>
                          <TableCell>{kids.length} TF</TableCell>
                          <TableCell className="text-right">
                            <span
                              className={bestReturn >= 0 ? 'text-emerald-400' : 'text-rose-400'}
                            >
                              {bestReturn}%
                            </span>
                            <span className="text-xs text-muted-foreground ml-1">
                              ({worstReturn}%)
                            </span>
                          </TableCell>
                          <TableCell className="text-right text-muted-foreground">-</TableCell>
                          <TableCell className="text-right text-muted-foreground">-</TableCell>
                          <TableCell className="text-right">{kids.length}</TableCell>
                          <TableCell>
                            <div className="flex items-center gap-1">
                              {kids.some((k) => k.status === 'completed') && (
                                <Button
                                  variant="ghost"
                                  size="icon"
                                  title="View batch results in new tab"
                                  onClick={(e) => {
                                    e.stopPropagation()
                                    window.open(`/backtest/results?batch_id=${batchId}`, '_blank')
                                  }}
                                >
                                  <LineChart className="h-4 w-4" />
                                </Button>
                              )}
                              <Button
                                variant="ghost"
                                size="icon"
                                title="Rerun all timeframes with the same parameters"
                                onClick={(e) => {
                                  e.stopPropagation()
                                  setJobsOpen(false)
                                  handleBatchReRun(kids)
                                }}
                              >
                                <RotateCcw className="h-4 w-4" />
                              </Button>
                              <Button
                                variant="ghost"
                                size="icon"
                                title="Delete whole batch"
                                onClick={(e) => {
                                  e.stopPropagation()
                                  handleDeleteBatch(batchId)
                                }}
                              >
                                <Trash2 className="h-4 w-4 text-rose-400" />
                              </Button>
                            </div>
                          </TableCell>
                        </TableRow>
                        {expanded &&
                          kids.map((job) => (
                            <TableRow
                              key={job.job_id}
                              className="border-l-2 border-slate-800 pl-6 cursor-pointer bg-slate-900/20"
                            >
                              <TableCell className="pl-8 text-xs text-muted-foreground">
                                {job.name}
                              </TableCell>
                              <TableCell>
                                <Badge variant={statusBadgeVariant(job.status)}>{job.status}</Badge>
                              </TableCell>
                              <TableCell className="text-xs">-</TableCell>
                              <TableCell className="pl-8">{job.interval}</TableCell>
                              <TableCell
                                className={`text-right ${
                                  job.total_return_pct >= 0 ? 'text-emerald-400' : 'text-rose-400'
                                }`}
                              >
                                {job.total_return_pct}%
                              </TableCell>
                              <TableCell className="text-right">
                                {job.sharpe_ratio ?? '-'}
                              </TableCell>
                              <TableCell className="text-right">{job.max_drawdown_pct}%</TableCell>
                              <TableCell className="text-right">{job.total_trades}</TableCell>
                              <TableCell>{jobRowActions(job, batchId)}</TableCell>
                            </TableRow>
                          ))}
                      </Fragment>
                    )
                  })}
                </TableBody>
              </Table>
            </div>
          </div>

          <DialogFooter>
            <Button
              variant="outline"
              onClick={async () => {
                const csrfToken = await fetchCSRFToken()
                await fetch('/backtest/api/cleanup', {
                  method: 'POST',
                  credentials: 'include',
                  headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
                  body: JSON.stringify({ days_to_keep: 30 }),
                })
                showToast.success('Old runs purged (retention 30 days)')
                loadJobs()
              }}
            >
              <Trash2 className="h-4 w-4" /> Purge runs older than 30 days
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
