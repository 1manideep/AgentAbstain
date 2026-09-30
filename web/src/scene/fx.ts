/**
 * Pending visual effects, keyed by the tick whose virtual time they belong to.
 * Effects are queued on arrival and fired by the frame loop when the render
 * clock reaches the tick (never at arrival), so replays and scrubs line up
 * with the interpolated motion. A tick key is equivalent to `vtsOf(tick)`
 * (ticks are monotone in vts) and also works before that tick's frame exists.
 */
import type { EventMsg } from '../protocol'

export const FX_KINDS: ReadonlySet<string> = new Set([
  'talk',
  'transfer',
  'windfall',
  'birth',
  'death',
  'degeneration',
  'forage',
  'gadget_verified',
  'epoch',
])

interface Node {
  tick: number
  seq: number
  event: EventMsg
}

class MinHeap {
  private a: Node[] = []
  get size(): number {
    return this.a.length
  }
  peek(): Node | undefined {
    return this.a[0]
  }
  push(n: Node): void {
    const a = this.a
    a.push(n)
    let i = a.length - 1
    while (i > 0) {
      const p = (i - 1) >> 1
      if (less(a[i]!, a[p]!)) {
        const t = a[i]!
        a[i] = a[p]!
        a[p] = t
        i = p
      } else break
    }
  }
  pop(): Node | undefined {
    const a = this.a
    if (a.length === 0) return undefined
    const top = a[0]!
    const last = a.pop()!
    if (a.length > 0) {
      a[0] = last
      let i = 0
      for (;;) {
        const l = 2 * i + 1
        const r = l + 1
        let m = i
        if (l < a.length && less(a[l]!, a[m]!)) m = l
        if (r < a.length && less(a[r]!, a[m]!)) m = r
        if (m === i) break
        const t = a[i]!
        a[i] = a[m]!
        a[m] = t
        i = m
      }
    }
    return top
  }
  clear(): void {
    this.a.length = 0
  }
}

function less(x: Node, y: Node): boolean {
  return x.tick !== y.tick ? x.tick < y.tick : x.seq < y.seq
}

export class FxQueue {
  private heap = new MinHeap()
  private seen = new Set<number>()

  push(e: EventMsg): void {
    if (!FX_KINDS.has(e.kind)) return
    if (this.seen.has(e.seq)) return
    this.seen.add(e.seq)
    if (this.seen.size > 5000) {
      // keep the set bounded: drop the oldest half by seq order
      const arr = Array.from(this.seen).sort((a, b) => a - b)
      this.seen = new Set(arr.slice(arr.length >> 1))
    }
    this.heap.push({ tick: e.tick, seq: e.seq, event: e })
  }

  /**
   * Pop every effect whose tick <= `renderTick`. Effects older than
   * `renderTick - staleTicks` are dropped instead of fired (initial catch-up,
   * fast scrubs) so a burst of history never explodes on screen.
   */
  drain(renderTick: number, fire: (e: EventMsg) => void, staleTicks = 2): void {
    for (;;) {
      const top = this.heap.peek()
      if (!top || top.tick > renderTick) return
      this.heap.pop()
      if (top.tick >= renderTick - staleTicks) fire(top.event)
    }
  }

  /** Re-queue effects with tick > fromTick (after a scrub backwards). */
  reseed(events: Iterable<EventMsg>, fromTick: number): void {
    this.heap.clear()
    this.seen.clear()
    for (const e of events) if (e.tick > fromTick) this.push(e)
  }

  clear(): void {
    this.heap.clear()
    this.seen.clear()
  }

  get size(): number {
    return this.heap.size
  }
}

export const fxQueue = new FxQueue()
