import { act, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

vi.mock('@/lib/Plot2D', () => ({ default: () => null }))

import { AnalyticsPanel } from './RunInsights'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

const analytics = (timeInMarket: number) => ({
  status: 'success',
  exposure: { time_in_market_pct: timeInMarket },
  trades: {
    streaks: {},
    by_symbol: [],
    by_exit_reason: [],
    by_side: [],
    excursion: null,
  },
  drawdown_periods: [],
  rolling: { window: 1, points: [] },
})

describe('AnalyticsPanel', () => {
  it("keeps the latest job's response when an older request resolves last", async () => {
    const first = deferred<Response>()
    const second = deferred<Response>()
    vi.stubGlobal(
      'fetch',
      vi.fn((url: RequestInfo | URL) =>
        String(url).endsWith('/job-a') ? first.promise : second.promise
      )
    )
    const view = render(<AnalyticsPanel jobId="job-a" />)
    view.rerender(<AnalyticsPanel jobId="job-b" />)

    await act(async () => {
      second.resolve({ json: async () => analytics(22) } as Response)
      await second.promise
    })
    await waitFor(() => expect(screen.getByText('22%')).toBeInTheDocument())

    await act(async () => {
      first.resolve({ json: async () => analytics(11) } as Response)
      await first.promise
    })
    expect(screen.getByText('22%')).toBeInTheDocument()
    expect(screen.queryByText('11%')).not.toBeInTheDocument()
    vi.unstubAllGlobals()
  })
})
