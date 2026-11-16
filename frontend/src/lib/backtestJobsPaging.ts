// Paging helpers for the backtest job history list (GET /backtest/api/jobs,
// newest first, `limit` / `offset` / `total`).

/** Rows per "Load more" step; also the server's default page size. */
export const JOBS_PAGE_SIZE = 500

/** Largest `limit` the server accepts in one request. */
export const JOBS_MAX_LIMIT = 2000

export interface PagedJob {
  job_id: string
  created_at?: string
}

/**
 * Merge a freshly fetched first page over the list already on screen.
 *
 * The fresh page is authoritative for its own time range: a loaded row that is
 * newer than the page's oldest row but missing from the page was deleted, so it
 * is dropped. Rows older than the page (pages the user loaded with "Load
 * more") are kept. A page shorter than `pageSize` is the whole list.
 */
export function mergeFirstPage<T extends PagedJob>(
  prev: T[],
  fresh: T[],
  pageSize: number = JOBS_PAGE_SIZE
): T[] {
  if (fresh.length < pageSize) return fresh
  const ids = new Set(fresh.map((job) => job.job_id))
  const oldest = fresh[fresh.length - 1].created_at ?? ''
  const older = prev.filter((job) => !ids.has(job.job_id) && (job.created_at ?? '') <= oldest)
  return [...fresh, ...older]
}

/** Append the next page, skipping rows already loaded (offsets shift when new runs arrive). */
export function appendPage<T extends PagedJob>(prev: T[], page: T[]): T[] {
  const ids = new Set(prev.map((job) => job.job_id))
  return [...prev, ...page.filter((job) => !ids.has(job.job_id))]
}
