import type { EventMsg } from '../protocol'

export interface EventFilter {
  /** null = every kind. */
  kinds: ReadonlySet<string> | null
  /** null = every agent; otherwise the event's agent_id or a payload participant must match. */
  agentId: string | null
}

export const EMPTY_FILTER: EventFilter = { kinds: null, agentId: null }

function participants(e: EventMsg): string[] {
  const p = e.payload as Record<string, unknown>
  const out: string[] = []
  if (e.agent_id) out.push(e.agent_id)
  for (const k of ['agent_id', 'speaker_id', 'listener_id', 'from', 'to', 'parent_id', 'replacement_id', 'owner']) {
    const v = p[k]
    if (typeof v === 'string' && v) out.push(v)
  }
  return out
}

export function matchesFilter(e: EventMsg, f: EventFilter): boolean {
  if (f.kinds && !f.kinds.has(e.kind)) return false
  if (f.agentId && !participants(e).includes(f.agentId)) return false
  return true
}

/**
 * Fixed-capacity ring of events with an absolute index space and a filtered
 * index maintained incrementally (O(1) per event, O(n) only on filter change).
 */
export class EventRing {
  readonly capacity: number
  private buf: (EventMsg | null)[]
  /** Absolute index of the next event; ring slot = abs % capacity. */
  total = 0
  lastSeq = 0
  filter: EventFilter = EMPTY_FILTER
  /** Absolute indexes of events matching `filter`, oldest first, from `filteredHead`. */
  private filtered: number[] = []
  private filteredHead = 0
  /** Bumped on every ingest that changed the filtered view. */
  rev = 0

  constructor(capacity = 2000) {
    this.capacity = capacity
    this.buf = new Array<EventMsg | null>(capacity).fill(null)
  }

  get oldestAbs(): number {
    return Math.max(0, this.total - this.capacity)
  }

  get size(): number {
    return Math.min(this.total, this.capacity)
  }

  atAbs(abs: number): EventMsg | null {
    if (abs < this.oldestAbs || abs >= this.total) return null
    return this.buf[abs % this.capacity]
  }

  /** Idempotent by seq: replays and overlaps are dropped. Returns whether it was stored. */
  push(e: EventMsg): boolean {
    if (e.seq <= this.lastSeq) return false
    this.lastSeq = e.seq
    const abs = this.total++
    this.buf[abs % this.capacity] = e
    this.pruneFiltered()
    if (matchesFilter(e, this.filter)) {
      this.filtered.push(abs)
      this.rev++
    }
    return true
  }

  private pruneFiltered(): void {
    const floor = this.oldestAbs
    while (this.filteredHead < this.filtered.length && this.filtered[this.filteredHead]! < floor) {
      this.filteredHead++
    }
    if (this.filteredHead > 1024) {
      this.filtered = this.filtered.slice(this.filteredHead)
      this.filteredHead = 0
    }
  }

  setFilter(f: EventFilter): void {
    this.filter = f
    this.filtered = []
    this.filteredHead = 0
    for (let abs = this.oldestAbs; abs < this.total; abs++) {
      const e = this.buf[abs % this.capacity]
      if (e && matchesFilter(e, f)) this.filtered.push(abs)
    }
    this.rev++
  }

  get filteredCount(): number {
    return this.filtered.length - this.filteredHead
  }

  /** i-th matching event (0 = oldest). */
  filteredAt(i: number): EventMsg | null {
    const abs = this.filtered[this.filteredHead + i]
    return abs === undefined ? null : this.atAbs(abs)
  }

  /** Iterate every stored event oldest → newest (used to reseed effects on scrub). */
  forEach(fn: (e: EventMsg) => void): void {
    for (let abs = this.oldestAbs; abs < this.total; abs++) {
      const e = this.buf[abs % this.capacity]
      if (e) fn(e)
    }
  }

  clear(): void {
    this.buf.fill(null)
    this.total = 0
    this.lastSeq = 0
    this.filtered = []
    this.filteredHead = 0
    this.rev++
  }
}
