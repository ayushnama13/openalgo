/**
 * Strategy backtesting workbench.
 *
 * Two modes share one strategy editor, one set of execution/risk settings and
 * one runs panel:
 * - Stock:    hand-picked symbol(s), one run per selected timeframe.
 * - Universe: an index universe (NIFTY 50, NIFTY 100, sector indices, ...),
 *             either one Portfolio run over every covered constituent or a
 *             Scan of one run per constituent, ranked on the results page.
 *
 * The stock-mode /run body is pinned by Backtest.run.test.tsx; universe-only
 * fields (mode, universe) are sent only in universe mode.
 */

import { FlaskConical, History, Loader2, Play, SlidersHorizontal, Square } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router'
import { fetchCSRFToken } from '@/api/client'
import { BacktestJobsPanel, type JobProgress } from '@/components/backtest/BacktestJobsPanel'
import EngineOptionsCard from '@/components/backtest/EngineOptionsCard'
import ExtraSymbols from '@/components/backtest/ExtraSymbols'
import OptimizeDialog from '@/components/backtest/OptimizeDialog'
import StrategyTemplates, {
  type SavedTemplate,
  type TemplateSettings,
} from '@/components/backtest/StrategyTemplates'
import UniversePicker from '@/components/backtest/UniversePicker'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { type JobSummary, useBacktestJobs } from '@/hooks/useBacktestJobs'
import { useSocket } from '@/hooks/useSocket'
import {
  buildEngineOptions,
  DEFAULT_ENGINE_OPTIONS,
  type EngineOptionsState,
  engineOptionsFromSaved,
} from '@/lib/backtestEngineOptions'
import { DEFAULT_STRATEGY_CODE } from '@/lib/backtestTemplates'
import {
  coveredSymbols,
  fetchUniverseCoverage,
  fetchUniverses,
  patchJobMeta,
  type UniverseCoverage,
  type UniverseListing,
  type UniverseRunType,
} from '@/lib/backtestUniverse'
import { cn } from '@/lib/utils'
import {
  type BatchRunSummary,
  type FullJobDetails,
  type ResultsData,
  useStrategyBacktestStore,
} from '@/stores/strategyBacktestStore'
import { showToast } from '@/utils/toast'

// ---------------------------------------------------------------------------
// Types and constants
// ---------------------------------------------------------------------------

type PageMode = 'stock' | 'universe'
type SettingsTab = 'data' | 'execution' | 'risk' | 'engine'

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

/** A multi-run batch being watched until every run is terminal. */
interface ActiveBatch {
  batch_id: string
  name: string
  mode: string
  total: number
  done: number
  completed: number
  failed: number
}

