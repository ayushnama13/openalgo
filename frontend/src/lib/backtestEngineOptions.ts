// Engine options that go to /backtest/api/run and /backtest/api/optimize as `engine_options`.

export interface EngineOptionsState {
  costModel: string // 'flat' | 'auto' | preset name
  spreadBps: string
  impactBps: string
  maxVolumePct: string
  intradayLeverage: string
  marginPctFut: string
  marginPctShortOpt: string
  trailPct: string
  allowShort: boolean
  paramsText: string // JSON object of strategy parameters
}

export const DEFAULT_ENGINE_OPTIONS: EngineOptionsState = {
  costModel: 'flat',
  spreadBps: '',
  impactBps: '',
  maxVolumePct: '',
  intradayLeverage: '',
  marginPctFut: '',
  marginPctShortOpt: '',
  trailPct: '',
  allowShort: true,
  paramsText: '',
}

export const COST_MODELS: { value: string; label: string; hint: string }[] = [
  { value: 'flat', label: 'Flat brokerage (bps)', hint: 'Uses the Brokerage field above' },
  { value: 'auto', label: 'Statutory India (auto)', hint: 'Picks by product and contract type' },
  { value: 'india_intraday_nse', label: 'India intraday (NSE)', hint: 'MIS equity' },
  { value: 'india_delivery_nse', label: 'India delivery (NSE)', hint: 'CNC equity' },
  { value: 'india_futures_nse', label: 'India futures', hint: 'NFO futures' },
  { value: 'india_options_nse', label: 'India options', hint: 'Charges on premium' },
]

export type ParsedParams =
  | { ok: true; value: Record<string, unknown> | null }
  | { ok: false; error: string }

export function parseParams(text: string): ParsedParams {
  const trimmed = text.trim()
  if (!trimmed) return { ok: true, value: null }
  try {
    const value = JSON.parse(trimmed)
    if (value === null || typeof value !== 'object' || Array.isArray(value)) {
      return { ok: false, error: 'Parameters must be a JSON object, e.g. {"fast": 10}' }
    }
    return { ok: true, value: value as Record<string, unknown> }
  } catch {
    return { ok: false, error: 'Parameters are not valid JSON' }
  }
}

const num = (v: string): number | undefined => {
  if (v.trim() === '') return undefined
  const n = Number(v)
  return Number.isFinite(n) ? n : undefined
}

export function buildEngineOptions(
  state: EngineOptionsState
): { ok: true; value: Record<string, unknown> } | { ok: false; error: string } {
  const out: Record<string, unknown> = {}
  if (state.costModel !== 'flat') out.cost_preset = state.costModel
  const pairs: [string, string][] = [
    ['spread_bps', state.spreadBps],
    ['impact_bps', state.impactBps],
    ['max_volume_pct', state.maxVolumePct],
    ['intraday_leverage', state.intradayLeverage],
    ['margin_pct_fut', state.marginPctFut],
    ['margin_pct_short_opt', state.marginPctShortOpt],
    ['trail_pct', state.trailPct],
  ]
  for (const [key, raw] of pairs) {
    const n = num(raw)
    if (n !== undefined) out[key] = n
  }
  if (!state.allowShort) out.allow_short = false
  const params = parseParams(state.paramsText)
  if (!params.ok) return params
  if (params.value) out.params = params.value
  return { ok: true, value: out }
}
