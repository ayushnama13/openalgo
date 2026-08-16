import { Disc, Layers, X } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { showToast } from '@/utils/toast'

export interface DepthItem {
  price?: number | null
  quantity?: number | null
  orders?: number | null
}

export interface Depth20Data {
  symbol?: string
  exchange?: string
  ltp?: number | null
  volume?: number | null
  oi?: number | null
  totalBuyQty?: number | null
  totalSellQty?: number | null
  bids: DepthItem[]
  asks: DepthItem[]
}

export interface Depth20PanelProps {
  symbol: string
  exchange?: string
  apiKey?: string
  wsUrl?: string
  /**
   * Optional replay snapshot row passed when in Replay mode.
   * Format: bid_1..20, bid_qty_1..20, ask_1..20, ask_qty_1..20, ltp, volume, oi
   */
  replayData?: Record<string, any> | null
  onClose?: () => void
  className?: string
}

async function fetchCSRF(): Promise<string> {
  const r = await fetch('/auth/csrf-token', { credentials: 'include' })
  return (await r.json()).csrf_token
}

export function Depth20Panel({
  symbol,
  exchange = 'NSE',
  apiKey,
  wsUrl = 'ws://127.0.0.1:8765',
  replayData,
  onClose,
  className,
}: Depth20PanelProps) {
  const [levelCount, setLevelCount] = useState<5 | 10 | 20>(20)
  const [depthData, setDepthData] = useState<Depth20Data>({ bids: [], asks: [] })
  const [isRecording, setIsRecording] = useState(false)
  const [recordingLoading, setRecordingLoading] = useState(false)
  const [connected, setConnected] = useState(false)

  const wsRef = useRef<WebSocket | null>(null)

  // Parse replayData if supplied (Replay Mode)
  useEffect(() => {
    if (!replayData) return

    const bids: DepthItem[] = []
    const asks: DepthItem[] = []

    for (let i = 1; i <= 20; i++) {
      const bPrice = replayData[`bid_${i}`]
      const bQty = replayData[`bid_qty_${i}`]
      const bOrd = replayData[`bid_orders_${i}`]
      if (bPrice != null || bQty != null) {
        bids.push({
          price: typeof bPrice === 'number' ? bPrice : Number(bPrice) || null,
          quantity: typeof bQty === 'number' ? bQty : Number(bQty) || null,
          orders: typeof bOrd === 'number' ? bOrd : Number(bOrd) || null,
        })
      }

      const aPrice = replayData[`ask_${i}`]
      const aQty = replayData[`ask_qty_${i}`]
      const aOrd = replayData[`ask_orders_${i}`]
      if (aPrice != null || aQty != null) {
        asks.push({
          price: typeof aPrice === 'number' ? aPrice : Number(aPrice) || null,
          quantity: typeof aQty === 'number' ? aQty : Number(aQty) || null,
          orders: typeof aOrd === 'number' ? aOrd : Number(aOrd) || null,
        })
      }
    }

    setDepthData({
      symbol,
      exchange,
      ltp: replayData.ltp,
      volume: replayData.volume,
      oi: replayData.oi,
      totalBuyQty: bids.reduce((acc, b) => acc + (b.quantity || 0), 0),
      totalSellQty: asks.reduce((acc, a) => acc + (a.quantity || 0), 0),
      bids,
      asks,
    })
  }, [replayData, symbol, exchange])

  // Live WebSocket Depth streaming (Live Mode)
  useEffect(() => {
    if (replayData || !apiKey || !symbol) return

    let isMounted = true
    let ws: WebSocket | null = null

    const connectWS = () => {
      try {
        ws = new WebSocket(wsUrl)
        wsRef.current = ws

        ws.onopen = () => {
          if (!isMounted) return
          setConnected(true)
          // Authenticate
          ws?.send(JSON.stringify({ action: 'authenticate', api_key: apiKey }))
        }

        ws.onmessage = (event) => {
          if (!isMounted) return
          try {
            const msg = JSON.parse(event.data)

            if (msg.status === 'success' && msg.message?.includes('Authenticated')) {
              // Subscribe to Depth with requested 20-level depth
              ws?.send(
                JSON.stringify({
                  action: 'subscribe',
                  symbols: [{ symbol: symbol.toUpperCase(), exchange: exchange.toUpperCase() }],
                  mode: 'Depth',
                  depth: 20,
                })
              )
              return
            }

            if (msg.type === 'market_data') {
              const pktSymbol = strNormalize(msg.symbol)
              if (
                pktSymbol !== symbol.toUpperCase() &&
                pktSymbol !== `${symbol.toUpperCase()}:50`
              ) {
                return
              }

              const md = msg.data || {}
              const depthObj = md.depth || {}
              let buyList: any[] = []
              let sellList: any[] = []

              if (Array.isArray(depthObj.buy)) buyList = depthObj.buy
              else if (Array.isArray(md.bids)) buyList = md.bids

              if (Array.isArray(depthObj.sell)) sellList = depthObj.sell
              else if (Array.isArray(md.asks)) sellList = md.asks

              const bids: DepthItem[] = buyList.map((item) => ({
                price: typeof item.price === 'number' ? item.price : Number(item.price) || null,
                quantity:
                  typeof item.quantity === 'number'
                    ? item.quantity
                    : Number(item.quantity) || null,
                orders: item.orders ?? item.order_count ?? null,
              }))

              const asks: DepthItem[] = sellList.map((item) => ({
                price: typeof item.price === 'number' ? item.price : Number(item.price) || null,
                quantity:
                  typeof item.quantity === 'number'
                    ? item.quantity
                    : Number(item.quantity) || null,
                orders: item.orders ?? item.order_count ?? null,
              }))

              setDepthData({
                symbol: symbol.toUpperCase(),
                exchange: exchange.toUpperCase(),
                ltp: md.ltp != null ? Number(md.ltp) : null,
                volume: md.volume != null ? Number(md.volume) : null,
                oi: md.oi != null ? Number(md.oi) : null,
                totalBuyQty:
                  md.total_buy_qty ?? md.total_buy_quantity ?? bids.reduce((acc, b) => acc + (b.quantity || 0), 0),
                totalSellQty:
                  md.total_sell_qty ?? md.total_sell_quantity ?? asks.reduce((acc, a) => acc + (a.quantity || 0), 0),
                bids,
                asks,
              })
            }
          } catch {
            /* ignore parse errors */
          }
        }

        ws.onclose = () => {
          if (isMounted) setConnected(false)
        }
        ws.onerror = () => {
          if (isMounted) setConnected(false)
        }
      } catch {
        if (isMounted) setConnected(false)
      }
    }

    connectWS()

    return () => {
      isMounted = false
      if (ws) {
        ws.close()
      }
    }
  }, [symbol, exchange, apiKey, wsUrl, replayData])

  // Check initial recorder status for this symbol
  useEffect(() => {
    if (replayData) return
    let alive = true
    fetch('/api/depth-recorder/status')
      .then((r) => r.json())
      .then((data) => {
        if (!alive) return
        const list = Array.isArray(data) ? data : [data]
        const active = list.some(
          (item: any) =>
            item?.symbol?.toUpperCase() === symbol.toUpperCase() &&
            item?.exchange?.toUpperCase() === exchange.toUpperCase() &&
            item?.status === 'recording'
        )
        setIsRecording(active)
      })
      .catch(() => {})

    return () => {
      alive = false
    }
  }, [symbol, exchange, replayData])

  const toggleRecording = async () => {
    if (recordingLoading || replayData) return
    setRecordingLoading(true)
    try {
      const csrf = await fetchCSRF()
      const action = isRecording ? 'stop' : 'start'
      const res = await fetch(`/api/depth-recorder/${action}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
        body: JSON.stringify({ symbol, exchange }),
      })
      const data = await res.json()
      if (res.ok && (data.status === 'started' || data.status === 'stopping' || data.status === 'stopped')) {
        const nowRecording = !isRecording
        setIsRecording(nowRecording)
        showToast.success(
          nowRecording
            ? `Started recording depth for ${symbol}`
            : `Stopped recording depth for ${symbol}`
        )
      } else {
        showToast.error(data.message || `Failed to ${action} recording`)
      }
    } catch {
      showToast.error('Depth recorder request failed')
    } finally {
      setRecordingLoading(false)
    }
  }

  // Sliced levels for display
  const bidsSlice = useMemo(() => depthData.bids.slice(0, levelCount), [depthData.bids, levelCount])
  const asksSlice = useMemo(() => depthData.asks.slice(0, levelCount), [depthData.asks, levelCount])

  // Calculate maximum quantity for scaling background bars
  const maxQty = useMemo(() => {
    let max = 1
    for (const b of bidsSlice) if (b.quantity && b.quantity > max) max = b.quantity
    for (const a of asksSlice) if (a.quantity && a.quantity > max) max = a.quantity
    return max
  }, [bidsSlice, asksSlice])

  // Total sums & imbalance calculation
  const sumBuy = depthData.totalBuyQty || bidsSlice.reduce((acc, b) => acc + (b.quantity || 0), 0)
  const sumSell = depthData.totalSellQty || asksSlice.reduce((acc, a) => acc + (a.quantity || 0), 0)
  const grandTotal = sumBuy + sumSell
  const buyPct = grandTotal > 0 ? (sumBuy / grandTotal) * 100 : 50

  const bestBid = bidsSlice[0]?.price || null
  const bestAsk = asksSlice[0]?.price || null
  const spread = bestBid && bestAsk ? Math.abs(bestAsk - bestBid) : null

  return (
    <div
      className={cn(
        'flex flex-col rounded-lg border bg-card/95 text-card-foreground shadow-lg backdrop-blur-sm',
        className
      )}
    >
      {/* Header bar */}
      <div className="flex items-center justify-between border-b px-3 py-2">
        <div className="flex items-center gap-2">
          <Layers className="h-4 w-4 text-primary" />
          <span className="font-semibold text-sm">20-Level Depth</span>
          <Badge variant="outline" className="text-[10px] font-mono uppercase">
            {symbol} · {exchange}
          </Badge>
          {!replayData && (
            <span
              className={cn(
                'inline-block h-2 w-2 rounded-full',
                connected ? 'bg-emerald-500 shadow-[0_0_6px] shadow-emerald-500/70' : 'bg-muted-foreground'
              )}
              title={connected ? 'Connected' : 'Connecting'}
            />
          )}
        </div>

        <div className="flex items-center gap-1.5">
          {/* Level count selector */}
          <div className="flex items-center rounded border bg-muted/40 p-0.5 text-xs">
            {([5, 10, 20] as const).map((lvl) => (
              <button
                key={lvl}
                type="button"
                onClick={() => setLevelCount(lvl)}
                className={cn(
                  'px-2 py-0.5 rounded text-[10px] font-medium transition-colors',
                  levelCount === lvl
                    ? 'bg-primary text-primary-foreground shadow-sm'
                    : 'text-muted-foreground hover:text-foreground'
                )}
              >
                {lvl}D
              </button>
            ))}
          </div>

          {/* Depth recorder toggle button */}
          {!replayData && (
            <Button
              variant={isRecording ? 'destructive' : 'outline'}
              size="sm"
              className="h-7 text-xs gap-1 px-2"
              onClick={toggleRecording}
              disabled={recordingLoading}
              title={isRecording ? 'Stop depth recording' : 'Record depth ticks'}
            >
              <Disc className={cn('h-3.5 w-3.5', isRecording && 'animate-pulse text-rose-200')} />
              <span>{isRecording ? 'REC' : 'Record'}</span>
            </Button>
          )}

          {onClose && (
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7 text-muted-foreground hover:text-foreground"
              onClick={onClose}
              title="Close depth panel"
            >
              <X className="h-4 w-4" />
            </Button>
          )}
        </div>
      </div>

      {/* Summary Stats Row */}
      <div className="grid grid-cols-4 gap-2 border-b bg-muted/20 px-3 py-1.5 text-xs">
        <div>
          <span className="text-[10px] text-muted-foreground block">LTP</span>
          <span className="font-mono font-semibold">
            {depthData.ltp != null ? depthData.ltp.toFixed(2) : '—'}
          </span>
        </div>
        <div>
          <span className="text-[10px] text-muted-foreground block">Spread</span>
          <span className="font-mono text-muted-foreground">
            {spread != null ? spread.toFixed(2) : '—'}
          </span>
        </div>
        <div>
          <span className="text-[10px] text-muted-foreground block">Volume</span>
          <span className="font-mono">
            {depthData.volume != null ? depthData.volume.toLocaleString() : '—'}
          </span>
        </div>
        <div>
          <span className="text-[10px] text-muted-foreground block">OI</span>
          <span className="font-mono">
            {depthData.oi != null ? depthData.oi.toLocaleString() : '—'}
          </span>
        </div>
      </div>

      {/* Buy/Sell Order Imbalance Bar */}
      <div className="px-3 py-1.5 border-b bg-muted/10">
        <div className="flex justify-between text-[10px] font-mono mb-1">
          <span className="text-emerald-500 font-semibold">
            Bids: {sumBuy.toLocaleString()} ({buyPct.toFixed(1)}%)
          </span>
          <span className="text-rose-500 font-semibold">
            Asks: {sumSell.toLocaleString()} ({(100 - buyPct).toFixed(1)}%)
          </span>
        </div>
        <div className="flex h-1.5 w-full overflow-hidden rounded-full bg-muted">
          <div
            className="bg-emerald-500 transition-all duration-300"
            style={{ width: `${buyPct}%` }}
          />
          <div
            className="bg-rose-500 transition-all duration-300"
            style={{ width: `${100 - buyPct}%` }}
          />
        </div>
      </div>

      {/* Order Book Table Header */}
      <div className="grid grid-cols-2 divide-x border-b bg-muted/40 text-[10px] font-medium text-muted-foreground">
        <div className="grid grid-cols-3 px-2 py-1 text-emerald-500">
          <span>Orders</span>
          <span className="text-right">Qty</span>
          <span className="text-right">Bid Price</span>
        </div>
        <div className="grid grid-cols-3 px-2 py-1 text-rose-500">
          <span>Ask Price</span>
          <span className="text-right">Qty</span>
          <span className="text-right">Orders</span>
        </div>
      </div>

      {/* Order Book Rows */}
      <div className="divide-y divide-border/40 overflow-y-auto max-h-[28rem] font-mono text-xs">
        {Array.from({ length: levelCount }).map((_, idx) => {
          const bid = bidsSlice[idx]
          const ask = asksSlice[idx]

          const bidPct = bid?.quantity && maxQty > 0 ? Math.min((bid.quantity / maxQty) * 100, 100) : 0
          const askPct = ask?.quantity && maxQty > 0 ? Math.min((ask.quantity / maxQty) * 100, 100) : 0

          return (
            <div key={idx} className="grid grid-cols-2 divide-x hover:bg-muted/30 transition-colors">
              {/* Bid Column */}
              <div className="relative grid grid-cols-3 px-2 py-1 items-center">
                <div className="absolute inset-0 overflow-hidden pointer-events-none">
                  <div
                    className="absolute top-0 bottom-0 right-0 bg-emerald-500/15 transition-all duration-300"
                    style={{ width: `${bidPct}%` }}
                  />
                </div>
                <span className="relative z-10 text-[10px] text-muted-foreground">
                  {bid?.orders ?? '-'}
                </span>
                <span className="relative z-10 text-right text-muted-foreground">
                  {bid?.quantity != null ? bid.quantity.toLocaleString() : '-'}
                </span>
                <span className="relative z-10 text-right font-medium text-emerald-500">
                  {bid?.price != null ? bid.price.toFixed(2) : '-'}
                </span>
              </div>

              {/* Ask Column */}
              <div className="relative grid grid-cols-3 px-2 py-1 items-center">
                <div className="absolute inset-0 overflow-hidden pointer-events-none">
                  <div
                    className="absolute top-0 bottom-0 left-0 bg-rose-500/15 transition-all duration-300"
                    style={{ width: `${askPct}%` }}
                  />
                </div>
                <span className="relative z-10 font-medium text-rose-500">
                  {ask?.price != null ? ask.price.toFixed(2) : '-'}
                </span>
                <span className="relative z-10 text-right text-muted-foreground">
                  {ask?.quantity != null ? ask.quantity.toLocaleString() : '-'}
                </span>
                <span className="relative z-10 text-right text-[10px] text-muted-foreground">
                  {ask?.orders ?? '-'}
                </span>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function strNormalize(val: any): string {
  if (typeof val !== 'string') return ''
  return val.toUpperCase().split(':')[0]
}
