/**
 * Depth Recorder API client (analytics endpoints)
 */

import { webClient } from './client'

export interface SessionEntry {
  symbol: string
  exchange: string
  day: string
  n_ticks: number
  first: string
  last: string
}

export interface HealthGap {
  from: string
  to: string
  duration_s: number
}

export interface HealthData {
  status: string
  total_packets: number
  packets_per_min: { minute: string; packets: number }[]
  gaps: HealthGap[]
}

export interface LeadLagCell {
  horizon: number
  bucket: string
  bucket_label: string
  pearson: number | null
  spearman: number | null
  n: number
}

export interface LeadLagData {
  status: string
  results: LeadLagCell[]
  scatter: { epoch: number; x: number; y: number }[]
}

export interface WallRow {
  price: number
  time_weighted_qty: number
  avg_order_size: number | null
  samples: number
}

export interface WallsData {
  status: string
  walls: WallRow[]
}

export interface DepthLevel {
  price: number | null
  quantity: number | null
  orders?: number | null
}

export interface Tick {
  id: number
  symbol: string
  exchange: string
  tick_time: string
  ltp: number | null
  volume: number | null
  oi: number | null
  total_buy_qty: number | null
  total_sell_qty: number | null
  depth_level?: number
  bids: DepthLevel[]
  asks: DepthLevel[]
}

export interface RecorderStatus {
  symbol: string
  exchange: string
  status: string
  ticks_written: number
  ticks_buffered: number
  last_tick_time: string | null
  connected: boolean
  error: string | null
  started_at: string | null
  depth_level?: number
  actual_depth?: number
}

export interface StatsData {
  total_ticks: number
  symbols: { symbol: string; exchange: string }[]
  latest_tick_time: string | null
}

export interface MetricsPayload {
  status: string
  symbol: string
  exchange: string
  day: string
  rows: number
  has_orders: boolean
  first: string
  last: string
  series: Record<string, Array<number | null>>
  ladder: Array<{ bids: DepthLevel[]; asks: DepthLevel[] }>
}

export async function fetchSessions(
  symbol?: string,
  exchange = 'NSE'
): Promise<SessionEntry[]> {
  const params = new URLSearchParams({ exchange })
  if (symbol) params.set('symbol', symbol)
  const r = await webClient.get(`/api/depth-recorder/sessions?${params}`)
  return r.data?.sessions ?? []
}

export async function fetchHealth(
  symbol: string,
  exchange: string,
  day: string
): Promise<HealthData> {
  const r = await webClient.get(
    `/api/depth-recorder/health?symbol=${symbol}&exchange=${exchange}&day=${day}`
  )
  return r.data
}

export async function fetchLeadLag(
  symbol: string,
  exchange: string,
  day: string,
  horizons = [1, 10, 30, 120]
): Promise<LeadLagData> {
  const r = await webClient.get(
    `/api/depth-recorder/leadlag?symbol=${symbol}&exchange=${exchange}&day=${day}&horizons=${horizons.join(',')}`
  )
  return r.data
}

export async function fetchWalls(
  symbol: string,
  exchange: string,
  day: string,
  top = 8
): Promise<WallsData> {
  const r = await webClient.get(
    `/api/depth-recorder/walls?symbol=${symbol}&exchange=${exchange}&day=${day}&top=${top}`
  )
  return r.data
}

export async function fetchMetrics(
  symbol: string,
  exchange: string,
  day: string,
  maxPoints = 12000
): Promise<MetricsPayload> {
  const r = await webClient.get(
    `/api/depth-recorder/metrics?symbol=${symbol}&exchange=${exchange}&day=${day}&max_points=${maxPoints}`
  )
  return r.data
}

export async function startRecorder(
  symbol: string,
  exchange: string,
  depth_level?: number
): Promise<{ status: string; message?: string; [key: string]: unknown }> {
  const body: Record<string, unknown> = { symbol, exchange }
  if (depth_level != null) body.depth_level = depth_level
  const r = await webClient.post('/api/depth-recorder/start', body)
  return r.data
}

export async function stopRecorder(
  symbol: string,
  exchange: string
): Promise<{ status: string; message?: string; [key: string]: unknown }> {
  const r = await webClient.post('/api/depth-recorder/stop', { symbol, exchange })
  return r.data
}

export async function deleteRecorderConfig(
  symbol: string,
  exchange: string
): Promise<{ status: string; message?: string; [key: string]: unknown }> {
  const r = await webClient.delete('/api/depth-recorder/config', { data: { symbol, exchange } })
  return r.data
}

export async function setDepthLevel(
  symbol: string,
  exchange: string,
  depth_level: number
): Promise<{ status: string; message?: string; depth_level?: number; actual_depth?: number; [key: string]: unknown }> {
  const r = await webClient.post('/api/depth-recorder/depth-level', { symbol, exchange, depth_level })
  return r.data
}

export async function fetchRecorderStatus(): Promise<RecorderStatus[]> {
  const r = await webClient.get('/api/depth-recorder/status')
  return r.data ?? []
}

export async function fetchStats(): Promise<StatsData> {
  const r = await webClient.get('/api/depth-recorder/stats')
  return r.data
}

export async function fetchTicks(
  symbol: string,
  exchange: string,
  limit = 1
): Promise<{ ticks: Tick[]; [key: string]: unknown }> {
  const r = await webClient.get(
    `/api/depth-recorder/ticks?symbol=${encodeURIComponent(symbol)}&exchange=${encodeURIComponent(exchange)}&limit=${limit}`
  )
  return r.data
}

export function heatmapUrl(symbol: string, exchange: string, day: string): string {
  return `/api/depth-recorder/heatmap.png?symbol=${encodeURIComponent(symbol)}&exchange=${encodeURIComponent(exchange)}&day=${encodeURIComponent(day)}`
}