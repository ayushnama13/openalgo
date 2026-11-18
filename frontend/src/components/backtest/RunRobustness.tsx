/**
 * Charges breakdown and robustness studies (cost stress, price noise) for one run.
 *
 * The studies are ordinary optimization jobs tagged with the run's id
 * (optimize.source_job_id), so the latest of each kind is found from the
 * optimizations list and shown here in compact form.
 */

import { Loader2 } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router'
import { fetchCSRFToken } from '@/api/client'
import {
  type NoiseResult,
  NoiseSummary,
  noiseHeadline,
  type StressResult,
  StressTable,
  stressHeadline,
  VerdictBanner,
} from '@/components/backtest/RobustnessViews'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Progress } from '@/components/ui/progress'
import type { FullJobDetails, Metrics } from '@/stores/strategyBacktestStore'
import { showToast } from '@/utils/toast'

const CHARGE_LABELS: Record<string, string> = {
  brokerage: 'Brokerage',
  stt: 'STT',
  exchange_txn: 'Exchange',
  sebi: 'SEBI',
  stamp_duty: 'Stamp duty',
  tax: 'GST',
  slippage: 'Slippage (cost model)',
}

const inr = (n: number) => n.toLocaleString('en-IN', { maximumFractionDigits: 2 })

export function ChargesCard({ metrics }: { metrics?: Metrics }) {
  const b = metrics?.charges_breakdown
  if (!b || b.total === undefined) return null
  const items = Object.entries(b).filter(([k, v]) => k !== 'total' && v > 0)
  const grossPnl = (metrics?.total_pnl ?? 0) + b.total
  const share = grossPnl > 0 ? (b.total / grossPnl) * 100 : null
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">Charges</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-7">
          {items.map(([k, v]) => (
            <div key={k} className="rounded-md border border-slate-800 bg-slate-900/60 p-2">
              <div className="text-[11px] text-muted-foreground">{CHARGE_LABELS[k] ?? k}</div>
              <div className="font-mono text-sm">{inr(v)}</div>
            </div>
          ))}
          <div className="rounded-md border border-slate-700 bg-slate-800/60 p-2">
            <div className="text-[11px] text-muted-foreground">Total</div>
            <div className="font-mono text-sm font-semibold">{inr(b.total)}</div>
          </div>
        </div>
        <p className="text-xs text-muted-foreground">
          {share !== null
            ? `Charges took ${share.toFixed(1)}% of the profit before costs.`
            : 'The run lost money before costs, so charges only deepened the loss.'}{' '}
          Slippage applied to fill prices is already inside the trade prices and is not listed here.
        </p>
      </CardContent>
    </Card>
  )
}

interface StudySummary {
  opt_id: string
  kind: string
  status: string
  progress: number
  error_message: string | null
  source_job_id?: string | null
}

interface StudyState {
  summary: StudySummary
  result: StressResult | NoiseResult | null
}

const ACTIVE = new Set(['pending', 'running'])

/** The /optimize body for this run: the run's own settings plus a study spec. */
function studyPayload(job: FullJobDetails, optimize: Record<string, unknown>) {
  return {
    name: `${optimize.kind === 'stress' ? 'Cost stress' : 'Noise test'}: ${job.name}`,
    strategy_code: job.strategy_code,
    symbols: job.symbols,
    exchange: job.exchange,
    interval: job.interval,
    start_date: job.start_date,
    end_date: job.end_date,
    initial_capital: job.initial_capital,
    sizing_type: job.sizing_type,
    sizing_value: job.sizing_value,
    slippage_bps: job.slippage_bps,
    brokerage_bps: job.brokerage_bps,
    product_type: job.product_type,
    missing_data_policy: job.missing_data_policy,
    stop_loss_pct: job.stop_loss_pct ?? null,
    take_profit_pct: job.take_profit_pct ?? null,
    daily_loss_limit: job.daily_loss_limit ?? null,
    max_positions: job.max_positions,
    engine_options: job.engine_options ?? {},
    optimize: { ...optimize, source_job_id: job.job_id },
  }
}

