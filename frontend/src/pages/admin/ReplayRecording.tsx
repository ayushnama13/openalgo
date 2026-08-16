import { ArrowLeft, Database, History, Pin, X } from 'lucide-react'
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

interface AvailableEntry {
  symbol: string
  date: string
  row_count: number
}

interface AutoRecordSymbol {
  symbol: string
  exchange: string
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
  const [availableTapes, setAvailableTapes] = useState<AvailableEntry[]>([])
  const [autoSymbols, setAutoSymbols] = useState<AutoRecordSymbol[]>([])
  const [autoSymbolInput, setAutoSymbolInput] = useState('')
  const [autoExchange, setAutoExchange] = useState('NSE')
  const [isLoading, setIsLoading] = useState(false)

  const fetchData = useCallback(async () => {
    setIsLoading(true)
    try {
      const [aRes, arRes] = await Promise.all([
        fetch('/api/replay/available', { credentials: 'include' }),
        fetch('/api/replay/auto_record/list', { credentials: 'include' }),
      ])
      if (aRes.ok) {
        const aData = await aRes.json()
        setAvailableTapes(aData.data || [])
      }
      if (arRes.ok) {
        const arData = await arRes.json()
        setAutoSymbols(arData.symbols || [])
      } else {
        showToast.error(`Failed to load always-record list (${arRes.status})`)
      }
    } catch (_err) {
      showToast.error('Failed to load recording data')
    } finally {
      setIsLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchData()
  }, [fetchData])

  const handleAddAutoSymbols = async () => {
    const syms = autoSymbolInput
      .split(',')
      .map((s) => s.trim().toUpperCase())
      .filter(Boolean)
    if (syms.length === 0) {
      showToast.error('Please enter at least one symbol')
      return
    }
    try {
      const symbols = syms.map((symbol) => ({ symbol, exchange: autoExchange }))
      const res = await apiPost('/api/replay/auto_record/add', { symbols })
      if (res.ok) {
        showToast.success('Added to always-record list')
        setAutoSymbolInput('')
        fetchData()
      } else {
        const text = await res.text()
        showToast.error(`Failed to update always-record list (${res.status}): ${text.slice(0, 200)}`)
      }
    } catch (err) {
      showToast.error(`Error updating always-record list: ${err}`)
    }
  }

  const handleRemoveAutoSymbol = async (s: AutoRecordSymbol) => {
    try {
      const res = await apiPost('/api/replay/auto_record/remove', { symbols: [s] })
      if (res.ok) {
        fetchData()
      } else {
        showToast.error('Failed to remove symbol')
      }
    } catch {
      showToast.error('Error removing symbol')
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
              Manage the always-record symbol list and view recorded Parquet tape archives.
            </p>
          </div>
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <Pin className="h-4 w-4 text-amber-500" /> Always-Record List
          </CardTitle>
          <CardDescription>
            Symbols saved here are re-armed automatically every trading day — no need to push Start
            Recording each morning. Managed once, applies forever until removed.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex gap-2">
            <Select value={autoExchange} onValueChange={setAutoExchange}>
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
              value={autoSymbolInput}
              onChange={(e) => setAutoSymbolInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') handleAddAutoSymbols()
              }}
              className="font-mono flex-1"
            />
            <Button onClick={handleAddAutoSymbols} className="bg-amber-600 hover:bg-amber-700">
              Add
            </Button>
          </div>
          {isLoading ? (
            <div className="flex justify-center py-4">
              <div className="animate-spin rounded-full h-5 w-5 border-b-2 border-primary" />
            </div>
          ) : autoSymbols.length > 0 ? (
            <div className="flex flex-wrap gap-1.5">
              {autoSymbols.map((s) => (
                <span
                  key={`${s.exchange}:${s.symbol}`}
                  className="inline-flex items-center gap-1 px-2 py-1 rounded bg-muted font-mono text-xs border"
                >
                  <span className="font-bold text-primary">{s.exchange}</span>:{s.symbol}
                  <button
                    type="button"
                    onClick={() => handleRemoveAutoSymbol(s)}
                    className="ml-1 text-muted-foreground hover:text-destructive"
                  >
                    <X className="h-3 w-3" />
                  </button>
                </span>
              ))}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">
              No always-record symbols set. Add some above.
            </p>
          )}
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
  )
}
