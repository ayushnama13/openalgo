import { ChevronLeft, ChevronRight } from 'lucide-react'
import { useState } from 'react'
import { Depth20Panel } from '@/components/trading/Depth20Panel'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { cn } from '@/lib/utils'

export type ReplayPanelTab = 'watchlist' | 'depth' | 'positions'

export interface ReplayQuote {
  ltp?: number
  percentChange?: number
}

/** One simulated fill, priced at the latest replay quote for its symbol. */
export interface ReplayPosition {
  id: string
  symbol: string
  action: string
  quantity: number
  fill_price: number
  ltp?: number
  pnl: number
}

interface ReplaySidePanelProps {
  collapsed: boolean
  onToggleCollapsed(): void
  tab: ReplayPanelTab
  onTabChange(tab: ReplayPanelTab): void
  symbols: string[]
  selectedSymbol: string
  /** Watchlist click: selects the symbol and loads it in the focused chart. */
  onSelectSymbol(symbol: string): void
  /** Ticket dropdown: selects the symbol for orders and depth, leaving the charts alone. */
  onTicketSymbol(symbol: string): void
  /** Replay quotes keyed by upper-cased symbol. */
  quotes: Record<string, ReplayQuote>
  /** Tape row for the selected symbol. Never null: null would switch the ladder to live mode. */
  depthRow: Record<string, unknown>
  positions: ReplayPosition[]
  totalPnL: number
  onResetPositions(): void
  qty: string
  onQtyChange(qty: string): void
  ltp?: number
  onOrder(action: 'BUY' | 'SELL'): void
  className?: string
}

const fmtPrice = (n: number | undefined) =>
  n != null ? n.toLocaleString('en-IN', { maximumFractionDigits: 2 }) : '—'
const fmtPnl = (n: number) => `${n >= 0 ? '+' : ''}${n.toFixed(2)}`

/**
 * Docked right column for the replay terminal: watchlist, order book and
 * simulated positions as tabs, with the order ticket pinned underneath so it
 * never overlaps the chart.
 */
