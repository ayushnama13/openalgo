import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const toast = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  info: vi.fn(),
  warning: vi.fn(),
}))
const navigate = vi.hoisted(() => vi.fn())

vi.mock('react-router', () => ({ useNavigate: () => navigate }))
vi.mock('@/hooks/useSocket', () => ({ useSocket: () => ({ socket: null }) }))
vi.mock('@/api/client', () => ({ fetchCSRFToken: vi.fn().mockResolvedValue('token') }))
vi.mock('@/components/ui/python-editor', () => ({ PythonEditor: () => null }))
vi.mock('@/components/backtest/EngineOptionsCard', () => ({ default: () => null }))
vi.mock('@/components/backtest/ExtraSymbols', () => ({ default: () => null }))
vi.mock('@/components/backtest/OptimizeDialog', () => ({ default: () => null }))
vi.mock('@/utils/toast', () => ({ showToast: toast }))

import Backtest from './Backtest'

const UNIVERSES = {
  status: 'success',
  as_of: '2026-11-18',
  source: 'niftyindices.com',
  universes: [
    { id: 'NIFTY50', name: 'NIFTY 50', category: 'broad', exchange: 'NSE', count: 3 },
    { id: 'NIFTYBANK', name: 'NIFTY BANK', category: 'sector', exchange: 'NSE', count: 2 },
  ],
}

const coverage = (interval: string) => ({
  status: 'success',
  universe: {
    id: 'NIFTY50',
    name: 'NIFTY 50',
    category: 'broad',
    exchange: 'NSE',
    interval,
    base_interval: interval === 'D' ? 'D' : '1m',
    as_of: '2026-11-18',
    source: 'niftyindices.com',
    survivorship_note: 'Current members only.',
    count: 3,
    with_data: 2,
    common_first: '2024-01-01',
    common_last: '2026-11-13',
    constituents: [
      {
        symbol: 'RELIANCE',
        name: 'Reliance',
        industry: 'Oil',
        has_data: true,
        first: '2024-01-01',
        last: '2026-11-13',
        records: 700,
      },
      {
        symbol: 'SBIN',
        name: 'SBI',
        industry: 'Banks',
        has_data: true,
        first: '2023-01-01',
        last: '2026-11-13',
        records: 900,
      },
      {
        symbol: 'INFY',
        name: 'Infosys',
        industry: 'IT',
        has_data: false,
        first: null,
        last: null,
        records: 0,
      },
    ],
  },
})

const STARRABLE_JOB = {
  job_id: 'job-1',
  name: 'SBIN D backtest',
  status: 'completed',
  symbols: ['NSE:SBIN'],
  exchange: 'NSE',
  interval: 'D',
  initial_capital: 100000,
  created_at: '2026-11-10T10:00:00',
  total_return_pct: 4.2,
  sharpe_ratio: 1.1,
  max_drawdown_pct: 2.5,
  win_rate_pct: 55,
  total_trades: 12,
  batch_seq: 0,
  mode: 'stock',
  starred: false,
  notes: null,
}

let runBodies: Record<string, unknown>[] = []
let requests: { method: string; path: string; search: string; body?: unknown }[] = []
let runResponse: Record<string, unknown> = {}
let batchPolls = 0

// Generous waits: under a parallel suite the listing -> coverage chain can take seconds.
const WAIT = { timeout: 6000 }

const enterUniverseMode = async () => {
  fireEvent.click(screen.getByRole('button', { name: 'Universe' }))
  await waitFor(() => expect(screen.getByText('2 / 3')).toBeInTheDocument(), WAIT)
}

const run = async () => {
  const before = runBodies.length
  fireEvent.click(screen.getByRole('button', { name: /Run \/ Queue Backtest/ }))
  await waitFor(() => expect(runBodies).toHaveLength(before + 1), WAIT)
  return runBodies[runBodies.length - 1]
}

