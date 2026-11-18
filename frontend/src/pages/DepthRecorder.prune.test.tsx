import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  pruneOldTicks: vi.fn(),
  fetchStats: vi.fn(),
  fetchRecorderStatus: vi.fn(),
}))

vi.mock('@/api/depth-recorder', () => ({
  pruneOldTicks: api.pruneOldTicks,
  fetchStats: api.fetchStats,
  fetchRecorderStatus: api.fetchRecorderStatus,
  fetchTicks: vi.fn().mockResolvedValue([]),
  deleteRecorderConfig: vi.fn(),
  setDepthLevel: vi.fn(),
  startRecorder: vi.fn(),
  stopRecorder: vi.fn(),
}))
vi.mock('@/components/depth-recorder/DepthAnalytics', () => ({ default: () => null }))
vi.mock('@/utils/toast', () => ({
  showToast: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
}))

import DepthRecorder from './DepthRecorder'

describe('DepthRecorder delete old data', () => {
  beforeEach(() => {
    api.pruneOldTicks.mockReset().mockResolvedValue({ status: 'success', deleted: 1234 })
    api.fetchRecorderStatus.mockReset().mockResolvedValue([])
    api.fetchStats.mockReset().mockResolvedValue({
      total_ticks: 360112,
      symbols: [{ symbol: 'RELIANCE', exchange: 'NSE' }],
      latest_tick_time: null,
    })
  })

  const openDialog = async () => {
    render(<DepthRecorder />)
    fireEvent.click(await screen.findByRole('button', { name: 'Delete old data...' }))
    return screen.findByLabelText('Delete ticks older than')
  }

  it('sends the days as an integer after the confirm step and closes on success', async () => {
    const input = await openDialog()
    expect(screen.getByText('This cannot be undone.')).toBeInTheDocument()
    expect(api.pruneOldTicks).not.toHaveBeenCalled()

    fireEvent.change(input, { target: { value: '7' } })
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))

    await waitFor(() => expect(api.pruneOldTicks).toHaveBeenCalledTimes(1))
    expect(api.pruneOldTicks.mock.calls[0][0]).toBe(7)
    await waitFor(() =>
      expect(screen.queryByText('This cannot be undone.')).not.toBeInTheDocument()
    )
  })

  it.each(['', '0', '1.5', '-3', 'abc'])('disables Delete for days=%j', async (value) => {
    const input = await openDialog()
    fireEvent.change(input, { target: { value } })
    expect(screen.getByRole('button', { name: 'Delete' })).toBeDisabled()
    expect(api.pruneOldTicks).not.toHaveBeenCalled()
  })

  it('keeps the dialog open when the server rejects the request', async () => {
    api.pruneOldTicks.mockRejectedValue(new Error('500'))
    await openDialog()
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(api.pruneOldTicks).toHaveBeenCalledTimes(1))
    expect(api.pruneOldTicks.mock.calls[0][0]).toBe(30)
    expect(await screen.findByRole('button', { name: 'Delete' })).toBeEnabled()
  })
})