function StudyBlock({
  title,
  state,
  starting,
  onStart,
  render,
}: {
  title: string
  state: StudyState | null
  starting: boolean
  onStart: () => void
  render: (r: StressResult | NoiseResult) => React.ReactNode
}) {
  const s = state?.summary
  const running = s ? ACTIVE.has(s.status) : false
  return (
    <div className="space-y-2 rounded-md border border-slate-800 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-sm font-medium">{title}</div>
        <div className="flex items-center gap-2">
          {s && (
            <Link
              className="text-xs text-primary hover:underline"
              to={`/backtest/optimizations/${s.opt_id}`}
            >
              Open full study
            </Link>
          )}
          <Button size="sm" variant="outline" disabled={running || starting} onClick={onStart}>
            {(running || starting) && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
            {s ? 'Run again' : 'Run'}
          </Button>
        </div>
      </div>
      {running && s && (
        <div className="space-y-1">
          <Progress value={s.progress} />
          <p className="text-xs text-muted-foreground">{Math.round(s.progress)}% complete</p>
        </div>
      )}
      {s?.status === 'failed' && (
        <p className="text-xs text-red-400">{s.error_message || 'The study failed.'}</p>
      )}
      {s?.status === 'cancelled' && <p className="text-xs text-amber-300">Cancelled.</p>}
      {state?.result && s?.status === 'completed' && render(state.result)}
      {!s && <p className="text-xs text-muted-foreground">Not run yet for this backtest.</p>}
    </div>
  )
}

export function RobustnessCard({ job }: { job: FullJobDetails }) {
  const [stress, setStress] = useState<StudyState | null>(null)
  const [noise, setNoise] = useState<StudyState | null>(null)
  const [starting, setStarting] = useState<'stress' | 'noise' | null>(null)
  const [pollKey, setPollKey] = useState(0)
  const jobRef = useRef(job.job_id)
  jobRef.current = job.job_id

  const load = useCallback(async () => {
    const jobId = job.job_id
    try {
      const res = await fetch('/backtest/api/optimizations', { credentials: 'include' })
      const body = await res.json()
      if (body.status !== 'success' || jobRef.current !== jobId) return false
      const mine = (body.optimizations as StudySummary[]).filter((o) => o.source_job_id === jobId)
      const pick = async (kind: string): Promise<StudyState | null> => {
        const summary = mine.find((o) => o.kind === kind)
        if (!summary) return null
        if (summary.status !== 'completed') return { summary, result: null }
        const r = await fetch(`/backtest/api/optimizations/${summary.opt_id}`, {
          credentials: 'include',
        })
        const d = await r.json()
        return { summary, result: d.status === 'success' ? d.optimization.result : null }
      }
      const [st, no] = await Promise.all([pick('stress'), pick('noise')])
      if (jobRef.current !== jobId) return false
      setStress(st)
      setNoise(no)
      return [st, no].some((x) => x && ACTIVE.has(x.summary.status))
    } catch {
      return false
    }
  }, [job.job_id])

  // biome-ignore lint/correctness/useExhaustiveDependencies: reset only when the run changes
  useEffect(() => {
    setStress(null)
    setNoise(null)
  }, [job.job_id])

  // biome-ignore lint/correctness/useExhaustiveDependencies: pollKey restarts polling after a study is queued
  useEffect(() => {
    let stop = false
    let timer: ReturnType<typeof setTimeout>
    const tick = async () => {
      const active = await load()
      if (!stop && active) timer = setTimeout(tick, 2500)
    }
    tick()
    return () => {
      stop = true
      clearTimeout(timer)
    }
  }, [load, pollKey])

  const start = async (kind: 'stress' | 'noise') => {
    setStarting(kind)
    try {
      const csrf = await fetchCSRFToken()
      const optimize =
        kind === 'stress' ? { kind, multipliers: [1, 1.5, 2, 3] } : { kind, runs: 20 }
      const res = await fetch('/backtest/api/optimize', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
        body: JSON.stringify(studyPayload(job, optimize)),
      })
      const body = await res.json()
      if (body.status !== 'success') throw new Error(body.message || 'Could not start the study')
      showToast.success(kind === 'stress' ? 'Cost stress test started' : 'Noise test started')
      setPollKey((k) => k + 1)
    } catch (e) {
      showToast.error(e instanceof Error ? e.message : 'Could not start the study')
    } finally {
      setStarting(null)
    }
  }

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">Robustness</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-3 lg:grid-cols-2">
        <StudyBlock
          title="Cost stress test"
          state={stress}
          starting={starting === 'stress'}
          onStart={() => start('stress')}
          render={(r) => (
            <div className="space-y-2">
              <VerdictBanner
                rating={(r as StressResult).verdict}
                title={stressHeadline(r as StressResult)}
              />
              <StressTable r={r as StressResult} compact />
            </div>
          )}
        />
        <StudyBlock
          title="Noise test (20 runs)"
          state={noise}
          starting={starting === 'noise'}
          onStart={() => start('noise')}
          render={(r) => (
            <div className="space-y-2">
              <VerdictBanner
                rating={(r as NoiseResult).verdict}
                title={noiseHeadline(r as NoiseResult)}
              />
              <NoiseSummary r={r as NoiseResult} />
            </div>
          )}
        />
      </CardContent>
    </Card>
  )
}