// Each test renders the whole page and one waits on a real 2.5 s poll.
describe('Backtest universe mode', { timeout: 20000 }, () => {
  beforeEach(() => {
    runBodies = []
    requests = []
    batchPolls = 0
    runResponse = { status: 'success', job_id: 'queued-1', jobs: [{ job_id: 'queued-1' }] }
    navigate.mockReset()
    for (const fn of Object.values(toast)) fn.mockReset()
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), 'http://localhost')
        const method = init?.method ?? 'GET'
        const reqBody = init?.body ? JSON.parse(String(init.body)) : undefined
        requests.push({ method, path: url.pathname, search: url.search, body: reqBody })
        let body: unknown = { status: 'error' }
        if (url.pathname === '/backtest/api/catalog') {
          body = { status: 'success', catalog: [{ symbol: 'RELIANCE', exchange: 'NSE' }] }
        } else if (url.pathname === '/backtest/api/range') {
          body = { status: 'error' }
        } else if (url.pathname === '/backtest/api/jobs') {
          body = { status: 'success', jobs: [STARRABLE_JOB], total: 1 }
        } else if (url.pathname === '/backtest/api/jobs/job-1/meta') {
          body = { status: 'success', job_id: 'job-1', starred: reqBody.starred, notes: '' }
        } else if (url.pathname === '/backtest/api/universes') {
          body = UNIVERSES
        } else if (url.pathname === '/backtest/api/universes/NIFTY50') {
          body = coverage(url.searchParams.get('interval') ?? 'D')
        } else if (url.pathname === '/backtest/api/run') {
          runBodies.push(reqBody)
          body = runResponse
        } else if (url.pathname === '/backtest/api/batches/batch-1') {
          batchPolls += 1
          const done = batchPolls > 1
          body = {
            status: 'success',
            batch_id: 'batch-1',
            name: 'NIFTY 50 scan D',
            mode: 'scan',
            universe: 'NIFTY50',
            runs: [
              {
                job_id: 'r1',
                name: 'a',
                interval: 'D',
                batch_seq: 0,
                status: 'completed',
                symbols: ['NSE:RELIANCE'],
              },
              {
                job_id: 'r2',
                name: 'b',
                interval: 'D',
                batch_seq: 1,
                status: done ? 'failed' : 'running',
                symbols: ['NSE:SBIN'],
              },
            ],
          }
        }
        return { ok: true, json: async () => body } as Response
      })
    )
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('does not fetch universes until Universe mode is opened', async () => {
    render(<Backtest />)
    await waitFor(
      () => expect(requests.some((r) => r.path === '/backtest/api/jobs')).toBe(true),
      WAIT
    )
    expect(requests.some((r) => r.path.startsWith('/backtest/api/universes'))).toBe(false)
  })

  it('sends a portfolio run over the universe with daily, delivery defaults', async () => {
    render(<Backtest />)
    await enterUniverseMode()
    await waitFor(() => expect(screen.getByDisplayValue('2024-01-01')).toBeInTheDocument(), WAIT)

    const body = await run()
    expect(body).toMatchObject({
      name: 'NIFTY 50 portfolio D',
      mode: 'universe',
      universe: 'NIFTY50',
      exchange: 'NSE',
      symbols: ['NSE:RELIANCE', 'NSE:SBIN'],
      intervals: ['D'],
      interval: 'D',
      product_type: 'CNC',
      start_date: '2024-01-01',
      end_date: '2026-11-13',
    })
  })

  it('keeps a scan on one timeframe and rejects Optimize', async () => {
    render(<Backtest />)
    await enterUniverseMode()
    fireEvent.click(screen.getByRole('button', { name: 'Scan' }))
    fireEvent.click(screen.getByRole('button', { name: '1h' }))
    fireEvent.click(screen.getByRole('button', { name: '15m' }))
    expect(screen.getByRole('button', { name: /Optimize/ })).toBeDisabled()

    const body = await run()
    expect(body).toMatchObject({ mode: 'scan', intervals: ['15m'], interval: '15m' })
    expect(String(body.name)).toBe('NIFTY 50 scan 15m')
    // Coverage follows the selected timeframe.
    expect(
      requests.some(
        (r) => r.path === '/backtest/api/universes/NIFTY50' && r.search === '?interval=15m'
      )
    ).toBe(true)
  })

  it('follows a multi-run batch to its results once every run is terminal', async () => {
    runResponse = {
      status: 'success',
      job_id: 'r1',
      batch_id: 'batch-1',
      jobs: [{ job_id: 'r1' }, { job_id: 'r2' }],
    }
    render(<Backtest />)
    await enterUniverseMode()
    fireEvent.click(screen.getByRole('button', { name: 'Scan' }))
    await run()

    await waitFor(() => expect(screen.getByText('1 / 2 done')).toBeInTheDocument(), WAIT)
    await waitFor(
      () => expect(navigate).toHaveBeenCalledWith('/backtest/results?batch_id=batch-1'),
      WAIT
    )
    expect(toast.warning).toHaveBeenCalledWith('1 of 2 runs failed')
  })

  it('stars a run through the meta endpoint', async () => {
    render(<Backtest />)
    const star = await screen.findByRole('button', { name: 'Star run' }, WAIT)
    fireEvent.click(star)
    await waitFor(
      () =>
        expect(requests).toContainEqual(
          expect.objectContaining({
            method: 'PATCH',
            path: '/backtest/api/jobs/job-1/meta',
            body: { starred: true },
          })
        ),
      WAIT
    )
    expect(await screen.findByRole('button', { name: 'Unstar run' }, WAIT)).toBeInTheDocument()
  })
})
