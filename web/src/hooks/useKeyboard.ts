import { useEffect } from 'react'
import { cameraCommands } from '../scene/sceneState'
import { history } from '../state/history'
import { useStore } from '../state/store'

function isTyping(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null
  if (!el || !el.tagName) return false
  const tag = el.tagName.toLowerCase()
  return tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable
}

/** space: pause/resume · , . step · L live · F frame population · Esc clear selection */
export function useKeyboard(): void {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return
      if (isTyping(e.target)) return
      switch (e.key) {
        case ' ':
          e.preventDefault()
          history.togglePause()
          break
        case ',':
          history.step(-1)
          break
        case '.':
          history.step(1)
          break
        case 'l':
        case 'L':
          history.goLive()
          break
        case 'f':
        case 'F':
          cameraCommands.frameRequested = true
          break
        case 'Escape': {
          const s = useStore.getState()
          if (s.followAgentId) s.follow(null)
          else s.select(null)
          break
        }
        default:
          return
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
}
