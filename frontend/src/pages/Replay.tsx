import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Navbar } from '@/components/layout/Navbar'
import {
  type ReplayPanelTab,
  type ReplayPosition,
  ReplaySidePanel,
} from '@/components/replay/ReplaySidePanel'
import {
  type AvailableEntry,
  type ClockState,
  type LayoutPreset,
  ReplayTopBar,
  type SessionDraft,
  timePart,
} from '@/components/replay/ReplayTopBar'
import { ChartPane } from '@/components/trading/ChartPane'
import { DrawingRail } from '@/components/trading/DrawingRail'
import type { DrawStats, TradingTerminal } from '@/lib/trading/terminal'
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
const PANEL_KEY = 'oa-replay-panel-collapsed'

/** Stable stand-in for "no tape row yet"; a null would put the depth ladder in live mode. */
const NO_DEPTH_ROW: Record<string, unknown> = {}

const DEFAULT_DRAFT: SessionDraft = {
  date: '2026-08-10',
  start: '09:15:00',
  end: '15:30:00',
  watchlist: 'NIFTY, RELIANCE',
}

const splitSymbols = (text: string) =>
  text
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)

interface SymbolState {
  timestamp?: string
  ltp?: number
  volume?: number
  oi?: number
  [depthField: string]: unknown
}

interface FillResult {
  status: string
  fill_price: number
  filled_qty: number
  unfilled_qty?: number
  slippage?: number
  reason?: string
  /** False when the tape held no order book at that instant and the fill fell back to LTP. */
  depth_used?: boolean
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

  /* ── right-side watchlist (TradingView style, replay-sourced) ────────── */
  const [replayMarketData, setReplayMarketData] = useState<
    Record<string, { ltp?: number; percentChange?: number }>
  >({})
  const firstLtpRef = useRef<Record<string, number>>({})

  /** Click a watchlist row: select it for the replay widgets and load it in the focused pane. */
  const selectWatchSymbol = useCallback(async (sym: string) => {
    setSelectedSymbol(sym)
    const t = activeRef.current
    if (!t) return
    const rows = await t.search(sym, undefined, 10)
    const row = rows.find((r) => r.symbol.toUpperCase() === sym.toUpperCase())
    if (row) t.loadSymbol(row)
  }, [])

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
  // Session settings as typed in the Session popover. `appliedDraft` is what the
  // server clock was last known to run, so the bar can flag unapplied edits.
  const [draft, setDraft] = useState<SessionDraft>(DEFAULT_DRAFT)
  const [appliedDraft, setAppliedDraft] = useState<SessionDraft>(DEFAULT_DRAFT)
  const draftSyncedRef = useRef(false)
  const [autoPauseOnFill] = useState(true)

  const draftDirty =
    draft.date !== appliedDraft.date ||
    draft.start !== appliedDraft.start ||
    draft.end !== appliedDraft.end ||
    draft.watchlist !== appliedDraft.watchlist

  // The inputs are hidden in a popover, so start them from the session the
  // clock is already running instead of fixed defaults. The watchlist is left
  // as typed: the server expands it (NIFTY adds its option strikes).
  useEffect(() => {
    if (!clock || draftSyncedRef.current) return
    draftSyncedRef.current = true
    const fromClock = {
      date: clock.session_date || DEFAULT_DRAFT.date,
      start: timePart(clock.start_time) || DEFAULT_DRAFT.start,
      end: timePart(clock.end_time) || DEFAULT_DRAFT.end,
    }
    setDraft((prev) => ({ ...prev, ...fromClock }))
    setAppliedDraft((prev) => ({ ...prev, ...fromClock }))
  }, [clock])

  // Keyed by content: the status poll delivers a new array every second, and
  // an unstable list would restart the watchlist poll each time.
  const clockWatchlistKey = (clock?.watchlist ?? []).join(',')
  const replayWatchlist = useMemo(
    () => splitSymbols(clockWatchlistKey || draft.watchlist),
    [clockWatchlistKey, draft.watchlist]
  )

  /** Poll replay state for every watchlist symbol; change is vs. session start. */
  useEffect(() => {
    if (replayWatchlist.length === 0) return
    let alive = true
    let timer: ReturnType<typeof setInterval> | undefined

    // One batched request for the whole watchlist instead of one per symbol.
    // 100 per call keeps the query string well inside the server's request
    // line limit (the endpoint itself accepts up to 200).
    const BATCH = 100
    const poll = async () => {
      const rows: Record<string, { ltp?: number; percentChange?: number }> = {}
      const chunks: string[][] = []
      for (let i = 0; i < replayWatchlist.length; i += BATCH) {
        chunks.push(replayWatchlist.slice(i, i + BATCH))
      }
      await Promise.all(
        chunks.map(async (chunk) => {
          try {
            const res = await fetch(
              `/api/replay/states?symbols=${encodeURIComponent(chunk.join(','))}`,
              { credentials: 'include' }
            )
            if (!res.ok) return
            const data = (await res.json()) as {
              states?: Record<string, { ltp?: number | null } | null>
            }
            for (const [key, state] of Object.entries(data.states ?? {})) {
              const ltp = state?.ltp
              if (ltp == null) continue
              if (firstLtpRef.current[key] == null) firstLtpRef.current[key] = ltp
              const base = firstLtpRef.current[key]
              rows[key] = {
                ltp,
                percentChange: base > 0 ? ((ltp - base) / base) * 100 : undefined,
              }
            }
          } catch {
            /* next cycle */
          }
        })
      )
      if (alive) setReplayMarketData((prev) => ({ ...prev, ...rows }))
    }

    poll()
    timer = setInterval(poll, 2000)
    return () => {
      alive = false
      if (timer) clearInterval(timer)
    }
  }, [replayWatchlist])

