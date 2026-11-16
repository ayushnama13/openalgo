import { fireEvent, render, screen } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { describe, expect, it, vi } from 'vitest'

vi.mock('react-router', () => ({
  Link: ({ children }: { children: React.ReactNode }) => <a href="/">{children}</a>,
}))

import { ReplayTopBar, timePart } from './ReplayTopBar'

// The popover positions itself with `new ResizeObserver(...)`; the shared test
// setup's stand-in is an arrow function and cannot be constructed.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal('ResizeObserver', ResizeObserverStub)

type Props = ComponentProps<typeof ReplayTopBar>

const clock = {
  current_time: '2026-11-13T10:42:15+05:30',
  start_time: '2026-11-13T09:15:00+05:30',
  end_time: '2026-11-13T15:30:00+05:30',
  is_playing: false,
  speed: 5,
  session_date: '2026-11-13',
  watchlist: ['NIFTY'],
}

const baseProps = (over: Partial<Props> = {}): Props => ({
  clock,
  progress: 23,
  onControl: vi.fn(),
  onSeekPercent: vi.fn(),
  onQuickJump: vi.fn(),
  // A draft that differs from the running session on purpose.
  draft: { date: '2026-08-10', start: '09:15:00', end: '15:30:00', watchlist: 'NIFTY, RELIANCE' },
  draftDirty: false,
  onDraftChange: vi.fn(),
  onApplySession: vi.fn(),
  layouts: [
    { id: 'single', label: 'Single', cols: '1fr', rows: '1fr', areas: '"a"', cells: ['a'] },
  ],
  layoutId: 'single',
  onLayoutChange: vi.fn(),
  available: [],
  onUseAvailable: vi.fn(),
  ...over,
})

describe('timePart', () => {
  it('extracts the wall-clock time from an ISO timestamp', () => {
    expect(timePart('2026-11-13T10:42:15+05:30')).toBe('10:42:15')
    expect(timePart('2026-11-13T10:42:15.250000+05:30')).toBe('10:42:15')
    expect(timePart('garbage')).toBe('garbage')
    expect(timePart(undefined)).toBe('')
  })
})

describe('ReplayTopBar', () => {
  it('shows the applied session from the clock, not the draft', () => {
    render(<ReplayTopBar {...baseProps()} />)
    expect(screen.getByText('2026-11-13')).toBeInTheDocument()
    expect(screen.queryByText('2026-08-10')).not.toBeInTheDocument()
    expect(screen.getByText('10:42:15')).toBeInTheDocument()
    expect(screen.getByText('Paused')).toBeInTheDocument()
    expect(screen.getByText('09:15', { selector: 'span' })).toBeInTheDocument()
    expect(screen.getByText('15:30', { selector: 'span' })).toBeInTheDocument()
    expect(screen.queryByText('not applied')).not.toBeInTheDocument()
  })

  it('flags an unapplied draft on the Session button', () => {
    render(<ReplayTopBar {...baseProps({ draftDirty: true })} />)
    expect(screen.getByRole('button', { name: /Session/ })).toHaveTextContent('not applied')
  })

  it('edits and applies the session from the popover', () => {
    const props = baseProps()
    render(<ReplayTopBar {...props} />)
    fireEvent.click(screen.getByRole('button', { name: /Session/ }))

    const date = screen.getByLabelText('Date')
    expect(date).toHaveValue('2026-08-10')
    fireEvent.change(date, { target: { value: '2026-11-14' } })
    expect(props.onDraftChange).toHaveBeenCalledWith({ date: '2026-11-14' })
    fireEvent.change(screen.getByLabelText('Watchlist'), { target: { value: 'SBIN' } })
    expect(props.onDraftChange).toHaveBeenCalledWith({ watchlist: 'SBIN' })

    fireEvent.click(screen.getByRole('button', { name: 'Apply Session' }))
    expect(props.onApplySession).toHaveBeenCalledTimes(1)
  })

  it('sends playback, step, seek and jump actions', () => {
    const props = baseProps()
    render(<ReplayTopBar {...props} />)
    fireEvent.click(screen.getByRole('button', { name: /Play/ }))
    fireEvent.click(screen.getByRole('button', { name: '+1s' }))
    fireEvent.click(screen.getByRole('button', { name: '+5s' }))
    expect((props.onControl as ReturnType<typeof vi.fn>).mock.calls).toEqual([
      ['play'],
      ['step', { seconds: 1 }],
      ['step', { seconds: 5 }],
    ])

    fireEvent.change(screen.getByLabelText('Session timeline'), { target: { value: '50' } })
    expect(props.onSeekPercent).toHaveBeenCalledWith(50)
    fireEvent.click(screen.getByRole('button', { name: '14:30' }))
    expect(props.onQuickJump).toHaveBeenCalledWith('14:30:00')
  })

  it('offers Pause while playing and hides playback until the clock has loaded', () => {
    const playing = baseProps({ clock: { ...clock, is_playing: true } })
    const { unmount } = render(<ReplayTopBar {...playing} />)
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))
    expect(playing.onControl).toHaveBeenCalledWith('pause')
    unmount()

    render(<ReplayTopBar {...baseProps({ clock: null })} />)
    expect(screen.queryByRole('button', { name: /Play/ })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Session timeline')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Session/ })).toBeInTheDocument()
  })
})
