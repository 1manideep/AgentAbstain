import { describe, expect, it } from 'vitest'
import { CLIP_NAMES } from './humans/clipNames'

describe('bubble styles', () => {
  it('give notes, marks and speech distinct looks; lies are tinted only when shown', async () => {
    const { bubbleStyle, setShowLies, showLies } = await import('./bubbleStyle')
    const speech = bubbleStyle('speech')
    const note = bubbleStyle('note')
    const mark = bubbleStyle('mark')
    expect(new Set([speech.bg.join(), note.bg.join(), mark.bg.join()]).size).toBe(3)
    expect(note.fontSize).toBeLessThan(speech.fontSize)
    expect(note.lift).toBeGreaterThan(speech.lift)
    expect(mark.ttlMs).toBeLessThan(note.ttlMs)
    expect(showLies()).toBe(false)
    expect(bubbleStyle('speech', true)).toBe(speech)
    expect(bubbleStyle('speech', true, true).bg).not.toEqual(speech.bg)
    setShowLies(true)
    expect(bubbleStyle('speech', true).bg).not.toEqual(speech.bg)
    expect(bubbleStyle('note', true)).toBe(note)
    setShowLies(false)
  })
})

describe('moves strip layout', () => {
  it('puts the eight base states on the raised back row and the thirteen gestures in front, each clip once', async () => {
    const { layoutFor, BACK_ROW_Y, BACK_ROW_Z } = await import('./humans/galleryLayout')
    const { slots, width } = layoutFor({ clip: null, still: false, t: 0.4, moves: true, preset: 3, tier: null })
    expect(slots.map((s) => s.label).sort()).toEqual([...CLIP_NAMES].sort())
    const back = slots.filter((s) => s.y === BACK_ROW_Y && s.z === BACK_ROW_Z)
    const front = slots.filter((s) => s.y === 0 && s.z === 0)
    expect(back).toHaveLength(8)
    expect(front).toHaveLength(13)
    for (const row of [back, front]) {
      const xs = row.map((s) => s.x).sort((a, b) => a - b)
      for (let i = 1; i < xs.length; i++) expect(xs[i]! - xs[i - 1]!).toBeGreaterThan(1.9)
      expect(Math.abs(xs[0]! + xs[xs.length - 1]!)).toBeLessThan(1e-9)
      expect(xs[xs.length - 1]! - xs[0]!).toBeLessThanOrEqual(width)
    }
    const row = layoutFor({ clip: null, still: false, t: 0.4, moves: false, preset: 3, tier: null })
    expect(row.slots).toHaveLength(10)
    expect(row.slots.every((s) => s.y === 0 && s.z === 0)).toBe(true)
  })

  it('moves a note card above a tall speech bubble on the same agent, and leaves it alone otherwise', async () => {
    const { noteClearY, STACK_GAP } = await import('./bubbleStyle')
    // a short speech bubble already clears the note: the default height stands
    expect(noteClearY(3.55, 0.3, 2.9, 0.3)).toBe(3.55)
    // three lines of speech reach past it: the note's lower edge sits one gap above the speech's upper edge
    const y = noteClearY(3.55, 0.3, 2.9, 1.2)
    expect(y).toBeGreaterThan(3.55)
    expect(y - 0.3 / 2 - (2.9 + 1.2 / 2)).toBeCloseTo(STACK_GAP, 9)
  })

  it('puts a mark on the side of the listener away from the speaker', async () => {
    const { markSide } = await import('./bubbleStyle')
    // camera right is +x; speaker to the listener's right (listener minus speaker points -x): mark goes left
    expect(markSide(-2, 0, 1, 0)).toBe(-1)
    // speaker to the left: mark goes right
    expect(markSide(2, 0.5, 1, 0)).toBe(1)
    // in line with the camera, or no offset at all: the default right
    expect(markSide(0, 3, 1, 0)).toBe(1)
    expect(markSide(0, 0, 1, 0)).toBe(1)
  })
})
