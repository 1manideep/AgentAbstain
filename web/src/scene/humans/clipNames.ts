/**
 * The clip vocabulary of the procedural characters: names, loop flags and durations in one place so the
 * clip author (clips.ts), the behaviour controller (CharacterSystem.tsx / behaviour.ts) and the gallery
 * agree without importing each other.
 *
 * Base states (blended by weight from the sampled snapshot): idle, idle_fidget, idle_guard, walk,
 * walk_brisk, sleep, degenerate, dead. Gestures (one-shots or short holds fired from world events):
 * talk, forage, write, read, give, receive, tinker, summon, think, shake, cheer, slump, wave.
 */
export type ClipName =
  | 'idle'
  | 'idle_fidget'
  | 'idle_guard'
  | 'walk'
  | 'walk_brisk'
  | 'sleep'
  | 'degenerate'
  | 'dead'
  | 'talk'
  | 'forage'
  | 'write'
  | 'read'
  | 'give'
  | 'receive'
  | 'tinker'
  | 'summon'
  | 'think'
  | 'shake'
  | 'cheer'
  | 'slump'
  | 'wave'

export const CLIP_NAMES: readonly ClipName[] = [
  'idle', 'idle_fidget', 'idle_guard', 'walk', 'walk_brisk', 'sleep', 'degenerate', 'dead',
  'talk', 'forage', 'write', 'read', 'give', 'receive', 'tinker', 'summon', 'think', 'shake', 'cheer', 'slump', 'wave',
]

/** The eight base states; everything else is a gesture. */
export const BASE_CLIPS: readonly ClipName[] = ['idle', 'idle_fidget', 'idle_guard', 'walk', 'walk_brisk', 'sleep', 'degenerate', 'dead']
export const GESTURE_CLIPS: readonly ClipName[] = ['talk', 'forage', 'write', 'read', 'give', 'receive', 'tinker', 'summon', 'think', 'shake', 'cheer', 'slump', 'wave']

/** Whether the clip loops (first and last keyframes are equal) or clamps at its end. */
export const CLIP_LOOPS: Readonly<Record<ClipName, boolean>> = {
  idle: true,
  idle_fidget: true,
  idle_guard: true,
  walk: true,
  walk_brisk: true,
  sleep: true,
  degenerate: true,
  dead: false,
  talk: false,
  forage: true,
  write: true,
  read: true,
  give: false,
  receive: false,
  tinker: true,
  summon: false,
  think: true,
  shake: false,
  cheer: false,
  slump: false,
  wave: false,
}

export const CLIP_DURATION: Readonly<Record<ClipName, number>> = {
  idle: 6,
  idle_fidget: 5,
  idle_guard: 6,
  walk: 1,
  walk_brisk: 0.85,
  sleep: 4,
  degenerate: 1.6,
  dead: 1.4,
  talk: 1.2,
  forage: 1.5,
  write: 2,
  read: 3,
  give: 1.4,
  receive: 1.2,
  tinker: 2,
  summon: 1.6,
  think: 3,
  shake: 1.0,
  cheer: 1.2,
  slump: 1.4,
  wave: 1.2,
}
