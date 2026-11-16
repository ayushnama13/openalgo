import { ChevronDown, Database, History, LayoutGrid, Play } from 'lucide-react'
import { Link } from 'react-router'
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
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { cn } from '@/lib/utils'

export interface ClockState {
  current_time: string
  start_time: string
  end_time: string
  is_playing: boolean
  speed: number
  session_date: string
  watchlist: string[]
}

export interface AvailableEntry {
  symbol: string
  date: string
  row_count: number
}

export interface LayoutPreset {
  id: string
  label: string
  cols: string
  rows: string
  areas: string
  cells: string[]
}

/** Session settings as typed; they take effect only when applied. */
export interface SessionDraft {
  date: string
  start: string
  end: string
  watchlist: string
}

const SPEEDS = [0.5, 1, 2, 5, 10, 20, 60]
const QUICK_JUMPS = ['09:15:00', '10:30:00', '12:00:00', '14:30:00', '15:30:00']

/** "2026-08-10T09:15:00+05:30" -> "09:15:00". Falls back to the input. */
export function timePart(iso: string | undefined): string {
  if (!iso) return ''
  return iso.match(/T(\d{2}:\d{2}:\d{2})/)?.[1] ?? iso
}

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

interface ReplayTopBarProps {
  clock: ClockState | null
  /** Scrubber position, 0-100. */
  progress: number
  onControl(action: string, payload?: object): void
  onSeekPercent(pct: number): void
  onQuickJump(time: string): void
  draft: SessionDraft
  /** True when the draft differs from what was last applied. */
  draftDirty: boolean
  onDraftChange(patch: Partial<SessionDraft>): void
  onApplySession(): void
  layouts: LayoutPreset[]
  layoutId: string
  onLayoutChange(id: string): void
  available: AvailableEntry[] | null
  onUseAvailable(entry: AvailableEntry): void
}

/**
 * Replay header: applied session, playback controls and the timeline. The
 * session inputs live in a popover; the bar itself always shows what the
 * server clock is running, never the unapplied draft.
 */
