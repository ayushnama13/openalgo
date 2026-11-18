import { describe, expect, it } from 'vitest'
import { appendPage, mergeFirstPage } from './backtestJobsPaging'

// Newest first, like the server: job "j9" is newer than "j1".
const job = (n: number) => ({
  job_id: `j${n}`,
  created_at: `2026-11-16T10:00:${String(n).padStart(2, '0')}`,
})
const ids = (rows: { job_id: string }[]) => rows.map((row) => row.job_id)

describe('mergeFirstPage', () => {
  it('replaces the list when the fresh page is short (it is the whole list)', () => {
    const prev = [job(5), job(4), job(3), job(2), job(1)]
    const fresh = [job(5), job(3)]
    expect(ids(mergeFirstPage(prev, fresh, 3))).toEqual(['j5', 'j3'])
  })

  it('keeps older loaded pages when a new job arrives at the head', () => {
    // Two pages of 3 loaded; j7 is new, so j4 slides out of the first page.
    const prev = [job(6), job(5), job(4), job(3), job(2), job(1)]
    const fresh = [job(7), job(6), job(5)]
    expect(ids(mergeFirstPage(prev, fresh, 3))).toEqual(['j7', 'j6', 'j5', 'j4', 'j3', 'j2', 'j1'])
  })

  it('drops a row deleted inside the first page range', () => {
    // j5 was deleted on the server, so the page now reaches down to j3.
    const prev = [job(6), job(5), job(4), job(3), job(2), job(1)]
    const fresh = [job(6), job(4), job(3)]
    expect(ids(mergeFirstPage(prev, fresh, 3))).toEqual(['j6', 'j4', 'j3', 'j2', 'j1'])
  })

  it('keeps an older row that shares the timestamp of the page boundary', () => {
    const twin = { job_id: 'twin', created_at: job(4).created_at }
    const prev = [job(6), job(5), job(4), twin, job(1)]
    const fresh = [job(6), job(5), job(4)]
    expect(ids(mergeFirstPage(prev, fresh, 3))).toEqual(['j6', 'j5', 'j4', 'twin', 'j1'])
  })

  it('takes updated fields from the fresh page', () => {
    const prev = [{ ...job(3), status: 'running' }, job(2), job(1)]
    const fresh = [{ ...job(3), status: 'completed' }, job(2), job(1)]
    expect(mergeFirstPage(prev, fresh, 3)[0]).toMatchObject({ status: 'completed' })
  })
})

describe('appendPage', () => {
  it('appends the next page', () => {
    expect(ids(appendPage([job(4), job(3)], [job(2), job(1)]))).toEqual(['j4', 'j3', 'j2', 'j1'])
  })

  it('skips rows already loaded when offsets have shifted', () => {
    expect(ids(appendPage([job(4), job(3)], [job(3), job(2)]))).toEqual(['j4', 'j3', 'j2'])
  })
})