const TERMINAL = new Set(['completed', 'failed', 'cancelled'])

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
const DEFAULT_UNIVERSE = 'NIFTY50'

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function Backtest() {
  const navigate = useNavigate()
  const { socket } = useSocket()
  const setResult = useStrategyBacktestStore((s) => s.setResult)
  const setBatch = useStrategyBacktestStore((s) => s.setBatch)

  // Mode
  const [mode, setMode] = useState<PageMode>('stock')
  const [settingsTab, setSettingsTab] = useState<SettingsTab>('data')

  // Stock data source
  const [catalog, setCatalog] = useState<CatalogItem[]>([])
  const [catalogLoading, setCatalogLoading] = useState(true)
  const [symbol, setSymbol] = useState('')
  const [exchange, setExchange] = useState('NSE')
  const [extraSymbols, setExtraSymbols] = useState<string[]>([])
  const [rangesByInterval, setRangesByInterval] = useState<Record<string, RangeInfo | null>>({})

  // Universe data source
  const [universeListing, setUniverseListing] = useState<UniverseListing | null>(null)
  const [universeListingLoading, setUniverseListingLoading] = useState(false)
  const [universeId, setUniverseId] = useState('')
  const [universeRunType, setUniverseRunType] = useState<UniverseRunType>('universe')
  const [coverage, setCoverage] = useState<UniverseCoverage | null>(null)
  const [coverageLoading, setCoverageLoading] = useState(false)

  // Shared form state
  const [intervals, setIntervals] = useState<string[]>(['1m'])
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')
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
  const [engineOptions, setEngineOptions] = useState<EngineOptionsState>(DEFAULT_ENGINE_OPTIONS)
  const rangeRequestGeneration = useRef(0)
  const [optimizeOpen, setOptimizeOpen] = useState(false)

  // Run state
  const [submitting, setSubmitting] = useState(false)
  const [activeJobId, setActiveJobId] = useState<string | null>(null)
  const [activeBatch, setActiveBatch] = useState<ActiveBatch | null>(null)
  const [jobProgress, setJobProgress] = useState<Record<string, JobProgress>>({})
  const [historyFocus, setHistoryFocus] = useState(0)
  const runsPanelRef = useRef<HTMLDivElement>(null)

  // A run found pending/running on load is followed to its results, unless it
  // belongs to a multi-run batch (that is followed through the batch instead).
  const onRunningJob = useCallback((job: JobSummary) => {
    if (!job.batch_id || job.batch_id === job.job_id) setActiveJobId(job.job_id)
  }, [])
  const { jobs, jobCount, loadingMore, loadJobs, loadMore, patchJob } = useBacktestJobs({
    onRunningJob,
  })

  // Mirrors read inside stable callbacks and effects.
  const symbolRef = useRef(symbol)
  symbolRef.current = symbol
  // Once the user (or a rerun) sets the dates, they are never re-seeded.
  const datesTouchedRef = useRef(false)

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
    const generation = ++rangeRequestGeneration.current
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
    if (generation === rangeRequestGeneration.current) setRangesByInterval(results)
  }, [])

  useEffect(() => {
    loadCatalog()
  }, [loadCatalog])

  useEffect(() => {
    if (mode === 'stock' && symbol && exchange && intervals.length) {
      loadRanges(symbol, exchange, intervals)
    }
  }, [mode, symbol, exchange, intervals, loadRanges])

  // Universe listing, fetched the first time Universe mode is opened.
  useEffect(() => {
    if (mode !== 'universe' || universeListing || universeListingLoading) return
    setUniverseListingLoading(true)
    fetchUniverses()
      .then((listing) => {
        if (!listing) {
          showToast.error('Failed to load index universes')
          return
        }
        setUniverseListing(listing)
        setUniverseId(
          (prev) =>
            prev ||
            listing.universes.find((u) => u.id === DEFAULT_UNIVERSE)?.id ||
            listing.universes[0]?.id ||
            ''
        )
      })
      .catch(() => showToast.error('Failed to load index universes'))
      .finally(() => setUniverseListingLoading(false))
  }, [mode, universeListing, universeListingLoading])

  // Coverage for the chosen universe on the first selected timeframe.
  const coverageInterval = intervals[0] ?? 'D'
  useEffect(() => {
    if (mode !== 'universe' || !universeId) return
    let cancelled = false
    setCoverageLoading(true)
    fetchUniverseCoverage(universeId, coverageInterval)
      .then((cov) => {
        if (cancelled) return
        setCoverage(cov)
        if (!cov) showToast.error('Failed to load universe coverage')
      })
      .catch(() => {
        if (!cancelled) showToast.error('Failed to load universe coverage')
      })
      .finally(() => {
        if (!cancelled) setCoverageLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [mode, universeId, coverageInterval])

  // Seed the date window from the data until the user picks one: the first
  // timeframe with data in Stock mode, the common window in Universe mode.
  useEffect(() => {
    if (datesTouchedRef.current) return
    if (mode === 'stock') {
      const firstAvail = INTERVALS.find((iv) => rangesByInterval[iv])
      const r = firstAvail ? rangesByInterval[firstAvail] : null
      if (r) {
        setStartDate(r.start_date.slice(0, 10))
        setEndDate(r.end_date.slice(0, 10))
      }
    } else if (coverage?.common_first && coverage.common_last) {
      setStartDate(coverage.common_first)
      setEndDate(coverage.common_last)
    }
  }, [mode, rangesByInterval, coverage])

  // -------------------------------------------------------------------------
  // Socket progress
  // -------------------------------------------------------------------------

  useEffect(() => {
    if (!socket) return
    const handleProgress = (data: JobProgress & { job_id: string }) => {
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
    socket.on('backtest_progress', handleProgress)
    return () => {
      socket.off('backtest_progress', handleProgress)
    }
  }, [socket])

  // -------------------------------------------------------------------------
  // Mode switching
  // -------------------------------------------------------------------------

  const switchMode = (next: PageMode) => {
    if (next === mode) return
    setMode(next)
    if (!datesTouchedRef.current) {
      setStartDate('')
      setEndDate('')
    }
    if (next === 'universe') {
      // Universes are mostly daily data held as delivery positions; move off
      // the stock-mode defaults, never off something the user chose.
      if (intervals.length === 1 && intervals[0] === '1m') setIntervals(['D'])
      if (productType === 'MIS') setProductType('CNC')
      if (universeRunType === 'scan' && intervals.length > 1) setIntervals([intervals[0]])
    }
  }

  const changeRunType = (next: UniverseRunType) => {
    setUniverseRunType(next)
    // A scan runs one timeframe; keep the first one selected.
    if (next === 'scan' && intervals.length > 1) setIntervals([intervals[0]])
  }

  const isScan = mode === 'universe' && universeRunType === 'scan'

  const toggleInterval = (iv: string) => {
    if (isScan) {
      setIntervals([iv])
      return
    }
    setIntervals((prev) => {
      if (prev.includes(iv)) return prev.length > 1 ? prev.filter((x) => x !== iv) : prev
      return [...prev, iv]
    })
  }

  const setDate = (which: 'start' | 'end', value: string) => {
    datesTouchedRef.current = true
    if (which === 'start') setStartDate(value)
    else setEndDate(value)
  }

  const useCommonWindow = () => {
    if (!coverage?.common_first || !coverage.common_last) return
    datesTouchedRef.current = true
    setStartDate(coverage.common_first)
    setEndDate(coverage.common_last)
  }

  // -------------------------------------------------------------------------
  // Run payload
  // -------------------------------------------------------------------------

  /** Run settings a saved template keeps; null (after a toast) when engine options are invalid. */
  const templateSettings = (): TemplateSettings | null => {
    const built = buildEngineOptions(engineOptions)
    if (!built.ok) {
      showToast.error(built.error)
      return null
    }
    return {
      interval: intervals[0],
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
      engine_options: built.value,
    }
  }

  /** Load a saved template: its code, plus whichever settings it kept. Symbols and dates stay. */
  const applyTemplate = (t: SavedTemplate) => {
    setStrategyCode(t.strategy_code)
    const s = t.settings
    if (!s) return
    const pct = (v: number | null | undefined) => (v == null ? '' : String(v))
    if (s.interval) setIntervals([s.interval])
    if (s.initial_capital != null) setInitialCapital(s.initial_capital)
    if (s.sizing_type) setSizingType(s.sizing_type)
    if (s.sizing_value != null) setSizingValue(s.sizing_value)
    if (s.slippage_bps != null) setSlippageBps(s.slippage_bps)
    if (s.brokerage_bps != null) setBrokerageBps(s.brokerage_bps)
    if (s.product_type) setProductType(s.product_type)
    if (s.missing_data_policy) setMissingDataPolicy(s.missing_data_policy)
    if ('stop_loss_pct' in s) setStopLossPct(pct(s.stop_loss_pct))
    if ('take_profit_pct' in s) setTakeProfitPct(pct(s.take_profit_pct))
    if ('daily_loss_limit' in s) setDailyLossLimit(pct(s.daily_loss_limit))
    if (s.max_positions != null) setMaxPositions(s.max_positions)
    if (s.engine_options) setEngineOptions(engineOptionsFromSaved(s.engine_options))
  }

  /** Run fields shared by "Run" and "Optimize"; null (after a toast) when the form is invalid. */
  const buildRunPayload = (): Record<string, unknown> | null => {
    if (mode === 'stock' && !symbol) {
      showToast.error('Please select a symbol')
      return null
    }
    if (mode === 'universe' && !universeId) {
      showToast.error('Please select a universe')
      return null
    }
    if (mode === 'universe' && coverage && coverage.id === universeId && !coverage.with_data) {
      showToast.error(`No ${coverage.name} constituent has ${coverage.base_interval} data`)
      return null
    }
    if (!intervals.length) {
      showToast.error('Please select at least one timeframe')
      return null
    }
    if (!strategyCode.trim()) {
      showToast.error('Strategy code is required')
      return null
    }
    const built = buildEngineOptions(engineOptions)
    if (!built.ok) {
      showToast.error(built.error)
      return null
    }
    const common = {
      strategy_code: strategyCode,
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
      engine_options: Object.keys(built.value).length ? built.value : undefined,
    }

    if (mode === 'universe') {
      const uName = universeListing?.universes.find((u) => u.id === universeId)?.name ?? universeId
      const tf = intervals.length > 1 ? 'MTF' : intervals[0]
      return {
        name: `${uName} ${universeRunType === 'scan' ? 'scan' : 'portfolio'} ${tf}`,
        ...common,
        // The server resolves the constituents itself; the covered list is for
        // the optimizer, which takes explicit symbols.
        symbols: coverage?.id === universeId ? coveredSymbols(coverage) : [],
        exchange: coverage?.exchange ?? 'NSE',
        mode: universeRunType,
        universe: universeId,
      }
    }

    const primary = `${exchange}:${symbol}`
    const allSymbols = [primary, ...extraSymbols.filter((k) => k !== primary)]
    return {
      name:
        allSymbols.length > 1
          ? `${symbol} +${allSymbols.length - 1} backtest`
          : intervals.length > 1
            ? `${symbol} MTF backtest`
            : `${symbol} ${intervals[0]} backtest`,
      symbols: allSymbols,
      exchange,
      ...common,
    }
  }

  const handleRun = async () => {
    const payload = buildRunPayload()
    if (!payload) return
    if (mode === 'stock') {
      const firstRange = rangesByInterval[intervals[0]]
      if (firstRange?.start_date && startDate < firstRange.start_date.slice(0, 10)) {
        showToast.warning('Start date is before the earliest available data')
      }
    }

    setSubmitting(true)
    try {
      const csrfToken = await fetchCSRFToken()
      const response = await fetch('/backtest/api/run', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify(payload),
      })
      const data = await response.json()
      if (data.status !== 'success') {
        showToast.error(data.message || 'Failed to submit backtest job')
        return
      }
      const queued: { job_id: string }[] = data.jobs ?? []
      const skipped: string[] = data.skipped ?? []
      if (data.batch_id && queued.length > 1) {
        setActiveJobId(null)
        setActiveBatch({
          batch_id: data.batch_id,
          name: String(payload.name),
          mode: String(payload.mode ?? 'stock'),
          total: queued.length,
          done: 0,
          completed: 0,
          failed: 0,
        })
        const unit = isScan ? 'stocks' : 'timeframes'
        showToast.success(`Queued ${queued.length} ${unit}`)
      } else {
        setActiveBatch(null)
        setActiveJobId(queued[0]?.job_id ?? data.job_id)
        showToast.success('Backtest job enqueued successfully!')
      }
      if (skipped.length) {
        showToast.info(`${skipped.length} constituent(s) skipped for lack of data`)
      }
      loadJobs()
    } catch {
      showToast.error('Failed to submit backtest job')
    } finally {
      setSubmitting(false)
    }
  }

  // Ctrl/Cmd+Enter runs from anywhere on the page, the editor included.
  const handleRunRef = useRef(handleRun)
  handleRunRef.current = handleRun
  const submittingRef = useRef(submitting)
  submittingRef.current = submitting
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault()
        if (!submittingRef.current) handleRunRef.current()
      }
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [])

  // -------------------------------------------------------------------------
  // Results navigation
  // -------------------------------------------------------------------------

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

  // A single run is followed through the job list until it is terminal.
  useEffect(() => {
    if (!activeJobId) return
    const job = jobs.find((j) => j.job_id === activeJobId)
    if (!job || !TERMINAL.has(job.status)) return
    setActiveJobId(null)
    if (job.status === 'completed') openResults(job.job_id)
  }, [jobs, activeJobId, openResults])

  // A batch can be far larger than the loaded job page (a scan of NIFTY 500),
  // so it is followed through its own endpoint instead.
  const activeBatchId = activeBatch?.batch_id
  useEffect(() => {
    if (!activeBatchId) return
    let stopped = false
    const poll = async () => {
      try {
        const response = await fetch(`/backtest/api/batches/${activeBatchId}`, {
          credentials: 'include',
        })
        const data = await response.json()
        if (stopped || data.status !== 'success') return
        const runs: BatchRunSummary[] = data.runs ?? []
        const done = runs.filter((r) => TERMINAL.has(r.status)).length
        const completed = runs.filter((r) => r.status === 'completed').length
        const failed = runs.filter((r) => r.status === 'failed').length
        if (runs.length && done === runs.length) {
          stopped = true
          setActiveBatch(null)
          loadJobs()
          if (completed) {
            setBatch({
              batch_id: data.batch_id,
              name: data.name,
              mode: data.mode,
              universe: data.universe,
              runs,
            })
            if (failed) showToast.warning(`${failed} of ${runs.length} runs failed`)
            navigate(`/backtest/results?batch_id=${activeBatchId}`)
          } else {
            showToast.warning('Batch finished with no completed runs - see the runs panel')
          }
          return
        }
        setActiveBatch((prev) =>
          prev && prev.batch_id === activeBatchId
            ? { ...prev, total: runs.length || prev.total, done, completed, failed }
            : prev
        )
      } catch {
        // transient; the next tick retries
      }
    }
    poll()
    const timer = setInterval(poll, 2500)
    return () => {
      stopped = true
      clearInterval(timer)
    }
  }, [activeBatchId, loadJobs, navigate, setBatch])

  // -------------------------------------------------------------------------
  // Runs panel handlers
  // -------------------------------------------------------------------------

  const applyJobParams = useCallback((job: FullJobDetails, opts?: { asStock?: boolean }) => {
    datesTouchedRef.current = true
    const runMode = opts?.asStock ? 'stock' : (job.mode ?? 'stock')
    setStrategyCode(job.strategy_code)
    if (runMode === 'universe' || runMode === 'scan') {
      setMode('universe')
      setUniverseRunType(runMode)
      if (job.universe) setUniverseId(job.universe)
    } else {
      const keys = job.symbols || []
      setMode('stock')
      setSymbol((keys[0] || '').split(':')[1] || keys[0] || '')
      setExtraSymbols(keys.slice(1))
    }
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
    setEngineOptions(engineOptionsFromSaved(job.engine_options))
    setSettingsTab('data')
  }, [])

  const fetchJobDetails = async (jobId: string): Promise<FullJobDetails | null> => {
    const response = await fetch(`/backtest/api/jobs/${jobId}`, { credentials: 'include' })
    const data = await response.json()
    return data.status === 'success' && data.job ? (data.job as FullJobDetails) : null
  }

  const handleReRun = async (job: JobSummary) => {
    try {
      const details = await fetchJobDetails(job.job_id)
      if (details) {
        // One stock out of a scan reruns as a plain stock backtest on that stock.
        applyJobParams(details, { asStock: job.mode === 'scan' })
        showToast.success('Job parameters loaded - press Run')
      }
    } catch {
      showToast.error('Failed to load job parameters')
    }
  }

  const handleBatchReRun = async (kids: JobSummary[]) => {
    try {
      const details = await fetchJobDetails(kids[0].job_id)
      if (details) {
        applyJobParams(details)
        if (kids[0].mode === 'scan') {
          showToast.success('Scan parameters loaded - press Run')
        } else {
          setIntervals(kids.map((k) => k.interval))
          showToast.success(`Batch parameters loaded for ${kids.length} timeframes - press Run`)
        }
      }
    } catch {
      showToast.error('Failed to load batch parameters')
    }
  }

  const postAndReload = async (
    url: string,
    method: 'POST' | 'DELETE',
    ok: string,
    fail: string
  ) => {
    try {
      const csrfToken = await fetchCSRFToken()
      const response = await fetch(url, {
        method,
        credentials: 'include',
        headers: { 'X-CSRFToken': csrfToken },
      })
      const data = await response.json()
      if (data.status === 'success') {
        showToast.success(ok)
        loadJobs({ all: true })
      } else {
        showToast.error(data.message || fail)
      }
    } catch {
      showToast.error(fail)
    }
  }

  const handleDeleteJob = (jobId: string) =>
    postAndReload(`/backtest/api/jobs/${jobId}`, 'DELETE', 'Job deleted', 'Failed to delete job')

  const handleDeleteBatch = (batchId: string) =>
    postAndReload(
      `/backtest/api/batches/${batchId}`,
      'DELETE',
      'Batch deleted',
      'Failed to delete batch'
    )

  const handleCancelJob = (jobId: string) =>
    postAndReload(`/backtest/api/cancel/${jobId}`, 'POST', 'Job cancelled', 'Failed to cancel job')

  const handleCancelBatch = (batchId: string) =>
    postAndReload(
      `/backtest/api/batches/${batchId}/cancel`,
      'POST',
      'Batch cancel requested',
      'Failed to cancel batch'
    )

  const handleCancelAllActive = async () => {
    try {
      const csrfToken = await fetchCSRFToken()
      const headers = { 'X-CSRFToken': csrfToken }
      if (activeBatch) {
        await fetch(`/backtest/api/batches/${activeBatch.batch_id}/cancel`, {
          method: 'POST',
          credentials: 'include',
          headers,
        })
      }
      const response = await fetch('/backtest/api/cancel-active', {
        method: 'POST',
        credentials: 'include',
        headers,
      })
      const data = await response.json()
      if (!response.ok || data.status !== 'success') {
        showToast.error(data.message || 'Failed to cancel active jobs')
        return
      }
      showToast.info(`Cancel requests sent: ${data.cancelled} of ${data.requested} active jobs`)
      loadJobs({ all: true })
    } catch {
      showToast.error('Failed to cancel active jobs')
    }
  }

  const handlePurge = async () => {
    try {
      const csrfToken = await fetchCSRFToken()
      const response = await fetch('/backtest/api/cleanup', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify({ days_to_keep: 30 }),
      })
      const data = await response.json()
      if (!response.ok || data.status !== 'success') {
        showToast.error(data.message || 'Failed to purge old runs')
        return
      }
      showToast.success('Old runs purged (retention 30 days)')
      loadJobs({ all: true })
    } catch {
      showToast.error('Failed to purge old runs')
    }
  }

  const handleToggleStar = async (job: JobSummary) => {
    const next = !job.starred
    patchJob(job.job_id, { starred: next })
    try {
      const saved = await patchJobMeta(job.job_id, { starred: next })
      patchJob(job.job_id, { starred: saved.starred })
    } catch (e) {
      patchJob(job.job_id, { starred: !next })
      showToast.error(e instanceof Error ? e.message : 'Failed to update run')
    }
  }

  const handleSaveNotes = async (job: JobSummary, notes: string) => {
    try {
      const saved = await patchJobMeta(job.job_id, { notes })
      patchJob(job.job_id, { notes: saved.notes })
      showToast.success('Notes saved')
    } catch (e) {
      showToast.error(e instanceof Error ? e.message : 'Failed to save notes')
      throw e
    }
  }

  const handleLoadMore = async () => {
    if (!(await loadMore())) showToast.error('Failed to load more jobs')
  }

  // Stable handler identities so the memoized runs panel skips form-only renders;
  // each call still reaches the latest closure.
  const handlers = {
    onCancelJob: handleCancelJob,
    onCancelAll: handleCancelAllActive,
    onPurge: handlePurge,
    onReRun: handleReRun,
    onBatchReRun: handleBatchReRun,
    onDeleteJob: handleDeleteJob,
    onDeleteBatch: handleDeleteBatch,
    onLoadMore: handleLoadMore,
    onToggleStar: handleToggleStar,
    onSaveNotes: handleSaveNotes,
  }
  const latestHandlers = useRef(handlers)
  latestHandlers.current = handlers
  const panelHandlers = useMemo(
    () => ({
      onCancelJob: (id: string) => latestHandlers.current.onCancelJob(id),
      onCancelAll: () => latestHandlers.current.onCancelAll(),
      onPurge: () => latestHandlers.current.onPurge(),
      onReRun: (job: JobSummary) => latestHandlers.current.onReRun(job),
      onBatchReRun: (kids: JobSummary[]) => latestHandlers.current.onBatchReRun(kids),
      onDeleteJob: (id: string) => latestHandlers.current.onDeleteJob(id),
      onDeleteBatch: (id: string) => latestHandlers.current.onDeleteBatch(id),
      onLoadMore: () => latestHandlers.current.onLoadMore(),
      onToggleStar: (job: JobSummary) => latestHandlers.current.onToggleStar(job),
      onSaveNotes: (job: JobSummary, notes: string) =>
        latestHandlers.current.onSaveNotes(job, notes),
    }),
    []
  )

  const showHistory = () => {
    setHistoryFocus((n) => n + 1)
    runsPanelRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
  }

  const activeCount = useMemo(
    () => jobs.filter((j) => j.status === 'running' || j.status === 'pending').length,
    [jobs]
  )

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  const timeframePicker = (
    <div className="space-y-1.5">
      <Label>{isScan ? 'Timeframe' : 'Timeframes'}</Label>
      <div className="flex flex-wrap gap-1.5">
        {INTERVALS.map((iv) => {
          const selected = intervals.includes(iv)
          const r = mode === 'stock' ? rangesByInterval[iv] : undefined
          return (
            <Button
              key={iv}
              size="sm"
              variant={selected ? 'default' : 'outline'}
              onClick={() => toggleInterval(iv)}
              title={
                mode !== 'stock'
                  ? iv
                  : r
                    ? `${iv}: ${r.record_count?.toLocaleString() ?? '?'} bars from ${r.start_date}`
                    : `${iv}: no available data`
              }
            >
              {iv}
            </Button>
          )
        })}
      </div>
      {isScan ? (
        <p className="text-xs text-muted-foreground">A scan runs on one timeframe.</p>
      ) : (
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
            {mode === 'stock'
              ? '- one run per timeframe'
              : '- one portfolio run per timeframe; coverage shown for the first'}
          </span>
        </div>
      )}
      {mode === 'stock' && intervals.length > 0 && (
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
  )

  const dateFields = (
    <div className="space-y-1.5">
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1.5">
          <Label>Start Date</Label>
          <Input type="date" value={startDate} onChange={(e) => setDate('start', e.target.value)} />
        </div>
        <div className="space-y-1.5">
          <Label>End Date</Label>
          <Input type="date" value={endDate} onChange={(e) => setDate('end', e.target.value)} />
        </div>
      </div>
      {mode === 'universe' && coverage?.common_first && (
        <Button variant="ghost" size="sm" className="h-6 px-2 text-xs" onClick={useCommonWindow}>
          Use common window ({coverage.common_first} to {coverage.common_last})
        </Button>
      )}
    </div>
  )

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="mr-auto min-w-0">
          <h1 className="text-2xl font-bold text-foreground">Strategy Backtesting</h1>
          <p className="text-sm text-muted-foreground">
            {mode === 'stock'
              ? 'Test a strategy on one stock or a hand-picked basket, across timeframes.'
              : 'Test a strategy across an index universe: as one portfolio, or stock by stock.'}
          </p>
        </div>
        <fieldset
          aria-label="Backtest mode"
          className="grid grid-cols-2 gap-1 rounded-lg border bg-muted/40 p-1"
        >
          {(['stock', 'universe'] as const).map((m) => (
            <button
              key={m}
              type="button"
              aria-pressed={mode === m}
              onClick={() => switchMode(m)}
              className={cn(
                'rounded-md px-4 py-1.5 text-sm font-medium transition-colors',
                mode === m
                  ? 'bg-background text-foreground shadow-sm'
                  : 'text-muted-foreground hover:text-foreground'
              )}
            >
              {m === 'stock' ? 'Stock' : 'Universe'}
            </button>
          ))}
        </fieldset>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" onClick={showHistory}>
            <History className="h-4 w-4" /> Job History ({jobCount.toLocaleString('en-IN')})
          </Button>
          <Button variant="outline" onClick={() => navigate('/backtest/optimizations')}>
            Optimizations
          </Button>
          <Button
            variant="outline"
            disabled={isScan}
            title={isScan ? 'Optimize a Portfolio run or a single stock, not a scan' : undefined}
            onClick={() => setOptimizeOpen(true)}
          >
            <SlidersHorizontal className="h-4 w-4" /> Optimize
          </Button>
          <Button
            onClick={handleRun}
            disabled={submitting}
            aria-label="Run / Queue Backtest"
            title="Run (Ctrl+Enter)"
          >
            {submitting ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" /> Queueing...
              </>
            ) : (
              <>
                <Play className="h-4 w-4" /> {isScan ? 'Run Scan' : 'Run Backtest'}
              </>
            )}
          </Button>
        </div>
      </div>

      {activeBatch && (
        <Card className="gap-0 border-primary/30 py-0">
          <div className="flex flex-wrap items-center gap-3 px-4 py-3">
            <Loader2 className="h-4 w-4 animate-spin text-primary" />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-baseline justify-between gap-2 text-sm">
                <span className="font-medium">{activeBatch.name}</span>
                <span className="tabular-nums text-muted-foreground">
                  {activeBatch.done} / {activeBatch.total} done
                  {activeBatch.failed ? ` - ${activeBatch.failed} failed` : ''}
                </span>
              </div>
              <Progress
                value={activeBatch.total ? (activeBatch.done / activeBatch.total) * 100 : 0}
                className="mt-1.5 h-1.5"
              />
            </div>
            <Button
              variant="ghost"
              size="sm"
              className="text-rose-500 hover:bg-rose-500/10 hover:text-rose-600"
              onClick={() => handleCancelBatch(activeBatch.batch_id)}
            >
              <Square className="h-3.5 w-3.5" /> Cancel
            </Button>
          </div>
        </Card>
      )}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
        {/* Strategy */}
        <Card className="gap-3">
          <CardHeader className="flex-row flex-wrap items-center justify-between gap-2 space-y-0">
            <CardTitle className="flex items-center gap-2 text-base">
              <FlaskConical className="h-4 w-4 text-primary" /> Strategy
            </CardTitle>
            <StrategyTemplates
              code={strategyCode}
              getSettings={templateSettings}
              onLoadBuiltIn={setStrategyCode}
              onLoadSaved={applyTemplate}
            />
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="overflow-hidden rounded-lg border">
              <PythonEditor value={strategyCode} onChange={setStrategyCode} height="520px" />
            </div>
            <p className="text-xs text-muted-foreground">
              Signal style: <code className="text-primary">strategy(df)</code> returns the bars with
              a <code className="text-primary">signal</code> column (1 buy, -1 exit long, 2 short,
              -2 cover) and runs on each symbol separately. Event style: subclass{' '}
              <code className="text-primary">Strategy</code> with{' '}
              <code className="text-primary">on_bar(ctx)</code> for cross-symbol logic, brackets and
              trailing stops
              {mode === 'universe' ? '; in a Portfolio run it sees every constituent' : ''}.
              Available: <code className="text-primary">ta</code>,{' '}
              <code className="text-primary">pd</code>, <code className="text-primary">np</code>,{' '}
              <code className="text-primary">PARAMS</code>,{' '}
              <code className="text-primary">SYMBOLS</code>. Sandboxed, no network or files.
            </p>
          </CardContent>
        </Card>

        {/* Settings: every tab stays mounted (hidden when inactive) so switching tabs
            is instant and nothing inside loses local state. */}
        <Card className="gap-3">
          <CardContent className="pt-0">
            <Tabs value={settingsTab} onValueChange={(v) => setSettingsTab(v as SettingsTab)}>
              <TabsList className="w-full">
                <TabsTrigger value="data">Data</TabsTrigger>
                <TabsTrigger value="execution">Execution</TabsTrigger>
                <TabsTrigger value="risk">Risk</TabsTrigger>
                <TabsTrigger value="engine">Engine</TabsTrigger>
              </TabsList>

              <TabsContent
                value="data"
                forceMount
                className="mt-3 space-y-4 data-[state=inactive]:hidden"
              >
                {mode === 'stock' ? (
                  <>
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
                        <SelectTrigger disabled={catalogLoading} className="w-full">
                          <SelectValue
                            placeholder={catalogLoading ? 'Loading...' : 'Select symbol'}
                          />
                        </SelectTrigger>
                        <SelectContent>
                          <ScrollArea className="h-72">
                            {catalog.map((item) => (
                              <SelectItem
                                key={`${item.exchange}:${item.symbol}`}
                                value={item.symbol}
                              >
                                {item.symbol} ({item.exchange})
                              </SelectItem>
                            ))}
                          </ScrollArea>
                        </SelectContent>
                      </Select>
                    </div>
                    {timeframePicker}
                    <ExtraSymbols
                      catalog={catalog}
                      primary={symbol ? `${exchange}:${symbol}` : ''}
                      value={extraSymbols}
                      onChange={setExtraSymbols}
                    />
                    {dateFields}
                    <p className="text-xs text-muted-foreground">
                      Green timeframes have data for the symbol. Runs without data are marked failed
                      at submit time; the rest of the batch proceeds.
                    </p>
                  </>
                ) : (
                  <>
                    <UniversePicker
                      listing={universeListing}
                      listingLoading={universeListingLoading}
                      universeId={universeId}
                      onUniverseChange={setUniverseId}
                      runType={universeRunType}
                      onRunTypeChange={changeRunType}
                      coverage={coverage?.id === universeId ? coverage : null}
                      coverageLoading={coverageLoading}
                    />
                    {timeframePicker}
                    {dateFields}
                  </>
                )}
              </TabsContent>

              <TabsContent
                value="execution"
                forceMount
                className="mt-3 space-y-4 data-[state=inactive]:hidden"
              >
                <div className="grid grid-cols-2 gap-3">
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
                      <SelectTrigger className="w-full">
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
                      <SelectTrigger className="w-full">
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
                  <div className="col-span-2 space-y-1.5">
                    <Label>Missing Data Policy</Label>
                    <Select value={missingDataPolicy} onValueChange={setMissingDataPolicy}>
                      <SelectTrigger className="w-full">
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
                </div>
                <p className="text-xs text-muted-foreground">
                  MIS positions are squared off at the exchange close (15:15 IST for NSE). Brokerage
                  and slippage apply per fill.
                  {mode === 'universe' && productType === 'MIS'
                    ? ' Daily-bar universe runs usually want CNC.'
                    : ''}
                </p>
              </TabsContent>

              <TabsContent
                value="risk"
                forceMount
                className="mt-3 space-y-4 data-[state=inactive]:hidden"
              >
                <div className="grid grid-cols-2 gap-3">
                  <div className="space-y-1.5">
                    <Label>Max Open Positions</Label>
                    <Input
                      type="number"
                      min={1}
                      value={maxPositions}
                      onChange={(e) => setMaxPositions(Number(e.target.value))}
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
                </div>
                <p className="text-xs text-muted-foreground">
                  {mode === 'universe' && universeRunType === 'universe'
                    ? 'Max Open Positions caps how many constituents the portfolio holds at once. '
                    : ''}
                  The daily loss limit stops the strategy for the rest of the day when breached.
                </p>
              </TabsContent>

              <TabsContent value="engine" forceMount className="mt-3 data-[state=inactive]:hidden">
                <EngineOptionsCard value={engineOptions} onChange={setEngineOptions} />
              </TabsContent>
            </Tabs>
          </CardContent>
        </Card>
      </div>

      <div ref={runsPanelRef} className="scroll-mt-4">
        <BacktestJobsPanel
          jobs={jobs}
          jobCount={jobCount}
          loadingMore={loadingMore}
          jobProgress={jobProgress}
          historyFocus={historyFocus}
          {...panelHandlers}
        />
      </div>
      {activeCount > 0 && (
        <p className="sr-only" aria-live="polite">
          {activeCount} backtest(s) active
        </p>
      )}

      <OptimizeDialog
        open={optimizeOpen}
        onOpenChange={setOptimizeOpen}
        buildRunPayload={buildRunPayload}
      />
    </div>
  )
}
