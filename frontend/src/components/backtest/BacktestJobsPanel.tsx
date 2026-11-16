import {
  ChevronDown,
  ChevronRight,
  LineChart,
  Loader2,
  RotateCcw,
  Square,
  Trash2,
} from 'lucide-react'
import { Fragment, memo, useEffect, useMemo, useRef, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Progress } from '@/components/ui/progress'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import type { JobSummary } from '@/hooks/useBacktestJobs'
import { JOBS_PAGE_SIZE } from '@/lib/backtestJobsPaging'

export interface JobProgress {
  percent: number
  current_date: string
  equity: number
  trades_count: number
}

type JobsTab = 'active' | 'history'

const fmtCount = (n: number) => n.toLocaleString('en-IN')

function statusBadgeVariant(status: string) {
  if (status === 'completed') return 'default'
  if (status === 'failed') return 'destructive'
  if (status === 'cancelled') return 'secondary'
  return 'outline'
}

function batchStatus(kids: JobSummary[]) {
  const statuses = kids.map((k) => k.status)
  if (statuses.every((s) => s === 'completed')) return 'completed'
  if (statuses.some((s) => s === 'running')) return 'running'
  if (statuses.some((s) => s === 'pending')) return 'pending'
  if (statuses.some((s) => s === 'failed') || statuses.some((s) => s === 'cancelled'))
    return 'partial'
  return 'pending'
}

const openInNewTab = (query: string) => window.open(`/backtest/results?${query}`, '_blank')

interface HistoryHandlers {
  onReRun(job: JobSummary): void
  onBatchReRun(kids: JobSummary[]): void
  onDeleteJob(jobId: string): void
  onDeleteBatch(batchId: string): void
  onLoadMore(): void
}

interface HistoryTableProps extends HistoryHandlers {
  jobs: JobSummary[]
  jobCount: number
  loadingMore: boolean
  /** Test hook: called once per render of the table. */
  onRender?(): void
}

/**
 * The history table can hold hundreds of rows, so it is memoised and only
 * receives the job list and handlers with a stable identity: typing in the
 * strategy editor or a progress event re-renders the page but not this.
 */
