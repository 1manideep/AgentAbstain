import { Color, Vector3 } from 'three'

/** Per-frame lighting state written by the Sky system and read by water/props shaders. */
export const skyState = {
  sunDir: new Vector3(0, 1, 0),
  sunColor: new Color('#ffe9c4'),
  horizon: new Color('#8fb4ff'),
  zenith: new Color('#2a5fd8'),
  daylight: 1,
  night: 0,
  storm: 0,
  cloud: 1,
  hour: 12,
}
