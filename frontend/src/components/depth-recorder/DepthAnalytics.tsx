import {
  ColorType,
  CrosshairMode,
  createChart,
  HistogramSeries,
  type IChartApi,
  LineSeries,
  type UTCTimestamp,
} from 'lightweight-charts'
import {
  Activity,
  BarChart3,
  Database,
  Eye,
  Flame,
  Play,
  RefreshCw,
  TrendingUp,
  TriangleAlert,
  Wallet,
} from 'lucide-react'
import type { MouseEvent as ReactMouseEvent } from 'react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  fetchHealth,
  fetchLeadLag,
  fetchMetrics,
  fetchSessions,
  fetchWalls,
  type HealthData,
  heatmapUrl,
  type LeadLagData,
  type MetricsPayload,
  type SessionEntry,
  type WallsData,
} from '@/api/depth-recorder'
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
import { useThemeStore } from '@/stores/themeStore'

// ─── Types ────────────────────────────────────────────────────────────────

// ─── Helpers ───────────────────────────────────────────────────────────────

function fmtNum(v: number | null | undefined, d = 2): string {
  if (v == null || Number.isNaN(v)) return '—'
  return new Intl.NumberFormat('en-IN', {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  }).format(v)
}

function fmtQty(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '—'
  if (v >= 1e7) return `${(v / 1e7).toFixed(2)}Cr`
  if (v >= 1e5) return `${(v / 1e5).toFixed(2)}L`
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)}K`
  return v.toLocaleString('en-IN')
}

function fmtTime(epoch: number | null | undefined): string {
  if (epoch == null) return '—'
  const d = new Date(epoch * 1000)
  return d.toLocaleTimeString('en-IN', {
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

function seriesPoints(
  s: Record<string, Array<number | null>>,
  key: string
): Array<{ time: UTCTimestamp; value: number }> {
  const t = s.time ?? []
  const v = s[key] ?? []
  const out: Array<{ time: UTCTimestamp; value: number }> = []
  for (let i = 0; i < t.length; i++) {
    if (v[i] == null) continue
    out.push({ time: t[i] as UTCTimestamp, value: v[i] as number })
  }
  return out
}

// ─── Heatmap ───────────────────────────────────────────────────────────────

function HeatmapTab({ symbol, exchange, day }: { symbol: string; exchange: string; day: string }) {
  const [meta, setMeta] = useState<Record<string, unknown> | null>(null)
  const [crosshair, setCrosshair] = useState<{ price: number; time: string } | null>(null)
  const [imgKey, setImgKey] = useState(0)

  const url = heatmapUrl(symbol, exchange, day)

  const loadMeta = useCallback(async () => {
    try {
      const r = await fetch(
        `/api/depth-recorder/heatmap?symbol=${encodeURIComponent(symbol)}&exchange=${encodeURIComponent(exchange)}&day=${encodeURIComponent(day)}`,
        { credentials: 'include' }
      )
      if (r.ok) setMeta(await r.json())
    } catch {}
  }, [symbol, exchange, day])

  useEffect(() => {
    setMeta(null)
    setImgKey((k) => k + 1)
    loadMeta()
  }, [loadMeta])

  const onMove = (e: ReactMouseEvent<HTMLDivElement>) => {
    const rect = e.currentTarget.getBoundingClientRect()
    const x = e.clientX - rect.left
    const y = e.clientY - rect.top
    const n = rect.width
    const h = rect.height
    const times = (meta?.times as string[] | undefined) ?? []
    const pmin = (meta?.price_min as number | undefined) ?? 0
    const pmax = (meta?.price_max as number | undefined) ?? 1
    if (n <= 0 || h <= 0) return
    const ti = Math.floor((x / n) * times.length)
    const price = pmax - (y / h) * (pmax - pmin)
    setCrosshair({ price, time: times[Math.min(Math.max(ti, 0), times.length - 1)] ?? '' })
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          Resting quantity over time × price. Log-scaled, clipped at the 95th percentile.
        </p>
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            setImgKey((k) => k + 1)
            loadMeta()
          }}
        >
          <RefreshCw className="w-3.5 h-3.5 mr-1.5" />
          Re-render
        </Button>
      </div>

      <div
        className="relative rounded-lg overflow-hidden border border-border cursor-crosshair"
        onMouseMove={onMove}
        onMouseLeave={() => setCrosshair(null)}
      >
        <img
          key={imgKey}
          src={url}
          alt="Liquidity heatmap"
          className="w-full h-auto block"
          draggable={false}
        />
        {crosshair && (
          <div className="pointer-events-none absolute top-2 left-2 bg-background/80 backdrop-blur rounded px-2 py-1 text-[11px] font-mono">
            {crosshair.time} · {fmtNum(crosshair.price, 2)}
          </div>
        )}
      </div>
    </div>
  )
}

// ─── Charts ─────────────────────────────────────────────────────────────────

function ChartsTab({ payload }: { payload: MetricsPayload | null }) {
  const { mode } = useThemeStore()
  const isDark = mode === 'dark'
  const containerRefs = useRef<Map<string, HTMLDivElement>>(new Map())
  const chartsRef = useRef<Map<string, IChartApi>>(new Map())
  const payloadRef = useRef<MetricsPayload | null>(null)

  // biome-ignore lint/correctness/useExhaustiveDependencies: charts are rebuilt only when payload or theme change; renderCharts re-created each render intentionally
  useEffect(() => {
    payloadRef.current = payload
    renderCharts()
  }, [payload, isDark])

  const refCallbacks = useMemo(() => {
    const cbs: Record<string, (el: HTMLDivElement | null) => void> = {}
    for (const key of ['price', 'ofi', 'obi']) {
      cbs[key] = (el: HTMLDivElement | null) => {
        if (el) containerRefs.current.set(key, el)
        else containerRefs.current.delete(key)
      }
    }
    return cbs
  }, [])

  const renderCharts = () => {
    // teardown old
    for (const [, inst] of chartsRef.current) inst.remove()
    chartsRef.current.clear()

    const data = payloadRef.current
    if (!data || data.status !== 'ok') return
    const s = data.series

    const makeOptions = (height: number) => ({
      width: containerRefs.current.get('price')?.offsetWidth || 600,
      height,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: isDark ? '#a6adbb' : '#333',
      },
      grid: {
        vertLines: { color: isDark ? 'rgba(166,173,187,0.08)' : 'rgba(0,0,0,0.08)' },
        horzLines: { color: isDark ? 'rgba(166,173,187,0.08)' : 'rgba(0,0,0,0.08)' },
      },
      rightPriceScale: { borderColor: 'rgba(166,173,187,0.2)' },
      timeScale: {
        borderColor: 'rgba(166,173,187,0.2)',
        timeVisible: true,
        secondsVisible: true,
        tickMarkFormatter: (t: number) => {
          const d = new Date((t + 19800) * 1000)
          const hh = String(d.getUTCHours()).padStart(2, '0')
          const mm = String(d.getUTCMinutes()).padStart(2, '0')
          return `${hh}:${mm}`
        },
      },
      crosshair: { mode: CrosshairMode.Normal },
    })

    const mkChart = (key: string, height: number) => {
      const el = containerRefs.current.get(key)
      if (!el) return
      const chart = createChart(el, makeOptions(height))
      chartsRef.current.set(key, chart)
      return chart
    }

    // Price pane: LTP + microprice overlay
    const pc = mkChart('price', 280)
    if (pc) {
      const ltp = pc.addSeries(LineSeries, { color: '#38bdf8', lineWidth: 2, title: 'LTP' })
      const micro = pc.addSeries(LineSeries, {
        color: '#a78bfa',
        lineWidth: 1,
        lineStyle: 3 as const,
        title: 'Microprice',
      })
      ltp.setData(seriesPoints(s, 'ltp'))
      micro.setData(seriesPoints(s, 'microprice'))
      pc.timeScale().fitContent()
    }

    // OFI histogram pane
    const oc = mkChart('ofi', 120)
    if (oc) {
      const pts = seriesPoints(s, 'ofi_1').map((p) => ({
        time: p.time,
        value: p.value,
        color: p.value >= 0 ? 'rgba(34,197,94,0.9)' : 'rgba(239,68,68,0.9)',
      }))
      const ofi = oc.addSeries(HistogramSeries, { priceFormat: { type: 'price' } })
      ofi.setData(pts)
      oc.timeScale().fitContent()
    }

    // OBI pane
    const ob = mkChart('obi', 120)
    if (ob) {
      const line = ob.addSeries(LineSeries, {
        color: '#facc15',
        lineWidth: 2,
        title: 'OBI LN',
        priceFormat: { type: 'custom', formatter: (v: number) => v.toFixed(3) },
      })
      const line1 = ob.addSeries(LineSeries, {
        color: '#34d399',
        lineWidth: 1,
        title: 'OBI L1',
        priceFormat: { type: 'custom', formatter: (v: number) => v.toFixed(3) },
      })
      line.setData(seriesPoints(s, 'obi_ln'))
      line1.setData(seriesPoints(s, 'obi_l1'))
      ob.timeScale().fitContent()
    }
  }

  return (
    <div className="space-y-3">
      <div ref={refCallbacks.price} className="w-full rounded-lg border border-border" />
      <div ref={refCallbacks.ofi} className="w-full rounded-lg border border-border" />
      <div ref={refCallbacks.obi} className="w-full rounded-lg border border-border" />
      {(!payload || payload.status !== 'ok') && (
        <p className="text-sm text-muted-foreground">No data for this session.</p>
      )}
    </div>
  )
}

// ─── Health ────────────────────────────────────────────────────────────────

function HealthTab({ symbol, exchange, day }: { symbol: string; exchange: string; day: string }) {
  const [data, setData] = useState<HealthData | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    setLoading(true)
    fetchHealth(symbol, exchange, day)
      .then(setData)
      .catch(() => setData(null))
      .finally(() => setLoading(false))
  }, [symbol, exchange, day])

  const maxPm = Math.max(...(data?.packets_per_min?.map((p) => p.packets) ?? [0]), 1)

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm flex items-center gap-2">
            <Activity className="w-4 h-4 text-cyan-400" />
            Packets per minute
          </CardTitle>
        </CardHeader>
        <CardContent>
          {loading && <p className="text-sm text-muted-foreground">Loading…</p>}
          {!loading && (!data || data.status !== 'ok') && (
            <p className="text-sm text-muted-foreground">No data.</p>
          )}
          {!loading && data && data.status === 'ok' && (
            <div className="flex items-end gap-[2px] h-24 overflow-x-auto">
              {data.packets_per_min.map((p, i) => (
                <div
                  key={i}
                  title={`${p.minute.slice(11, 16)} — ${p.packets} packets`}
                  className="min-w-[3px] bg-cyan-500/70 rounded-t hover:bg-cyan-400"
                  style={{ height: `${Math.max((p.packets / maxPm) * 100, 2)}%` }}
                />
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm flex items-center gap-2">
            <TriangleAlert className="w-4 h-4 text-amber-400" />
            Feed gaps (&gt;3s, the charts' blind spots)
          </CardTitle>
        </CardHeader>
        <CardContent>
          {data?.gaps?.length === 0 && (
            <p className="text-sm text-emerald-400">No gaps — feed was continuous.</p>
          )}
          {data?.gaps?.length ? (
            <div className="overflow-x-auto">
              <table className="w-full text-xs font-mono">
                <thead>
                  <tr className="text-muted-foreground text-left">
                    <th className="py-1 pr-3">From</th>
                    <th className="py-1 pr-3">To</th>
                    <th className="py-1 pr-3">Duration</th>
                  </tr>
                </thead>
                <tbody>
                  {data.gaps.map((g, i) => (
                    <tr key={i} className="border-t border-border/50">
                      <td className="py-1 pr-3">{g.from.slice(11, 19)}</td>
                      <td className="py-1 pr-3">{g.to.slice(11, 19)}</td>
                      <td className="py-1 text-rose-400">{g.duration_s}s</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            !loading &&
            data &&
            data.status === 'ok' && <p className="text-sm text-muted-foreground">No gaps found.</p>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

// ─── Lead-Lag ───────────────────────────────────────────────────────────────

function LeadLagTab({ symbol, exchange, day }: { symbol: string; exchange: string; day: string }) {
  const [data, setData] = useState<LeadLagData | null>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const scatterRef = useRef<{ x: number; y: number }[]>([])

  const drawScatter = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    const pts = scatterRef.current
    const W = canvas.width
    const H = canvas.height
    ctx.clearRect(0, 0, W, H)
    if (!pts.length) return
    const xs = pts.map((p) => p.x)
    const ys = pts.map((p) => p.y)
    const xmin = Math.min(...xs)
    const xmax = Math.max(...xs)
    const ymin = Math.min(...ys)
    const ymax = Math.max(...ys)
    const pad = 24
    const xr = (x: number) => pad + ((x - xmin) / (xmax - xmin || 1)) * (W - pad * 2)
    const yr = (y: number) => H - pad - ((y - ymin) / (ymax - ymin || 1)) * (H - pad * 2)

    ctx.strokeStyle = 'rgba(148,163,184,0.4)'
    ctx.beginPath()
    ctx.moveTo(pad, pad)
    ctx.lineTo(pad, H - pad)
    ctx.lineTo(W - pad, H - pad)
    ctx.stroke()

    ctx.fillStyle = 'rgba(56,189,248,0.55)'
    for (const p of pts) {
      ctx.fillRect(xr(p.x) - 1, yr(p.y) - 1, 2, 2)
    }
  }, [])

  useEffect(() => {
    fetchLeadLag(symbol, exchange, day)
      .then((d) => {
        setData(d)
        scatterRef.current = d.scatter ?? []
        drawScatter()
      })
      .catch(() => setData(null))
  }, [symbol, exchange, day, drawScatter])

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">
            OFI (1s bucket) → forward mid-return correlation — the kill gate
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-muted-foreground text-left">
                  <th className="py-1 pr-3">Horizon</th>
                  {['open', 'midday', 'close'].map((b) => (
                    <th key={b} className="py-1 pr-3">
                      {b === 'open' ? 'Open' : b === 'midday' ? 'Midday' : 'Close'}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {[1, 10, 30, 120].map((h) => (
                  <tr key={h} className="border-t border-border/50">
                    <td className="py-1.5 pr-3 font-mono">{h}s</td>
                    {['open', 'midday', 'close'].map((b) => {
                      const cell = data?.results?.find((r) => r.horizon === h && r.bucket === b)
                      const v = cell?.pearson
                      return (
                        <td key={b} className="py-1.5 pr-3 font-mono">
                          {v == null ? (
                            '—'
                          ) : (
                            <span
                              className={cn(
                                Math.abs(v) > 0.2 && 'font-bold',
                                v > 0.2 ? 'text-emerald-400' : v < -0.2 ? 'text-rose-400' : ''
                              )}
                            >
                              {v.toFixed(3)}
                              <span className="text-muted-foreground text-[10px] ml-1">
                                ({cell?.n})
                              </span>
                            </span>
                          )}
                        </td>
                      )
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-[11px] text-muted-foreground mt-2">
            Pearson on 1s OFI buckets vs mid return over the forward horizon. If every cell is
            noise, the recorded depth isn't predictive that day.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">Scatter — OFI vs next-10s mid return</CardTitle>
        </CardHeader>
        <CardContent>
          <canvas
            ref={canvasRef}
            width={700}
            height={300}
            className="w-full rounded border border-border"
          />
        </CardContent>
      </Card>
    </div>
  )
}

// ─── Replay ─────────────────────────────────────────────────────────────────

function ReplayTab({ payload }: { payload: MetricsPayload | null }) {
  const [idx, setIdx] = useState(0)
  const [speed, setSpeed] = useState(1)
  const [playing, setPlaying] = useState(false)
  const timerRef = useRef<number | null>(null)

  const n = payload?.rows ?? 0

  // biome-ignore lint/correctness/useExhaustiveDependencies: payload is an intentional reset trigger — the body only calls setters, but must re-fire when a new session loads
  useEffect(() => {
    setIdx(0)
    setPlaying(false)
  }, [payload])

  useEffect(() => {
    if (!playing || !n) return
    timerRef.current = window.setInterval(() => {
      setIdx((i) => {
        if (i >= n - 1) {
          setPlaying(false)
          return i
        }
        return i + 1
      })
    }, 1000 / speed)
    return () => {
      if (timerRef.current) window.clearInterval(timerRef.current)
    }
  }, [playing, speed, n])

  const row = payload?.ladder?.[idx]
  const time = payload?.series?.time?.[idx]
  const ltp = payload?.series?.ltp?.[idx]
  const ofi = payload?.series?.ofi_1?.[idx]
  const cvd = payload?.series?.cvd?.[idx]
  const obi = payload?.series?.obi_ln?.[idx]

  if (!payload || payload.status !== 'ok') {
    return <p className="text-sm text-muted-foreground">No data for this session.</p>
  }

  const maxQty = Math.max(
    ...(row?.bids ?? []).map((b) => b.quantity ?? 0),
    ...(row?.asks ?? []).map((a) => a.quantity ?? 0),
    1
  )

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 flex-wrap">
        <Button variant="outline" size="sm" onClick={() => setPlaying((p) => !p)} disabled={!n}>
          <Play className="w-3.5 h-3.5 mr-1.5" />
          {playing ? 'Pause' : 'Play'}
        </Button>
        {[1, 2, 10].map((s) => (
          <Button
            key={s}
            variant={speed === s ? 'default' : 'outline'}
            size="sm"
            onClick={() => setSpeed(s)}
            disabled={!n}
          >
            {s}x
          </Button>
        ))}
        <div className="flex-1" />
        <span className="text-xs font-mono text-muted-foreground">
          {idx + 1} / {n}
        </span>
      </div>

      <input
        type="range"
        min={0}
        max={Math.max(n - 1, 0)}
        value={idx}
        onChange={(e) => setIdx(Number(e.target.value))}
        className="w-full"
        disabled={!n}
      />

      <div className="grid grid-cols-4 gap-3">
        <Card>
          <CardContent className="pt-4">
            <div className="text-[10px] uppercase text-muted-foreground">Time</div>
            <div className="font-mono text-lg">{time ? fmtTime(time as number) : '—'}</div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4">
            <div className="text-[10px] uppercase text-muted-foreground">LTP</div>
            <div className="font-mono text-lg text-cyan-400">{fmtNum(ltp)}</div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4">
            <div className="text-[10px] uppercase text-muted-foreground">OFI</div>
            <div className="font-mono text-lg">{fmtNum(ofi)}</div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4">
            <div className="text-[10px] uppercase text-muted-foreground">OBI LN</div>
            <div className="font-mono text-lg text-amber-400">{fmtNum(obi, 3)}</div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardContent className="pt-4">
          <div className="text-sm font-bold mb-2 flex items-center gap-2">
            <Eye className="w-4 h-4 text-violet-400" />
            Ladder · cumulative CVD {fmtQty(cvd)}
          </div>
          <div className="grid grid-cols-2 gap-1 text-xs font-mono">
            <div className="space-y-0.5">
              {row?.bids?.map((b, i) => (
                <div
                  key={i}
                  className="relative flex items-center px-1 py-0.5 rounded overflow-hidden"
                >
                  <div
                    className="absolute right-0 top-0 bottom-0 bg-emerald-500/10"
                    style={{ width: `${Math.min(((b.quantity ?? 0) / maxQty) * 100, 100)}%` }}
                  />
                  <span className="relative z-10 flex-1 text-emerald-400">
                    {fmtQty(b.quantity)}
                  </span>
                  <span className="relative z-10 text-foreground">{fmtNum(b.price)}</span>
                  <span className="relative z-10 text-muted-foreground text-[10px] ml-1 w-6 text-right">
                    {b.orders ?? ''}
                  </span>
                </div>
              ))}
            </div>
            <div className="space-y-0.5">
              {row?.asks?.map((a, i) => (
                <div
                  key={i}
                  className="relative flex items-center px-1 py-0.5 rounded overflow-hidden"
                >
                  <div
                    className="absolute left-0 top-0 bottom-0 bg-rose-500/10"
                    style={{ width: `${Math.min(((a.quantity ?? 0) / maxQty) * 100, 100)}%` }}
                  />
                  <span className="relative z-10 flex-1 text-foreground">{fmtNum(a.price)}</span>
                  <span className="relative z-10 text-rose-400">{fmtQty(a.quantity)}</span>
                  <span className="relative z-10 text-muted-foreground text-[10px] ml-1 w-6 text-right">
                    {a.orders ?? ''}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}

// ─── Walls ──────────────────────────────────────────────────────────────────

function WallsTab({ symbol, exchange, day }: { symbol: string; exchange: string; day: string }) {
  const [data, setData] = useState<WallsData | null>(null)

  useEffect(() => {
    fetchWalls(symbol, exchange, day)
      .then(setData)
      .catch(() => setData(null))
  }, [symbol, exchange, day])

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm flex items-center gap-2">
          <Wallet className="w-4 h-4 text-amber-400" />
          Support / resistance walls — top price levels by time-weighted resting size
        </CardTitle>
      </CardHeader>
      <CardContent>
        {!data || data.status !== 'ok' ? (
          <p className="text-sm text-muted-foreground">No data.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-muted-foreground text-left">
                  <th className="py-1 pr-3">Price</th>
                  <th className="py-1 pr-3">Time-wtd qty</th>
                  <th className="py-1 pr-3">Avg order size</th>
                  <th className="py-1 pr-3">Samples</th>
                </tr>
              </thead>
              <tbody>
                {data.walls.map((w, i) => (
                  <tr key={i} className="border-t border-border/50">
                    <td className="py-1.5 pr-3 font-mono font-bold">{fmtNum(w.price)}</td>
                    <td className="py-1.5 pr-3 font-mono">{fmtQty(w.time_weighted_qty)}</td>
                    <td className="py-1.5 pr-3 font-mono">
                      {w.avg_order_size == null ? '—' : fmtQty(w.avg_order_size)}
                    </td>
                    <td className="py-1.5 pr-3 font-mono text-muted-foreground">{w.samples}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

// ─── Main analytics tab ─────────────────────────────────────────────────────

const TABS = [
  { id: 'health', label: 'Health', icon: Activity },
  { id: 'charts', label: 'Charts', icon: BarChart3 },
  { id: 'leadlag', label: 'Lead-Lag', icon: TrendingUp },
  { id: 'replay', label: 'Replay', icon: Eye },
  { id: 'heatmap', label: 'Heatmap', icon: Flame },
  { id: 'walls', label: 'Walls', icon: Wallet },
] as const

type TabId = (typeof TABS)[number]['id']

export default function DepthAnalytics() {
  const [sessions, setSessions] = useState<SessionEntry[]>([])
  const [sessionsError, setSessionsError] = useState<string | null>(null)
  const [symbol, setSymbol] = useState('')
  const [exchange, setExchange] = useState('NSE')
  const [day, setDay] = useState('')
  const [activeTab, setActiveTab] = useState<TabId>('health')
  const [metrics, setMetrics] = useState<MetricsPayload | null>(null)
  const [loading, setLoading] = useState(false)

  const loadSessions = useCallback(async () => {
    try {
      const all = await fetchSessions(symbol || undefined, exchange)
      setSessionsError(null)
      setSessions(all)
      if (all.length && !all.some((s) => s.day === day)) {
        setDay(all[0].day)
      }
    } catch {
      setSessions([])
      setSessionsError(
        'Could not load recorded sessions from the backend — is it running current code?'
      )
    }
  }, [symbol, exchange, day])

  useEffect(() => {
    loadSessions()
  }, [loadSessions])

  const daysForSymbol = useMemo(() => {
    const filtered = sessions.filter(
      (s) => (symbol ? s.symbol === symbol : true) && s.exchange === exchange
    )
    const days = [...new Set(filtered.map((s) => s.day))].sort().reverse()
    return {
      symbols: [...new Set(sessions.filter((s) => s.exchange === exchange).map((s) => s.symbol))],
      days,
    }
  }, [sessions, symbol, exchange])

  useEffect(() => {
    if (!symbol) return
    setLoading(true)
    fetchMetrics(symbol, exchange, day)
      .then(setMetrics)
      .catch(() => setMetrics(null))
      .finally(() => setLoading(false))
  }, [symbol, exchange, day])

  return (
    <div className="space-y-4">
      {/* Session picker */}
      <Card>
        <CardContent className="pt-4">
          <div className="flex flex-wrap gap-3 items-end">
            <div>
              <label className="text-[10px] uppercase tracking-wider text-muted-foreground">
                Symbol
              </label>
              <Input
                className="w-36 font-mono uppercase"
                placeholder="RELIANCE"
                value={symbol}
                onChange={(e) => setSymbol(e.target.value.toUpperCase())}
              />
            </div>
            <div>
              <label className="text-[10px] uppercase tracking-wider text-muted-foreground">
                Exchange
              </label>
              <Select value={exchange} onValueChange={setExchange}>
                <SelectTrigger className="w-24">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {['NSE', 'BSE', 'NFO', 'BFO', 'MCX', 'CDS'].map((ex) => (
                    <SelectItem key={ex} value={ex}>
                      {ex}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div>
              <label className="text-[10px] uppercase tracking-wider text-muted-foreground">
                Session day
              </label>
              <Select value={day} onValueChange={setDay}>
                <SelectTrigger className="w-40">
                  <SelectValue placeholder="Pick a day" />
                </SelectTrigger>
                <SelectContent>
                  {daysForSymbol.days.map((d) => (
                    <SelectItem key={d} value={d}>
                      {d}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <Button variant="outline" size="sm" onClick={loadSessions}>
              <RefreshCw className="w-3.5 h-3.5 mr-1.5" />
              Refresh
            </Button>
          </div>

          {sessionsError && (
            <p className="w-full text-xs text-rose-400 mt-2 flex items-center gap-2">
              <TriangleAlert className="w-3.5 h-3.5" />
              {sessionsError}
            </p>
          )}
          {!sessionsError && sessions.length > 0 && daysForSymbol.days.length === 0 && (
            <p className="w-full text-xs text-muted-foreground mt-2">
              No recorded sessions for {symbol || 'any symbol'}/{exchange}. Start a recorder
              (Recorders tab) or seed the depth DB for this day.
            </p>
          )}

          {symbol && (
            <div className="flex items-center gap-2 mt-3 text-xs text-muted-foreground">
              <Database className="w-3.5 h-3.5" />
              {metrics?.rows != null && (
                <span>
                  {metrics.rows.toLocaleString('en-IN')} resampled rows
                  {metrics.has_orders && ' · with order counts'}
                </span>
              )}
              {metrics?.first && (
                <span>
                  · {metrics.first.slice(11, 16)} → {metrics.last.slice(11, 16)} IST
                </span>
              )}
              {loading && <Badge variant="outline">loading…</Badge>}
            </div>
          )}
        </CardContent>
      </Card>

      <Tabs value={activeTab} onValueChange={(v) => setActiveTab(v as TabId)}>
        <TabsList className="flex-wrap h-auto">
          {TABS.map((t) => (
            <TabsTrigger key={t.id} value={t.id} className="gap-1.5">
              <t.icon className="w-3.5 h-3.5" />
              {t.label}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      <div className="mt-4">
        {activeTab === 'health' && <HealthTab symbol={symbol} exchange={exchange} day={day} />}
        {activeTab === 'charts' && <ChartsTab payload={metrics} />}
        {activeTab === 'leadlag' && <LeadLagTab symbol={symbol} exchange={exchange} day={day} />}
        {activeTab === 'replay' && <ReplayTab payload={metrics} />}
        {activeTab === 'heatmap' && <HeatmapTab symbol={symbol} exchange={exchange} day={day} />}
        {activeTab === 'walls' && <WallsTab symbol={symbol} exchange={exchange} day={day} />}
      </div>
    </div>
  )
}
