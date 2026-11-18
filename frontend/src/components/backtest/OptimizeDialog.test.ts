import { describe, expect, it } from 'vitest'
import { countCombos } from './OptimizeDialog'

describe('countCombos', () => {
  it('rejects an empty parameter space', () => {
    expect(countCombos({})).toBeNull()
  })
})
