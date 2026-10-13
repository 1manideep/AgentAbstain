/**
 * Looks of the in-world bubbles (Effects.tsx): speech for talk, a cream note card when an agent writes to its
 * memory (remember, revise_self, nightly maintenance) and a red one-letter mark when a listener learns it was
 * lied to (claim feedback). Pure data so tests import it without React. A speech bubble whose claims the kernel
 * found false is tinted only when the operator turns `showLies` on.
 */
import { BUBBLE_FONT, BUBBLE_WIDTH, NOTE_FONT, NOTE_WIDTH } from './bubbleText'

export const BUBBLE_MS = 3000
export const NOTE_MS = 2200
export const MARK_MS = 1600

/** What a bubble is: speech (talk), a note card (memory writing) or a one-glyph mark (a lie found out). */
export type BubbleKind = 'speech' | 'note' | 'mark'

export interface BubbleStyle {
  /** Text colour. */
  ink: string
  /** Backdrop colour, linear 0..1 (the backdrop shader takes it per instance). */
  bg: readonly [number, number, number]
  /** Backdrop opacity before the fade. */
  bgAlpha: number
  fontSize: number
  maxWidth: number
  /** Height of the bubble's anchor above the agent's feet. */
  lift: number
  ttlMs: number
}

const SPEECH_STYLE: BubbleStyle = { ink: '#f3f6ff', bg: [0.04, 0.06, 0.1], bgAlpha: 0.82, fontSize: BUBBLE_FONT, maxWidth: BUBBLE_WIDTH, lift: 2.9, ttlMs: BUBBLE_MS }
const LIE_STYLE: BubbleStyle = { ...SPEECH_STYLE, bg: [0.3, 0.05, 0.07] }
const NOTE_STYLE: BubbleStyle = { ink: '#2a251c', bg: [0.95, 0.91, 0.8], bgAlpha: 0.9, fontSize: NOTE_FONT, maxWidth: NOTE_WIDTH, lift: 3.55, ttlMs: NOTE_MS }
const MARK_STYLE: BubbleStyle = { ink: '#ffffff', bg: [0.78, 0.16, 0.16], bgAlpha: 0.92, fontSize: BUBBLE_FONT * 1.3, maxWidth: 0.6, lift: 2.5, ttlMs: MARK_MS }

/** Operator detail: tint the speech of an agent the kernel caught lying (off by default; see `setShowLies`). */
const lieTint = { on: false }
export function setShowLies(on: boolean): void {
  lieTint.on = on
}
export function showLies(): boolean {
  return lieTint.on
}

/** The look of a bubble by kind; a speech bubble whose claims were false is tinted only when `lies` is shown. */
export function bubbleStyle(kind: BubbleKind, lied = false, lies = lieTint.on): BubbleStyle {
  if (kind === 'note') return NOTE_STYLE
  if (kind === 'mark') return MARK_STYLE
  return lied && lies ? LIE_STYLE : SPEECH_STYLE
}


/** Gap between a note card and the speech bubble under it; covers the 0.12 rise either bubble makes as it fades in. */
export const STACK_GAP = 0.14

/**
 * Centre height for a note card on an agent that is also speaking. Both bubbles are centre-anchored at their own
 * lift, so a speech bubble of three lines reaches past the note's default height; the note moves up just enough to
 * clear it. Heights are the drawn backdrop heights (text block plus padding).
 */
export function noteClearY(noteY: number, noteH: number, speechY: number, speechH: number, gap = STACK_GAP): number {
  return Math.max(noteY, speechY + speechH / 2 + noteH / 2 + gap)
}

/**
 * Which side of the listener's head a mark goes on: +1 for the camera's right, -1 for its left. It takes the side
 * facing away from the speaker (the speaker's bubble is over there), and the right when there is no speaker or the
 * two stand in line with the camera. `dx`/`dz` run from the speaker to the listener; `rx`/`rz` is the camera's right.
 */
export function markSide(dx: number, dz: number, rx: number, rz: number): 1 | -1 {
  return dx * rx + dz * rz < -1e-6 ? -1 : 1
}
