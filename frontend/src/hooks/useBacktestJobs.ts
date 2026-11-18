import { useCallback, useEffect, useRef, useState } from 'react'
import {
  appendPage,
  JOBS_MAX_LIMIT,
  JOBS_PAGE_SIZE,
  mergeFirstPage,
} from '@/lib/backtestJobsPaging'

export type BacktestJobStatus = 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'

export interface JobSummary {
  job_id: string
  name: string
  status: BacktestJobStatus
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
  /** "stock" | "universe" | "scan"; older servers omit it (= stock). */
  mode?: string
  universe?: string | null
  starred?: boolean
  notes?: string | null
}

async function fetchJobsPage(offset: number, limit: number) {
  const response = await fetch(`/backtest/api/jobs?limit=${limit}&offset=${offset}`, {
    credentials: 'include',
  })
  const data = await response.json()
  if (data.status !== 'success') return null
  const rows = (data.jobs || []) as JobSummary[]
  return { jobs: rows, total: Number(data.total ?? rows.length) }
}

interface UseBacktestJobsOptions {
  /** Called for each pending or running job found by a load, oldest last. */
  onRunningJob?(job: JobSummary): void
}

/**
 * The paged backtest job list (GET /backtest/api/jobs, newest first).
 *
 * `loadJobs()` refreshes only the newest page, which is where running jobs
 * live, and merges it over what is loaded; `loadJobs({ all: true })` refetches
 * every loaded row and is for deletes, cancels and purges. While any job is
 * pending or running the list is polled every 2.5 s.
 */
export function useBacktestJobs({ onRunningJob }: UseBacktestJobsOptions = {}) {
  const [jobs, setJobs] = useState<JobSummary[]>([])
  const [total, setTotal] = useState(0)
  const [loadingMore, setLoadingMore] = useState(false)
  const [runningJobIds, setRunningJobIds] = useState<Set<string>>(new Set())

  // Read inside loadJobs through refs so its identity never changes: it is a
  // dependency of the mount effect and of the poll.
  const loadedRef = useRef(0)
  const onRunningRef = useRef(onRunningJob)
  onRunningRef.current = onRunningJob

  useEffect(() => {
    loadedRef.current = jobs.length
  }, [jobs])

  const loadJobs = useCallback(async (opts?: { all?: boolean }) => {
    try {
      const all = opts?.all === true
      const want = all ? Math.max(JOBS_PAGE_SIZE, loadedRef.current) : JOBS_PAGE_SIZE
      let fresh: JobSummary[] = []
      let freshTotal = 0
      for (let offset = 0; offset < want; offset += JOBS_MAX_LIMIT) {
        const limit = Math.min(JOBS_MAX_LIMIT, want - offset)
        const page = await fetchJobsPage(offset, limit)
        if (!page) return
        fresh = appendPage(fresh, page.jobs)
        freshTotal = page.total
        if (page.jobs.length < limit) break
      }
      setJobs((prev) => (all ? fresh : mergeFirstPage(prev, fresh, JOBS_PAGE_SIZE)))
      setTotal(freshTotal)
      const running = new Set<string>()
      for (const job of fresh) {
        if (job.status === 'pending' || job.status === 'running') {
          running.add(job.job_id)
          onRunningRef.current?.(job)
        }
      }
      setRunningJobIds(running)
    } catch {
      // ignore - the jobs panel is optional
    }
  }, [])

  /** Append the next page. Resolves false when the request failed. */
  const loadMore = useCallback(async () => {
    setLoadingMore(true)
    try {
      const page = await fetchJobsPage(loadedRef.current, JOBS_PAGE_SIZE)
      if (!page) return false
      setJobs((prev) => appendPage(prev, page.jobs))
      setTotal(page.total)
      return true
    } catch {
      return false
    } finally {
      setLoadingMore(false)
    }
  }, [])

  /** Apply a field change to a loaded row without refetching (star, note). */
  const patchJob = useCallback((jobId: string, fields: Partial<JobSummary>) => {
    setJobs((prev) => prev.map((j) => (j.job_id === jobId ? { ...j, ...fields } : j)))
  }, [])

  useEffect(() => {
    loadJobs()
  }, [loadJobs])

  // Poll while any run is active to catch its terminal state.
  useEffect(() => {
    if (!runningJobIds.size) return
    const timer = setInterval(() => {
      loadJobs()
    }, 2500)
    return () => clearInterval(timer)
  }, [runningJobIds, loadJobs])

  return {
    jobs,
    /** Runs on the server; never less than what is loaded (the total can lag a new run). */
    jobCount: Math.max(total, jobs.length),
    loadingMore,
    loadJobs,
    loadMore,
    patchJob,
  }
}