const HistoryTable = memo(function HistoryTable({
  jobs,
  jobCount,
  loadingMore,
  onReRun,
  onBatchReRun,
  onDeleteJob,
  onDeleteBatch,
  onLoadMore,
  onRender,
}: HistoryTableProps) {
  onRender?.()
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  // A legacy or single run is a batch of one (batch_id = job_id) and renders as a flat row.
  const groups = useMemo(() => {
    const map = new Map<string, JobSummary[]>()
    for (const job of jobs) {
      const key = job.batch_id || job.job_id
      const arr = map.get(key) ?? []
      arr.push(job)
      map.set(key, arr)
    }
    return [...map.entries()].map(([batchId, kids]) => ({
      batchId,
      kids: [...kids].sort((a, b) => (a.batch_seq ?? 0) - (b.batch_seq ?? 0)),
    }))
  }, [jobs])

  const toggle = (batchId: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(batchId)) next.delete(batchId)
      else next.add(batchId)
      return next
    })
  }

  const rowActions = (job: JobSummary, kids?: JobSummary[]) => (
    <div className="flex items-center justify-end gap-1">
      {job.status === 'completed' && (
        <Button
          variant="ghost"
          size="icon"
          title="View results in new tab"
          onClick={(e) => {
            e.stopPropagation()
            openInNewTab(`job_id=${job.job_id}`)
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
          if (kids) onBatchReRun(kids)
          else onReRun(job)
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
          onDeleteJob(job.job_id)
        }}
      >
        <Trash2 className="h-4 w-4 text-rose-400" />
      </Button>
    </div>
  )

  const metricCells = (job: JobSummary) => (
    <>
      <TableCell
        className={`text-right ${job.total_return_pct >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}
      >
        {job.total_return_pct}%
      </TableCell>
      <TableCell className="text-right">{job.sharpe_ratio ?? '-'}</TableCell>
      <TableCell className="text-right">{job.max_drawdown_pct}%</TableCell>
      <TableCell className="text-right">{job.total_trades}</TableCell>
    </>
  )

  return (
    <div className="space-y-3">
      {/* Native scroll container rather than ScrollArea: Radix renders its
          viewport as display:table, which shrink-wraps to content instead of
          taking the available width, so a nested overflow-x-auto never gets a
          definite width to scroll against. */}
      <div className="max-h-[26rem] overflow-auto rounded-md border border-border">
        <Table className="min-w-[750px]">
          <TableHeader className="sticky top-0 z-10 bg-card">
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
            {groups.length === 0 && (
              <TableRow>
                <TableCell colSpan={9} className="py-10 text-center text-muted-foreground">
                  No backtest jobs have been submitted yet
                </TableCell>
              </TableRow>
            )}
            {groups.map(({ batchId, kids }) => {
              if (kids.length === 1) {
                const job = kids[0]
                return (
                  <TableRow key={job.job_id}>
                    <TableCell className="font-medium">{job.name}</TableCell>
                    <TableCell>
                      <Badge variant={statusBadgeVariant(job.status)}>{job.status}</Badge>
                    </TableCell>
                    <TableCell className="text-xs">{(job.symbols || []).join(', ')}</TableCell>
                    <TableCell>{job.interval}</TableCell>
                    {metricCells(job)}
                    <TableCell>{rowActions(job)}</TableCell>
                  </TableRow>
                )
              }
              const open = expanded.has(batchId)
              const rollup = batchStatus(kids)
              const bestReturn = Math.max(...kids.map((k) => k.total_return_pct))
              const worstReturn = Math.min(...kids.map((k) => k.total_return_pct))
              return (
                <Fragment key={batchId}>
                  <TableRow className="cursor-pointer bg-muted/40" onClick={() => toggle(batchId)}>
                    <TableCell className="font-medium">
                      <span className="flex items-center gap-1.5">
                        {open ? (
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
                    <TableCell className="text-xs">{(kids[0].symbols || []).join(', ')}</TableCell>
                    <TableCell>{kids.length} TF</TableCell>
                    <TableCell className="text-right">
                      <span className={bestReturn >= 0 ? 'text-emerald-400' : 'text-rose-400'}>
                        {bestReturn}%
                      </span>
                      <span className="ml-1 text-xs text-muted-foreground">({worstReturn}%)</span>
                    </TableCell>
                    <TableCell className="text-right text-muted-foreground">-</TableCell>
                    <TableCell className="text-right text-muted-foreground">-</TableCell>
                    <TableCell className="text-right">{kids.length}</TableCell>
                    <TableCell>
                      <div className="flex items-center justify-end gap-1">
                        {kids.some((k) => k.status === 'completed') && (
                          <Button
                            variant="ghost"
                            size="icon"
                            title="View batch results in new tab"
                            onClick={(e) => {
                              e.stopPropagation()
                              openInNewTab(`batch_id=${batchId}`)
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
                            onBatchReRun(kids)
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
                            onDeleteBatch(batchId)
                          }}
                        >
                          <Trash2 className="h-4 w-4 text-rose-400" />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                  {open &&
                    kids.map((job) => (
                      <TableRow key={job.job_id} className="bg-muted/20">
                        <TableCell className="pl-8 text-xs text-muted-foreground">
                          {job.name}
                        </TableCell>
                        <TableCell>
                          <Badge variant={statusBadgeVariant(job.status)}>{job.status}</Badge>
                        </TableCell>
                        <TableCell className="text-xs">-</TableCell>
                        <TableCell className="pl-8">{job.interval}</TableCell>
                        {metricCells(job)}
                        <TableCell>{rowActions(job, kids)}</TableCell>
                      </TableRow>
                    ))}
                </Fragment>
              )
            })}
          </TableBody>
        </Table>
      </div>

      {jobs.length < jobCount && (
        <div className="flex flex-wrap items-center justify-center gap-3 text-sm text-muted-foreground">
          <Button variant="outline" size="sm" disabled={loadingMore} onClick={onLoadMore}>
            {loadingMore
              ? 'Loading...'
              : `Load ${fmtCount(Math.min(JOBS_PAGE_SIZE, jobCount - jobs.length))} more`}
          </Button>
          <span>
            Showing {fmtCount(jobs.length)} of {fmtCount(jobCount)}
          </span>
        </div>
      )}
    </div>
  )
})

interface BacktestJobsPanelProps extends HistoryHandlers {
  jobs: JobSummary[]
  jobCount: number
  loadingMore: boolean
  jobProgress: Record<string, JobProgress>
  onCancelJob(jobId: string): void
  onCancelAll(): void
  /** Purge runs older than 30 days; resolves once the request has finished. */
  onPurge(): Promise<void>
  onHistoryRender?(): void
}

/** Bottom panel of the backtest page: runs in flight, and the paged job history. */
export function BacktestJobsPanel({
  jobs,
  jobCount,
  loadingMore,
  jobProgress,
  onCancelJob,
  onCancelAll,
  onPurge,
  onHistoryRender,
  ...handlers
}: BacktestJobsPanelProps) {
  const active = useMemo(
    () => jobs.filter((j) => j.status === 'running' || j.status === 'pending'),
    [jobs]
  )
  const runningCount = active.filter((j) => j.status === 'running').length
  const pendingCount = active.length - runningCount
  const hasActive = active.length > 0

  // Follow the work: show runs in flight while there are any, history otherwise.
  const [tab, setTab] = useState<JobsTab>('history')
  useEffect(() => {
    setTab(hasActive ? 'active' : 'history')
  }, [hasActive])

  const [purgeOpen, setPurgeOpen] = useState(false)
  const [purging, setPurging] = useState(false)

  // The page recreates its handlers on every render; give the memoised table
  // wrappers whose identity never changes.
  const latest = useRef(handlers)
  latest.current = handlers
  const stable = useMemo<HistoryHandlers>(
    () => ({
      onReRun: (job) => latest.current.onReRun(job),
      onBatchReRun: (kids) => latest.current.onBatchReRun(kids),
      onDeleteJob: (jobId) => latest.current.onDeleteJob(jobId),
      onDeleteBatch: (batchId) => latest.current.onDeleteBatch(batchId),
      onLoadMore: () => latest.current.onLoadMore(),
    }),
    []
  )

  const confirmPurge = async () => {
    setPurging(true)
    try {
      await onPurge()
      setPurgeOpen(false)
    } finally {
      setPurging(false)
    }
  }

  return (
    <Card className="gap-0 py-0">
      <div className="flex flex-wrap items-center gap-2 border-b px-4 py-2.5">
        <Tabs value={tab} onValueChange={(v) => setTab(v as JobsTab)}>
          <TabsList>
            <TabsTrigger value="active">Active ({fmtCount(active.length)})</TabsTrigger>
            <TabsTrigger value="history">History ({fmtCount(jobCount)})</TabsTrigger>
          </TabsList>
        </Tabs>
        {runningCount > 0 && (
          <Badge variant="default" className="bg-emerald-600 text-xs">
            {runningCount} Running
          </Badge>
        )}
        {pendingCount > 0 && (
          <Badge variant="outline" className="border-amber-500/40 text-xs text-amber-500">
            {pendingCount} Queued
          </Badge>
        )}
        <div className="ml-auto flex items-center gap-2">
          {hasActive && (
            <Button variant="destructive" size="sm" onClick={onCancelAll}>
              <Square className="h-4 w-4" /> Cancel All Active
            </Button>
          )}
          {tab === 'history' && (
            <Button variant="outline" size="sm" onClick={() => setPurgeOpen(true)}>
              <Trash2 className="h-4 w-4" /> Purge runs older than 30 days
            </Button>
          )}
        </div>
      </div>

      <div className="p-4">
        {tab === 'active' &&
          (hasActive ? (
            <div className="space-y-3">
              {active.map((j) => {
                const p = jobProgress[j.job_id]
                const running = j.status === 'running'
                const symbolClean = (j.symbols?.[0] || '').replace('NSE:', '').replace('BSE:', '')
                return (
                  <div key={j.job_id} className="space-y-2 rounded-lg border bg-background/80 p-3">
                    <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
                      <div className="flex items-center gap-2">
                        <span className="font-semibold">{j.name || symbolClean}</span>
                        <Badge variant="outline" className="text-xs">
                          {j.interval}
                        </Badge>
                        {running ? (
                          <Badge variant="default" className="bg-emerald-600 text-[10px]">
                            Running
                          </Badge>
                        ) : (
                          <Badge variant="secondary" className="text-[10px] text-amber-500">
                            Queued
                          </Badge>
                        )}
                      </div>
                      <div className="flex items-center gap-2">
                        {running && p?.current_date && (
                          <span className="font-mono text-xs text-muted-foreground">
                            {p.current_date} • Equity: ₹{p.equity.toLocaleString()}
                          </span>
                        )}
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-7 px-2 text-rose-500 hover:bg-rose-500/10 hover:text-rose-600"
                          onClick={() => onCancelJob(j.job_id)}
                        >
                          <Square className="mr-1 h-3.5 w-3.5" /> Cancel
                        </Button>
                      </div>
                    </div>

                    {running ? (
                      <div className="space-y-1">
                        <div className="flex items-center justify-between text-xs text-muted-foreground">
                          <span>Trades: {p?.trades_count ?? 0}</span>
                          <span className="font-medium text-foreground">
                            {p ? `${p.percent.toFixed(1)}%` : '0.0%'}
                          </span>
                        </div>
                        <Progress value={p?.percent ?? 0} className="h-1.5" />
                      </div>
                    ) : (
                      <div className="flex items-center gap-1.5 pt-0.5 text-xs text-muted-foreground">
                        <Loader2 className="h-3 w-3 animate-spin text-amber-500" />
                        Waiting in execution queue for next available worker thread...
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          ) : (
            <p className="py-8 text-center text-sm text-muted-foreground">
              Nothing is running. Queue a backtest above and it shows up here.
            </p>
          ))}

        {tab === 'history' && (
          <HistoryTable
            jobs={jobs}
            jobCount={jobCount}
            loadingMore={loadingMore}
            onRender={onHistoryRender}
            {...stable}
          />
        )}
      </div>

      <Dialog open={purgeOpen} onOpenChange={(open) => !purging && setPurgeOpen(open)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Purge old runs</DialogTitle>
            <DialogDescription>
              Deletes every backtest run older than 30 days, with its trades and equity curve.
            </DialogDescription>
          </DialogHeader>
          <p className="text-sm text-rose-400">This cannot be undone.</p>
          <DialogFooter>
            <Button variant="outline" disabled={purging} onClick={() => setPurgeOpen(false)}>
              Cancel
            </Button>
            <Button variant="destructive" disabled={purging} onClick={confirmPurge}>
              {purging ? 'Purging...' : 'Purge'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  )
}
