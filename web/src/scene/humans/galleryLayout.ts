/** Static layout of the character gallery (`#/characters`), shared by Gallery.tsx and its tests. */
import { BASE_CLIPS, GESTURE_CLIPS, type ClipName } from './clipNames'
import { PRESETS } from './presets'

export interface Params {
  clip: ClipName | null
  still: boolean
  t: number
  moves: boolean
  preset: number
  tier: string | null
}

export interface Slot {
  label: string
  x: number
  /** Floor height under the figure (the moves strip's back row stands on a platform). */
  y: number
  z: number
  height: number
}

/** Moves strip: the back row stands this high, this far behind the front row. */
export const BACK_ROW_Y = 2.6
export const BACK_ROW_Z = -1.6

/** Static layout: positions, labels and heights derive from the params alone. */
export function layoutFor(params: Params): { spacing: number; width: number; slots: Slot[] } {
  const slots: Slot[] = []
  if (params.moves) {
    const h = PRESETS[params.preset]!.height
    const row = (clips: readonly ClipName[], spacing: number, y: number, z: number) => {
      const n = clips.length
      for (let i = 0; i < n; i++) slots.push({ label: clips[i]!, x: (i - (n - 1) / 2) * spacing, y, z, height: h })
      return spacing * (n - 1)
    }
    const back = row(BASE_CLIPS, 2.6, BACK_ROW_Y, BACK_ROW_Z)
    const front = row(GESTURE_CLIPS, 2.15, 0, 0)
    return { spacing: 2.15, width: Math.max(back, front) + 3.2, slots }
  }
  const spacing = 1.38
  const n = PRESETS.length
  for (let i = 0; i < n; i++) slots.push({ label: PRESETS[i]!.name, x: (i - (n - 1) / 2) * spacing, y: 0, z: 0, height: PRESETS[i]!.height })
  return { spacing, width: spacing * (n - 1) + 2.2, slots }
}

