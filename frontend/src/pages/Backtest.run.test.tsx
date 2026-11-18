import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const toast = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  info: vi.fn(),
  warning: vi.fn(),
}))

vi.mock('react-router', () => ({ useNavigate: () => vi.fn() }))
vi.mock('@/hooks/useSocket', () => ({ useSocket: () => ({ socket: null }) }))
vi.mock('@/api/client', () => ({ fetchCSRFToken: vi.fn().mockResolvedValue('token') }))
vi.mock('@/components/ui/python-editor', () => ({ PythonEditor: () => null }))
vi.mock('@/components/backtest/EngineOptionsCard', () => ({ default: () => null }))
vi.mock('@/components/backtest/ExtraSymbols', () => ({ default: () => null }))
vi.mock('@/components/backtest/OptimizeDialog', () => ({ default: () => null }))
vi.mock('@/utils/toast', () => ({ showToast: toast }))

import Backtest from './Backtest'

const STORED_JOB = {
  job_id: 'job-1',
  name: 'SBIN 5m backtest',
  status: 'completed',
  symbols: ['NSE:SBIN'],
  exchange: 'NSE',
  interval: '5m',
  start_date: '2026-03-01',
  end_date: '2026-06-30',
  initial_capital: 500000,
  created_at: '2026-11-10T10:00:00',
  total_return_pct: 4.2,
  sharpe_ratio: 1.1,
  max_drawdown_pct: 2.5,
  win_rate_pct: 55,
  total_trades: 12,
  batch_seq: 0,
}
const STORED_JOB_DETAILS = {
  ...STORED_JOB,
  strategy_code: 'def strategy(df):\n    df["signal"] = 0\n    return df\n',
  sizing_type: 'pct_equity',
  sizing_value: 10,
  slippage_bps: 2,
  brokerage_bps: 3,
  product_type: 'NRML',
  missing_data_policy: 'forward_fill',
  stop_loss_pct: 2,
  take_profit_pct: 4,
  daily_loss_limit: 1000,
  max_positions: 2,
}

let runBodies: Record<string, unknown>[] = []

/** The input under a field label, opening settings tabs if the page has them. */
const field = (label: string): HTMLInputElement => {
  for (const tab of [null, 'Data', 'Execution', 'Risk', 'Engine']) {
    if (tab) {
      const trigger = screen.queryByRole('tab', { name: tab })
      if (!trigger) continue
      fireEvent.mouseDown(trigger)
      fireEvent.click(trigger)
    }
    const el = screen.queryByText(label, { selector: 'label' })
    const input = el?.parentElement?.querySelector('input')
    if (input) return input
  }
  throw new Error(`no field labelled ${label}`)
}

/** History is a dialog in the old layout and an always-present panel in the new one. */
const openHistory = () => {
  const opener = screen.queryByRole('button', { name: /Job History/ })
  if (opener) fireEvent.click(opener)
  const tab = screen.queryByRole('tab', { name: /^History/ })
  if (tab) {
    fireEvent.mouseDown(tab)
    fireEvent.click(tab)
  }
}

const run = async () => {
  const before = runBodies.length
  fireEvent.click(screen.getByRole('button', { name: /Run \/ Queue Backtest/ }))
  await waitFor(() => expect(runBodies).toHaveLength(before + 1))
  return runBodies[runBodies.length - 1]
}