export function ReplayTopBar({
  clock,
  progress,
  onControl,
  onSeekPercent,
  onQuickJump,
  draft,
  draftDirty,
  onDraftChange,
  onApplySession,
  layouts,
  layoutId,
  onLayoutChange,
  available,
  onUseAvailable,
}: ReplayTopBarProps) {
  const speeds = clock && !SPEEDS.includes(clock.speed) ? [...SPEEDS, clock.speed] : SPEEDS

  return (
    <div className="shrink-0 border-b bg-background/95 px-3 py-1.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        {/* Applied session */}
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold">Replay</span>
          {clock && (
            <>
              <Badge variant={clock.is_playing ? 'default' : 'secondary'} className="gap-1.5">
                <span
                  className={cn(
                    'h-2 w-2 rounded-full',
                    clock.is_playing ? 'animate-pulse bg-emerald-400' : 'bg-amber-400'
                  )}
                />
                {clock.is_playing ? 'Playing' : 'Paused'}
              </Badge>
              <span className="font-mono text-xs text-muted-foreground" title="Session date">
                {clock.session_date}
              </span>
            </>
          )}
          <Popover>
            <PopoverTrigger asChild>
              <Button variant="outline" size="sm" className="h-7 gap-1.5 text-xs">
                Session
                {draftDirty && (
                  <span className="rounded bg-amber-500/20 px-1 text-[10px] font-medium text-amber-500">
                    not applied
                  </span>
                )}
                <ChevronDown className="h-3.5 w-3.5 opacity-60" />
              </Button>
            </PopoverTrigger>
            <PopoverContent align="start" className="w-80 space-y-3 text-xs">
              <div className="grid grid-cols-[4.5rem_1fr] items-center gap-x-2 gap-y-2">
                <label htmlFor="replay-session-date" className="text-muted-foreground">
                  Date
                </label>
                <Input
                  id="replay-session-date"
                  className="h-7 font-mono text-xs"
                  placeholder="YYYY-MM-DD"
                  value={draft.date}
                  onChange={(e) => onDraftChange({ date: e.target.value })}
                />
                <label htmlFor="replay-session-start" className="text-muted-foreground">
                  Start
                </label>
                <Input
                  id="replay-session-start"
                  className="h-7 font-mono text-xs"
                  placeholder="HH:MM:SS"
                  value={draft.start}
                  onChange={(e) => onDraftChange({ start: e.target.value })}
                />
                <label htmlFor="replay-session-end" className="text-muted-foreground">
                  End
                </label>
                <Input
                  id="replay-session-end"
                  className="h-7 font-mono text-xs"
                  placeholder="HH:MM:SS"
                  value={draft.end}
                  onChange={(e) => onDraftChange({ end: e.target.value })}
                />
                <label htmlFor="replay-session-watchlist" className="text-muted-foreground">
                  Watchlist
                </label>
                <Input
                  id="replay-session-watchlist"
                  className="h-7 font-mono text-xs"
                  placeholder="NIFTY, RELIANCE"
                  value={draft.watchlist}
                  onChange={(e) => onDraftChange({ watchlist: e.target.value })}
                />
              </div>
              <p className="text-[11px] text-muted-foreground">
                Comma-separated symbols. Applying restarts the replay clock for this session.
              </p>
              <Button size="sm" className="h-7 w-full text-xs" onClick={onApplySession}>
                Apply Session
              </Button>
            </PopoverContent>
          </Popover>
        </div>

        {/* Playback */}
        {clock && (
          <div className="flex items-center gap-1.5">
            {clock.is_playing ? (
              <Button
                variant="outline"
                size="sm"
                className="h-7 text-xs"
                onClick={() => onControl('pause')}
              >
                Pause
              </Button>
            ) : (
              <Button
                size="sm"
                className="h-7 bg-emerald-600 text-xs hover:bg-emerald-700"
                onClick={() => onControl('play')}
              >
                <Play className="mr-1 h-3 w-3" /> Play
              </Button>
            )}
            <Button
              variant="outline"
              size="sm"
              className="h-7 text-xs"
              onClick={() => onControl('step', { seconds: 1 })}
            >
              +1s
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="h-7 text-xs"
              onClick={() => onControl('step', { seconds: 5 })}
            >
              +5s
            </Button>
            <Select
              value={String(clock.speed)}
              onValueChange={(v) => onControl('speed', { speed: Number(v) })}
            >
              <SelectTrigger className="h-7 w-[4.5rem] text-xs" aria-label="Playback speed">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {speeds.map((spd) => (
                  <SelectItem key={spd} value={String(spd)}>
                    {spd}x
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <span
              className="pl-1 font-mono text-sm font-semibold text-primary tabular-nums"
              title={clock.current_time}
            >
              {timePart(clock.current_time)}
            </span>
          </div>
        )}

        {/* Tapes and chart layout */}
        <div className="ml-auto flex items-center gap-2">
          <Dialog>
            <DialogTrigger asChild>
              <Button variant="outline" size="sm" className="h-7 gap-1.5 text-xs">
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
                <div className="space-y-2 rounded-lg border bg-muted/30 p-3">
                  <div className="font-semibold">Automatic Recording Active</div>
                  <p className="text-[11px] text-muted-foreground">
                    All watchlist symbols are automatically recorded second-by-second when a session
                    is configured. To change recording settings or targets, visit the Admin panel.
                  </p>
                  <Button asChild size="sm" className="h-7 w-full text-xs">
                    <Link to="/admin/replay">Open Recording Settings (Admin)</Link>
                  </Button>
                </div>

                <div className="space-y-2 border-t pt-3">
                  <div className="font-semibold text-muted-foreground">
                    Available Recorded Tapes:
                  </div>
                  {available && available.length > 0 ? (
                    <div className="max-h-48 space-y-1 overflow-y-auto">
                      {available.map((entry) => (
                        <button
                          key={`${entry.symbol}:${entry.date}`}
                          type="button"
                          onClick={() => onUseAvailable(entry)}
                          className="flex w-full items-center justify-between rounded border px-2 py-1.5 text-left font-mono text-xs hover:bg-muted/40"
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
              <Button variant="outline" size="sm" className="h-7 gap-1.5">
                <LayoutGrid className="h-4 w-4" />
                <span className="text-xs font-medium">Layout</span>
                <ChevronDown className="h-3.5 w-3.5 opacity-60" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-56">
              <div className="grid grid-cols-4 gap-1 p-1">
                {layouts.map((l) => (
                  <DropdownMenuItem
                    key={l.id}
                    onSelect={() => onLayoutChange(l.id)}
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

      {/* Timeline */}
      {clock && (
        <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
          <span className="font-mono text-[10px] text-muted-foreground">
            {timePart(clock.start_time).slice(0, 5)}
          </span>
          <input
            type="range"
            min="0"
            max="100"
            step="0.1"
            aria-label="Session timeline"
            value={progress}
            onChange={(e) => onSeekPercent(parseFloat(e.target.value))}
            className="h-1.5 min-w-40 flex-1 cursor-pointer rounded bg-muted accent-primary"
          />
          <span className="font-mono text-[10px] text-muted-foreground">
            {timePart(clock.end_time).slice(0, 5)}
          </span>
          <div className="flex items-center gap-1">
            <span className="mr-1 text-[10px] text-muted-foreground">Jump:</span>
            {QUICK_JUMPS.map((time) => (
              <Button
                key={time}
                size="sm"
                variant="ghost"
                className="h-6 px-1.5 text-[10px]"
                onClick={() => onQuickJump(time)}
              >
                {time.slice(0, 5)}
              </Button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
