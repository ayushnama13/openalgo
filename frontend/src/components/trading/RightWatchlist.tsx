import { ChevronLeft, ChevronRight, Plus, Search, Trash2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ScrollArea } from '@/components/ui/scroll-area'
import { cn } from '@/lib/utils'

export interface WatchlistRow {
  ltp?: number
  percentChange?: number
}

interface WatchlistProps {
  symbols: string[]
  selectedSymbol?: string
  onSelectSymbol(symbol: string): void
  onAddSymbol?(symbol: string): void
  onRemoveSymbol?(symbol: string): void
  /** Externally supplied quotes (e.g. replay state); merged over polling. */
  marketData?: Record<string, WatchlistRow>
  /** When set, polls /api/v1/quotes for every symbol on a 4s cadence. */
  apiKey?: string
  className?: string
}

/**
 * TradingView-style collapsible right sidebar. Polls live quotes itself when
 * `apiKey` is given; external `marketData` (replay states) merges on top.
 */
export function RightWatchlist({
  symbols,
  selectedSymbol,
  onSelectSymbol,
  onAddSymbol,
  onRemoveSymbol,
  marketData = {},
  apiKey,
  className,
}: WatchlistProps) {
  const [collapsed, setCollapsed] = useState(false)
  const [newSymbol, setNewSymbol] = useState('')
  const [search, setSearch] = useState('')
  const [polled, setPolled] = useState<Record<string, WatchlistRow>>({})

  useEffect(() => {
    if (!apiKey || symbols.length === 0) return
    let alive = true
    let timer: ReturnType<typeof setInterval> | undefined

    const poll = async () => {
      const rows: Record<string, WatchlistRow> = {}
      await Promise.all(
        symbols.map(async (sym) => {
          try {
            const res = await fetch('/api/v1/quotes', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ apikey: apiKey, symbol: sym, exchange: 'NSE' }),
            })
            const j = (await res.json()) as {
              data?: {
                ltp?: number
                lastprice?: number
                close?: number
                open?: number
                previousclose?: number
                change?: number
                change_percent?: number
                changepercent?: number
                change_percentage?: number
              }
            }
            const q = j.data || {}
            const ltp = q.ltp ?? q.lastprice ?? q.close
            if (ltp == null) return
            let pct =
              q.change_percent ?? q.changepercent ?? q.change_percentage
            if (pct == null && q.change != null && ltp > 0) pct = (q.change / (ltp - q.change)) * 100
            if (pct == null && q.previousclose != null && q.previousclose > 0)
              pct = ((ltp - q.previousclose) / q.previousclose) * 100
            if (pct == null && q.open != null && q.open > 0) pct = ((ltp - q.open) / q.open) * 100
            rows[sym.toUpperCase()] = { ltp, percentChange: pct ?? undefined }
          } catch {
            /* next cycle */
          }
        })
      )
      if (alive) setPolled((prev) => ({ ...prev, ...rows }))
    }

    poll()
    timer = setInterval(poll, 4000)
    return () => {
      alive = false
      if (timer) clearInterval(timer)
    }
  }, [apiKey, symbols])

  const merged: Record<string, WatchlistRow> = { ...polled, ...marketData }

  const filteredSymbols = symbols.filter((s) => s.toLowerCase().includes(search.toLowerCase()))

  const handleAdd = (e: React.FormEvent) => {
    e.preventDefault()
    if (!newSymbol.trim()) return
    onAddSymbol?.(newSymbol.trim().toUpperCase())
    setNewSymbol('')
  }

  if (collapsed) {
    return (
      <div
        className={cn(
          'relative flex flex-col items-center border-l bg-background/60 py-2 w-8 shrink-0',
          className
        )}
      >
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7 text-muted-foreground hover:text-foreground"
          onClick={() => setCollapsed(false)}
          title="Expand Watchlist"
        >
          <ChevronLeft className="h-4 w-4" />
        </Button>
        <div className="mt-4 [writing-mode:vertical-lr] text-[11px] font-medium tracking-wider text-muted-foreground uppercase">
          Watchlist
        </div>
      </div>
    )
  }

  return (
    <div
      className={cn(
        'relative flex flex-col w-64 shrink-0 border-l bg-background/80 backdrop-blur-sm',
        className
      )}
    >
      {/* Header */}
      <div className="flex items-center justify-between border-b px-3 py-2">
        <div className="flex items-center gap-1.5 font-semibold text-xs uppercase tracking-wider text-muted-foreground">
          <span>Watchlist</span>
          <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-mono">
            {symbols.length}
          </span>
        </div>
        <Button
          variant="ghost"
          size="icon"
          className="h-6 w-6 text-muted-foreground hover:text-foreground"
          onClick={() => setCollapsed(true)}
          title="Collapse Watchlist"
        >
          <ChevronRight className="h-4 w-4" />
        </Button>
      </div>

      {/* Search / Add Bar */}
      <div className="p-2 border-b space-y-2">
        <div className="relative">
          <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-muted-foreground" />
          <Input
            placeholder="Search symbol..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="h-8 pl-8 text-xs font-mono"
          />
        </div>
        {onAddSymbol && (
          <form onSubmit={handleAdd} className="flex gap-1">
            <Input
              placeholder="Add symbol..."
              value={newSymbol}
              onChange={(e) => setNewSymbol(e.target.value)}
              className="h-7 text-xs font-mono"
            />
            <Button type="submit" size="sm" className="h-7 px-2">
              <Plus className="h-3.5 w-3.5" />
            </Button>
          </form>
        )}
      </div>

      {/* Symbol List */}
      <ScrollArea className="flex-1">
        <div className="p-1 space-y-0.5">
          {filteredSymbols.length === 0 ? (
            <div className="py-8 text-center text-xs text-muted-foreground">No symbols found</div>
          ) : (
            filteredSymbols.map((sym) => {
              const key = sym.toUpperCase()
              const data = merged[key] || {}
              const isSelected = selectedSymbol?.toUpperCase() === key
              const pct = data.percentChange
              const isPositive = pct != null && pct >= 0

              return (
                <div
                  key={key}
                  onClick={() => onSelectSymbol(sym)}
                  className={cn(
                    'group flex items-center justify-between rounded px-2.5 py-2 text-xs font-mono cursor-pointer transition-colors hover:bg-muted/60',
                    isSelected && 'bg-primary/10 border border-primary/30 text-primary font-bold'
                  )}
                >
                  <div className="flex flex-col">
                    <span className="font-sans font-semibold">{sym}</span>
                    <span className="text-[10px] text-muted-foreground">NSE</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <div className="text-right">
                      <div className="font-bold">
                        {data.ltp != null ? data.ltp.toLocaleString() : '—'}
                      </div>
                      <div
                        className={cn(
                          'text-[10px]',
                          pct == null
                            ? 'text-muted-foreground'
                            : isPositive
                              ? 'text-emerald-500'
                              : 'text-rose-500'
                        )}
                      >
                        {pct != null ? `${isPositive ? '+' : ''}${pct.toFixed(2)}%` : '—'}
                      </div>
                    </div>
                    {onRemoveSymbol && (
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation()
                          onRemoveSymbol(sym)
                        }}
                        className="opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-rose-500 transition-opacity p-1"
                        title="Remove symbol"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    )}
                  </div>
                </div>
              )
            })
          )}
        </div>
      </ScrollArea>
    </div>
  )
}
