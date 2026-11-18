import { Play } from 'lucide-react'
import { useState } from 'react'
import type { RecorderStatus } from '@/api/depth-recorder'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { cn } from '@/lib/utils'

export const DEPTH_EXCHANGES = ['NSE', 'BSE', 'NFO', 'BFO', 'MCX', 'CDS']
export const DEPTH_LEVELS = [5, 20, 50, 200]

/** A symbol on the page: a configured recorder, or stored ticks with no recorder (rec = null). */
export interface RecorderItem {
  key: string
  symbol: string
  exchange: string
  rec: RecorderStatus | null
}

export const recorderKey = (symbol: string, exchange: string) => `${exchange}:${symbol}`

export type OrbStatus = 'success' | 'warning' | 'error' | 'idle'

export function statusColor(status: string | undefined): OrbStatus {
  if (status === 'recording') return 'success'
  if (status === 'reconnecting' || status === 'connecting' || status === 'starting')
    return 'warning'
  if (status === 'waiting_market_hours') return 'warning'
  if (status === 'subscribe_failed') return 'error'
  return 'idle'
}

export function Orb({ status }: { status: OrbStatus }) {
  return (
    <div className="relative flex-shrink-0">
      <div
        className={cn(
          'h-2.5 w-2.5 rounded-full',
          status === 'success' && 'bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.9)]',
          status === 'warning' && 'bg-amber-400 shadow-[0_0_6px_rgba(251,191,36,0.9)]',
          status === 'error' && 'bg-rose-400 shadow-[0_0_6px_rgba(251,113,133,0.9)]',
          status === 'idle' && 'bg-muted-foreground/40'
        )}
      />
      {status !== 'idle' && (
        <div
          className={cn(
            'absolute inset-0 animate-ping rounded-full opacity-50',
            status === 'success' && 'bg-emerald-400',
            status === 'warning' && 'bg-amber-400',
            status === 'error' && 'bg-rose-400'
          )}
        />
      )}
    </div>
  )
}

export function fmtClock(iso: string | null | undefined): string {
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

interface RecorderListProps {
  items: RecorderItem[]
  selectedKey: string | null
  onSelect(key: string): void
  /** Start a recorder; resolves once the request has finished either way. */
  onStart(symbol: string, exchange: string, depthLevel: number): Promise<void>
  className?: string
}

/** Left column of the depth recorder: the add form and every tracked symbol. */
export function RecorderList({
  items,
  selectedKey,
  onSelect,
  onStart,
  className,
}: RecorderListProps) {
  const [symbol, setSymbol] = useState('RELIANCE')
  const [exchange, setExchange] = useState('NSE')
  const [depthLevel, setDepthLevel] = useState(5)
  const [starting, setStarting] = useState(false)

  const start = async () => {
    if (starting) return
    setStarting(true)
    try {
      await onStart(symbol.trim(), exchange, depthLevel)
    } finally {
      setStarting(false)
    }
  }

  return (
    <div className={cn('rounded-xl border border-border bg-card/60 backdrop-blur', className)}>
      {/* Add recorder */}
      <div className="space-y-2 border-b p-3">
        <div className="text-[10px] tracking-wider text-muted-foreground uppercase">
          Add recorder
        </div>
        <div className="flex gap-2">
          <Input
            id="depth-symbol-input"
            aria-label="Symbol"
            className="h-8 min-w-0 flex-1 font-mono text-xs uppercase"
            placeholder="RELIANCE"
            value={symbol}
            onChange={(e) => setSymbol(e.target.value.toUpperCase())}
            onKeyDown={(e) => e.key === 'Enter' && start()}
          />
          <Select value={exchange} onValueChange={setExchange}>
            <SelectTrigger
              id="depth-exchange-select"
              aria-label="Exchange"
              className="h-8 w-[5.5rem] text-xs"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {DEPTH_EXCHANGES.map((ex) => (
                <SelectItem key={ex} value={ex}>
                  {ex}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex flex-1 items-center rounded border bg-muted/40 p-0.5 text-xs">
            {DEPTH_LEVELS.map((lvl) => (
              <button
                key={lvl}
                type="button"
                onClick={() => setDepthLevel(lvl)}
                aria-pressed={depthLevel === lvl}
                className={cn(
                  'flex-1 rounded px-1.5 py-1 text-[11px] font-medium transition-colors',
                  depthLevel === lvl
                    ? 'bg-cyan-500 font-semibold text-black shadow-sm'
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
            aria-label="Custom depth level"
            className="h-8 w-16 font-mono text-xs"
            value={depthLevel}
            onChange={(e) => {
              const v = parseInt(e.target.value, 10)
              setDepthLevel(Number.isNaN(v) ? 5 : Math.min(200, Math.max(5, v)))
            }}
            title="Custom depth level (5-200)"
          />
        </div>
        <Button
          id="depth-start-btn"
          size="sm"
          onClick={start}
          disabled={starting}
          className="h-8 w-full bg-cyan-500 font-semibold text-black hover:bg-cyan-400"
        >
          <Play className="mr-1.5 h-3.5 w-3.5" />
          {starting ? 'Starting…' : 'Start Recording'}
        </Button>
        <p className="text-[10px] leading-snug text-muted-foreground">
          Records only during market hours (9:15–15:30 IST, weekdays) and reconnects on its own.
        </p>
      </div>

      {/* Tracked symbols */}
      {items.length === 0 ? (
        <p className="px-3 py-8 text-center text-xs text-muted-foreground">
          No recorders yet. Add one above.
        </p>
      ) : (
        <ul className="max-h-[28rem] divide-y divide-border/50 overflow-y-auto">
          {items.map((item) => {
            const selected = item.key === selectedKey
            return (
              <li key={item.key}>
                <button
                  type="button"
                  onClick={() => onSelect(item.key)}
                  aria-pressed={selected}
                  className={cn(
                    'flex w-full items-center gap-2.5 border-l-2 border-transparent px-3 py-2 text-left transition-colors hover:bg-muted/50',
                    selected && 'border-cyan-400 bg-muted/60'
                  )}
                >
                  <Orb status={statusColor(item.rec?.status)} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-semibold">
                      {item.symbol}
                      <span className="ml-1 text-xs font-normal text-muted-foreground">
                        {item.exchange}
                      </span>
                    </span>
                    <span className="block truncate text-[11px] text-muted-foreground">
                      {item.rec
                        ? `${item.rec.status} · ${item.rec.ticks_written.toLocaleString('en-IN')} saved`
                        : 'stored ticks, no recorder'}
                    </span>
                  </span>
                  {item.rec?.last_tick_time && (
                    <span className="font-mono text-[10px] text-muted-foreground tabular-nums">
                      {fmtClock(item.rec.last_tick_time)}
                    </span>
                  )}
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
