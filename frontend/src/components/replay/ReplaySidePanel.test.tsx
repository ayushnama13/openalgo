import { fireEvent, render, screen } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { describe, expect, it, vi } from 'vitest'

const depthProps = vi.hoisted(() => ({ last: null as Record<string, unknown> | null }))
vi.mock('@/components/trading/Depth20Panel', () => ({
  Depth20Panel: (props: Record<string, unknown>) => {
    depthProps.last = props
    return <div data-testid="depth-ladder" />
  },
}))

import { ReplaySidePanel } from './ReplaySidePanel'

type Props = ComponentProps<typeof ReplaySidePanel>

const baseProps = (over: Partial<Props> = {}): Props => ({
  collapsed: false,
  onToggleCollapsed: vi.fn(),
  tab: 'watchlist',
  onTabChange: vi.fn(),
  symbols: ['NIFTY', 'reliance'],
  selectedSymbol: 'NIFTY',
  onSelectSymbol: vi.fn(),
  onTicketSymbol: vi.fn(),
  quotes: {
    NIFTY: { ltp: 24812.5, percentChange: 0.42 },
    RELIANCE: { ltp: 1284, percentChange: -0.2 },
  },
  depthRow: {},
  positions: [],
  totalPnL: 0,
  onResetPositions: vi.fn(),
  qty: '50',
  onQtyChange: vi.fn(),
  ltp: 24812.5,
  onOrder: vi.fn(),
  ...over,
})

describe('ReplaySidePanel', () => {
  it('shows quotes by upper-cased symbol and selects a row through the chart-loading handler', () => {
    const props = baseProps()
    render(<ReplaySidePanel {...props} />)

    const row = screen.getByRole('button', { name: /reliance/ })
    expect(row).toHaveTextContent('1,284')
    expect(row).toHaveTextContent('-0.20%')
    expect(screen.getByRole('button', { name: /NIFTY/, pressed: true })).toHaveTextContent('+0.42%')

    fireEvent.click(row)
    expect(props.onSelectSymbol).toHaveBeenCalledWith('reliance')
    expect(props.onTicketSymbol).not.toHaveBeenCalled()
  })

  it('keeps the order ticket visible on every tab and sends the side', () => {
    const props = baseProps({ tab: 'depth' })
    render(<ReplaySidePanel {...props} />)

    expect(screen.getByTestId('depth-ladder')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /SELL/ }))
    fireEvent.click(screen.getByRole('button', { name: /BUY/ }))
    expect((props.onOrder as ReturnType<typeof vi.fn>).mock.calls).toEqual([['SELL'], ['BUY']])

    fireEvent.change(screen.getByLabelText('Qty'), { target: { value: '75' } })
    expect(props.onQtyChange).toHaveBeenCalledWith('75')
  })

  it('passes the ladder a replay row, never null (null would start live mode)', () => {
    const row = { ltp: 1, bid_1: 0.95 }
    render(<ReplaySidePanel {...baseProps({ tab: 'depth', depthRow: row })} />)
    expect(depthProps.last?.replayData).toBe(row)
    expect(depthProps.last?.symbol).toBe('NIFTY')
    expect(depthProps.last?.onClose).toBeUndefined()
  })

  it('lists positions with signed quantity and P&L, and resets them', () => {
    const props = baseProps({
      tab: 'positions',
      totalPnL: -35,
      positions: [
        {
          id: 'a',
          symbol: 'NIFTY',
          action: 'BUY',
          quantity: 50,
          fill_price: 24800,
          ltp: 24812.5,
          pnl: 625,
        },
        {
          id: 'b',
          symbol: 'RELIANCE',
          action: 'SELL',
          quantity: 10,
          fill_price: 1218,
          ltp: 1284,
          pnl: -660,
        },
      ],
    })
    render(<ReplaySidePanel {...props} />)

    expect(screen.getByRole('tab', { name: 'Positions (2)' })).toBeInTheDocument()
    expect(screen.getByText('+50')).toBeInTheDocument()
    expect(screen.getByText('-10')).toBeInTheDocument()
    expect(screen.getByText('+625.00')).toBeInTheDocument()
    expect(screen.getByText('-660.00')).toBeInTheDocument()
    expect(screen.getByText('PnL: ₹-35.00 (2)')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Reset' }))
    expect(props.onResetPositions).toHaveBeenCalledTimes(1)
  })

  it('collapses to a strip that can be expanded again', () => {
    const props = baseProps({ collapsed: true })
    render(<ReplaySidePanel {...props} />)
    expect(screen.queryByRole('button', { name: /BUY/ })).not.toBeInTheDocument()
    fireEvent.click(screen.getByTitle('Expand panel'))
    expect(props.onToggleCollapsed).toHaveBeenCalledTimes(1)
  })
})
