import { describe, expect, it } from 'vitest'
import { binReturns } from './ScanResults'

describe('binReturns', () => {
  it('returns nothing for no runs', () => {
    expect(binReturns([]).counts).toEqual([])
  })

  it('puts every return in a bin, the maximum included', () => {
    const values = [-10, -2, 0, 3, 7.5, 12, 30]
    const bins = binReturns(values)
    expect(bins.counts).toHaveLength(8)
    expect(bins.counts.reduce((a, b) => a + b, 0)).toBe(values.length)
    expect(bins.counts[0]).toBe(1)
    expect(bins.counts[7]).toBe(1)
    expect(bins.labels[0]).toBe('-10.0% to -5.0%')
  })

  it('handles identical returns without a zero-width bin', () => {
    const bins = binReturns([4, 4, 4])
    expect(bins.width).toBe(1)
    expect(bins.counts.reduce((a, b) => a + b, 0)).toBe(3)
  })
})
