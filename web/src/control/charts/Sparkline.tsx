import { memo, useMemo } from 'react'
import { INK, linePath, SERIES } from './chartTheme'

interface SparklineProps {
  values: number[]
  width?: number
  height?: number
  color?: string
  /** Formats the last value's direct label. */
  format?: (v: number) => string
}

/** 12–240 point sparkline: 2px line, end marker, last value labelled. */
export const Sparkline = memo(function Sparkline({ values, width = 220, height = 44, color = SERIES[0], format }: SparklineProps) {
  const { path, last, lastY, lastX, endLabel } = useMemo(() => {
    if (values.length === 0) return { path: '', last: NaN, lastY: 0, lastX: 0, endLabel: '' }
    const min = Math.min(...values)
    const max = Math.max(...values)
    const span = max - min || 1
    const padR = 44
    const w = width - padR
    const pts: Array<[number, number]> = values.map((v, i) => [
      values.length === 1 ? w : (i / (values.length - 1)) * w,
      4 + (1 - (v - min) / span) * (height - 8),
    ])
    const lv = values[values.length - 1]!
    const lp = pts[pts.length - 1]!
    return { path: linePath(pts), last: lv, lastY: lp[1], lastX: lp[0], endLabel: format ? format(lv) : lv.toFixed(3) }
  }, [values, width, height, format])
  if (!Number.isFinite(last)) return <div className="muted small">No calls yet.</div>
  return (
    <svg className="sparkline" viewBox={`0 0 ${width} ${height}`} width="100%" height={height} role="img" aria-label="call history">
      <path d={path} fill="none" stroke={color} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={lastX} cy={lastY} r="4" fill={color} stroke={INK.surface} strokeWidth="2" />
      <text x={lastX + 8} y={lastY + 4} fill={INK.secondary} fontSize="11" className="tnum">
        {endLabel}
      </text>
    </svg>
  )
})
