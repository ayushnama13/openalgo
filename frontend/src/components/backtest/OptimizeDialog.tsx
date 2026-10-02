import { Loader2, SlidersHorizontal } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router'
import { fetchCSRFToken } from '@/api/client'
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
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { showToast } from '@/utils/toast'

export const OBJECTIVES = [
  { value: 'sharpe_ratio', label: 'Sharpe ratio' },
  { value: 'sortino_ratio', label: 'Sortino ratio' },
  { value: 'total_return_pct', label: 'Total return %' },
  { value: 'cagr_pct', label: 'CAGR %' },
  { value: 'calmar', label: 'Calmar (CAGR / max drawdown)' },
  { value: 'profit_factor', label: 'Profit factor' },
  { value: 'expectancy', label: 'Expectancy per trade' },
  { value: 'win_rate_pct', label: 'Win rate %' },
]

const EXAMPLE = `{
  "fast": {"min": 5, "max": 20, "step": 5},
  "slow": [20, 30, 50]
}`

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Returns the run fields from the Backtest form, or null (after toasting) when invalid. */
  buildRunPayload: () => Record<string, unknown> | null
}

/** Number of combinations a space expands to, or null when the shape is wrong. */
function countCombos(space: unknown): number | null {
  if (!space || typeof space !== 'object' || Array.isArray(space)) return null
  let total = 1
  for (const spec of Object.values(space as Record<string, unknown>)) {
    if (Array.isArray(spec)) {
      if (!spec.length) return null
      total *= spec.length
    } else if (spec && typeof spec === 'object' && 'min' in spec && 'max' in spec) {
      const { min, max, step = 1 } = spec as { min: number; max: number; step?: number }
      if (!(step > 0) || max < min) return null
      total *= Math.floor((max - min) / step + 1e-9) + 1
    } else {
      return null
    }
  }
  return total
}

export default function OptimizeDialog({ open, onOpenChange, buildRunPayload }: Props) {
  const navigate = useNavigate()
  const [kind, setKind] = useState('sweep')
  const [spaceText, setSpaceText] = useState(EXAMPLE)
  const [method, setMethod] = useState('grid')
  const [maxTrials, setMaxTrials] = useState(100)
  const [objective, setObjective] = useState('sharpe_ratio')
  const [minTrades, setMinTrades] = useState(5)
  const [oosPct, setOosPct] = useState(25)
  const [trainPct, setTrainPct] = useState(60)
  const [testPct, setTestPct] = useState(20)
  const [windows, setWindows] = useState(5)
  const [submitting, setSubmitting] = useState(false)

  const parsed = useMemo(() => {
    try {
      return { space: JSON.parse(spaceText) as unknown, error: null as string | null }
    } catch {
      return { space: null, error: 'Not valid JSON' }
    }
  }, [spaceText])
  const combos = parsed.space ? countCombos(parsed.space) : null
  const limit = Math.min(maxTrials, 300)
  const tooBig = method === 'grid' && combos !== null && combos > limit

  const submit = async () => {
    if (combos === null) {
      showToast.error('Parameter space must map names to a list or {min, max, step}')
      return
    }
    const run = buildRunPayload()
    if (!run) return
    setSubmitting(true)
    try {
      const csrf = await fetchCSRFToken()
      const optimize: Record<string, unknown> = {
        kind,
        param_space: parsed.space,
        method,
        max_trials: maxTrials,
        objective,
        min_trades: minTrades,
      }
      if (kind === 'sweep') optimize.oos_pct = oosPct > 0 ? oosPct : null
      else Object.assign(optimize, { train_pct: trainPct, test_pct: testPct, n_windows: windows })
      const { intervals: _ignored, ...rest } = run
      const res = await fetch('/backtest/api/optimize', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
        body: JSON.stringify({ ...rest, optimize }),
      })
      const body = await res.json()
      if (body.status !== 'success') throw new Error(body.message || 'Submit failed')
      showToast.success('Optimization queued')
      onOpenChange(false)
      navigate(`/backtest/optimizations/${body.opt_id}`)
    } catch (e) {
      showToast.error(e instanceof Error ? e.message : 'Submit failed')
    } finally {
      setSubmitting(false)
    }
  }

  const num = (label: string, value: number, set: (n: number) => void, min = 0, max?: number) => (
    <div className="space-y-1.5">
      <Label>{label}</Label>
      <Input
        type="number"
        min={min}
        max={max}
        value={value}
        onChange={(e) => set(Number(e.target.value))}
      />
    </div>
  )

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <SlidersHorizontal className="h-4 w-4" /> Optimize strategy parameters
          </DialogTitle>
          <DialogDescription>
            Uses the symbols, dates, costs and risk settings currently on this page and the first
            selected timeframe. The strategy reads parameters from <code>self.params[...]</code>{' '}
            (event strategy) or <code>PARAMS[...]</code> (signal strategy).
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label>Study</Label>
              <Select value={kind} onValueChange={setKind}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="sweep">Parameter sweep</SelectItem>
                  <SelectItem value="walk_forward">Walk-forward analysis</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label>Rank by</Label>
              <Select value={objective} onValueChange={setObjective}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {OBJECTIVES.map((o) => (
                    <SelectItem key={o.value} value={o.value}>
                      {o.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="space-y-1.5">
            <Label>Parameter space (JSON)</Label>
            <textarea
              className="w-full rounded-md border border-slate-800 bg-slate-950 p-2 font-mono text-xs"
              rows={6}
              value={spaceText}
              onChange={(e) => setSpaceText(e.target.value)}
            />
            <p
              className={`text-[11px] ${parsed.error || combos === null ? 'text-red-400' : 'text-muted-foreground'}`}
            >
              {parsed.error ??
                (combos === null
                  ? 'Each parameter needs a list of values or {"min", "max", "step"}'
                  : `${combos} combination${combos === 1 ? '' : 's'}${tooBig ? ` exceeds the ${limit} limit: narrow it or switch to random` : ''}`)}
            </p>
          </div>

          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <div className="space-y-1.5">
              <Label>Search</Label>
              <Select value={method} onValueChange={setMethod}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="grid">Grid</SelectItem>
                  <SelectItem value="random">Random</SelectItem>
                </SelectContent>
              </Select>
            </div>
            {num('Max trials', maxTrials, setMaxTrials, 1, 300)}
            {num('Min trades', minTrades, setMinTrades, 0)}
            {kind === 'sweep' ? (
              num('Hold out last % (OOS)', oosPct, setOosPct, 0, 50)
            ) : (
              <>
                {num('Train %', trainPct, setTrainPct, 10, 90)}
                {num('Test %', testPct, setTestPct, 5, 50)}
                {num('Max windows', windows, setWindows, 1, 20)}
              </>
            )}
          </div>
          <p className="text-xs text-muted-foreground">
            {kind === 'sweep'
              ? 'The held-out tail is never used to rank candidates, so comparing in-sample and out-of-sample scores shows overfitting.'
              : 'Parameters are chosen on each training window only and scored on the unseen window after it. The test windows are stitched into one out-of-sample curve.'}
          </p>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={submitting || tooBig || combos === null}>
            {submitting && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            Start
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