export function ReplaySidePanel({
  collapsed,
  onToggleCollapsed,
  tab,
  onTabChange,
  symbols,
  selectedSymbol,
  onSelectSymbol,
  onTicketSymbol,
  quotes,
  depthRow,
  positions,
  totalPnL,
  onResetPositions,
  qty,
  onQtyChange,
  ltp,
  onOrder,
  className,
}: ReplaySidePanelProps) {
  const [search, setSearch] = useState('')

  if (collapsed) {
    return (
      <aside
        className={cn(
          'flex w-8 shrink-0 flex-col items-center border-l bg-background/60 py-2',
          className
        )}
      >
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7 text-muted-foreground hover:text-foreground"
          onClick={onToggleCollapsed}
          title="Expand panel"
        >
          <ChevronLeft className="h-4 w-4" />
        </Button>
        <div className="mt-4 text-[11px] font-medium tracking-wider text-muted-foreground uppercase [writing-mode:vertical-lr]">
          Watchlist · Depth · Positions
        </div>
      </aside>
    )
  }

  const visibleSymbols = symbols.filter((s) => s.toLowerCase().includes(search.toLowerCase()))
  const ticketSymbols = symbols.some((s) => s.toUpperCase() === selectedSymbol.toUpperCase())
    ? symbols
    : [selectedSymbol, ...symbols]

  return (
    <aside
      className={cn('flex min-h-0 w-80 shrink-0 flex-col border-l bg-background/80', className)}
    >
      <Tabs
        value={tab}
        onValueChange={(v) => onTabChange(v as ReplayPanelTab)}
        className="min-h-0 flex-1 gap-0"
      >
        <div className="flex shrink-0 items-center gap-1 border-b px-2 py-1.5">
          <TabsList className="h-8 flex-1">
            <TabsTrigger value="watchlist" className="text-xs">
              Watchlist
            </TabsTrigger>
            <TabsTrigger value="depth" className="text-xs">
              Depth
            </TabsTrigger>
            <TabsTrigger value="positions" className="text-xs">
              Positions{positions.length > 0 ? ` (${positions.length})` : ''}
            </TabsTrigger>
          </TabsList>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 text-muted-foreground hover:text-foreground"
            onClick={onToggleCollapsed}
            title="Collapse panel"
          >
            <ChevronRight className="h-4 w-4" />
          </Button>
        </div>

        <TabsContent value="watchlist" className="min-h-0 overflow-y-auto">
          {symbols.length > 8 && (
            <div className="border-b p-2">
              <Input
                placeholder="Filter symbols..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="h-7 font-mono text-xs"
              />
            </div>
          )}
          <div className="space-y-0.5 p-1">
            {visibleSymbols.length === 0 ? (
              <div className="py-8 text-center text-xs text-muted-foreground">No symbols</div>
            ) : (
              visibleSymbols.map((sym) => {
                const key = sym.toUpperCase()
                const quote = quotes[key] ?? {}
                const pct = quote.percentChange
                const selected = selectedSymbol.toUpperCase() === key
                return (
                  <button
                    key={key}
                    type="button"
                    onClick={() => onSelectSymbol(sym)}
                    aria-pressed={selected}
                    className={cn(
                      'flex w-full items-center justify-between rounded border border-transparent px-2.5 py-1.5 text-left font-mono text-xs transition-colors hover:bg-muted/60',
                      selected && 'border-primary/30 bg-primary/10 font-bold text-primary'
                    )}
                  >
                    <span className="truncate font-sans font-semibold">{sym}</span>
                    <span className="flex shrink-0 items-baseline gap-2 pl-2 tabular-nums">
                      <span className="font-bold">{fmtPrice(quote.ltp)}</span>
                      <span
                        className={cn(
                          'w-14 text-right text-[10px]',
                          pct == null
                            ? 'text-muted-foreground'
                            : pct >= 0
                              ? 'text-emerald-500'
                              : 'text-rose-500'
                        )}
                      >
                        {pct != null ? `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%` : '—'}
                      </span>
                    </span>
                  </button>
                )
              })
            )}
          </div>
        </TabsContent>

        <TabsContent value="depth" className="min-h-0 overflow-y-auto">
          <Depth20Panel
            symbol={selectedSymbol}
            exchange="NSE"
            replayData={depthRow}
            hideTitle
            className="rounded-none border-0 bg-transparent shadow-none backdrop-blur-none"
          />
        </TabsContent>

        <TabsContent value="positions" className="min-h-0 overflow-y-auto">
          {positions.length === 0 ? (
            <div className="px-4 py-8 text-center text-xs text-muted-foreground">
              No simulated fills yet. Use Buy or Sell below.
            </div>
          ) : (
            <table className="w-full font-mono text-[11px] tabular-nums">
              <thead className="sticky top-0 bg-background text-[10px] text-muted-foreground">
                <tr className="border-b">
                  <th className="px-2 py-1 text-left font-medium">Symbol</th>
                  <th className="px-1 py-1 text-right font-medium">Qty</th>
                  <th className="px-1 py-1 text-right font-medium">Fill</th>
                  <th className="px-1 py-1 text-right font-medium">LTP</th>
                  <th className="px-2 py-1 text-right font-medium">P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((p) => (
                  <tr key={p.id} className="border-b border-border/40">
                    <td className="max-w-24 truncate px-2 py-1 font-sans" title={p.symbol}>
                      {p.symbol}
                    </td>
                    <td
                      className={cn(
                        'px-1 py-1 text-right',
                        p.action === 'BUY' ? 'text-emerald-500' : 'text-rose-500'
                      )}
                    >
                      {p.action === 'BUY' ? '+' : '-'}
                      {p.quantity}
                    </td>
                    <td className="px-1 py-1 text-right">{fmtPrice(p.fill_price)}</td>
                    <td className="px-1 py-1 text-right">{fmtPrice(p.ltp)}</td>
                    <td
                      className={cn(
                        'px-2 py-1 text-right font-semibold',
                        p.pnl >= 0 ? 'text-emerald-500' : 'text-rose-500'
                      )}
                    >
                      {fmtPnl(p.pnl)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </TabsContent>
      </Tabs>

      {/* Order ticket */}
      <div className="shrink-0 space-y-2 border-t bg-muted/20 p-2 text-xs">
        <div className="flex items-center gap-2">
          <Select value={selectedSymbol} onValueChange={onTicketSymbol}>
            <SelectTrigger className="h-8 min-w-0 flex-1 text-xs font-bold" aria-label="Symbol">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {ticketSymbols.map((s) => (
                <SelectItem key={s} value={s}>
                  {s}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <label htmlFor="replay-ticket-qty" className="text-muted-foreground">
            Qty
          </label>
          <Input
            id="replay-ticket-qty"
            type="number"
            min={1}
            value={qty}
            onChange={(e) => onQtyChange(e.target.value)}
            className="h-8 w-20 text-center font-mono text-xs font-bold"
          />
        </div>
        <div className="grid grid-cols-2 gap-2">
          <button
            type="button"
            onClick={() => onOrder('SELL')}
            className="flex cursor-pointer flex-col items-center justify-center rounded bg-rose-600 py-1.5 font-bold text-white transition-colors hover:bg-rose-700"
          >
            <span className="font-mono text-xs">{ltp ?? '—'}</span>
            <span className="text-[9px] tracking-wider uppercase opacity-90">SELL</span>
          </button>
          <button
            type="button"
            onClick={() => onOrder('BUY')}
            className="flex cursor-pointer flex-col items-center justify-center rounded bg-emerald-600 py-1.5 font-bold text-white transition-colors hover:bg-emerald-700"
          >
            <span className="font-mono text-xs">{ltp ?? '—'}</span>
            <span className="text-[9px] tracking-wider uppercase opacity-90">BUY</span>
          </button>
        </div>
        <div className="flex items-center justify-between font-mono text-[11px]">
          <span className={cn('font-bold', totalPnL >= 0 ? 'text-emerald-500' : 'text-rose-500')}>
            PnL: ₹{totalPnL.toFixed(2)} ({positions.length})
          </span>
          {positions.length > 0 && (
            <button
              type="button"
              onClick={onResetPositions}
              className="cursor-pointer text-[10px] text-rose-400 hover:underline"
              title="Reset trades"
            >
              Reset
            </button>
          )}
        </div>
      </div>
    </aside>
  )
}
