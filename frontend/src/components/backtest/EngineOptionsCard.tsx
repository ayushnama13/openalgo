import { Cog } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { COST_MODELS, type EngineOptionsState, parseParams } from '@/lib/backtestEngineOptions'

interface Props {
  value: EngineOptionsState
  onChange: (next: EngineOptionsState) => void
  /** Hide the strategy-parameters box (the optimizer supplies its own). */
  hideParams?: boolean
}

export default function EngineOptionsCard({ value, onChange, hideParams }: Props) {
  const set = <K extends keyof EngineOptionsState>(key: K, v: EngineOptionsState[K]) =>
    onChange({ ...value, [key]: v })
  const parsed = parseParams(value.paramsText)
  const cost = COST_MODELS.find((c) => c.value === value.costModel)

  const field = (
    label: string,
    key: keyof EngineOptionsState,
    placeholder: string,
    hint?: string
  ) => (
    <div className="space-y-1.5">
      <Label>{label}</Label>
      <Input
        type="number"
        min={0}
        step="any"
        placeholder={placeholder}
        value={value[key] as string}
        onChange={(e) => set(key, e.target.value as never)}
      />
      {hint && <p className="text-[11px] text-muted-foreground">{hint}</p>}
    </div>
  )

  return (
    <Card className="xl:col-span-2">
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <Cog className="h-4 w-4 text-primary" /> Execution realism, costs and contracts
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <div className="space-y-1.5">
            <Label>Cost model</Label>
            <Select value={value.costModel} onValueChange={(v) => set('costModel', v)}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {COST_MODELS.map((c) => (
                  <SelectItem key={c.value} value={c.value}>
                    {c.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-[11px] text-muted-foreground">{cost?.hint}</p>
          </div>
          {field('Spread (bps)', 'spreadBps', '0', 'Half is paid on every fill')}
          {field('Market impact (bps)', 'impactBps', '0', 'Scales with sqrt(qty / bar volume)')}
          {field(
            'Max % of bar volume',
            'maxVolumePct',
            'unlimited',
            'Larger orders fill over several bars'
          )}
          {field(
            'MIS leverage (x)',
            'intradayLeverage',
            '1',
            'Buying power multiple for intraday equity'
          )}
          {field('Futures margin %', 'marginPctFut', '12', 'Blocked per unit of notional')}
          {field('Short option margin %', 'marginPctShortOpt', '15', 'Of strike value per unit')}
          {field('Trailing stop %', 'trailPct', 'off', 'Applied to every position')}
        </div>
        <div className="flex items-center gap-2">
          <input
            id="allow-short"
            type="checkbox"
            checked={value.allowShort}
            onChange={(e) => set('allowShort', e.target.checked)}
          />
          <Label htmlFor="allow-short">Allow short selling</Label>
        </div>
        {!hideParams && (
          <div className="space-y-1.5">
            <Label>Strategy parameters (JSON, optional)</Label>
            <textarea
              className="w-full rounded-md border border-slate-800 bg-slate-950 p-2 font-mono text-xs"
              rows={3}
              placeholder='{"fast": 10, "slow": 30}'
              value={value.paramsText}
              onChange={(e) => set('paramsText', e.target.value)}
            />
            <p className={`text-[11px] ${parsed.ok ? 'text-muted-foreground' : 'text-red-400'}`}>
              {parsed.ok
                ? 'Read as self.params[...] in an event strategy, or PARAMS[...] in a signal strategy.'
                : parsed.error}
            </p>
          </div>
        )}
        <p className="text-xs text-muted-foreground">
          Futures and options use the lot size, contract type and strike stored in Historify. For
          contracts, fixed quantity means lots, and percent or capital sizing is the margin to
          commit.
        </p>
      </CardContent>
    </Card>
  )
}
