import { Pause, Play, Trash2, TrendingDown, TrendingUp } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { fetchSessions, type SessionEntry, type Tick } from '@/api/depth-recorder'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { cn } from '@/lib/utils'
import DepthAnalytics from './DepthAnalytics'
import { DEPTH_LEVELS, fmtClock, Orb, type RecorderItem, statusColor } from './RecorderList'

type DetailTab = 'live' | 'sessions' | 'analytics'

function fmt(n: number | null | undefined): string {
  if (n == null) return '—'
  return new Intl.NumberFormat('en-IN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(n)
}

function fmtQty(n: number | null | undefined): string {
  if (n == null) return '—'
  if (n >= 1e7) return `${(n / 1e7).toFixed(2)}Cr`
  if (n >= 1e5) return `${(n / 1e5).toFixed(2)}L`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`
  return n.toLocaleString('en-IN')
}

function DepthTable({ tick }: { tick: Tick }) {
  const [showAll, setShowAll] = useState(false)
  const bidsAll = (tick.bids || []).filter((b) => b.price)
  const asksAll = (tick.asks || []).filter((a) => a.price)
  const maxLen = Math.max(bidsAll.length, asksAll.length)
  const limit = showAll ? Math.max(maxLen, 5) : 5
  const bids = bidsAll.slice(0, limit)
  const asks = asksAll.slice(0, limit)
  const maxBidQty = Math.max(...bids.map((b) => b.quantity ?? 0), 1)
  const maxAskQty = Math.max(...asks.map((a) => a.quantity ?? 0), 1)

  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-1 font-mono text-xs">
        <div className="space-y-0.5">
          <div className="flex justify-between px-1 pb-0.5 text-[10px] tracking-wider text-emerald-400/70 uppercase">
            <span>Qty</span>
            <span>Bid</span>
          </div>
          {bids.map((b, i) => {
            const pct = Math.min(((b.quantity ?? 0) / maxBidQty) * 100, 100)
            return (
              <div
                key={i}
                className="relative flex items-center overflow-hidden rounded px-1 py-0.5"
              >
                <div
                  className="absolute top-0 right-0 bottom-0 bg-emerald-500/10 transition-all"
                  style={{ width: `${pct}%` }}
                />
                <span className="relative z-10 flex-1 text-emerald-400">{fmtQty(b.quantity)}</span>
                <span className="relative z-10 text-foreground">{fmt(b.price)}</span>
              </div>
            )
          })}
        </div>

        <div className="space-y-0.5">
          <div className="flex justify-between px-1 pb-0.5 text-[10px] tracking-wider text-rose-400/70 uppercase">
            <span>Ask</span>
            <span>Qty</span>
          </div>
          {asks.map((a, i) => {
            const pct = Math.min(((a.quantity ?? 0) / maxAskQty) * 100, 100)
            return (
              <div
                key={i}
                className="relative flex items-center overflow-hidden rounded px-1 py-0.5"
              >
                <div
                  className="absolute top-0 bottom-0 left-0 bg-rose-500/10 transition-all"
                  style={{ width: `${pct}%` }}
                />
                <span className="relative z-10 flex-1 text-foreground">{fmt(a.price)}</span>
                <span className="relative z-10 text-rose-400">{fmtQty(a.quantity)}</span>
              </div>
            )
          })}
        </div>
      </div>
      {maxLen > 5 && (
        <div className="flex justify-center pt-0.5">
          <Button
            variant="ghost"
            size="sm"
            className="h-6 text-[11px] text-muted-foreground hover:text-foreground"
            onClick={() => setShowAll((prev) => !prev)}
          >
            {showAll ? 'Show top 5 levels' : `Show all levels (${maxLen})`}
          </Button>
        </div>
      )}
    </div>
  )
}

function SessionsTable({
  symbol,
  exchange,
  onAnalyse,
}: {
  symbol: string
  exchange: string
  onAnalyse(day: string): void
}) {
  const [sessions, setSessions] = useState<SessionEntry[] | null>(null)
  const [error, setError] = useState(false)

  const load = useCallback(async () => {
    try {
      const all = await fetchSessions(symbol, exchange)
      setSessions(all.filter((s) => s.symbol === symbol && s.exchange === exchange))
      setError(false)
    } catch {
      setSessions([])
      setError(true)
    }
  }, [symbol, exchange])

  useEffect(() => {
    setSessions(null)
    load()
  }, [load])

  if (sessions === null) {
    return <p className="py-8 text-center text-xs text-muted-foreground">Loading sessions…</p>
  }
  if (error) {
    return (
      <p className="py-8 text-center text-xs text-rose-400">Could not load recorded sessions.</p>
    )
  }
  if (sessions.length === 0) {
    return (
      <p className="py-8 text-center text-xs text-muted-foreground">
        No recorded days for {symbol} · {exchange} yet.
      </p>
    )
  }
  return (
    <table className="w-full text-xs tabular-nums">
      <thead className="text-[10px] tracking-wider text-muted-foreground uppercase">
        <tr className="border-b">
          <th className="py-1.5 text-left font-medium">Day</th>
          <th className="py-1.5 text-right font-medium">Ticks</th>
          <th className="py-1.5 text-right font-medium">First</th>
          <th className="py-1.5 text-right font-medium">Last</th>
          <th className="py-1.5" />
        </tr>
      </thead>
      <tbody className="font-mono">
        {sessions.map((s) => (
          <tr key={s.day} className="border-b border-border/40">
            <td className="py-1.5">{s.day}</td>
            <td className="py-1.5 text-right">{s.n_ticks.toLocaleString('en-IN')}</td>
            <td className="py-1.5 text-right text-muted-foreground">{fmtClock(s.first)}</td>
            <td className="py-1.5 text-right text-muted-foreground">{fmtClock(s.last)}</td>
            <td className="py-1 text-right">
              <Button
                variant="outline"
                size="sm"
                className="h-6 font-sans text-[11px]"
                onClick={() => onAnalyse(s.day)}
              >
                Analyse
              </Button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

interface RecorderDetailProps {
  item: RecorderItem
  /** Most recent stored tick for this symbol, if any. */
  latestTick: Tick | null
  onStart(depthLevel: number): void
  onStop(): void
  onDelete(): void
  onUpdateDepth(depthLevel: number): void
  className?: string
}

/** Right column of the depth recorder: one symbol's controls, live ladder, days and analytics. */
export function RecorderDetail({
  item,
  latestTick,
  onStart,
  onStop,
  onDelete,
  onUpdateDepth,
  className,
}: RecorderDetailProps) {
  const { symbol, exchange, rec } = item
  const [tab, setTab] = useState<DetailTab>('live')
  const [analyticsDay, setAnalyticsDay] = useState('')
  const isActive = rec?.status === 'recording'
  const depth = rec?.depth_level ?? 5

  // The tab is kept when another symbol is picked; the chosen day is not.
  // biome-ignore lint/correctness/useExhaustiveDependencies: reset on symbol change only
  useEffect(() => {
    setAnalyticsDay('')
  }, [symbol, exchange])

  return (
    <div className={cn('rounded-xl border border-border bg-card/60 backdrop-blur', className)}>
      {/* Symbol header and controls */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b px-4 py-3">
        <Orb status={statusColor(rec?.status)} />
        <span className="text-base font-bold tracking-tight">
          {symbol}
          <span className="ml-1 text-sm font-normal text-muted-foreground">· {exchange}</span>
        </span>
        <Badge
          variant="outline"
          className={cn(
            'px-1.5 text-[10px]',
            isActive
              ? 'border-emerald-500/40 text-emerald-400'
              : 'border-muted-foreground/30 text-muted-foreground'
          )}
        >
          {rec ? rec.status : 'no recorder'}
        </Badge>

        {rec && (
          <div className="flex items-center gap-1.5 text-xs">
            <span className="text-muted-foreground">Depth</span>
            <Select
              value={String(DEPTH_LEVELS.includes(depth) ? depth : 5)}
              onValueChange={(val) => {
                const d = Number(val)
                if (!Number.isNaN(d)) onUpdateDepth(d)
              }}
            >
              <SelectTrigger className="h-7 w-[4.5rem] text-xs" aria-label="Depth level">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {DEPTH_LEVELS.map((lvl) => (
                  <SelectItem key={lvl} value={String(lvl)}>
                    {lvl}D
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {!DEPTH_LEVELS.includes(depth) && <span className="font-mono">({depth}D)</span>}
            {rec.actual_depth != null && rec.actual_depth !== rec.depth_level && (
              <span className="text-[11px] text-amber-400" title="Fallback depth from broker">
                (Actual: {rec.actual_depth}D)
              </span>
            )}
          </div>
        )}

        <div className="ml-auto flex items-center gap-1.5">
          {isActive ? (
            <Button variant="outline" size="sm" className="h-7 text-xs" onClick={onStop}>
              <Pause className="mr-1 h-3.5 w-3.5" /> Stop
            </Button>
          ) : (
            <Button
              variant="outline"
              size="sm"
              className="h-7 text-xs"
              onClick={() => onStart(depth)}
            >
              <Play className="mr-1 h-3.5 w-3.5" /> Start
            </Button>
          )}
          {rec && (
            <Button
              variant="ghost"
              size="sm"
              className="h-7 text-xs text-muted-foreground hover:text-rose-400"
              onClick={onDelete}
              title="Remove this recorder (stored ticks are kept)"
            >
              <Trash2 className="mr-1 h-3.5 w-3.5" /> Remove
            </Button>
          )}
        </div>
      </div>

      <div className="space-y-4 p-4">
        <Tabs value={tab} onValueChange={(v) => setTab(v as DetailTab)}>
          <TabsList>
            <TabsTrigger value="live">Live</TabsTrigger>
            <TabsTrigger value="sessions">Sessions</TabsTrigger>
            <TabsTrigger value="analytics">Analytics</TabsTrigger>
          </TabsList>
        </Tabs>

        {tab === 'live' && (
          <div className="max-w-2xl space-y-3">
            {rec && (
              <div className="grid grid-cols-3 gap-2 text-center">
                <div className="rounded-md bg-muted/40 py-2">
                  <div className="font-mono text-sm font-bold text-cyan-400">
                    {rec.ticks_written.toLocaleString('en-IN')}
                  </div>
                  <div className="text-[9px] tracking-wider text-muted-foreground uppercase">
                    Saved
                  </div>
                </div>
                <div className="rounded-md bg-muted/40 py-2">
                  <div className="font-mono text-sm font-bold text-amber-400">
                    {rec.ticks_buffered}
                  </div>
                  <div className="text-[9px] tracking-wider text-muted-foreground uppercase">
                    Buffer
                  </div>
                </div>
                <div className="rounded-md bg-muted/40 py-2">
                  <div className="font-mono text-sm font-bold">{fmtClock(rec.last_tick_time)}</div>
                  <div className="text-[9px] tracking-wider text-muted-foreground uppercase">
                    Last tick
                  </div>
                </div>
              </div>
            )}

            {latestTick ? (
              <div className="space-y-2">
                <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
                  <span>
                    {isActive ? 'Latest snapshot' : 'Last stored snapshot'} ·{' '}
                    {fmtClock(latestTick.tick_time)}
                  </span>
                  <div className="flex items-center gap-3 font-mono">
                    <span>
                      LTP{' '}
                      <span className="font-semibold text-foreground">{fmt(latestTick.ltp)}</span>
                    </span>
                    <span className="flex items-center gap-0.5 text-emerald-400">
                      <TrendingUp className="h-3 w-3" />
                      {fmtQty(latestTick.total_buy_qty)}
                    </span>
                    <span className="flex items-center gap-0.5 text-rose-400">
                      <TrendingDown className="h-3 w-3" />
                      {fmtQty(latestTick.total_sell_qty)}
                    </span>
                  </div>
                </div>
                <DepthTable tick={latestTick} />
              </div>
            ) : (
              <p className="py-6 text-center text-xs text-muted-foreground">
                No depth snapshot stored for this symbol yet.
              </p>
            )}

            {rec?.error && (
              <div className="rounded bg-rose-500/10 px-2 py-1 font-mono text-[11px] text-rose-400">
                {rec.error}
              </div>
            )}
          </div>
        )}

        {tab === 'sessions' && (
          <div className="max-w-2xl">
            <SessionsTable
              symbol={symbol}
              exchange={exchange}
              onAnalyse={(day) => {
                setAnalyticsDay(day)
                setTab('analytics')
              }}
            />
          </div>
        )}

        {tab === 'analytics' && (
          <DepthAnalytics
            key={`${exchange}:${symbol}:${analyticsDay}`}
            initialSymbol={symbol}
            initialExchange={exchange}
            initialDay={analyticsDay}
          />
        )}
      </div>
    </div>
  )
}
