import { ChevronDown, LayoutGrid, Play, History, Eye, Layers, Database } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router'
import { Navbar } from '@/components/layout/Navbar'
import { ChartPane } from '@/components/trading/ChartPane'
import { DrawingRail } from '@/components/trading/DrawingRail'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import type { DrawStats, TradingTerminal } from '@/lib/trading/terminal'
import { cn } from '@/lib/utils'
import { showToast } from '@/utils/toast'

const NO_DRAW: DrawStats = {
  count: 0,
  canUndo: false,
  canRedo: false,
  hasSelection: false,
  magnet: false,
  tool: null,
  shortcuts: {},
}

interface LayoutPreset {
  id: string
  label: string
  cols: string
  rows: string
  areas: string
  cells: string[]
}

const LAYOUTS: LayoutPreset[] = [
  { id: 'single', label: 'Single', cols: '1fr', rows: '1fr', areas: '"a"', cells: ['a'] },
  {
    id: 'cols2',
    label: '2 columns',
    cols: '1fr 1fr',
    rows: '1fr',
    areas: '"a b"',
    cells: ['a', 'b'],
  },
  {
    id: 'rows2',
    label: '2 rows',
    cols: '1fr',
    rows: '1fr 1fr',
    areas: '"a" "b"',
    cells: ['a', 'b'],
  },
  {
    id: 'oneTwo',
    label: '1 + 2',
    cols: '1.4fr 1fr',
    rows: '1fr 1fr',
    areas: '"a b" "a c"',
    cells: ['a', 'b', 'c'],
  },
  {
    id: 'grid4',
    label: '2 × 2',
    cols: '1fr 1fr',
    rows: '1fr 1fr',
    areas: '"a b" "c d"',
    cells: ['a', 'b', 'c', 'd'],
  },
  {
    id: 'grid6',
    label: '3 × 2',
    cols: '1fr 1fr 1fr',
    rows: '1fr 1fr',
    areas: '"a b c" "d e f"',
    cells: ['a', 'b', 'c', 'd', 'e', 'f'],
  },
]

const LAYOUT_KEY = 'oa-replay-layout'

function LayoutIcon({ preset, className }: { preset: LayoutPreset; className?: string }) {
  return (
    <span
      className={cn('grid h-4 w-4 gap-px', className)}
      style={{
        gridTemplateColumns: preset.cols,
        gridTemplateRows: preset.rows,
        gridTemplateAreas: preset.areas,
      }}
      aria-hidden="true"
    >
      {preset.cells.map((c) => (
        <span key={c} style={{ gridArea: c }} className="rounded-[1px] bg-current" />
      ))}
    </span>
  )
}

interface ClockState {
  current_time: string
  start_time: string
  end_time: string
  is_playing: boolean
  speed: number
  session_date: string
  watchlist: string[]
}

interface SymbolState {
  timestamp?: string
  ltp?: number
  volume?: number
  oi?: number
  bid_1?: number
  bid_qty_1?: number
  bid_2?: number
  bid_qty_2?: number
  bid_3?: number
  bid_qty_3?: number
  bid_4?: number
  bid_qty_4?: number
  bid_5?: number
  bid_qty_5?: number
  ask_1?: number
  ask_qty_1?: number
  ask_2?: number
  ask_qty_2?: number
  ask_3?: number
  ask_qty_3?: number
  ask_4?: number
  ask_qty_4?: number
  ask_5?: number
  ask_qty_5?: number
}

interface FillResult {
  status: string
  fill_price: number
  filled_qty: number
  unfilled_qty?: number
  slippage?: number
  reason?: string
}

interface DemoTrade {
  id: string
  symbol: string
  action: string
  quantity: number
  fill_price: number
  timestamp: string
  current_ltp?: number
  pnl?: number
}

interface AvailableEntry {
  symbol: string
  date: string
  row_count: number
}

