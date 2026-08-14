import { ArrowLeft, Database, History, Play, Square } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { showToast } from '@/utils/toast'

interface RecordingTarget {
  symbol: string
  exchange: string
  date: string
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

export default function ReplayRecordingPage() {
  const [sessionDate, setSessionDate] = useState(() => new Date().toISOString().split('T')[0])
  const [symbolInput, setSymbolInput] = useState('')
  const [exchange, setExchange] = useState('NSE')
  const [targets, setTargets] = useState<RecordingTarget[]>([])
  const [availableTapes, setAvailableTapes] = useState<AvailableEntry[]>([])
  const [isLoading, setIsLoading] = useState(false)

  const fetchData = useCallback(async () => {
    setIsLoading(true)
    try {
      const [tRes, aRes] = await Promise.all([
        fetch(`/api/replay/record/status?date=${encodeURIComponent(sessionDate)}`, { credentials: 'include' }),
        fetch('/api/replay/available', { credentials: 'include' }),
      ])
      if (tRes.ok) {
        const tData = await tRes.json()
        setTargets(tData.targets || [])
      }
      if (aRes.ok) {
        const aData = await aRes.json()
        setAvailableTapes(aData.data || [])
      }
    } catch (_err) {
      showToast.error('Failed to load recording data')
    } finally {
      setIsLoading(false)
    }
  }, [sessionDate])

  useEffect(() => {
    fetchData()
  }, [fetchData])

  const handleStartRecording = async () => {
    const syms = symbolInput
      .split(',')
      .map((s) => s.trim().toUpperCase())
      .filter(Boolean)
    if (syms.length === 0) {
      showToast.error('Please enter at least one symbol')
      return
    }
    try {
      const symbols = syms.map((symbol) => ({ symbol, exchange }))
      const res = await apiPost('/api/replay/record/start', { symbols, date: sessionDate })
      if (res.ok) {
        const data = await res.json()
        showToast.success(`Started recording ${data.count} symbol(s) for ${data.date}`)
        setSymbolInput('')
        fetchData()
      } else {
        showToast.error('Failed to start recording')
      }
    } catch {
      showToast.error('Error starting recording')
    }
  }

  const handleStopRecording = async () => {
    try {
      const res = await apiPost('/api/replay/record/stop', { date: sessionDate })
      if (res.ok) {
        showToast.info('Stopped all active recordings for date')
        fetchData()
      } else {
        showToast.error('Failed to stop recording')
      }
    } catch {
      showToast.error('Error stopping recording')
    }
  }

  const handlePrune = async () => {
    try {
      const res = await apiPost('/api/replay/prune', { days_to_keep: 7 })
      if (res.ok) {
        const data = await res.json()
        showToast.success(`Pruned ${data.removed_files} tape archive(s) older than 7 days (weekly retention)`)
        fetchData()
      } else {
        showToast.error('Failed to prune old tapes')
      }
    } catch {
      showToast.error('Error pruning old tapes')
    }
  }

  return (
    <div className="py-6 space-y-6 container mx-auto px-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Button variant="ghost" size="icon" asChild>
            <Link to="/admin">
              <ArrowLeft className="h-5 w-5" />
            </Link>
          </Button>
          <div>
            <h1 className="text-2xl font-bold flex items-center gap-2">
              <History className="h-6 w-6 text-primary" /> Replay Tape Recording Management
            </h1>
            <p className="text-muted-foreground mt-1">
              Configure active second-by-second live market tick recording targets and view recorded Parquet tape archives.
            </p>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* Active Recording Target Control */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base flex items-center gap-2">
              <Play className="h-4 w-4 text-emerald-500" /> Active Recording Targets
            </CardTitle>
            <CardDescription>
              Symbols added here are recorded tick-by-tick in real-time as market data arrives.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <label className="text-xs text-muted-foreground">Recording Date</label>
              <Input
                type="date"
                value={sessionDate}
                onChange={(e) => setSessionDate(e.target.value)}
                className="font-mono"
              />
            </div>

            <div className="space-y-2">
              <label className="text-xs text-muted-foreground">Add Symbols (Comma separated)</label>
              <div className="flex gap-2">
                <Select value={exchange} onValueChange={setExchange}>
                  <SelectTrigger className="w-28">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="NSE">NSE</SelectItem>
                    <SelectItem value="BSE">BSE</SelectItem>
                    <SelectItem value="NFO">NFO</SelectItem>
                    <SelectItem value="NSE_INDEX">NSE_INDEX</SelectItem>
                    <SelectItem value="MCX">MCX</SelectItem>
                  </SelectContent>
                </Select>
                <Input
                  placeholder="e.g. NIFTY, RELIANCE, SBIN"
                  value={symbolInput}
                  onChange={(e) => setSymbolInput(e.target.value)}
                  className="font-mono flex-1"
                />
              </div>
            </div>

            <div className="flex gap-2 pt-2">
              <Button onClick={handleStartRecording} className="flex-1 bg-emerald-600 hover:bg-emerald-700">
                Start Recording
              </Button>
              <Button variant="destructive" onClick={handleStopRecording} className="gap-1.5">
                <Square className="h-3.5 w-3.5" /> Stop All
              </Button>
            </div>

            <div className="border-t pt-4 space-y-2">
              <div className="text-xs font-semibold text-muted-foreground">
                Currently Active Targets for {sessionDate} ({targets.length}):
              </div>
              {targets.length > 0 ? (
                <div className="flex flex-wrap gap-1.5 max-h-40 overflow-y-auto">
                  {targets.map((t) => (
                    <span
                      key={`${t.exchange}:${t.symbol}`}
                      className="inline-flex items-center gap-1 px-2 py-1 rounded bg-muted font-mono text-xs border"
                    >
                      <span className="font-bold text-primary">{t.exchange}</span>:{t.symbol}
                    </span>
                  ))}
                </div>
              ) : (
                <p className="text-xs text-muted-foreground">No active recording targets for this date.</p>
              )}
            </div>
          </CardContent>
        </Card>

        {/* Recorded Parquet Archives */}
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <div>
                <CardTitle className="text-base flex items-center gap-2">
                  <Database className="h-4 w-4 text-cyan-500" /> Recorded Tape Archives ({availableTapes.length})
                </CardTitle>
                <CardDescription>
                  Parquet files stored on disk (weekly retention: 7 days max).
                </CardDescription>
              </div>
              <Button variant="outline" size="sm" className="h-8 text-xs text-amber-500 border-amber-500/30" onClick={handlePrune}>
                Prune &gt; 7 Days
              </Button>
            </div>
          </CardHeader>
          <CardContent>
            {isLoading ? (
              <div className="flex justify-center py-8">
                <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-primary" />
              </div>
            ) : availableTapes.length > 0 ? (
              <div className="max-h-80 overflow-y-auto rounded-md border border-slate-800">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Symbol</TableHead>
                      <TableHead>Date</TableHead>
                      <TableHead className="text-right">Rows</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {availableTapes.map((entry) => (
                      <TableRow key={`${entry.symbol}:${entry.date}`}>
                        <TableCell className="font-mono font-medium">{entry.symbol}</TableCell>
                        <TableCell className="font-mono text-xs text-muted-foreground">{entry.date}</TableCell>
                        <TableCell className="font-mono text-right">{entry.row_count.toLocaleString()}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground py-8 text-center">No recorded tape archives found.</p>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
