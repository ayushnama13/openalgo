import { AlertTriangle, Loader2 } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import type { UniverseCoverage, UniverseListing, UniverseRunType } from '@/lib/backtestUniverse'
import { cn } from '@/lib/utils'

const RUN_TYPES: { value: UniverseRunType; label: string; hint: string }[] = [
  {
    value: 'universe',
    label: 'Portfolio',
    hint: 'One run that trades every covered constituent from a shared capital pool, capped by Max Open Positions. Answers: does the strategy work as a portfolio?',
  },
  {
    value: 'scan',
    label: 'Scan',
    hint: 'One independent run per constituent on a single timeframe, then ranked. Answers: where does the strategy work, and is the edge broad or a few lucky names?',
  },
]

const CATEGORY_LABELS: Record<string, string> = { broad: 'Broad market', sector: 'Sector' }

interface Props {
  listing: UniverseListing | null
  listingLoading: boolean
  universeId: string
  onUniverseChange(id: string): void
  runType: UniverseRunType
  onRunTypeChange(runType: UniverseRunType): void
  coverage: UniverseCoverage | null
  coverageLoading: boolean
}

/** Universe selection for the Data tab in Universe mode: index, run type and data coverage. */
export default function UniversePicker({
  listing,
  listingLoading,
  universeId,
  onUniverseChange,
  runType,
  onRunTypeChange,
  coverage,
  coverageLoading,
}: Props) {
  const [missingOnly, setMissingOnly] = useState(false)

  const groups = useMemo(() => {
    const map = new Map<string, UniverseListing['universes']>()
    for (const u of listing?.universes ?? []) {
      const arr = map.get(u.category) ?? []
      arr.push(u)
      map.set(u.category, arr)
    }
    return [...map.entries()]
  }, [listing])

  const missing = coverage ? coverage.count - coverage.with_data : 0
  const rows = useMemo(
    () => (coverage?.constituents ?? []).filter((c) => !missingOnly || !c.has_data),
    [coverage, missingOnly]
  )
  const hint = RUN_TYPES.find((r) => r.value === runType)?.hint

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label>Universe</Label>
          <Select value={universeId} onValueChange={onUniverseChange}>
            <SelectTrigger disabled={listingLoading} className="w-full">
              <SelectValue placeholder={listingLoading ? 'Loading...' : 'Select an index'} />
            </SelectTrigger>
            <SelectContent>
              {groups.map(([category, items]) => (
                <SelectGroup key={category}>
                  <SelectLabel>{CATEGORY_LABELS[category] ?? category}</SelectLabel>
                  {items.map((u) => (
                    <SelectItem key={u.id} value={u.id}>
                      {u.name}
                      <span className="text-muted-foreground"> ({u.count})</span>
                    </SelectItem>
                  ))}
                </SelectGroup>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label>Run Type</Label>
          <div className="grid grid-cols-2 gap-1 rounded-md border p-1">
            {RUN_TYPES.map((r) => (
              <button
                key={r.value}
                type="button"
                aria-pressed={runType === r.value}
                onClick={() => onRunTypeChange(r.value)}
                className={cn(
                  'rounded px-2 py-1 text-sm font-medium transition-colors',
                  runType === r.value
                    ? 'bg-primary text-primary-foreground'
                    : 'text-muted-foreground hover:bg-muted'
                )}
              >
                {r.label}
              </button>
            ))}
          </div>
        </div>
      </div>
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}

      {coverageLoading && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> Checking Historify coverage...
        </div>
      )}

      {coverage && !coverageLoading && (
        <div className="space-y-3">
          <div className="grid grid-cols-3 gap-2 text-center">
            <Stat
              label={`Have ${coverage.base_interval} data`}
              value={`${coverage.with_data} / ${coverage.count}`}
              tone={coverage.with_data === 0 ? 'bad' : missing > 0 ? 'warn' : 'good'}
            />
            <Stat label="Common from" value={coverage.common_first ?? '-'} />
            <Stat label="Common to" value={coverage.common_last ?? '-'} />
          </div>

          {coverage.with_data === 0 ? (
            <p className="rounded-md border border-rose-500/40 bg-rose-500/10 p-2 text-xs text-rose-300">
              No constituent has {coverage.base_interval} data in Historify. Download it from
              Historify first, then come back.
            </p>
          ) : (
            missing > 0 && (
              <p className="text-xs text-muted-foreground">
                {missing} constituent{missing === 1 ? '' : 's'} without data will be skipped.
              </p>
            )
          )}

          <div className="flex gap-2 rounded-md border border-amber-500/30 bg-amber-500/5 p-2 text-xs text-amber-800 dark:text-amber-200/90">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-600 dark:text-amber-400" />
            <span>
              {coverage.survivorship_note}
              {coverage.as_of ? ` Members as of ${coverage.as_of}.` : ''}
            </span>
          </div>

          <div className="space-y-1.5">
            <div className="flex items-center justify-between">
              <span className="text-xs font-medium text-muted-foreground">
                Constituents ({rows.length})
              </span>
              {missing > 0 && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-6 px-2 text-xs"
                  onClick={() => setMissingOnly((v) => !v)}
                >
                  {missingOnly ? 'Show all' : 'Show missing only'}
                </Button>
              )}
            </div>
            <div className="max-h-56 overflow-auto rounded-md border">
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-card text-muted-foreground">
                  <tr className="text-left">
                    <th className="px-2 py-1.5 font-medium">Symbol</th>
                    <th className="px-2 py-1.5 font-medium">Industry</th>
                    <th className="px-2 py-1.5 text-right font-medium">Data</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((c) => (
                    <tr key={c.symbol} className="border-t border-border/50" title={c.name}>
                      <td className="px-2 py-1 font-mono">
                        <span
                          className={cn(
                            'mr-1.5 inline-block h-1.5 w-1.5 rounded-full align-middle',
                            c.has_data ? 'bg-emerald-400' : 'bg-slate-600'
                          )}
                        />
                        {c.symbol}
                      </td>
                      <td className="max-w-40 truncate px-2 py-1 text-muted-foreground">
                        {c.industry || '-'}
                      </td>
                      <td className="whitespace-nowrap px-2 py-1 text-right tabular-nums text-muted-foreground">
                        {c.has_data ? `${c.first} to ${c.last}` : 'no data'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string
  value: string
  tone?: 'good' | 'warn' | 'bad'
}) {
  return (
    <div className="rounded-md border bg-muted/30 px-2 py-1.5">
      <div
        className={cn(
          'text-sm font-semibold tabular-nums',
          tone === 'good' && 'text-emerald-400',
          tone === 'warn' && 'text-amber-600 dark:text-amber-400',
          tone === 'bad' && 'text-rose-400'
        )}
      >
        {value}
      </div>
      <div className="text-[11px] text-muted-foreground">{label}</div>
    </div>
  )
}