  // Docked side panel
  const [panelCollapsed, setPanelCollapsed] = useState(() => {
    const saved = localStorage.getItem(PANEL_KEY)
    return saved != null ? saved === '1' : window.innerWidth < 1024
  })
  const [panelTab, setPanelTab] = useState<ReplayPanelTab>('watchlist')

  useEffect(() => {
    localStorage.setItem(PANEL_KEY, panelCollapsed ? '1' : '0')
  }, [panelCollapsed])

  // Available Data
  const [availableData, setAvailableData] = useState<AvailableEntry[] | null>(null)

  const [selectedSymbol, setSelectedSymbol] = useState('NIFTY')
  const [symbolState, setSymbolState] = useState<SymbolState | null>(null)

  // Demo Order Simulation & PnL
  const [simQty, setSimQty] = useState('1')
  const [demoTrades, setDemoTrades] = useState<DemoTrade[]>([])

  // Price every fill at render. The selected symbol is refreshed each second
  // by fetchSymbolState; other symbols take the watchlist quote, so a row
  // never shows a P&L frozen at the moment its symbol was deselected.
  const positions = useMemo<ReplayPosition[]>(
    () =>
      demoTrades.map((trade) => {
        const quote = replayMarketData[trade.symbol.toUpperCase()]?.ltp
        const ltp =
          trade.symbol === selectedSymbol
            ? (trade.current_ltp ?? quote)
            : (quote ?? trade.current_ltp)
        const mult = trade.action === 'BUY' ? 1 : -1
        return {
          id: trade.id,
          symbol: trade.symbol,
          action: trade.action,
          quantity: trade.quantity,
          fill_price: trade.fill_price,
          ltp,
          pnl: ltp != null ? (ltp - trade.fill_price) * trade.quantity * mult : (trade.pnl ?? 0),
        }
      }),
    [demoTrades, replayMarketData, selectedSymbol]
  )
  const totalPnL = positions.reduce((sum, p) => sum + p.pnl, 0)

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
    setDraft((prev) => {
      const list = splitSymbols(prev.watchlist)
      return {
        ...prev,
        date: entry.date,
        watchlist: list.includes(entry.symbol)
          ? prev.watchlist
          : [...list, entry.symbol].join(', '),
      }
    })
    showToast.info(
      `Added ${entry.symbol} (${entry.date}) to the session draft. Open Session and apply it.`
    )
  }

  const handleConfigureSession = async () => {
    const submitted = draft
    try {
      const res = await apiPost('/api/replay/session', {
        date: submitted.date,
        start_time: submitted.start,
        end_time: submitted.end,
        watchlist: splitSymbols(submitted.watchlist),
      })
      if (res.ok) {
        setAppliedDraft(submitted)
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
        if (data.status === 'filled' || data.status === 'partial') {
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
          const qtyNote =
            data.status === 'partial'
              ? ` (partial ${data.filled_qty}, ${data.unfilled_qty} unfilled)`
              : ''
          const depthNote = data.depth_used === false ? ' - no depth on tape, LTP used' : ''
          showToast.success(`Demo ${action} filled @ ${data.fill_price}${qtyNote}${depthNote}`)
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
    // Jump within the session the clock is running, not the unapplied draft.
    const date = clock?.session_date || appliedDraft.date
    if (!date) return
    const targetIso = `${date}T${timeStr}+05:30`
    handleControl('seek', { timestamp: targetIso })
  }

  const layout = LAYOUTS.find((l) => l.id === layoutId) ?? LAYOUTS[0]

  return (
    <>
      <Navbar />
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
        <ReplayTopBar
          clock={clock}
          progress={getScrubberProgress()}
          onControl={handleControl}
          onSeekPercent={handleScrubberChange}
          onQuickJump={handleQuickJump}
          draft={draft}
          draftDirty={draftDirty}
          onDraftChange={(patch) => setDraft((prev) => ({ ...prev, ...patch }))}
          onApplySession={handleConfigureSession}
          layouts={LAYOUTS}
          layoutId={layoutId}
          onLayoutChange={setLayoutId}
          available={availableData}
          onUseAvailable={handleUseAvailable}
        />

        {/* Drawing rail, chart grid and the docked side panel */}
        <main className="flex min-h-0 flex-1">
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

          <div className="flex min-h-0 min-w-0 flex-1 flex-col">
            {noApiKey ? (
              <div className="flex h-full flex-col items-center justify-center gap-2 p-8 text-center">
                <p className="text-sm text-muted-foreground">
                  No API key found for charting terminal.
                </p>
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
          </div>

          <ReplaySidePanel
            collapsed={panelCollapsed}
            onToggleCollapsed={() => setPanelCollapsed((v) => !v)}
            tab={panelTab}
            onTabChange={setPanelTab}
            symbols={replayWatchlist}
            selectedSymbol={selectedSymbol}
            onSelectSymbol={selectWatchSymbol}
            onTicketSymbol={setSelectedSymbol}
            quotes={replayMarketData}
            depthRow={symbolState ?? NO_DEPTH_ROW}
            positions={positions}
            totalPnL={totalPnL}
            onResetPositions={() => setDemoTrades([])}
            qty={simQty}
            onQtyChange={setSimQty}
            ltp={symbolState?.ltp}
            onOrder={handleSimulateOrderCustom}
          />
        </main>
      </div>
    </>
  )
}
