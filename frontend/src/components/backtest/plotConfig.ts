import type * as PlotlyTypes from 'plotly.js'

export const PLOT_LAYOUT: Partial<PlotlyTypes.Layout> = {
  paper_bgcolor: 'transparent',
  plot_bgcolor: 'transparent',
  font: { color: '#94a3b8', family: 'Inter, system-ui, sans-serif', size: 11 },
  margin: { l: 50, r: 16, t: 30, b: 40 },
  xaxis: {
    showgrid: true,
    gridcolor: 'rgba(148,163,184,0.15)',
    zeroline: false,
  },
  yaxis: {
    showgrid: true,
    gridcolor: 'rgba(148,163,184,0.15)',
    zeroline: false,
  },
  hoverlabel: { bgcolor: '#1e293b', bordercolor: '#334155', font: { color: '#f8fafc' } },
}

export const PLOT_CONFIG: Partial<PlotlyTypes.Config> = {
  displaylogo: false,
  responsive: true,
  modeBarButtonsToRemove: ['lasso2d', 'select2d', 'autoScale2d'],
}
