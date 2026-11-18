import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  fetchStats: vi.fn(),
  fetchRecorderStatus: vi.fn(),
  fetchTicks: vi.fn(),
  fetchSessions: vi.fn(),
  startRecorder: vi.fn(),
  stopRecorder: vi.fn(),
  deleteRecorderConfig: vi.fn(),
  setDepthLevel: vi.fn(),
  pruneOldTicks: vi.fn(),
}))
const analyticsProps = vi.hoisted(() => ({ last: null as Record<string, unknown> | null }))

vi.mock('@/api/depth-recorder', () => api)
vi.mock('@/components/depth-recorder/DepthAnalytics', () => ({
  default: (props: Record<string, unknown>) => {
    analyticsProps.last = props
    return <div data-testid="analytics" />
  },
}))
vi.mock('@/utils/toast', () => ({
  showToast: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
}))

import DepthRecorder from './DepthRecorder'

const recorder = (symbol: string, status: string, ticks: number) => ({
  symbol,
  exchange: 'NSE',
  status,
  ticks_written: ticks,
  ticks_buffered: 3,
  last_tick_time: status === 'recording' ? '2026-11-13T10:42:15' : null,
  connected: status === 'recording',
  error: null,
  started_at: null,
  depth_level: 20,
  actual_depth: 20,
})

const tick = (symbol: string, ltp: number) => ({
  id: 1,
  symbol,
  exchange: 'NSE',
  tick_time: '2026-11-13T10:42:15',
  ltp,
  volume: 1,
  oi: 0,
  total_buy_qty: 1200,
  total_sell_qty: 900,
  bids: [{ price: ltp - 0.05, quantity: 1200 }],
  asks: [{ price: ltp + 0.05, quantity: 900 }],
})

describe('DepthRecorder list and detail', () => {
  beforeEach(() => {
    for (const fn of Object.values(api)) fn.mockReset()
    analyticsProps.last = null
    api.fetchRecorderStatus.mockResolvedValue([
      recorder('RELIANCE', 'recording', 2164),
      recorder('SBIN', 'stopped', 50),
    ])
    api.fetchStats.mockResolvedValue({
      total_ticks: 360112,
      symbols: [
        { symbol: 'RELIANCE', exchange: 'NSE' },
        { symbol: 'SBIN', exchange: 'NSE' },
        { symbol: 'INFY', exchange: 'NSE' },
      ],
      latest_tick_time: '2026-11-13T10:42:15',
    })
    api.fetchTicks.mockImplementation(async (symbol: string) => ({
      ticks: [tick(symbol, symbol === 'RELIANCE' ? 1284.1 : 812.3)],
    }))
    api.fetchSessions.mockResolvedValue([
      { symbol: 'SBIN', exchange: 'NSE', day: '2026-11-12', n_ticks: 50, first: '', last: '' },
    ])
    api.startRecorder.mockResolvedValue({ status: 'started' })
    api.stopRecorder.mockResolvedValue({ status: 'stopping' })
    api.deleteRecorderConfig.mockResolvedValue({ status: 'success' })
  })

  const row = (name: RegExp) => screen.findByRole('button', { name, pressed: undefined })

  it('lists recorders plus symbols that only have stored ticks, and shows totals', async () => {
    render(<DepthRecorder />)
    expect(await row(/RELIANCE.*recording.*2,164 saved/)).toHaveAttribute('aria-pressed', 'true')
    expect(await row(/SBIN.*stopped/)).toHaveAttribute('aria-pressed', 'false')
    expect(await row(/INFY.*stored ticks, no recorder/)).toBeInTheDocument()
    expect(screen.getByText('3,60,112')).toBeInTheDocument()
  })

  it('fetches the latest tick only for the selected symbol', async () => {
    render(<DepthRecorder />)
    expect(await screen.findByText('1,284.10')).toBeInTheDocument()
    expect(api.fetchTicks.mock.calls.every(([symbol]) => symbol === 'RELIANCE')).toBe(true)

    fireEvent.click(await row(/SBIN.*stopped/))
    expect(await screen.findByText('812.30')).toBeInTheDocument()
    expect(screen.getByText(/Last stored snapshot/)).toBeInTheDocument()
    expect(api.fetchTicks).toHaveBeenCalledWith('SBIN', 'NSE', 1)
    expect(api.fetchTicks).not.toHaveBeenCalledWith('INFY', 'NSE', 1)
  })

  it('stops, restarts and removes the selected recorder', async () => {
    render(<DepthRecorder />)
    fireEvent.click(await screen.findByRole('button', { name: /Stop/ }))
    await waitFor(() => expect(api.stopRecorder).toHaveBeenCalledWith('RELIANCE', 'NSE'))

    fireEvent.click(await row(/SBIN.*stopped/))
    fireEvent.click(await screen.findByRole('button', { name: /^Start$/ }))
    await waitFor(() => expect(api.startRecorder).toHaveBeenCalledWith('SBIN', 'NSE', 20))

    fireEvent.click(screen.getByRole('button', { name: /Remove/ }))
    await waitFor(() => expect(api.deleteRecorderConfig).toHaveBeenCalledWith('SBIN', 'NSE'))
  })

  it('starts a recorder from the add form with the chosen depth', async () => {
    render(<DepthRecorder />)
    fireEvent.change(await screen.findByLabelText('Symbol'), { target: { value: 'tcs' } })
    fireEvent.click(screen.getByRole('button', { name: '50D' }))
    fireEvent.click(screen.getByRole('button', { name: /Start Recording/ }))
    await waitFor(() => expect(api.startRecorder).toHaveBeenCalledWith('TCS', 'NSE', 50))
  })

  it('opens analytics for a recorded day from the Sessions tab', async () => {
    render(<DepthRecorder />)
    fireEvent.click(await row(/SBIN.*stopped/))
    const sessionsTab = screen.getByRole('tab', { name: 'Sessions' })
    fireEvent.mouseDown(sessionsTab)
    fireEvent.click(sessionsTab)

    const dayRow = (await screen.findByText('2026-11-12')).closest('tr') as HTMLElement
    expect(api.fetchSessions).toHaveBeenCalledWith('SBIN', 'NSE')
    fireEvent.click(within(dayRow).getByRole('button', { name: 'Analyse' }))

    expect(await screen.findByTestId('analytics')).toBeInTheDocument()
    expect(analyticsProps.last).toMatchObject({
      initialSymbol: 'SBIN',
      initialExchange: 'NSE',
      initialDay: '2026-11-12',
    })
  })
})
