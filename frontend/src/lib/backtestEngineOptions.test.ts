import { describe, expect, it } from 'vitest'
import { buildEngineOptions, engineOptionsFromSaved } from './backtestEngineOptions'

describe('engineOptionsFromSaved', () => {
  it('round-trips saved engine options through the form state', () => {
    const saved = {
      cost_preset: 'india_options_nse',
      spread_bps: 1.25,
      impact_bps: 2,
      max_volume_pct: 12.5,
      intraday_leverage: 3,
      margin_pct_fut: 8,
      margin_pct_short_opt: 15,
      trail_pct: 4.5,
      allow_short: false,
      params: { fast: 10, nested: { enabled: true } },
      ignored: 'value',
    }

    const rebuilt = buildEngineOptions(engineOptionsFromSaved(saved))

    const { ignored: _ignored, ...expected } = saved
    expect(rebuilt).toEqual({ ok: true, value: expected })
  })
})
