/**
 * Day cycle mapping (pure functions, unit tested). The hour of day comes from
 * the render clock's tick_of_day interpolated by the segment fraction, so the
 * sun moves smoothly between ticks. Dawn ≈ 6, noon 12, dusk ≈ 18, night after
 * 20 (hello.config.ticks_per_day sets how many ticks make a day).
 */

export function hourOfDay(tickOfDay: number, frac: number, ticksPerDay: number): number {
  if (!(ticksPerDay > 0)) return 12
  const h = ((tickOfDay + Math.min(1, Math.max(0, frac))) / ticksPerDay) * 24
  return ((h % 24) + 24) % 24
}

/** Sun elevation as sin(altitude): 0 at 6:00 and 18:00, 1 at noon, −1 at midnight. */
export function sunElevation(hour: number): number {
  return Math.sin(((hour - 6) / 24) * Math.PI * 2)
}

/** Azimuth in radians: east at dawn, south at noon, west at dusk. */
export function sunAzimuth(hour: number): number {
  return ((hour - 6) / 24) * Math.PI * 2
}

/** Unit direction toward the sun in scene space (x east, y up, z south). */
export function sunDirection(hour: number, out: { x: number; y: number; z: number }): void {
  const el = sunElevation(hour)
  const az = sunAzimuth(hour)
  const c = Math.sqrt(Math.max(0, 1 - el * el))
  out.x = Math.cos(az) * c
  out.y = el
  out.z = Math.sin(az) * c * 0.6 + 0.35 * c
  const len = Math.hypot(out.x, out.y, out.z) || 1
  out.x /= len
  out.y /= len
  out.z /= len
}

export type DayPhase = 'night' | 'dawn' | 'day' | 'dusk'

export function dayPhase(hour: number): DayPhase {
  if (hour >= 5 && hour < 7.5) return 'dawn'
  if (hour >= 7.5 && hour < 17) return 'day'
  if (hour >= 17 && hour < 20) return 'dusk'
  return 'night'
}

/** 0 at night → 1 in full day, with soft dawn/dusk ramps (drives light intensity). */
export function daylight(hour: number): number {
  const el = sunElevation(hour)
  const k = (el + 0.18) / 0.5
  return k < 0 ? 0 : k > 1 ? 1 : k * k * (3 - 2 * k)
}

/** Warmth of the sun colour: 1 while the sun crosses the horizon, 0 at noon and at night. */
export function horizonWarmth(hour: number): number {
  const el = sunElevation(hour)
  const k = 1 - Math.abs(el + 0.05) / 0.32
  return k < 0 ? 0 : k > 1 ? 1 : k
}

/** Night amount 0..1 (stars, moon light). */
export function nightAmount(hour: number): number {
  const el = sunElevation(hour)
  const k = (-el - 0.05) / 0.3
  return k < 0 ? 0 : k > 1 ? 1 : k
}
