import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('react-router', () => ({ useNavigate: () => vi.fn() }))
vi.mock('@/hooks/useSocket', () => ({ useSocket: () => ({ socket: null }) }))
vi.mock('@/api/client', () => ({ fetchCSRFToken: vi.fn().mockResolvedValue('token') }))
vi.mock('@/components/ui/python-editor', () => ({ PythonEditor: () => null }))
vi.mock('@/components/backtest/EngineOptionsCard', () => ({ default: () => null }))
vi.mock('@/components/backtest/ExtraSymbols', () => ({ default: () => null }))
vi.mock('@/components/backtest/OptimizeDialog', () => ({ default: () => null }))
// Small pages keep the rendered table small; the helpers themselves are real.
vi.mock('@/lib/backtestJobsPaging', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/backtestJobsPaging')>()),
  JOBS_PAGE_SIZE: 5,
  JOBS_MAX_LIMIT: 8,
}))
vi.mock('@/utils/toast', () => ({
  showToast: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
}))

import Backtest from './Backtest'

const TOTAL = 13

// Newest first, like the server.
let serverJobs: Record<string, unknown>[] = []
const jobRequests: string[] = []

const makeJobs = () =>
  Array.from({ length: TOTAL }, (_, i) => {
    const n = TOTAL - i
    return {
      job_id: `job-${n}`,
      name: `run ${n}`,
      status: 'completed',
      symbols: ['RELIANCE'],
      exchange: 'NSE',
      interval: '1m',
      initial_capital: 100000,
      created_at: new Date(Date.UTC(2026, 0, 1) + n * 1000).toISOString(),
      total_return_pct: 1,
      sharpe_ratio: 1,
      max_drawdown_pct: 1,
      win_rate_pct: 50,
      total_trades: 3,
      batch_seq: 0,
    }
  })

describe('Backtest job history paging', () => {
  beforeEach(() => {
    serverJobs = makeJobs()
    jobRequests.length = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), 'http://localhost')
        let body: unknown = { status: 'error' }
        if (url.pathname === '/backtest/api/jobs') {
          jobRequests.push(url.search)
          const limit = Number(url.searchParams.get('limit') ?? 500)
          const offset = Number(url.searchParams.get('offset') ?? 0)
          body = {
            status: 'success',
            jobs: serverJobs.slice(offset, offset + limit),
            total: serverJobs.length,
          }
        } else if (url.pathname.startsWith('/backtest/api/jobs/') && init?.method === 'DELETE') {
          const id = url.pathname.split('/').pop()
          serverJobs = serverJobs.filter((job) => job.job_id !== id)
          body = { status: 'success' }
        }
        return { ok: true, json: async () => body } as Response
      })
    )
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('shows the server total, loads further pages on demand and survives a delete', async () => {
    render(<Backtest />)

    fireEvent.click(await screen.findByRole('button', { name: /Job History \(13\)/ }))
    expect(await screen.findByText('Showing 5 of 13')).toBeInTheDocument()
    expect(jobRequests[0]).toBe('?limit=5&offset=0')
    expect(screen.getByText('run 9')).toBeInTheDocument()
    expect(screen.queryByText('run 8')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Load 5 more' }))
    expect(await screen.findByText('Showing 10 of 13')).toBeInTheDocument()
    expect(screen.getByText('run 4')).toBeInTheDocument()

    // Deleting a row on the second page refetches everything that is loaded,
    // in chunks no larger than the server's limit.
    const row = screen.getByText('run 6').closest('tr') as HTMLElement
    fireEvent.click(row.querySelector('button[title="Delete"]') as HTMLElement)
    await waitFor(() => expect(screen.queryByText('run 6')).not.toBeInTheDocument())
    expect(await screen.findByText('Showing 10 of 12')).toBeInTheDocument()
    expect(jobRequests.slice(-2)).toEqual(['?limit=8&offset=0', '?limit=2&offset=8'])

    fireEvent.click(screen.getByRole('button', { name: 'Load 2 more' }))
    await waitFor(() => expect(screen.queryByText(/^Showing /)).not.toBeInTheDocument())
    expect(screen.getByText('run 1')).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /Job History \(12\)/, hidden: true })
    ).toBeInTheDocument()
    // One initial load, two "load more" steps and one two-part refetch: no polling loop.
    expect(jobRequests).toHaveLength(5)
  }, 20000)
})
