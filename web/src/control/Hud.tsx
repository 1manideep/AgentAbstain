import { memo, useCallback } from 'react'
import { useShallow } from 'zustand/react/shallow'
import { history, SPEEDS, type PlaybackSpeed } from '../state/history'
import { useStore, type ConnState } from '../state/store'
import { cameraCommands } from '../scene/sceneState'

function connLabel(c: ConnState): { text: string; cls: string } {
  switch (c) {
    case 'open':
      return { text: 'live', cls: 'ok' }
    case 'fixture':
      return { text: 'fixture', cls: 'ok' }
    case 'connecting':
      return { text: 'connecting', cls: 'warn' }
    case 'reconnecting':
      return { text: 'reconnecting', cls: 'warn' }
    case 'closed':
      return { text: 'offline', cls: 'bad' }
  }
}

function hourOf(tickOfDay: number, ticksPerDay: number): string {
  if (ticksPerDay <= 0) return '--:--'
  const h = (tickOfDay / ticksPerDay) * 24
  const hh = Math.floor(h)
  const mm = Math.floor((h - hh) * 60)
  return `${String(hh).padStart(2, '0')}:${String(mm).padStart(2, '0')}`
}

const Playback = memo(function Playback() {
  const { mode, speed, frameIndex, frameCount, lagMs } = useStore(
    useShallow((s) => ({
      mode: s.clock.mode,
      speed: s.clock.speed,
      frameIndex: s.clock.frameIndex,
      frameCount: s.clock.frameCount,
      lagMs: s.clock.lagMs,
    })),
  )
  const onScrub = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    history.scrubToIndex(Number(e.currentTarget.value))
  }, [])
  const live = mode === 'live'
  return (
    <div className="playback" role="group" aria-label="playback">
      <button type="button" className={'btn live' + (live ? ' active' : '')} onClick={() => history.goLive()} title="Live (L)">
        <span className="dot" /> Live
      </button>
      <div className="speeds" role="group" aria-label="speed">
        {SPEEDS.map((v) => (
          <button
            key={v}
            type="button"
            className={'btn tiny' + (speed === v ? ' active' : '')}
            onClick={() => history.setSpeed(v as PlaybackSpeed)}
            title={v === 0 ? 'Pause (space)' : `${v}×`}
          >
            {v === 0 ? '❚❚' : `${v}×`}
          </button>
        ))}
      </div>
      <button type="button" className="btn tiny" onClick={() => history.step(-1)} title="Step back (,)">
        ‹
      </button>
      <input
        className="timeline"
        type="range"
        min={0}
        max={Math.max(0, frameCount - 1)}
        value={Math.max(0, frameIndex)}
        onChange={onScrub}
        aria-label="timeline"
        title={`frame ${frameIndex + 1} / ${frameCount}`}
      />
      <button type="button" className="btn tiny" onClick={() => history.step(1)} title="Step forward (.)">
        ›
      </button>
      <span className="muted tnum lag" title="render clock lag behind newest frame">
        {live ? `${(lagMs / 1000).toFixed(1)}s` : 'scrub'}
      </span>
      <button type="button" className="btn tiny" onClick={() => (cameraCommands.frameRequested = true)} title="Frame population (F)">
        ⌖
      </button>
    </div>
  )
})

export const Hud = memo(function Hud() {
  const { conn, runId, runStatus, serverPaused } = useStore(
    useShallow((s) => ({ conn: s.conn, runId: s.runId, runStatus: s.runStatus, serverPaused: s.serverPaused })),
  )
  const { tick, day, tickOfDay, newestTick } = useStore(
    useShallow((s) => ({ tick: s.clock.tick, day: s.clock.day, tickOfDay: s.clock.tickOfDay, newestTick: s.clock.newestTick })),
  )
  const ticksPerDay = useStore((s) => s.config?.ticks_per_day ?? 24)
  const perf = useStore((s) => s.perf)
  const c = connLabel(conn)
  return (
    <header className="hud">
      <div className="hud-left">
        <span className="brand">THE VOID</span>
        <span className={'pill ' + c.cls} title={`connection: ${c.text}`}>
          <span className="dot" />
          {c.text}
        </span>
        <span className="run muted" title="run id">
          {runId ?? '—'}
        </span>
        {runStatus && runStatus !== 'running' ? <span className="pill warn">{runStatus}</span> : null}
      </div>
      <div className="hud-center">
        <span className="clock-item">
          <span className="muted">day</span> <b className="tnum">{day}</b>
        </span>
        <span className="clock-item">
          <span className="muted">tick</span> <b className="tnum">{tick}</b>
          <span className="muted tnum small"> / {newestTick}</span>
        </span>
        <span className="clock-item">
          <span className="muted">hour</span> <b className="tnum">{hourOf(tickOfDay, ticksPerDay)}</b>
        </span>
        {serverPaused ? (
          <span className="pill warn" title="the simulation server is paused">
            ❚❚ server paused
          </span>
        ) : null}
        <span className="muted small tnum perf" title="frames per second · draw calls · device pixel ratio">
          {perf.fps} fps · {perf.calls} dc · {perf.dpr.toFixed(1)}×
        </span>
      </div>
      <div className="hud-right">
        <Playback />
      </div>
    </header>
  )
})
