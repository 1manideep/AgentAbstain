import { memo, useCallback, useState } from 'react'

export interface TipRow {
  label: string
  value: string
  color?: string
}

export interface TipState {
  x: number
  y: number
  title: string
  rows: TipRow[]
}

/** Hover tooltip state for one chart; the chart positions it in its own box. */
export function useChartTip(): {
  tip: TipState | null
  show: (x: number, y: number, title: string, rows: TipRow[]) => void
  hide: () => void
} {
  const [tip, setTip] = useState<TipState | null>(null)
  const show = useCallback((x: number, y: number, title: string, rows: TipRow[]) => setTip({ x, y, title, rows }), [])
  const hide = useCallback(() => setTip(null), [])
  return { tip, show, hide }
}

/** Values lead, labels follow; series identity is a short line key, not coloured text. */
export const ChartTip = memo(function ChartTip({ tip, width }: { tip: TipState | null; width: number }) {
  if (!tip) return null
  const flip = tip.x > width * 0.6
  return (
    <div
      className="chart-tip"
      style={{ left: flip ? undefined : tip.x + 10, right: flip ? width - tip.x + 10 : undefined, top: Math.max(0, tip.y - 8) }}
      role="status"
    >
      <div className="chart-tip-title">{tip.title}</div>
      {tip.rows.map((r, i) => (
        <div className="chart-tip-row" key={i}>
          {r.color ? <span className="chart-tip-key" style={{ background: r.color }} /> : null}
          <span className="chart-tip-value">{r.value}</span>
          <span className="chart-tip-label">{r.label}</span>
        </div>
      ))}
    </div>
  )
})
