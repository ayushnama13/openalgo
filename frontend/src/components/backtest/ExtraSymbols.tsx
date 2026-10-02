import { Plus, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

interface CatalogItem {
  symbol: string
  exchange: string
}

interface Props {
  catalog: CatalogItem[]
  /** Fully-qualified "EXCHANGE:SYMBOL" keys already chosen as extra symbols. */
  value: string[]
  /** The primary symbol key, excluded from suggestions. */
  primary: string
  onChange: (next: string[]) => void
  max?: number
}

export default function ExtraSymbols({ catalog, value, primary, onChange, max = 49 }: Props) {
  const [query, setQuery] = useState('')

  const matches = useMemo(() => {
    const q = query.trim().toUpperCase()
    if (q.length < 1) return []
    return catalog
      .map((c) => `${c.exchange}:${c.symbol}`)
      .filter((k) => k !== primary && !value.includes(k) && k.includes(q))
      .slice(0, 8)
  }, [catalog, query, value, primary])

  const add = (key: string) => {
    if (value.length >= max || value.includes(key)) return
    onChange([...value, key])
    setQuery('')
  }

  return (
    <div className="space-y-1.5">
      <Label>Additional symbols (multi-symbol run)</Label>
      <div className="flex flex-wrap gap-1.5">
        {value.map((k) => (
          <Badge key={k} variant="secondary" className="gap-1">
            {k}
            <button
              type="button"
              aria-label={`Remove ${k}`}
              onClick={() => onChange(value.filter((v) => v !== k))}
            >
              <X className="h-3 w-3" />
            </button>
          </Badge>
        ))}
      </div>
      <div className="relative">
        <Input
          placeholder="Search to add, e.g. INFY or NFO:NIFTY"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        {matches.length > 0 && (
          <div className="absolute z-20 mt-1 w-full rounded-md border border-slate-700 bg-slate-900 p-1 shadow-lg">
            {matches.map((k) => (
              <Button
                key={k}
                type="button"
                variant="ghost"
                size="sm"
                className="h-7 w-full justify-start gap-2 text-xs"
                onClick={() => add(k)}
              >
                <Plus className="h-3 w-3" /> {k}
              </Button>
            ))}
          </div>
        )}
      </div>
      <p className="text-[11px] text-muted-foreground">
        Every symbol trades under the same strategy, sharing one capital pool and the max positions
        limit.
      </p>
    </div>
  )
}
