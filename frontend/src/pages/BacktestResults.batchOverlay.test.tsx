import { act, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/lib/Plot2D', () => ({ default: () => null }))

import { BatchOverlay } from './BacktestResults'
import { useStrategyBacktestStore } from '@/stores/strategyBacktestStore'

describe('BatchOverlay', () => {
  afterEach(() => {
    useStrategyBacktestStore.getState().clear()
    vi.unstubAllGlobals()
  })

  it('refetches curves when the batch id changes despite a populated cache', async () => {
    const requested: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        requested.push(url)
        return {
          json: async () => ({ status: 'success', curves: [] }),
        } as Response
      })
    )
    act(() => useStrategyBacktestStore.getState().setBatchCurves('batch-a', []))
    const view = render(<BatchOverlay batchId="batch-a" />)
    view.rerender(<BatchOverlay batchId="batch-b" />)

    await waitFor(() => expect(requested).toContain('/backtest/api/batches/batch-b/curves'))
    await waitFor(() => expect(useStrategyBacktestStore.getState().batchCurvesFor).toBe('batch-b'))
  })
})
