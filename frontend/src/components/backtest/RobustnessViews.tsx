import type * as PlotlyTypes from 'plotly.js'
import { useMemo } from 'react'
import { PLOT_CONFIG, PLOT_LAYOUT } from '@/components/backtest/plotConfig'
import { binReturns } from '@/components/backtest/ScanResults'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import Plot from '@/lib/Plot2D'

export type Rating = 'PASS' | 'MARGINAL' | 'FAIL'

export interface RunMetrics {
  total_return_pct: number | null
  sharpe_ratio: number | null
  max_drawdown_pct: number | null
  total_trades: number | null
  profit_factor?: number | null
}

export interface StressResult {
  kind: 'stress'
  rows: { multiplier: number; metrics?: RunMetrics; error?: string }[]
  breakeven_multiplier: number | null
  verdict: Rating
  warnings: string[]
  notes?: string[]
}

type Pcts = {
  p5: number | null
  p25: number | null
  p50: number | null
  p75: number | null
  p95: number | null
}

export interface NoiseResult {
  kind: 'noise'
  runs: number
  noise_pct: number
  baseline: RunMetrics | null
  results: (RunMetrics | { error: string })[]
  distribution: { total_return_pct: Pcts; sharpe_ratio: Pcts }
  profitable_share: number
  verdict: Rating
  warnings: string[]
  notes?: string[]
}

export interface WalkForwardVerdict {
  rating: Rating
  reasons: string[]
}

const fmt = (n: number | null | undefined, d = 2) =>
  n === null || n === undefined ? '-' : n.toLocaleString('en-IN', { maximumFractionDigits: d })
const pct = (n: number | null | undefined) => (n === null || n === undefined ? '-' : `${fmt(n)}%`)

const RATING_TONE: Record<Rating, string> = {
  PASS: 'border-emerald-800 bg-emerald-950/30 text-emerald-300',
  MARGINAL: 'border-amber-800 bg-amber-950/30 text-amber-300',
  FAIL: 'border-red-900 bg-red-950/40 text-red-300',
}

export function RatingBadge({ rating }: { rating: Rating }) {
  return (
    <span
      className={`inline-block rounded border px-1.5 py-0.5 text-[11px] font-semibold tracking-wide ${RATING_TONE[rating]}`}
    >
      {rating}
    </span>
  )
}

