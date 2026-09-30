import { useEffect, useState } from 'react'
import { terrainState } from './terrain'

/** Re-renders when the terrain is (re)built so static placements can resample heights. */
export function useTerrainRev(): number {
  const [rev, setRev] = useState(terrainState.rev)
  useEffect(() => {
    const fn = () => setRev(terrainState.rev)
    terrainState.listeners.add(fn)
    return () => {
      terrainState.listeners.delete(fn)
    }
  }, [])
  return rev
}
