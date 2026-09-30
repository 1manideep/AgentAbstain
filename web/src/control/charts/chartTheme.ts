/**
 * Chart tokens (dataviz skill, dark mode, validated against the card surface
 * #0e1320: all eight categorical slots pass lightness, chroma, CVD and contrast).
 * Marks wear series colours; text wears text tokens; entities (tiers) keep the
 * colour the server gave them so the scene and the charts agree.
 */
export const SERIES = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767'] as const
/** Ordinal blue ramp (steps 250 → 600) for ordered categories such as generations. */
export const ORDINAL_BLUE = ['#86b6ef', '#6da7ec', '#5598e7', '#3987e5', '#2a78d6', '#256abf', '#1c5cab', '#184f95'] as const
export const STATUS = { good: '#0ca30c', warning: '#fab219', serious: '#ec835a', critical: '#d03b3b' } as const
export const INK = {
  primary: '#f1f3fa',
  secondary: '#b7bfd4',
  muted: '#7d869c',
  grid: '#1c2334',
  axis: '#2c3449',
  surface: '#0e1320',
} as const

export const EPOCH_COLORS: Record<string, string> = {
  drought: SERIES[3],
  storm: SERIES[6],
  boom: SERIES[2],
  arrival: SERIES[4],
  custom: SERIES[0],
}

export function ordinalColor(i: number): string {
  return ORDINAL_BLUE[Math.min(ORDINAL_BLUE.length - 1, Math.max(0, i))]!
}

/** "Nice" tick values for an axis from 0 to max. */
export function niceTicks(max: number, count = 4): number[] {
  if (!(max > 0)) return [0]
  const raw = max / count
  const mag = 10 ** Math.floor(Math.log10(raw))
  const norm = raw / mag
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag
  const ticks: number[] = []
  for (let v = 0; v <= max + 1e-9; v += step) ticks.push(Number(v.toFixed(6)))
  if (ticks[ticks.length - 1]! < max) ticks.push(Number((ticks[ticks.length - 1]! + step).toFixed(6)))
  return ticks
}

export function fmtUsd(v: number, digits = 3): string {
  if (!Number.isFinite(v)) return '—'
  const abs = Math.abs(v)
  if (abs >= 1000) return `$${(v / 1000).toFixed(1)}K`
  if (abs >= 10) return `$${v.toFixed(2)}`
  return `$${v.toFixed(digits)}`
}

export function fmtPct(v: number, digits = 0): string {
  return Number.isFinite(v) ? `${(v * 100).toFixed(digits)}%` : '—'
}

export function fmtNum(v: number, digits = 2): string {
  return Number.isFinite(v) ? v.toFixed(digits) : '—'
}

export function fmtCompact(v: number): string {
  if (!Number.isFinite(v)) return '—'
  if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(1)}M`
  if (Math.abs(v) >= 1e3) return `${(v / 1e3).toFixed(1)}K`
  return Number.isInteger(v) ? String(v) : v.toFixed(2)
}

/** Path for a polyline through points, or '' when fewer than two. */
export function linePath(points: Array<[number, number]>): string {
  if (points.length < 2) return ''
  let d = `M${points[0]![0].toFixed(1)} ${points[0]![1].toFixed(1)}`
  for (let i = 1; i < points.length; i++) d += `L${points[i]![0].toFixed(1)} ${points[i]![1].toFixed(1)}`
  return d
}

/** Rounded-top bar path (4px radius at the data end, square at the baseline). */
export function columnPath(x: number, y: number, w: number, h: number, r = 4): string {
  if (h <= 0.01) return ''
  const rr = Math.min(r, w / 2, h)
  return `M${x} ${y + h}V${y + rr}Q${x} ${y} ${x + rr} ${y}H${x + w - rr}Q${x + w} ${y} ${x + w} ${y + rr}V${y + h}Z`
}

/** Rounded-end horizontal bar (rounded at the data end, square at the baseline). */
export function barPath(x: number, y: number, w: number, h: number, r = 4): string {
  if (w <= 0.01) return ''
  const rr = Math.min(r, h / 2, w)
  return `M${x} ${y}H${x + w - rr}Q${x + w} ${y} ${x + w} ${y + rr}V${y + h - rr}Q${x + w} ${y + h} ${x + w - rr} ${y + h}H${x}Z`
}
