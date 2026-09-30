import { useEffect } from 'react'
import { ControlRoom } from './control/ControlRoom'
import { Hud } from './control/Hud'
import { useKeyboard } from './hooks/useKeyboard'
import { startFeed } from './net/ws'
import { Scene } from './scene/Scene'
import { history } from './state/history'

export default function App() {
  useKeyboard()
  useEffect(() => startFeed(), [])
  useEffect(() => {
    const onVis = () => {
      if (document.visibilityState === 'visible') history.snap()
    }
    document.addEventListener('visibilitychange', onVis)
    return () => document.removeEventListener('visibilitychange', onVis)
  }, [])
  return (
    <div className="app">
      <Hud />
      <main className="stage">
        <Scene />
        <div className="stage-hint muted small">drag to orbit · scroll to zoom · click selects · double-click follows · F frames</div>
      </main>
      <aside className="control-room">
        <ControlRoom />
      </aside>
    </div>
  )
}
