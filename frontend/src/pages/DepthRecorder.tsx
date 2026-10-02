import {
  Activity,
  Circle,
  Database,
  Pause,
  Play,
  RefreshCw,
  Trash2,
  TrendingDown,
  TrendingUp,
} from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  deleteRecorderConfig,
  fetchRecorderStatus,
  fetchStats,
  fetchTicks,
  setDepthLevel,
  startRecorder,
  stopRecorder,
  type RecorderStatus,
  type StatsData,
  type Tick,
} from '@/api/depth-recorder'
import DepthAnalytics from '@/components/depth-recorder/DepthAnalytics'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { cn } from '@/lib/utils'
import { showToast } from '@/utils/toast'

// ─── Helpers ─────────────────────────────────────────────────────────────────

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

function fmtTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleTimeString('en-IN', {
      hour12: false,
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    })
  } catch {
    return iso
  }
}

function statusColor(status: string): 'success' | 'warning' | 'error' | 'idle' {
  if (status === 'recording') return 'success'
  if (status === 'reconnecting' || status === 'connecting' || status === 'starting')
    return 'warning'
  if (status === 'stopped' || status === 'stopping') return 'idle'
  if (status === 'waiting_market_hours') return 'warning'
  if (status === 'subscribe_failed') return 'error'
  return 'idle'
}

function Orb({ status }: { status: 'success' | 'warning' | 'error' | 'idle' }) {
  return (
    <div className="relative flex-shrink-0">
      <div
        className={cn(
          'w-2.5 h-2.5 rounded-full',
          status === 'success' && 'bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.9)]',
          status === 'warning' && 'bg-amber-400 shadow-[0_0_6px_rgba(251,191,36,0.9)]',
          status === 'error' && 'bg-rose-400 shadow-[0_0_6px_rgba(251,113,133,0.9)]',
          status === 'idle' && 'bg-muted-foreground/40'
        )}
      />
      {status !== 'idle' && (
        <div
          className={cn(
            'absolute inset-0 rounded-full animate-ping opacity-50',
            status === 'success' && 'bg-emerald-400',
            status === 'warning' && 'bg-amber-400',
            status === 'error' && 'bg-rose-400'
          )}
        />
      )}
    </div>
  )
}

