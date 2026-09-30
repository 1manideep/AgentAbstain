/**
 * The single per-frame driver: advances the render clock, samples the history
 * buffer once, runs every scene system in order, and publishes a coarse clock
 * view to the store at 10 Hz (never per frame). Also exposes render stats on
 * window.__void for the Playwright smoke test.
 */
import { useFrame, useThree } from '@react-three/fiber'
import { useEffect, useRef } from 'react'
import type { EventMsg } from '../protocol'
import { history } from '../state/history'
import { useStore } from '../state/store'
import { fxQueue } from './fx'
import { mirror, runSystems, sampled, type FrameCtx } from './sceneState'

declare global {
  interface Window {
    __void?: {
      gl: { info: { render: { calls: number; triangles: number } } }
      stats: () => { calls: number; triangles: number; fps: number; dpr: number; frames: number }
      history: typeof history
    }
  }
}

const ctx: FrameCtx = {
  dt: 0,
  now: 0,
  renderVts: 0,
  speed: 1,
  out: sampled.out,
  reducedMotion: false,
}

export function Driver() {
  const gl = useThree((s) => s.gl)
  const frames = useRef(0)
  const fpsWindow = useRef({ t0: 0, n: 0, fps: 0 })
  const lastClockCommit = useRef(0)
  const lastTick = useRef(-1)

  useEffect(() => {
    window.__void = {
      gl,
      stats: () => ({
        calls: gl.info.render.calls,
        triangles: gl.info.render.triangles,
        fps: fpsWindow.current.fps,
        dpr: gl.getPixelRatio(),
        frames: frames.current,
      }),
      history,
    }
    return () => {
      delete window.__void
    }
  }, [gl])

  useFrame((state, delta) => {
    const dt = delta > 0.1 ? 0.1 : delta < 0 ? 0 : delta
    const p = history.playback
    const before = p.renderVts
    const renderVts = history.advance(dt)
    const out = sampled.out
    history.sample(renderVts, out)

    // Scrubbing backwards: re-arm effects for the ticks we will see again.
    if (lastTick.current >= 0 && out.tick < lastTick.current - 1) {
      fxQueue.reseed(eventsIterable(), out.tick)
    }
    lastTick.current = out.tick

    ctx.dt = dt
    ctx.now = state.clock.elapsedTime
    ctx.renderVts = renderVts
    ctx.speed = dt > 0 ? (renderVts - before) / (dt * 1000) : 0
    ctx.out = out
    ctx.reducedMotion = mirror.reducedMotion
    runSystems(ctx)

    frames.current++
    const w = fpsWindow.current
    w.n++
    const now = state.clock.elapsedTime
    if (now - w.t0 >= 1) {
      w.fps = w.n / (now - w.t0)
      w.n = 0
      w.t0 = now
      useStore.getState().setPerf({ fps: Math.round(w.fps), calls: gl.info.render.calls, dpr: gl.getPixelRatio() })
    }
    if (now - lastClockCommit.current >= 0.1) {
      lastClockCommit.current = now
      const st = useStore.getState()
      const newest = history.newest()
      st.commitClock({
        tick: out.tick,
        day: out.day,
        tickOfDay: out.tickOfDay,
        frameIndex: history.renderIndex(),
        frameCount: history.count,
        newestTick: newest ? newest.tick : 0,
        mode: p.mode,
        speed: p.speed,
        rate: p.rate,
        lagMs: Math.round(history.newestVts - renderVts),
      })
      st.setPlayback({ mode: p.mode, speed: p.speed })
    }
  })
  return null
}

function* eventsIterable() {
  const ring = useStore.getState().events
  const list: EventMsg[] = []
  ring.forEach((e) => list.push(e))
  yield* list
}
