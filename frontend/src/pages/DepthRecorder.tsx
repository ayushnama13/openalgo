import { Database, RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  deleteRecorderConfig,
  fetchRecorderStatus,
  fetchStats,
  fetchTicks,
  pruneOldTicks,
  type RecorderStatus,
  type StatsData,
  setDepthLevel,
  startRecorder,
  stopRecorder,
  type Tick,
} from '@/api/depth-recorder'
import { RecorderDetail } from '@/components/depth-recorder/RecorderDetail'
import {
  fmtClock,
  type RecorderItem,
  RecorderList,
  recorderKey,
} from '@/components/depth-recorder/RecorderList'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { showToast } from '@/utils/toast'

export default function DepthRecorder() {
  const [statuses, setStatuses] = useState<RecorderStatus[]>([])
  const [stats, setStats] = useState<StatsData | null>(null)
  const [latestTicks, setLatestTicks] = useState<Record<string, Tick>>({})
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [pruneOpen, setPruneOpen] = useState(false)
  const [pruneDays, setPruneDays] = useState('30')
  const [pruning, setPruning] = useState(false)
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
        setLatestTicks((prev) => ({ ...prev, [recorderKey(sym, exch)]: tick }))
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

  // Every configured recorder, then symbols that only have stored ticks.
  const items = useMemo<RecorderItem[]>(() => {
    const list: RecorderItem[] = statuses.map((rec) => ({
      key: recorderKey(rec.symbol, rec.exchange),
      symbol: rec.symbol,
      exchange: rec.exchange,
      rec,
    }))
    const seen = new Set(list.map((item) => item.key))
    for (const s of stats?.symbols ?? []) {
      const key = recorderKey(s.symbol, s.exchange)
      if (seen.has(key)) continue
      seen.add(key)
      list.push({ key, symbol: s.symbol, exchange: s.exchange, rec: null })
    }
    return list
  }, [statuses, stats])

  const selected = items.find((item) => item.key === selectedKey) ?? items[0] ?? null
  const selectedSymbol = selected?.symbol
  const selectedExchange = selected?.exchange
  const selectedRecording = selected?.rec?.status === 'recording'
  // Changes with every status poll while the selected recorder is live.
  const selectedLastTick = selected?.rec?.last_tick_time ?? null

  // Only the selected symbol's ladder is on screen, so only its latest tick is
  // fetched: once when it is picked, then on each new tick while it records.
  // biome-ignore lint/correctness/useExhaustiveDependencies: selectedLastTick is the refresh trigger
  useEffect(() => {
    if (!selectedSymbol || !selectedExchange) return
    fetchLatestTickCb(selectedSymbol, selectedExchange)
  }, [selectedSymbol, selectedExchange, selectedRecording, selectedLastTick, fetchLatestTickCb])

  const handleStart = async (sym: string, exch: string, depthLevel: number) => {
    if (!sym) {
      showToast.error('Enter a symbol')
      return
    }
    try {
      const d = await startRecorder(sym, exch, depthLevel)
      if (d.status === 'started' || d.status === 'already_recording') {
        showToast.success(`Recording started for ${sym} · ${exch} (${depthLevel}D)`)
        setSelectedKey(recorderKey(sym, exch))
      } else {
        showToast.error(d.message || 'Failed to start')
      }
      fetchStatusesCb()
    } catch {
      showToast.error('Request failed')
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

  // The prune endpoint only accepts a whole number of days >= 1.
  const pruneDaysValue = /^\d+$/.test(pruneDays.trim()) ? Number(pruneDays.trim()) : 0
  const pruneDaysValid = pruneDaysValue >= 1

  const handlePrune = async () => {
    if (!pruneDaysValid || pruning) return
    setPruning(true)
    try {
      const res = await pruneOldTicks(pruneDaysValue)
      if (res.status === 'success') {
        const deleted = res.deleted ?? 0
        showToast.success(
          deleted > 0
            ? `Deleted ${deleted.toLocaleString('en-IN')} ticks older than ${pruneDaysValue} days`
            : `No ticks older than ${pruneDaysValue} days`
        )
        setPruneOpen(false)
        fetchStatsCb()
      } else {
        showToast.error(res.message || 'Failed to delete old depth data')
      }
    } catch {
      showToast.error('Failed to delete old depth data')
    } finally {
      setPruning(false)
    }
  }

  return (
    <div className="space-y-4 py-6">
      {/* Title and totals */}
      <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-bold">
            <Database className="h-6 w-6 text-cyan-400" />
            Depth Recorder
          </h1>
          <p className="mt-0.5 text-sm text-muted-foreground">
            Stream &amp; store live market depth ticks to SQLite, then analyse them
          </p>
        </div>

        <dl className="flex flex-wrap items-center gap-x-5 gap-y-1 text-xs">
          {[
            {
              label: 'Total ticks',
              value: stats ? stats.total_ticks.toLocaleString('en-IN') : '—',
            },
            { label: 'Symbols', value: stats ? stats.symbols.length : '—' },
            { label: 'Recording', value: recordingCount },
            { label: 'Latest tick', value: fmtClock(stats?.latest_tick_time) },
          ].map(({ label, value }) => (
            <div key={label}>
              <dt className="text-[10px] tracking-wider text-muted-foreground uppercase">
                {label}
              </dt>
              <dd className="font-mono text-base font-bold tabular-nums">{value}</dd>
            </div>
          ))}
        </dl>

        <div className="ml-auto flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => setPruneOpen(true)}>
            Delete old data...
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              fetchStatusesCb()
              fetchStatsCb()
            }}
          >
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
            Refresh
          </Button>
        </div>
      </div>

      {/* Recorders on the left, the selected one on the right */}
      <div className="grid items-start gap-4 lg:grid-cols-[19rem_minmax(0,1fr)]">
        <RecorderList
          items={items}
          selectedKey={selected?.key ?? null}
          onSelect={setSelectedKey}
          onStart={handleStart}
        />
        {selected ? (
          <RecorderDetail
            item={selected}
            latestTick={latestTicks[selected.key] ?? null}
            onStart={(depth) => handleStart(selected.symbol, selected.exchange, depth)}
            onStop={() => handleStop(selected.symbol, selected.exchange)}
            onDelete={() => handleDelete(selected.symbol, selected.exchange)}
            onUpdateDepth={(depth) => handleUpdateDepth(selected.symbol, selected.exchange, depth)}
          />
        ) : (
          <div className="flex flex-col items-center justify-center gap-3 rounded-xl border border-dashed border-border py-16 text-muted-foreground">
            <Database className="h-12 w-12 opacity-20" />
            <p className="text-sm">No recorders yet. Add one on the left.</p>
          </div>
        )}
      </div>

      <Dialog open={pruneOpen} onOpenChange={(open) => !pruning && setPruneOpen(open)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Delete old depth data</DialogTitle>
            <DialogDescription>
              Removes recorded ticks for every symbol from before the chosen number of days ago.
              Today's recording is never touched.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <label htmlFor="depth-prune-days">Delete ticks older than</label>
            <Input
              id="depth-prune-days"
              type="number"
              min={1}
              step={1}
              inputMode="numeric"
              value={pruneDays}
              onChange={(e) => setPruneDays(e.target.value)}
              disabled={pruning}
              className="h-8 w-20 font-mono"
            />
            <span>days</span>
          </div>
          {!pruneDaysValid && (
            <p className="text-xs text-rose-400">Enter a whole number of days, 1 or more.</p>
          )}
          <p className="text-sm text-rose-400">This cannot be undone.</p>
          <DialogFooter>
            <Button variant="outline" disabled={pruning} onClick={() => setPruneOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={!pruneDaysValid || pruning}
              onClick={handlePrune}
            >
              {pruning ? 'Deleting...' : 'Delete'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