// ─── Depth Table ──────────────────────────────────────────────────────────────

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
      <div className="grid grid-cols-2 gap-1 text-xs font-mono">
        {/* Bids */}
        <div className="space-y-0.5">
          <div className="flex justify-between text-emerald-400/70 text-[10px] uppercase tracking-wider px-1 pb-0.5">
            <span>Qty</span>
            <span>Bid</span>
          </div>
          {bids.map((b, i) => {
            const pct = Math.min(((b.quantity ?? 0) / maxBidQty) * 100, 100)
            return (
              <div key={i} className="relative flex items-center px-1 py-0.5 rounded overflow-hidden">
                <div
                  className="absolute right-0 top-0 bottom-0 bg-emerald-500/10 transition-all"
                  style={{ width: `${pct}%` }}
                />
                <span className="relative z-10 flex-1 text-emerald-400">{fmtQty(b.quantity)}</span>
                <span className="relative z-10 text-foreground">{fmt(b.price)}</span>
              </div>
            )
          })}
        </div>

        {/* Asks */}
        <div className="space-y-0.5">
          <div className="flex justify-between text-rose-400/70 text-[10px] uppercase tracking-wider px-1 pb-0.5">
            <span>Ask</span>
            <span>Qty</span>
          </div>
          {asks.map((a, i) => {
            const pct = Math.min(((a.quantity ?? 0) / maxAskQty) * 100, 100)
            return (
              <div key={i} className="relative flex items-center px-1 py-0.5 rounded overflow-hidden">
                <div
                  className="absolute left-0 top-0 bottom-0 bg-rose-500/10 transition-all"
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

// ─── Recorder Card ────────────────────────────────────────────────────────────

function RecorderCard({
  rec,
  latestTick,
  onStop,
  onDelete,
  onUpdateDepth,
}: {
  rec: RecorderStatus
  latestTick: Tick | null
  onStop: () => void
  onDelete: () => void
  onUpdateDepth: (depth: number) => void
}) {
  const orb = statusColor(rec.status)
  const isActive = rec.status === 'recording'

  return (
    <Card className="border-border bg-card/60 backdrop-blur">
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Orb status={orb} />
            <span className="font-bold tracking-tight">
              {rec.symbol}
              <span className="text-muted-foreground font-normal text-sm ml-1">
                · {rec.exchange}
              </span>
            </span>
          </div>
          <div className="flex items-center gap-1">
            <Badge
              variant="outline"
              className={cn(
                'text-[10px] px-1.5',
                isActive
                  ? 'border-emerald-500/40 text-emerald-400'
                  : 'border-muted-foreground/30 text-muted-foreground'
              )}
            >
              {rec.status}
            </Badge>
            {isActive && (
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7 text-muted-foreground hover:text-foreground"
                onClick={onStop}
                title="Stop recording"
              >
                <Pause className="w-3.5 h-3.5" />
              </Button>
            )}
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7 text-muted-foreground hover:text-rose-400"
              onClick={onDelete}
              title="Remove"
            >
              <Trash2 className="w-3.5 h-3.5" />
            </Button>
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-3 pt-0">
        {/* Depth level info & selector */}
        <div className="flex items-center justify-between text-xs bg-muted/30 rounded px-2.5 py-1.5">
          <div className="flex items-center gap-1.5 font-mono">
            <span className="text-muted-foreground font-sans">Depth:</span>
            <span className="font-bold">{rec.depth_level ?? 5}D</span>
            {rec.actual_depth != null && rec.actual_depth !== rec.depth_level && (
              <span className="text-amber-400 text-[11px]" title="Fallback depth from broker">
                (Actual: {rec.actual_depth}D)
              </span>
            )}
          </div>
          <div className="flex items-center gap-1">
            <Select
              value={String([5, 20, 50, 200].includes(rec.depth_level ?? 5) ? (rec.depth_level ?? 5) : '5')}
              onValueChange={(val) => {
                const d = Number(val)
                if (!Number.isNaN(d)) {
                  onUpdateDepth(d)
                }
              }}
            >
              <SelectTrigger className="h-6 w-16 text-[11px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="5">5D</SelectItem>
                <SelectItem value="20">20D</SelectItem>
                <SelectItem value="50">50D</SelectItem>
                <SelectItem value="200">200D</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>

        {/* Stats row */}
        <div className="grid grid-cols-3 gap-2 text-center">
          <div className="rounded-md bg-muted/40 py-2">
            <div className="text-sm font-bold font-mono text-cyan-400">
              {rec.ticks_written.toLocaleString('en-IN')}
            </div>
            <div className="text-[9px] uppercase tracking-wider text-muted-foreground">Saved</div>
          </div>
          <div className="rounded-md bg-muted/40 py-2">
            <div className="text-sm font-bold font-mono text-amber-400">{rec.ticks_buffered}</div>
            <div className="text-[9px] uppercase tracking-wider text-muted-foreground">Buffer</div>
          </div>
          <div className="rounded-md bg-muted/40 py-2">
            <div className="text-sm font-bold font-mono">
              {rec.last_tick_time ? fmtTime(rec.last_tick_time) : '—'}
            </div>
            <div className="text-[9px] uppercase tracking-wider text-muted-foreground">
              Last tick
            </div>
          </div>
        </div>

        {/* Latest tick depth */}
        {latestTick && (
          <div className="space-y-2">
            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span>Latest snapshot · {fmtTime(latestTick.tick_time)}</span>
              <div className="flex items-center gap-3 font-mono">
                <span>
                  LTP <span className="text-foreground font-semibold">{fmt(latestTick.ltp)}</span>
                </span>
                <span className="text-emerald-400 flex items-center gap-0.5">
                  <TrendingUp className="w-3 h-3" />
                  {fmtQty(latestTick.total_buy_qty)}
                </span>
                <span className="text-rose-400 flex items-center gap-0.5">
                  <TrendingDown className="w-3 h-3" />
                  {fmtQty(latestTick.total_sell_qty)}
                </span>
              </div>
            </div>
            <DepthTable tick={latestTick} />
          </div>
        )}

        {rec.error && (
          <div className="text-[11px] text-rose-400 bg-rose-500/10 rounded px-2 py-1 font-mono">
            {rec.error}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

// ─── Main Page ────────────────────────────────────────────────────────────────

const EXCHANGES = ['NSE', 'BSE', 'NFO', 'BFO', 'MCX', 'CDS']

export default function DepthRecorder() {
  const [statuses, setStatuses] = useState<RecorderStatus[]>([])
  const [stats, setStats] = useState<StatsData | null>(null)
  const [latestTicks, setLatestTicks] = useState<Record<string, Tick>>({})
  const [symbol, setSymbol] = useState('RELIANCE')
  const [exchange, setExchange] = useState('NSE')
  const [depthLevel, setDepthLevelState] = useState<number>(5)
  const [loading, setLoading] = useState(false)
  const [tab, setTab] = useState('recorders')
  const pollerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const fetchStatusesCb = useCallback(async () => {
    try {
      const data = await fetchRecorderStatus()
      setStatuses(data)
    } catch {}
  }, [])

  const fetchStatsCb = useCallback(async () => {
    try {
      const data = await fetchStats()
      if (data) {
        setStats(data)
      }
    } catch {}
  }, [])

  const fetchLatestTickCb = useCallback(async (sym: string, exch: string) => {
    try {
      const data = await fetchTicks(sym, exch, 1)
      const tick = data.ticks?.[0]
      if (tick) {
        setLatestTicks((prev) => ({ ...prev, [`${exch}:${sym}`]: tick }))
      }
    } catch {}
  }, [])

  // Poll every 2 s
  useEffect(() => {
    fetchStatusesCb()
    fetchStatsCb()
    pollerRef.current = setInterval(() => {
      fetchStatusesCb()
      fetchStatsCb()
    }, 2000)
    return () => {
      if (pollerRef.current) clearInterval(pollerRef.current)
    }
  }, [fetchStatusesCb, fetchStatsCb])

  // Fetch latest ticks for recording symbols
  useEffect(() => {
    const recording = statuses.filter((s) => s.status === 'recording')
    recording.forEach((s) => fetchLatestTickCb(s.symbol, s.exchange))
  }, [statuses, fetchLatestTickCb])

  const handleStart = async () => {
    if (!symbol.trim()) {
      showToast.error('Enter a symbol')
      return
    }
    setLoading(true)
    try {
      const d = await startRecorder(symbol, exchange, depthLevel)
      if (d.status === 'started' || d.status === 'already_recording') {
        showToast.success(`Recording started for ${symbol} · ${exchange} (${depthLevel}D)`)
      } else {
        showToast.error(d.message || 'Failed to start')
      }
      fetchStatusesCb()
    } catch {
      showToast.error('Request failed')
    } finally {
      setLoading(false)
    }
  }

  const handleStop = async (sym: string, exch: string) => {
    try {
      await stopRecorder(sym, exch)
      showToast.success(`Stopped ${sym}`)
      fetchStatusesCb()
    } catch {
      showToast.error('Stop failed')
    }
  }

  const handleDelete = async (sym: string, exch: string) => {
    try {
      await deleteRecorderConfig(sym, exch)
      showToast.success(`Removed ${sym}`)
      setStatuses((prev) => prev.filter((s) => !(s.symbol === sym && s.exchange === exch)))
    } catch {
      showToast.error('Delete failed')
    }
  }

  const handleUpdateDepth = async (sym: string, exch: string, newDepth: number) => {
    try {
      const res = await setDepthLevel(sym, exch, newDepth)
      if (res.status === 'ok') {
        showToast.success(`Updated depth to ${res.actual_depth ?? newDepth}D for ${sym}`)
      } else {
        showToast.error(res.message || 'Failed to update depth level')
      }
      fetchStatusesCb()
    } catch {
      showToast.error('Failed to update depth level')
    }
  }

  const recordingCount = statuses.filter((s) => s.status === 'recording').length

  return (
    <div className="py-6 space-y-6">
      {/* ── Header ── */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <Database className="w-6 h-6 text-cyan-400" />
            Depth Recorder
          </h1>
          <p className="text-sm text-muted-foreground mt-0.5">
            Stream &amp; store live market depth ticks to SQLite, then analyse them
          </p>
        </div>

        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            fetchStatusesCb()
            fetchStatsCb()
          }}
          className="self-start sm:self-auto"
        >
          <RefreshCw className="w-3.5 h-3.5 mr-1.5" />
          Refresh
        </Button>
      </div>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="recorders">Recorders</TabsTrigger>
          <TabsTrigger value="analytics">Analytics</TabsTrigger>
        </TabsList>
      </Tabs>

      {tab === 'analytics' ? (
        <DepthAnalytics />
      ) : (
        <>
          {/* ── Global Stats ── */}
          {stats && (
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
              {[
                {
                  label: 'Total Ticks',
                  value: stats.total_ticks.toLocaleString('en-IN'),
                  icon: Activity,
                  color: 'text-cyan-400',
                },
                {
                  label: 'Symbols Tracked',
                  value: stats.symbols.length,
                  icon: Database,
                  color: 'text-violet-400',
                },
                {
                  label: 'Active Recorders',
                  value: recordingCount,
                  icon: Circle,
                  color: 'text-emerald-400',
                },
                {
                  label: 'Latest Tick',
                  value: stats.latest_tick_time ? fmtTime(stats.latest_tick_time) : '—',
                  icon: Activity,
                  color: 'text-amber-400',
                },
              ].map(({ label, value, icon: Icon, color }) => (
                <div
                  key={label}
                  className="rounded-xl border border-border bg-card/60 backdrop-blur px-4 py-3"
                >
                  <div className="flex items-center gap-2 mb-1">
                    <Icon className={cn('w-3.5 h-3.5', color)} />
                    <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
                      {label}
                    </span>
                  </div>
                  <div className="text-xl font-bold font-mono">{value}</div>
                </div>
              ))}
            </div>
          )}

          {/* ── Add Recorder ── */}
          <Card className="border-border bg-card/60 backdrop-blur">
            <CardHeader className="pb-3">
              <CardTitle className="text-base">Add Recorder</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="flex flex-wrap gap-3 items-center">
                <Input
                  id="depth-symbol-input"
                  className="w-40 font-mono uppercase"
                  placeholder="RELIANCE"
                  value={symbol}
                  onChange={(e) => setSymbol(e.target.value.toUpperCase())}
                  onKeyDown={(e) => e.key === 'Enter' && handleStart()}
                />
                <Select value={exchange} onValueChange={setExchange}>
                  <SelectTrigger id="depth-exchange-select" className="w-28">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {EXCHANGES.map((ex) => (
                      <SelectItem key={ex} value={ex}>
                        {ex}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <div className="flex items-center gap-1.5">
                  <div className="flex items-center rounded border bg-muted/40 p-0.5 text-xs">
                    {[5, 20, 50, 200].map((lvl) => (
                      <button
                        key={lvl}
                        type="button"
                        onClick={() => setDepthLevelState(lvl)}
                        className={cn(
                          'px-2.5 py-1 rounded text-[11px] font-medium transition-colors',
                          depthLevel === lvl
                            ? 'bg-cyan-500 text-black font-semibold shadow-sm'
                            : 'text-muted-foreground hover:text-foreground'
                        )}
                      >
                        {lvl}D
                      </button>
                    ))}
                  </div>
                  <Input
                    type="number"
                    min={5}
                    max={200}
                    className="w-20 font-mono text-xs h-9"
                    value={depthLevel}
                    onChange={(e) => {
                      const v = parseInt(e.target.value, 10)
                      if (!Number.isNaN(v)) {
                        setDepthLevelState(Math.min(200, Math.max(5, v)))
                      } else {
                        setDepthLevelState(5)
                      }
                    }}
                    placeholder="Depth"
                    title="Custom depth level (5-200)"
                  />
                </div>
                <Button
                  id="depth-start-btn"
                  onClick={handleStart}
                  disabled={loading}
                  className="bg-cyan-500 hover:bg-cyan-400 text-black font-semibold"
                >
                  <Play className="w-3.5 h-3.5 mr-1.5" />
                  {loading ? 'Starting…' : 'Start Recording'}
                </Button>
              </div>
              <p className="text-[11px] text-muted-foreground mt-2">
                Connects to <code className="text-cyan-400">ws://127.0.0.1:8765</code>, subscribes
                Depth mode, and writes ticks to <code className="text-cyan-400">depth_ticks</code>{' '}
                table. Auto-reconnects on disconnect. Active only during market hours (9:15–15:30
                IST weekdays).
              </p>
            </CardContent>
          </Card>

          {/* ── Recorder Cards ── */}
          {statuses.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-16 text-muted-foreground gap-3">
              <Database className="w-12 h-12 opacity-20" />
              <p className="text-sm">No recorders yet. Add one above.</p>
            </div>
          ) : (
            <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
              {statuses.map((rec) => (
                <RecorderCard
                  key={`${rec.exchange}:${rec.symbol}`}
                  rec={rec}
                  latestTick={latestTicks[`${rec.exchange}:${rec.symbol}`] ?? null}
                  onStop={() => handleStop(rec.symbol, rec.exchange)}
                  onDelete={() => handleDelete(rec.symbol, rec.exchange)}
                  onUpdateDepth={(d) => handleUpdateDepth(rec.symbol, rec.exchange, d)}
                />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}