describe('Backtest run request', () => {
  beforeEach(() => {
    runBodies = []
    for (const fn of Object.values(toast)) fn.mockReset()
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), 'http://localhost')
        let body: unknown = { status: 'error' }
        if (url.pathname === '/backtest/api/catalog') {
          body = { status: 'success', catalog: [{ symbol: 'RELIANCE', exchange: 'NSE' }] }
        } else if (url.pathname === '/backtest/api/range') {
          body =
            url.searchParams.get('symbol') === 'RELIANCE'
              ? {
                  status: 'success',
                  first_timestamp: 1,
                  last_timestamp: 2,
                  start_date: '2026-01-01 09:15:00',
                  end_date: '2026-11-13 15:30:00',
                  record_count: 80000,
                }
              : { status: 'error' }
        } else if (url.pathname === '/backtest/api/jobs') {
          body = { status: 'success', jobs: [STORED_JOB], total: 1 }
        } else if (url.pathname === '/backtest/api/jobs/job-1') {
          body = { status: 'success', job: STORED_JOB_DETAILS }
        } else if (url.pathname === '/backtest/api/run') {
          runBodies.push(JSON.parse(String(init?.body)))
          body = { status: 'success', job_id: `queued-${runBodies.length}` }
        }
        return { ok: true, json: async () => body } as Response
      })
    )
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('sends the defaults, seeded from the catalog and the available data range', async () => {
    render(<Backtest />)
    await waitFor(() => expect(field('Start Date')).toHaveValue('2026-01-01'))

    const body = await run()
    expect(body).toEqual({
      name: 'RELIANCE 1m backtest',
      strategy_code: expect.stringContaining('def strategy(df):'),
      symbols: ['NSE:RELIANCE'],
      exchange: 'NSE',
      intervals: ['1m'],
      interval: '1m',
      start_date: '2026-01-01',
      end_date: '2026-11-13',
      initial_capital: 100000,
      sizing_type: 'fixed_qty',
      sizing_value: 1,
      slippage_bps: 0,
      brokerage_bps: 20,
      product_type: 'MIS',
      missing_data_policy: 'skip',
      stop_loss_pct: null,
      take_profit_pct: null,
      daily_loss_limit: null,
      max_positions: 5,
    })
  })

  it('sends edited fields and extra timeframes', async () => {
    render(<Backtest />)
    await waitFor(() => expect(field('Start Date')).toHaveValue('2026-01-01'))

    fireEvent.change(field('Initial Capital'), { target: { value: '250000' } })
    fireEvent.change(field('End Date'), { target: { value: '2026-06-30' } })
    fireEvent.change(field('Slippage (bps)'), { target: { value: '5' } })
    fireEvent.change(field('Brokerage (bps)'), { target: { value: '7' } })
    fireEvent.change(field('Max Open Positions'), { target: { value: '3' } })
    fireEvent.change(field('Stop Loss %'), { target: { value: '1.5' } })
    fireEvent.change(field('Take Profit %'), { target: { value: '3' } })
    fireEvent.change(field('Daily Loss Limit (Rs)'), { target: { value: '2500' } })
    field('Start Date') // back to the tab that holds the timeframes
    fireEvent.click(screen.getByRole('button', { name: '15m' }))

    const body = await run()
    expect(body).toMatchObject({
      name: 'RELIANCE MTF backtest',
      intervals: ['1m', '15m'],
      interval: '1m',
      start_date: '2026-01-01',
      end_date: '2026-06-30',
      initial_capital: 250000,
      slippage_bps: 5,
      brokerage_bps: 7,
      max_positions: 3,
      stop_loss_pct: 1.5,
      take_profit_pct: 3,
      daily_loss_limit: 2500,
    })
  })

  it('loads a past run into the form on Rerun and sends those parameters', async () => {
    render(<Backtest />)
    await waitFor(() => expect(field('Start Date')).toHaveValue('2026-01-01'))

    openHistory()
    fireEvent.click(await screen.findByTitle('Rerun with same parameters'))
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith('Job parameters loaded - press Run')
    )
    await waitFor(() => expect(field('Start Date')).toHaveValue('2026-03-01'))

    const body = await run()
    expect(body).toEqual({
      name: 'SBIN 5m backtest',
      strategy_code: STORED_JOB_DETAILS.strategy_code,
      symbols: ['NSE:SBIN'],
      exchange: 'NSE',
      intervals: ['5m'],
      interval: '5m',
      start_date: '2026-03-01',
      end_date: '2026-06-30',
      initial_capital: 500000,
      sizing_type: 'pct_equity',
      sizing_value: 10,
      slippage_bps: 2,
      brokerage_bps: 3,
      product_type: 'NRML',
      missing_data_policy: 'forward_fill',
      stop_loss_pct: 2,
      take_profit_pct: 4,
      daily_loss_limit: 1000,
      max_positions: 2,
    })
  })
})
