// Universe-mode backtests: index universes (NIFTY 50, NIFTY 100, sector
// indices, ...) and their Historify coverage, from /backtest/api/universes.

import { fetchCSRFToken } from '@/api/client'

/** How a universe run is planned on the server (`mode` in the /run body). */
export type UniverseRunType = 'universe' | 'scan'

export interface UniverseSummary {
  id: string
  name: string
  category: 'broad' | 'sector' | string
  exchange: string
  count: number
}

export interface UniverseListing {
  as_of: string | null
  source: string | null
  universes: UniverseSummary[]
}

export interface UniverseConstituent {
  symbol: string
  name: string
  industry: string
  has_data: boolean
  first: string | null
  last: string | null
  records: number
}

export interface UniverseCoverage {
  id: string
  name: string
  category: string
  exchange: string
  interval: string
  base_interval: string
  as_of: string | null
  source: string | null
  survivorship_note: string
  count: number
  with_data: number
  common_first: string | null
  common_last: string | null
  constituents: UniverseConstituent[]
}

export async function fetchUniverses(): Promise<UniverseListing | null> {
  const response = await fetch('/backtest/api/universes', { credentials: 'include' })
  const data = await response.json()
  if (data.status !== 'success') return null
  return { as_of: data.as_of ?? null, source: data.source ?? null, universes: data.universes ?? [] }
}

export async function fetchUniverseCoverage(
  id: string,
  interval: string
): Promise<UniverseCoverage | null> {
  const params = new URLSearchParams({ interval })
  const response = await fetch(`/backtest/api/universes/${encodeURIComponent(id)}?${params}`, {
    credentials: 'include',
  })
  const data = await response.json()
  return data.status === 'success' ? (data.universe as UniverseCoverage) : null
}

/** Covered constituents as "NSE:SYMBOL" keys, in index order. */
export function coveredSymbols(coverage: UniverseCoverage | null): string[] {
  if (!coverage) return []
  return coverage.constituents
    .filter((c) => c.has_data)
    .map((c) => `${coverage.exchange}:${c.symbol}`)
}

export interface JobMeta {
  starred: boolean
  notes: string
}

/** PATCH a run's star and/or note. Resolves the stored values, or throws with the server message. */
export async function patchJobMeta(jobId: string, patch: Partial<JobMeta>): Promise<JobMeta> {
  const csrfToken = await fetchCSRFToken()
  const response = await fetch(`/backtest/api/jobs/${encodeURIComponent(jobId)}/meta`, {
    method: 'PATCH',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
    body: JSON.stringify(patch),
  })
  const data = await response.json()
  if (data.status !== 'success') throw new Error(data.message || 'Failed to update run')
  return { starred: Boolean(data.starred), notes: String(data.notes ?? '') }
}

/** "Scan" / "Universe" label for a run mode, or null for a plain stock run. */
export function modeLabel(mode?: string | null): string | null {
  if (mode === 'scan') return 'Scan'
  if (mode === 'universe') return 'Universe'
  return null
}