/** One-line verdict with its reasons underneath. */
export function VerdictBanner({
  rating,
  title,
  reasons,
}: {
  rating: Rating
  title: string
  reasons?: string[]
}) {
  return (
    <div className={`rounded-md border p-3 ${RATING_TONE[rating]}`}>
      <div className="flex items-center gap-2 text-sm font-medium">
        <RatingBadge rating={rating} />
        <span>{title}</span>
      </div>
      {reasons && reasons.length > 0 && (
        <ul className="mt-2 space-y-0.5 text-xs opacity-90">
          {reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

export function stressHeadline(r: StressResult) {
  if (r.breakeven_multiplier === null) return 'Profitable at every tested cost level'
  if (r.breakeven_multiplier <= 1) return 'Not profitable even at current costs'
  const below = r.rows
    .filter((x) => x.metrics && x.multiplier < (r.breakeven_multiplier as number))
    .map((x) => x.multiplier)
  const last = below.length ? Math.max(...below) : 1
  return `Profitable up to ${last}x costs, loses money at ${r.breakeven_multiplier}x`
}

export function noiseHeadline(r: NoiseResult) {
  return `${Math.round(r.profitable_share * 100)}% of ${r.runs} noisy runs were profitable (noise ${r.noise_pct}% of price)`
}

export function StressTable({ r, compact = false }: { r: StressResult; compact?: boolean }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Costs</TableHead>
          <TableHead className="text-right">Return</TableHead>
          <TableHead className="text-right">Sharpe</TableHead>
          <TableHead className="text-right">Max DD</TableHead>
          {!compact && <TableHead className="text-right">Trades</TableHead>}
          {!compact && <TableHead className="text-right">Profit factor</TableHead>}
        </TableRow>
      </TableHeader>
      <TableBody>
        {r.rows.map((row) => (
          <TableRow key={row.multiplier}>
            <TableCell className="font-mono">{row.multiplier}x</TableCell>
            {row.metrics ? (
              <>
                <TableCell
                  className={`text-right ${(row.metrics.total_return_pct ?? 0) > 0 ? 'text-emerald-400' : 'text-red-400'}`}
                >
                  {pct(row.metrics.total_return_pct)}
                </TableCell>
                <TableCell className="text-right">{fmt(row.metrics.sharpe_ratio)}</TableCell>
                <TableCell className="text-right">{pct(row.metrics.max_drawdown_pct)}</TableCell>
                {!compact && (
                  <TableCell className="text-right">{fmt(row.metrics.total_trades, 0)}</TableCell>
                )}
                {!compact && (
                  <TableCell className="text-right">{fmt(row.metrics.profit_factor)}</TableCell>
                )}
              </>
            ) : (
              <TableCell colSpan={compact ? 3 : 5} className="text-xs text-red-400">
                {row.error}
              </TableCell>
            )}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

export function StressView({ r }: { r: StressResult }) {
  return (
    <div className="space-y-4">
      <VerdictBanner rating={r.verdict} title={stressHeadline(r)} />
      <StressTable r={r} />
      <p className="text-xs text-muted-foreground">
        Each row reruns the whole period with brokerage, slippage, spread, impact and every
        statutory charge multiplied. PASS means the strategy still makes money at twice today's
        costs, MARGINAL at 1.5 times, FAIL below that.
      </p>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-slate-800 bg-slate-900/60 p-3">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="text-base font-semibold">{value}</div>
    </div>
  )
}

export function NoiseSummary({ r }: { r: NoiseResult }) {
  const d = r.distribution.total_return_pct
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
      <Stat label="Original return" value={pct(r.baseline?.total_return_pct)} />
      <Stat label="Median noisy return" value={pct(d.p50)} />
      <Stat label="Worst 5% of runs" value={pct(d.p5)} />
      <Stat label="Profitable runs" value={`${Math.round(r.profitable_share * 100)}%`} />
    </div>
  )
}

export function NoiseView({ r }: { r: NoiseResult }) {
  const hist = useMemo(() => {
    const values = r.results
      .map((x) => ('error' in x ? null : x.total_return_pct))
      .filter((v): v is number => v !== null && v !== undefined)
    if (!values.length) return null
    const base = r.baseline?.total_return_pct
    const bins = binReturns(values)
    return {
      // The 2D Plotly build has no histogram trace: bin here and draw bars.
      data: [
        {
          type: 'bar',
          x: bins.centers,
          y: bins.counts,
          width: bins.width * 0.95,
          customdata: bins.labels,
          marker: { color: '#38bdf8' },
          name: 'Noisy runs',
          hovertemplate: '%{customdata}: %{y} runs<extra></extra>',
        },
      ] as PlotlyTypes.Data[],
      layout: {
        ...PLOT_LAYOUT,
        height: 240,
        xaxis: { ...(PLOT_LAYOUT.xaxis ?? {}), title: { text: 'Total return %' } },
        yaxis: { ...(PLOT_LAYOUT.yaxis ?? {}), title: { text: 'Runs' } },
        shapes:
          base === null || base === undefined
            ? []
            : [
                {
                  type: 'line',
                  x0: base,
                  x1: base,
                  yref: 'paper',
                  y0: 0,
                  y1: 1,
                  line: { color: '#fbbf24', width: 2, dash: 'dash' },
                },
              ],
        annotations:
          base === null || base === undefined
            ? []
            : [
                {
                  x: base,
                  yref: 'paper',
                  y: 1,
                  text: 'Original',
                  showarrow: false,
                  font: { color: '#fbbf24', size: 11 },
                  yanchor: 'bottom',
                },
              ],
      } as Partial<PlotlyTypes.Layout>,
    }
  }, [r])
  const s = r.distribution.sharpe_ratio
  return (
    <div className="space-y-4">
      <VerdictBanner rating={r.verdict} title={noiseHeadline(r)} />
      <NoiseSummary r={r} />
      {hist && (
        <Plot
          data={hist.data}
          layout={hist.layout}
          config={PLOT_CONFIG}
          style={{ width: '100%', height: 240 }}
          useResizeHandler
        />
      )}
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Percentile</TableHead>
            <TableHead className="text-right">Return</TableHead>
            <TableHead className="text-right">Sharpe</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {(['p5', 'p25', 'p50', 'p75', 'p95'] as const).map((k) => (
            <TableRow key={k}>
              <TableCell>{k.slice(1)}th</TableCell>
              <TableCell className="text-right">
                {pct(r.distribution.total_return_pct[k])}
              </TableCell>
              <TableCell className="text-right">{fmt(s[k])}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <p className="text-xs text-muted-foreground">
        Each run moves every open, high, low and close by a small random amount (about {r.noise_pct}
        % of price) and reruns the strategy. PASS means at least 80% of runs stay profitable and the
        median keeps half of the original return. A strategy tuned to exact price levels falls apart
        here.
      </p>
    </div>
  )
}