async function fetchCSRF(): Promise<string> {
  const r = await fetch('/auth/csrf-token', { credentials: 'include' })
  return (await r.json()).csrf_token
}

async function apiPost(path: string, body: object): Promise<Response> {
  const csrf = await fetchCSRF()
  return fetch(path, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
    body: JSON.stringify(body),
  })
}

export default function Replay() {
  const [layoutId, setLayoutId] = useState(() => {
    const saved = localStorage.getItem(LAYOUT_KEY)
    return LAYOUTS.some((l) => l.id === saved) ? (saved as string) : 'single'
  })
  const [apiKey, setApiKey] = useState<string | null>(null)
  const [wsUrl, setWsUrl] = useState<string | null>(null)
  const [noApiKey, setNoApiKey] = useState(false)

  /* Drawing rail state */
  const [tool, setTool] = useState<string | null>(null)
  const [magnet, setMagnet] = useState(false)
  const [showRail, setShowRail] = useState(true)
  const [stats, setStats] = useState<DrawStats>(NO_DRAW)
  const activeRef = useRef<TradingTerminal | null>(null)

  const focusPane = useCallback((t: TradingTerminal | null) => {
    activeRef.current = t
    if (t) setStats(t.drawStats())
  }, [])
  const railStats: DrawStats = { ...stats, tool, magnet }

  const armByShortcut = useCallback((e: KeyboardEvent) => {
    const t = activeRef.current
    if (!t || !t.armByShortcut(e)) return false
    setTool(t.drawStats().tool)
    setStats(t.drawStats())
    return true
  }, [])

  const act = (fn: (t: TradingTerminal) => void) => {
    const t = activeRef.current
    if (!t) return
    fn(t)
    setStats(t.drawStats())
  }

  useEffect(() => {
    localStorage.setItem(LAYOUT_KEY, layoutId)
  }, [layoutId])

  // Fetch API key + WS URL
  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        const [keyRes, cfgRes] = await Promise.all([
          fetch('/api/websocket/apikey').then((r) => r.json()),
          fetch('/api/websocket/config').then((r) => r.json()),
        ])
        if (!alive) return
        if (keyRes.status !== 'success') {
          setNoApiKey(true)
          return
        }
        setApiKey(keyRes.api_key)
        setWsUrl(cfgRes.websocket_url || 'ws://127.0.0.1:8765')
      } catch {
        if (alive) setNoApiKey(true)
      }
    })()
    return () => {
      alive = false
    }
  }, [])

  // Replay clock state & controls
  const [clock, setClock] = useState<ClockState | null>(null)
  const [sessionDate, setSessionDate] = useState('2026-08-10')
  const [startTime, setStartTime] = useState('09:15:00')
  const [endTime, setEndTime] = useState('15:30:00')
  const [watchlistInput, setWatchlistInput] = useState('NIFTY, RELIANCE')
  const [autoPauseOnFill] = useState(true)

  // Floating Widgets Toggles & Drag State
  const [showDepthDOM, setShowDepthDOM] = useState(false)
  const [showScalperWidget, setShowScalperWidget] = useState(true)
  const [depthPos, setDepthPos] = useState({ x: 16, y: 70 })
  const [isDraggingDepth, setIsDraggingDepth] = useState(false)
  const dragRef = useRef({ startX: 0, startY: 0, initialX: 16, initialY: 70 })

  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (!isDraggingDepth) return
      const dx = e.clientX - dragRef.current.startX
      const dy = e.clientY - dragRef.current.startY
      setDepthPos({
        x: Math.max(0, dragRef.current.initialX + dx),
        y: Math.max(0, dragRef.current.initialY + dy),
      })
    }

    const handleMouseUp = () => {
      setIsDraggingDepth(false)
    }

    if (isDraggingDepth) {
      window.addEventListener('mousemove', handleMouseMove)
      window.addEventListener('mouseup', handleMouseUp)
    }
    return () => {
      window.removeEventListener('mousemove', handleMouseMove)
      window.removeEventListener('mouseup', handleMouseUp)
    }
  }, [isDraggingDepth])

  // Available Data
  const [availableData, setAvailableData] = useState<AvailableEntry[] | null>(null)

  const [selectedSymbol, setSelectedSymbol] = useState('NIFTY')
  const [symbolState, setSymbolState] = useState<SymbolState | null>(null)

  // Demo Order Simulation & PnL
  const [simQty, setSimQty] = useState('1')
  const [demoTrades, setDemoTrades] = useState<DemoTrade[]>([])
  const totalPnL = demoTrades.reduce((sum, t) => sum + (t.pnl || 0), 0)

  const fetchStatus = useCallback(async () => {
    try {
      const res = await fetch('/api/replay/status', { credentials: 'include' })
      if (res.ok) {
        const data = await res.json()
        setClock(data)
      }
    } catch (err) {
      console.error('Failed to fetch replay status', err)
    }
  }, [])

  const fetchSymbolState = useCallback(async () => {
    if (!selectedSymbol) return
    try {
      const res = await fetch(`/api/replay/state?symbol=${encodeURIComponent(selectedSymbol)}`, {
        credentials: 'include',
      })
      if (res.ok) {
        const data = await res.json()
        setSymbolState(data.state)
        const ltp = data.state?.ltp
        if (ltp != null) {
          setDemoTrades((prev) =>
            prev.map((trade) => {
              if (trade.symbol !== selectedSymbol) return trade
              const mult = trade.action === 'BUY' ? 1 : -1
              const pnl = (ltp - trade.fill_price) * trade.quantity * mult
              return { ...trade, current_ltp: ltp, pnl }
            })
          )
        }
      }
    } catch (err) {
      console.error('Failed to fetch symbol state', err)
    }
  }, [selectedSymbol])

  const fetchAvailable = useCallback(async () => {
    try {
      const res = await fetch('/api/replay/available', { credentials: 'include' })
      if (res.ok) {
        const data = await res.json()
        setAvailableData(data.data || [])
      }
    } catch (err) {
      console.error('Failed to fetch available replay data', err)
    }
  }, [])

  useEffect(() => {
    fetchStatus()
    fetchAvailable()
  }, [fetchStatus, fetchAvailable])

  useEffect(() => {
    fetchSymbolState()
    const interval = setInterval(() => {
      fetchStatus()
      fetchSymbolState()
    }, 1000)
    return () => clearInterval(interval)
  }, [fetchStatus, fetchSymbolState])

  useEffect(() => {
    const interval = setInterval(() => {
      fetchAvailable()
    }, 5000)
    return () => clearInterval(interval)
  }, [fetchAvailable])

  const handleUseAvailable = (entry: AvailableEntry) => {
    setSessionDate(entry.date)
    setWatchlistInput((prev) => {
      const list = prev
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean)
      if (list.includes(entry.symbol)) return prev
      return [...list, entry.symbol].join(', ')
    })
    showToast.info(`Added ${entry.symbol} (${entry.date}) to watchlist — configure session to apply`)
  }

  const handleConfigureSession = async () => {
    const list = watchlistInput
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean)
    try {
      const res = await apiPost('/api/replay/session', {
        date: sessionDate,
        start_time: startTime,
        end_time: endTime,
        watchlist: list,
      })
      if (res.ok) {
        showToast.success('Replay session configured successfully')
        fetchStatus()
      } else {
        showToast.error('Failed to configure replay session')
      }
    } catch {
      showToast.error('Error configuring session')
    }
  }

  const handleControl = async (action: string, payload: object = {}) => {
    try {
      const res = await apiPost('/api/replay/control', { action, ...payload })
      if (res.ok) {
        fetchStatus()
        fetchSymbolState()
      }
    } catch {
      showToast.error('Control action failed')
    }
  }

  const handleSimulateOrderCustom = async (action: string = 'BUY') => {
    if (!selectedSymbol) return
    try {
      const res = await apiPost('/api/replay/simulate_order', {
        symbol: selectedSymbol,
        action,
        quantity: parseFloat(simQty) || 1,
        price_type: 'MARKET',
        limit_price: 0.0,
      })
      if (res.ok) {
        const data: FillResult = await res.json()
        if (data.status === 'filled') {
          const newTrade: DemoTrade = {
            id: Math.random().toString(36).substring(2, 9),
            symbol: selectedSymbol,
            action,
            quantity: data.filled_qty,
            fill_price: data.fill_price,
            timestamp: clock?.current_time || new Date().toISOString(),
            current_ltp: data.fill_price,
            pnl: 0,
          }
          setDemoTrades((prev) => [newTrade, ...prev])
          showToast.success(`Demo ${action} filled @ ${data.fill_price}`)
          if (autoPauseOnFill && clock?.is_playing) {
            handleControl('pause')
          }
        } else {
          showToast.info(`Order simulation: ${data.status}`)
        }
      }
    } catch {
      showToast.error('Simulation failed')
    }
  }

  // Calculate timeline progress
  const getScrubberProgress = () => {
    if (!clock) return 0
    try {
      const start = new Date(clock.start_time).getTime()
      const end = new Date(clock.end_time).getTime()
      const curr = new Date(clock.current_time).getTime()
      if (end <= start) return 0
      const pct = ((curr - start) / (end - start)) * 100
      return Math.min(100, Math.max(0, pct))
    } catch {
      return 0
    }
  }

  const handleScrubberChange = (targetPct: number) => {
    if (!clock) return
    try {
      const start = new Date(clock.start_time).getTime()
      const end = new Date(clock.end_time).getTime()
      const targetTime = new Date(start + (targetPct / 100) * (end - start)).toISOString()
      handleControl('seek', { timestamp: targetTime })
    } catch {
      showToast.error('Invalid scrubber position')
    }
  }

  const handleQuickJump = (timeStr: string) => {
    if (!sessionDate) return
    const targetIso = `${sessionDate}T${timeStr}+05:30`
    handleControl('seek', { timestamp: targetIso })
  }

  const layout = LAYOUTS.find((l) => l.id === layoutId) ?? LAYOUTS[0]

  return (
    <>
      <Navbar />
      <div className="flex flex-1 flex-col overflow-hidden">
        {/* Replay Control & Session Toolbar */}
        <div className="border-b bg-background/95 px-4 py-2 space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-4">
            <div className="flex items-center gap-3">
              {clock && (
                <div className="flex items-center gap-2">
                  <Badge variant={clock.is_playing ? 'default' : 'secondary'} className="gap-1.5">
                    <span className={cn('h-2 w-2 rounded-full', clock.is_playing ? 'bg-emerald-400 animate-pulse' : 'bg-amber-400')} />
                    {clock.is_playing ? 'Playing' : 'Paused'}
                  </Badge>
                  <span className="font-mono text-xs font-semibold text-primary">
                    {clock.current_time}
                  </span>
                  <Badge variant="outline" className="text-xs">{clock.speed}x</Badge>
                </div>
              )}
            </div>

            {/* Playback Controls */}
            {clock && (
              <div className="flex items-center gap-1.5">
                {clock.is_playing ? (
                  <Button variant="outline" size="sm" className="h-7 text-xs" onClick={() => handleControl('pause')}>
                    Pause
                  </Button>
                ) : (
                  <Button size="sm" className="h-7 text-xs bg-emerald-600 hover:bg-emerald-700" onClick={() => handleControl('play')}>
                    <Play className="h-3 w-3 mr-1" /> Play
                  </Button>
                )}
                <Button variant="outline" size="sm" className="h-7 text-xs" onClick={() => handleControl('step', { seconds: 1 })}>
                  +1s
                </Button>
                <Button variant="outline" size="sm" className="h-7 text-xs" onClick={() => handleControl('step', { seconds: 5 })}>
                  +5s
                </Button>
                <div className="h-4 w-px bg-border mx-1" />
                {[0.5, 1, 2, 5, 10, 20, 60].map((spd) => (
                  <Button
                    key={spd}
                    size="sm"
                    variant={clock.speed === spd ? 'default' : 'outline'}
                    className="h-7 px-2 text-[10px]"
                    onClick={() => handleControl('speed', { speed: spd })}
                  >
                    {spd}x
                  </Button>
                ))}
              </div>
            )}

            {/* Toolbar Action Toggles & Layout selector */}
            <div className="flex items-center gap-2">
              <Button
                variant={showDepthDOM ? 'default' : 'outline'}
                size="sm"
                className="h-8 gap-1.5 text-xs"
                onClick={() => setShowDepthDOM((v) => !v)}
              >
                <Eye className="h-3.5 w-3.5" /> Market Depth
              </Button>

              <Button
                variant={showScalperWidget ? 'default' : 'outline'}
                size="sm"
                className="h-8 gap-1.5 text-xs"
                onClick={() => setShowScalperWidget((v) => !v)}
              >
                <Layers className="h-3.5 w-3.5" /> Scalper Bar
              </Button>

              {/* Tape Archives & Admin Redirect Dialog */}
              <Dialog>
                <DialogTrigger asChild>
                  <Button variant="outline" size="sm" className="h-8 gap-1.5 text-xs">
                    <Database className="h-3.5 w-3.5" /> Tapes
                  </Button>
                </DialogTrigger>
                <DialogContent className="max-w-md">
                  <DialogHeader>
                    <DialogTitle className="flex items-center gap-2">
                      <History className="h-5 w-5" /> Tape Archives & Recording
                    </DialogTitle>
                  </DialogHeader>
                  <div className="space-y-4 py-2 text-xs">
                    <div className="rounded-lg border p-3 bg-muted/30 space-y-2">
                      <div className="font-semibold">Automatic Recording Active</div>
                      <p className="text-muted-foreground text-[11px]">
                        All watchlist symbols are automatically recorded second-by-second when a session is configured. To change recording settings or targets, visit the Admin panel.
                      </p>
                      <Button asChild size="sm" className="w-full h-7 text-xs">
                        <Link to="/admin/replay">Open Recording Settings (Admin)</Link>
                      </Button>
                    </div>

                    <div className="border-t pt-3 space-y-2">
                      <div className="font-semibold text-muted-foreground">Available Recorded Tapes:</div>
                      {availableData && availableData.length > 0 ? (
                        <div className="max-h-48 overflow-y-auto space-y-1">
                          {availableData.map((entry) => (
                            <button
                              key={`${entry.symbol}:${entry.date}`}
                              type="button"
                              onClick={() => handleUseAvailable(entry)}
                              className="w-full flex items-center justify-between text-xs font-mono border rounded px-2 py-1.5 hover:bg-muted/40 text-left"
                            >
                              <span>{entry.symbol}</span>
                              <span className="text-muted-foreground">{entry.date}</span>
                              <span className="text-muted-foreground">{entry.row_count} rows</span>
                            </button>
                          ))}
                        </div>
                      ) : (
                        <p className="text-muted-foreground">No recorded tape data available.</p>
                      )}
                    </div>
                  </div>
                </DialogContent>
              </Dialog>

              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button variant="outline" size="sm" className="h-8 gap-1.5">
                    <LayoutGrid className="h-4 w-4" />
                    <span className="text-xs font-medium">Layout</span>
                    <ChevronDown className="h-3.5 w-3.5 opacity-60" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start" className="w-56">
                  <div className="grid grid-cols-4 gap-1 p-1">
                    {LAYOUTS.map((l) => (
                      <DropdownMenuItem
                        key={l.id}
                        onSelect={() => setLayoutId(l.id)}
                        title={l.label}
                        className={cn(
                          'flex aspect-square flex-col items-center justify-center gap-1 rounded border',
                          l.id === layoutId
                            ? 'border-primary bg-primary/10 text-primary'
                            : 'text-muted-foreground'
                        )}
                      >
                        <LayoutIcon preset={l} />
                        <span className="text-[9px] font-medium">{l.cells.length}</span>
                      </DropdownMenuItem>
                    ))}
                  </div>
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          </div>

          {/* Interactive Session Timeline Scrubber & Quick Jumps */}
          {clock && (
            <div className="flex flex-wrap items-center gap-3 pt-1 border-t text-xs">
              <span className="text-[10px] text-muted-foreground font-mono">09:15 AM</span>
              <input
                type="range"
                min="0"
                max="100"
                step="0.1"
                value={getScrubberProgress()}
                onChange={(e) => handleScrubberChange(parseFloat(e.target.value))}
                className="flex-1 h-1.5 accent-primary bg-muted rounded cursor-pointer"
              />
              <span className="text-[10px] text-muted-foreground font-mono">03:30 PM</span>

              {/* Quick Jump Buttons */}
              <div className="flex items-center gap-1 pl-2">
                <span className="text-[10px] text-muted-foreground mr-1">Jump:</span>
                <Button size="sm" variant="ghost" className="h-6 px-1.5 text-[10px]" onClick={() => handleQuickJump('09:15:00')}>
                  09:15
                </Button>
                <Button size="sm" variant="ghost" className="h-6 px-1.5 text-[10px]" onClick={() => handleQuickJump('10:30:00')}>
                  10:30
                </Button>
                <Button size="sm" variant="ghost" className="h-6 px-1.5 text-[10px]" onClick={() => handleQuickJump('12:00:00')}>
                  12:00
                </Button>
                <Button size="sm" variant="ghost" className="h-6 px-1.5 text-[10px]" onClick={() => handleQuickJump('14:30:00')}>
                  14:30
                </Button>
                <Button size="sm" variant="ghost" className="h-6 px-1.5 text-[10px]" onClick={() => handleQuickJump('15:30:00')}>
                  15:30
                </Button>
              </div>
            </div>
          )}

          {/* Session Configuration Bar */}
          <div className="flex flex-wrap items-center gap-2 pt-1 border-t text-xs">
            <div className="flex items-center gap-1.5">
              <span className="text-muted-foreground">Date:</span>
              <Input className="h-7 w-28 text-xs font-mono" value={sessionDate} onChange={(e) => setSessionDate(e.target.value)} />
            </div>
            <div className="flex items-center gap-1.5">
              <span className="text-muted-foreground">Start:</span>
              <Input className="h-7 w-20 text-xs font-mono" value={startTime} onChange={(e) => setStartTime(e.target.value)} />
            </div>
            <div className="flex items-center gap-1.5">
              <span className="text-muted-foreground">End:</span>
              <Input className="h-7 w-20 text-xs font-mono" value={endTime} onChange={(e) => setEndTime(e.target.value)} />
            </div>
            <div className="flex items-center gap-1.5 flex-1 min-w-[200px]">
              <span className="text-muted-foreground">Watchlist:</span>
              <Input className="h-7 text-xs font-mono" value={watchlistInput} onChange={(e) => setWatchlistInput(e.target.value)} />
            </div>
            <Button size="sm" className="h-7 text-xs" onClick={handleConfigureSession}>
              Apply Session
            </Button>
          </div>
        </div>

        {/* Main Terminal Area with Drawing Rail + Maximized Chart Grid + Floating Widgets */}
        <main className="flex min-h-0 flex-1 relative">
          {showRail && apiKey && wsUrl && (
            <DrawingRail
              stats={railStats}
              onPick={(id) => setTool(id)}
              onUndo={() => act((t) => t.undoDraw())}
              onRedo={() => act((t) => t.redoDraw())}
              onRemove={(all) => act((t) => t.removeDrawings(all))}
              onMagnet={(v) => setMagnet(v)}
              onShortcut={armByShortcut}
            />
          )}

          {/* Maximized Chart Grid Area */}
          <div className="flex flex-col min-h-0 flex-1 relative">
            {noApiKey ? (
              <div className="flex h-full flex-col items-center justify-center gap-2 text-center p-8">
                <p className="text-sm text-muted-foreground">No API key found for charting terminal.</p>
                <a href="/apikey" className="text-sm font-medium text-primary underline">
                  Generate an API key
                </a>
              </div>
            ) : apiKey && wsUrl ? (
              <div
                className="grid h-full min-h-0 gap-2 p-2"
                style={{
                  gridTemplateColumns: layout.cols,
                  gridTemplateRows: layout.rows,
                  gridTemplateAreas: layout.areas,
                }}
              >
                {layout.cells.map((cell, i) => (
                  <ChartPane
                    key={`p${i}`}
                    paneId={`p${i}`}
                    apiKey={apiKey}
                    wsUrl={wsUrl}
                    style={{ gridArea: cell }}
                    sharedTool={tool}
                    sharedMagnet={magnet}
                    onFocusPane={focusPane}
                    onDrawStats={setStats}
                    onToggleRail={() => setShowRail((v) => !v)}
                    railVisible={showRail}
                  />
                ))}
              </div>
            ) : (
              <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
                Loading trading terminal panes...
              </div>
            )}

            {/* TradingView / Dhan Style Floating Buy/Sell Scalper Bar (Top-Left of Chart) */}
            {showScalperWidget && (
              <div className="absolute top-3 left-3 z-30 flex items-center shadow-xl rounded-md overflow-hidden border bg-background/95 backdrop-blur-sm text-xs font-mono">
                {/* Symbol selector dropdown */}
                {clock?.watchlist && clock.watchlist.length > 0 && (
                  <div className="border-r border-border bg-muted/30 px-1">
                    <Select value={selectedSymbol} onValueChange={setSelectedSymbol}>
                      <SelectTrigger className="h-8 w-24 text-[11px] border-0 shadow-none font-sans font-bold">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {clock.watchlist.map((s) => (
                          <SelectItem key={s} value={s}>
                            {s}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                )}

                {/* Sell Button */}
                <button
                  type="button"
                  onClick={() => handleSimulateOrderCustom('SELL')}
                  className="flex flex-col items-center justify-center px-4 py-1.5 bg-rose-600 hover:bg-rose-700 text-white font-bold transition-colors cursor-pointer"
                >
                  <span className="text-xs font-mono">{symbolState?.ltp ?? '—'}</span>
                  <span className="text-[9px] tracking-wider uppercase opacity-90">SELL</span>
                </button>

                {/* Quantity Input */}
                <div className="flex items-center px-2 bg-background border-x border-border">
                  <input
                    type="number"
                    value={simQty}
                    onChange={(e) => setSimQty(e.target.value)}
                    className="w-12 text-center bg-transparent font-bold focus:outline-none text-xs font-mono"
                    title="Quantity"
                  />
                </div>

                {/* Buy Button */}
                <button
                  type="button"
                  onClick={() => handleSimulateOrderCustom('BUY')}
                  className="flex flex-col items-center justify-center px-4 py-1.5 bg-emerald-600 hover:bg-emerald-700 text-white font-bold transition-colors cursor-pointer"
                >
                  <span className="text-xs font-mono">{symbolState?.ltp ?? '—'}</span>
                  <span className="text-[9px] tracking-wider uppercase opacity-90">BUY</span>
                </button>

                {/* Positions PnL & Reset */}
                <div className="flex items-center gap-2 px-3 bg-muted/20 border-l border-border text-[11px]">
                  <span className={cn('font-bold', totalPnL >= 0 ? 'text-emerald-500' : 'text-rose-500')}>
                    PnL: ₹{totalPnL.toFixed(2)} ({demoTrades.length})
                  </span>
                  {demoTrades.length > 0 && (
                    <button
                      type="button"
                      onClick={() => setDemoTrades([])}
                      className="text-[10px] text-rose-400 hover:underline cursor-pointer"
                      title="Reset trades"
                    >
                      Reset
                    </button>
                  )}
                </div>
              </div>
            )}

            {/* Floating Market Depth / DOM Widget */}
            {showDepthDOM && (
              <div
                className="absolute z-30 w-64 rounded-lg border bg-background/95 p-3 shadow-xl backdrop-blur-sm space-y-2 text-xs font-mono select-none"
                style={{
                  left: `${depthPos.x}px`,
                  top: `${depthPos.y}px`,
                  cursor: isDraggingDepth ? 'grabbing' : 'default',
                }}
              >
                <div
                  className="flex items-center justify-between border-b pb-1.5 font-sans cursor-grab active:cursor-grabbing"
                  onMouseDown={(e) => {
                    setIsDraggingDepth(true)
                    dragRef.current = {
                      startX: e.clientX,
                      startY: e.clientY,
                      initialX: depthPos.x,
                      initialY: depthPos.y,
                    }
                  }}
                >
                  <div className="font-bold flex items-center gap-1.5 text-xs pointer-events-none">
                    <Eye className="h-3.5 w-3.5 text-primary" /> Market Depth ({selectedSymbol}) ⠿
                  </div>
                  <Button variant="ghost" size="sm" className="h-5 w-5 p-0" onClick={() => setShowDepthDOM(false)}>
                    ✕
                  </Button>
                </div>

                {symbolState ? (
                  <div className="space-y-2">
                    <div className="grid grid-cols-3 gap-1 text-center border p-1 rounded bg-muted/20 text-[11px]">
                      <div>
                        <div className="text-[9px] text-muted-foreground">LTP</div>
                        <div className="font-bold">{symbolState.ltp ?? '—'}</div>
                      </div>
                      <div>
                        <div className="text-[9px] text-muted-foreground">Vol</div>
                        <div>{symbolState.volume ?? '—'}</div>
                      </div>
                      <div>
                        <div className="text-[9px] text-muted-foreground">OI</div>
                        <div>{symbolState.oi ?? '—'}</div>
                      </div>
                    </div>

                    <div className="grid grid-cols-2 gap-1.5 text-[10px]">
                      <div className="border rounded p-1 space-y-0.5 bg-emerald-500/5 max-h-48 overflow-y-auto">
                        <div className="font-bold text-emerald-500 border-b pb-0.5 sticky top-0 bg-background">Bids (20 Lvl)</div>
                        {Array.from({ length: 20 }, (_, i) => i + 1).map((i) => (
                          <div key={i} className="flex justify-between">
                            <span>{(symbolState as any)[`bid_${i}`] ?? '—'}</span>
                            <span className="text-muted-foreground">{(symbolState as any)[`bid_qty_${i}`] ?? ''}</span>
                          </div>
                        ))}
                      </div>
                      <div className="border rounded p-1 space-y-0.5 bg-rose-500/5 max-h-48 overflow-y-auto">
                        <div className="font-bold text-rose-500 border-b pb-0.5 sticky top-0 bg-background">Asks (20 Lvl)</div>
                        {Array.from({ length: 20 }, (_, i) => i + 1).map((i) => (
                          <div key={i} className="flex justify-between">
                            <span>{(symbolState as any)[`ask_${i}`] ?? '—'}</span>
                            <span className="text-muted-foreground">{(symbolState as any)[`ask_qty_${i}`] ?? ''}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                ) : (
                  <p className="text-muted-foreground text-center py-4 font-sans">No depth data</p>
                )}
              </div>
            )}
          </div>
        </main>
      </div>
    </>
  )
}
