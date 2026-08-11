import { create } from 'zustand'

export interface BacktestTrade {
  id: number
  symbol: string
  exchange: string
  action: string
  entry_time: string
  entry_price: number
  exit_time: string
  exit_price: number
  quantity: number
  pnl: number
  pnl_pct: number
  holding_period_bars: number
  entry_reason?: string
  exit_reason?: string
  costs: number
}

export interface Metrics {
  initial_capital: number
  ending_equity: number
  total_pnl: number
  total_return_pct: number
  cagr_pct: number
  sharpe_ratio: number
  sortino_ratio: number
  max_drawdown_pct: number
  total_trades: number
  win_rate_pct: number
  profit_factor: number
  avg_win: number
  avg_loss: number
  win_loss_ratio: number
  expectancy: number
  avg_holding_bars: number
  avg_holding_minutes: number
  monthly_heatmap?: {
    years: string[]
    months: string[]
    matrix: Record<string, Record<string, number | null>>
  }
  notes?: string[]
}

export interface EquityPoint {
  timestamp: string
  equity: number
  drawdown_pct: number
  cash: number
  positions_value: number
}

export interface BenchmarkPoint {
  timestamp: string
  benchmark_equity: number
}

export interface ResultsData {
  job_id: string
  name: string
  metrics: Metrics
  equity_curve: EquityPoint[]
  trades: BacktestTrade[]
  benchmark_curve: BenchmarkPoint[]
}

export interface FullJobDetails {
  job_id: string
  name: string
  status: string
  strategy_code: string
  symbols: string[]
  exchange: string
  interval: string
  start_date?: string
  end_date?: string
  initial_capital: number
  sizing_type: string
  sizing_value: number
  slippage_bps: number
  brokerage_bps: number
  product_type: string
  missing_data_policy: string
  stop_loss_pct?: number | null
  take_profit_pct?: number | null
  daily_loss_limit?: number | null
  max_positions: number
  created_at?: string
  completed_at?: string
  error_message?: string
  metrics?: Metrics
  batch_id?: string
  batch_seq?: number
}

export interface BatchRunSummary {
  job_id: string
  name: string
  interval: string
  batch_seq: number
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'
  metrics?: Metrics | null
  error_message?: string | null
}

export interface BatchDetails {
  batch_id: string
  name: string
  runs: BatchRunSummary[]
}

export interface BatchCurvePoint {
  timestamp: string
  equity: number
  drawdown_pct: number
}

export interface BatchCurve {
  job_id: string
  interval: string
  batch_seq: number
  status: string
  points: BatchCurvePoint[]
}

interface StrategyBacktestStore {
  result: ResultsData | null
  jobDetails: FullJobDetails | null
  setResult: (result: ResultsData, jobDetails?: FullJobDetails | null) => void
  batch: BatchDetails | null
  setBatch: (batch: BatchDetails | null) => void
  batchCurves: BatchCurve[]
  setBatchCurves: (curves: BatchCurve[]) => void
  resultsByJob: Record<string, ResultsData>
  setResultsByJob: (jobId: string, data: ResultsData) => void
  clear: () => void
}

export const useStrategyBacktestStore = create<StrategyBacktestStore>((set) => ({
  result: null,
  jobDetails: null,
  setResult: (result, jobDetails = null) => set({ result, jobDetails }),
  batch: null,
  setBatch: (batch) => set({ batch }),
  batchCurves: [],
  setBatchCurves: (batchCurves) => set({ batchCurves }),
  resultsByJob: {},
  setResultsByJob: (jobId, data) =>
    set((state) => ({ resultsByJob: { ...state.resultsByJob, [jobId]: data } })),
  clear: () =>
    set({
      result: null,
      jobDetails: null,
      batch: null,
      batchCurves: [],
      resultsByJob: {},
    }),
}))
