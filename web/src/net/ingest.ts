/**
 * One entry point for every server message, shared by the WebSocket client and
 * the fixture replayer. Snapshots go to the History buffer first (typed-array
 * frames), then to the store's low-churn reducers; events feed both the store
 * ring and the effects queue.
 */
import type { ServerMsg } from '../protocol'
import { history } from '../state/history'
import { useStore } from '../state/store'
import { fxQueue } from '../scene/fx'

const KNOWN: ReadonlySet<string> = new Set([
  'hello',
  'roster',
  'snapshot',
  'gadgets',
  'tasks',
  'chronicle',
  'event',
  'metrics',
  'day',
  'status',
])

export function isServerMsg(x: unknown): x is ServerMsg {
  return typeof x === 'object' && x !== null && typeof (x as { type?: unknown }).type === 'string' && KNOWN.has((x as { type: string }).type)
}

export function ingest(msg: ServerMsg): void {
  const s = useStore.getState()
  switch (msg.type) {
    case 'hello': {
      const newRun = s.runId !== null && s.runId !== msg.run_id
      if (newRun) {
        history.reset()
        fxQueue.clear()
      }
      s.applyHello(msg)
      history.configure(msg.config.population_cap, 8)
      break
    }
    case 'roster':
      s.applyRoster(msg.agents)
      break
    case 'snapshot':
      history.push(msg)
      s.applySnapshot(msg)
      break
    case 'event':
      if (s.applyEvent(msg)) fxQueue.push(msg)
      break
    case 'metrics':
      s.applyMetrics(msg.row)
      break
    case 'day':
      s.applyDay(msg.row)
      break
    case 'gadgets':
      s.applyGadgets(msg)
      break
    case 'tasks':
      s.applyTasks(msg)
      break
    case 'chronicle':
      s.applyChronicle(msg)
      break
    case 'status':
      s.applyStatus(msg)
      break
  }
}
