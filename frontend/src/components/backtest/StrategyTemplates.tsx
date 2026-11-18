/**
 * Strategy template picker for the backtest editor: the built-in starters plus
 * templates the user saved (code and run settings, /backtest/api/strategies).
 * Symbols and dates are never part of a template.
 */

import { Loader2 } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
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
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { STRATEGY_TEMPLATES } from '@/lib/backtestTemplates'
import { showToast } from '@/utils/toast'

export interface TemplateSettings {
  interval?: string
  initial_capital?: number
  sizing_type?: string
  sizing_value?: number
  slippage_bps?: number
  brokerage_bps?: number
  product_type?: string
  missing_data_policy?: string
  stop_loss_pct?: number | null
  take_profit_pct?: number | null
  daily_loss_limit?: number | null
  max_positions?: number
  engine_options?: Record<string, unknown>
}

export interface SavedTemplate {
  id: string
  name: string
  description: string | null
  strategy_code: string
  settings: TemplateSettings | null
  created_at: string
  updated_at: string
}

const API = '/backtest/api/strategies'
const MANAGE = '__manage'

class ApiError extends Error {
  readonly status: number
  readonly existingId?: string
  constructor(message: string, status: number, existingId?: string) {
    super(message)
    this.status = status
    this.existingId = existingId
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {}
  if (method !== 'GET') {
    headers['X-CSRFToken'] = await fetchCSRFToken()
    headers['Content-Type'] = 'application/json'
  }
  const res = await fetch(path, {
    method,
    credentials: 'include',
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok || data.status !== 'success') {
    throw new ApiError(data.message || 'Request failed', res.status, data.existing_id)
  }
  return data as T
}

const errText = (e: unknown) => (e instanceof Error ? e.message : 'Request failed')

/** "3d ago" style age from an ISO timestamp; '' when unparseable. */
export function ageText(iso: string, now = Date.now()) {
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return ''
  const mins = Math.max(0, Math.round((now - t) / 60000))
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.round(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.round(hours / 24)
  return days < 60 ? `${days}d ago` : new Date(t).toLocaleDateString('en-IN')
}

interface Props {
  /** Current editor code. */
  code: string
  /** Current run settings, or null (after a toast) when the form is invalid. */
  getSettings: () => TemplateSettings | null
  onLoadBuiltIn: (code: string) => void
  onLoadSaved: (t: SavedTemplate) => void
}

export default function StrategyTemplates({
  code,
  getSettings,
  onLoadBuiltIn,
  onLoadSaved,
}: Props) {
  const [templates, setTemplates] = useState<SavedTemplate[]>([])
  const [picked, setPicked] = useState('')
  const [loaded, setLoaded] = useState<SavedTemplate | null>(null)
  const [saveOpen, setSaveOpen] = useState(false)
  const [manageOpen, setManageOpen] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const data = await call<{ strategies: SavedTemplate[] }>('GET', API)
      setTemplates(data.strategies)
      return data.strategies
    } catch (e) {
      showToast.error(`Could not load saved templates: ${errText(e)}`)
      return null
    }
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  const handlePick = (value: string) => {
    if (value === MANAGE) {
      setManageOpen(true)
      return
    }
    setPicked(value)
    if (value.startsWith('builtin:')) {
      const t = STRATEGY_TEMPLATES.find((x) => `builtin:${x.label}` === value)
      if (t) {
        onLoadBuiltIn(t.code)
        setLoaded(null)
      }
      return
    }
    const t = templates.find((x) => `user:${x.id}` === value)
    if (t) {
      onLoadSaved(t)
      setLoaded(t)
      showToast.success(t.settings ? `Loaded "${t.name}" with its settings` : `Loaded "${t.name}"`)
    }
  }

  // A template deleted or renamed in Manage must not linger in the trigger.
  const afterManage = (list: SavedTemplate[]) => {
    if (!loaded) return
    const still = list.find((t) => t.id === loaded.id)
    setLoaded(still ?? null)
    if (!still) setPicked('')
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <Select value={picked} onValueChange={handlePick}>
        <SelectTrigger className="w-56" aria-label="Load template">
          <SelectValue placeholder="Load template" />
        </SelectTrigger>
        <SelectContent>
          {templates.length > 0 && (
            <SelectGroup>
              <SelectLabel>My templates</SelectLabel>
              {templates.map((t) => (
                <SelectItem key={t.id} value={`user:${t.id}`}>
                  {t.name}
                  {t.settings?.interval && (
                    <span className="ml-2 text-xs text-muted-foreground">
                      {t.settings.interval}
                    </span>
                  )}
                </SelectItem>
              ))}
            </SelectGroup>
          )}
          {templates.length > 0 && <SelectSeparator />}
          <SelectGroup>
            <SelectLabel>Built-in</SelectLabel>
            {STRATEGY_TEMPLATES.map((t) => (
              <SelectItem key={t.label} value={`builtin:${t.label}`}>
                {t.label}
              </SelectItem>
            ))}
          </SelectGroup>
          <SelectSeparator />
          <SelectItem value={MANAGE}>Manage templates...</SelectItem>
        </SelectContent>
      </Select>
      <Button variant="outline" size="sm" onClick={() => setSaveOpen(true)}>
        Save template
      </Button>

      <SaveTemplateDialog
        open={saveOpen}
        onOpenChange={setSaveOpen}
        initial={loaded}
        code={code}
        getSettings={getSettings}
        onSaved={async (t) => {
          setLoaded(t)
          await refresh()
          setPicked(`user:${t.id}`)
        }}
      />
      <ManageTemplatesDialog
        open={manageOpen}
        onOpenChange={setManageOpen}
        templates={templates}
        refresh={async () => {
          const list = await refresh()
          if (list) afterManage(list)
        }}
      />
    </div>
  )
}

function SaveTemplateDialog({
  open,
  onOpenChange,
  initial,
  code,
  getSettings,
  onSaved,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  initial: SavedTemplate | null
  code: string
  getSettings: () => TemplateSettings | null
  onSaved: (t: SavedTemplate) => void
}) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [clash, setClash] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Saving again after loading a template defaults to updating that template.
  useEffect(() => {
    if (!open) return
    setName(initial?.name ?? '')
    setDescription(initial?.description ?? '')
    setClash(false)
    setError(null)
  }, [open, initial])

  const save = async (overwrite: boolean) => {
    if (!name.trim()) {
      setError('Give the template a name')
      return
    }
    if (!code.trim()) {
      setError('The editor is empty')
      return
    }
    const settings = getSettings()
    if (!settings) return
    setBusy(true)
    setError(null)
    try {
      const data = await call<{ strategy: SavedTemplate }>('POST', API, {
        name: name.trim(),
        description: description.trim(),
        strategy_code: code,
        settings,
        overwrite,
      })
      showToast.success(overwrite ? `Updated "${data.strategy.name}"` : 'Template saved')
      onSaved(data.strategy)
      onOpenChange(false)
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setClash(true)
      else setError(errText(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Save template</DialogTitle>
          <DialogDescription>
            Saves the code with the timeframe, capital, sizing, costs, stops and engine options.
            Symbols and dates are not saved.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label htmlFor="tpl-name">Name</Label>
            <Input
              id="tpl-name"
              value={name}
              maxLength={128}
              autoFocus
              onChange={(e) => {
                setName(e.target.value)
                setClash(false)
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !busy) save(clash)
              }}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="tpl-desc">Description (optional)</Label>
            <Textarea
              id="tpl-desc"
              rows={2}
              maxLength={1000}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
          {clash && (
            <p className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-sm">
              A template named "{name.trim()}" already exists. Overwrite it with the current code
              and settings?
            </p>
          )}
          {error && <p className="text-sm text-destructive">{error}</p>}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={busy}>
            Cancel
          </Button>
          <Button
            variant={clash ? 'destructive' : 'default'}
            onClick={() => save(clash)}
            disabled={busy}
          >
            {busy && <Loader2 className="mr-1.5 h-4 w-4 animate-spin" />}
            {clash ? 'Overwrite' : 'Save'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function ManageTemplatesDialog({
  open,
  onOpenChange,
  templates,
  refresh,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  templates: SavedTemplate[]
  refresh: () => Promise<void>
}) {
  const [editing, setEditing] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [confirming, setConfirming] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  useEffect(() => {
    if (!open) {
      setEditing(null)
      setConfirming(null)
    }
  }, [open])

  const rename = async (t: SavedTemplate) => {
    const next = draft.trim()
    if (!next || next === t.name) {
      setEditing(null)
      return
    }
    setBusy(t.id)
    try {
      await call('PUT', `${API}/${t.id}`, { name: next })
      setEditing(null)
      await refresh()
    } catch (e) {
      showToast.error(
        e instanceof ApiError && e.status === 409
          ? `A template named "${next}" already exists`
          : errText(e)
      )
    } finally {
      setBusy(null)
    }
  }

  const remove = async (t: SavedTemplate) => {
    setBusy(t.id)
    try {
      await call('DELETE', `${API}/${t.id}`)
      showToast.success(`Deleted "${t.name}"`)
      setConfirming(null)
      await refresh()
    } catch (e) {
      showToast.error(errText(e))
    } finally {
      setBusy(null)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[80vh] max-w-lg overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Manage templates</DialogTitle>
          <DialogDescription>
            Rename or delete your saved templates. To change a template's code or settings, load it,
            edit, and save it again under the same name.
          </DialogDescription>
        </DialogHeader>
        {templates.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted-foreground">
            No saved templates yet. Use "Save template" next to the picker.
          </p>
        ) : (
          <ul className="divide-y divide-border rounded-md border border-border">
            {templates.map((t) => (
              <li key={t.id} className="flex items-center gap-2 px-3 py-2">
                <div className="min-w-0 flex-1">
                  {editing === t.id ? (
                    <Input
                      value={draft}
                      maxLength={128}
                      autoFocus
                      aria-label="New name"
                      className="h-8"
                      onChange={(e) => setDraft(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') rename(t)
                        if (e.key === 'Escape') {
                          e.stopPropagation()
                          setEditing(null)
                        }
                      }}
                    />
                  ) : (
                    <>
                      <div className="truncate text-sm font-medium">{t.name}</div>
                      <div className="truncate text-xs text-muted-foreground">
                        {[t.settings?.interval, `edited ${ageText(t.updated_at)}`, t.description]
                          .filter(Boolean)
                          .join(' | ')}
                      </div>
                    </>
                  )}
                </div>
                {busy === t.id ? (
                  <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
                ) : editing === t.id ? (
                  <>
                    <Button size="sm" onClick={() => rename(t)}>
                      Save
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setEditing(null)}>
                      Cancel
                    </Button>
                  </>
                ) : confirming === t.id ? (
                  <>
                    <span className="text-xs text-muted-foreground">Delete?</span>
                    <Button size="sm" variant="destructive" onClick={() => remove(t)}>
                      Delete
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setConfirming(null)}>
                      Keep
                    </Button>
                  </>
                ) : (
                  <>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => {
                        setEditing(t.id)
                        setDraft(t.name)
                        setConfirming(null)
                      }}
                    >
                      Rename
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="text-destructive"
                      onClick={() => {
                        setConfirming(t.id)
                        setEditing(null)
                      }}
                    >
                      Delete
                    </Button>
                  </>
                )}
              </li>
            ))}
          </ul>
        )}
      </DialogContent>
    </Dialog>
  )
}
