/**
 * Ten distinct character presets for the procedural humanoids, keyed 0..9,
 * and a deterministic name → preset mapping: the ten demo agents (Ada … Juno)
 * are pinned by an explicit table; any other name (arrivals such as Kai,
 * children) picks a preset with a stable string hash so the same agent always
 * looks the same across sessions and clients.
 */

export type HairStyle = 'short' | 'curly' | 'bun' | 'ponytail' | 'bald' | 'undercut' | 'long' | 'braid' | 'buzz' | 'hat'
export type TopCut = 'tunic' | 'jacket' | 'vest' | 'dress' | 'tee'
export type Sleeves = 'long' | 'short'
export type Legwear = 'pants' | 'shorts' | 'bare'
export type Accessory = 'glasses' | 'scarf' | 'backpack' | 'toolbelt' | 'bracelet' | 'necklace'
export type TrimPart = 'headband' | 'belt' | 'wrist'

export interface Preset {
  id: number
  name: string
  description: string
  /** Standing height in world units, top of the skull (1.55–1.92). */
  height: number
  /** Shoulder width multiplier (1 = two heads wide). */
  shoulder: number
  /** Hip width multiplier. */
  hip: number
  /** Limb thickness multiplier. */
  limb: number
  /** Torso depth multiplier. */
  bulk: number
  skin: string
  hair: HairStyle
  hairColor: string
  top: TopCut
  topColor: string
  /** Shirt showing under a vest / open jacket. */
  shirtColor: string
  sleeves: Sleeves
  legs: Legwear
  bottomColor: string
  shoeColor: string
  /** Scarf, hat, backpack colour. */
  accentColor: string
  accessories: Accessory[]
  /** Parts drawn with the tier-tinted trim material. */
  trim: TrimPart[]
}

export const PRESETS: readonly Preset[] = [
  {
    id: 0,
    name: 'Ada',
    description: 'Medium build, dark hair in a bun, cream shirt under a teal vest, glasses.',
    height: 1.68,
    shoulder: 0.96,
    hip: 1.0,
    limb: 0.95,
    bulk: 0.95,
    skin: '#e3b78f',
    hair: 'bun',
    hairColor: '#2a1c14',
    top: 'vest',
    topColor: '#2d8f86',
    shirtColor: '#efe6d2',
    sleeves: 'long',
    legs: 'pants',
    bottomColor: '#2b3550',
    shoeColor: '#5a3b26',
    accentColor: '#2d8f86',
    accessories: ['glasses'],
    trim: ['headband', 'belt', 'wrist'],
  },
  {
    id: 1,
    name: 'Bao',
    description: 'Broad-shouldered, short black hair, olive field jacket, backpack.',
    height: 1.74,
    shoulder: 1.14,
    hip: 1.02,
    limb: 1.12,
    bulk: 1.1,
    skin: '#d9a97c',
    hair: 'short',
    hairColor: '#141010',
    top: 'jacket',
    topColor: '#5f6b3a',
    shirtColor: '#cfd6c2',
    sleeves: 'long',
    legs: 'pants',
    bottomColor: '#3a3d45',
    shoeColor: '#1f1d1c',
    accentColor: '#8a4b2e',
    accessories: ['backpack'],
    trim: ['belt', 'wrist'],
  },
  {
    id: 2,
    name: 'Cyra',
    description: 'Slim, deep brown skin, long braid, plum dress, gold necklace.',
    height: 1.62,
    shoulder: 0.88,
    hip: 0.98,
    limb: 0.86,
    bulk: 0.9,
    skin: '#5b3a24',
    hair: 'braid',
    hairColor: '#0f0b0a',
    top: 'dress',
    topColor: '#7b2d63',
    shirtColor: '#7b2d63',
    sleeves: 'short',
    legs: 'bare',
    bottomColor: '#7b2d63',
    shoeColor: '#c9a86a',
    accentColor: '#e0b45a',
    accessories: ['necklace'],
    trim: ['headband', 'belt', 'wrist'],
  },
  {
    id: 3,
    name: 'Dev',
    description: 'Average build, undercut, mustard tee, dark jeans, white trainers.',
    height: 1.8,
    shoulder: 1.02,
    hip: 0.96,
    limb: 1.0,
    bulk: 0.98,
    skin: '#9c6a45',
    hair: 'undercut',
    hairColor: '#1a1210',
    top: 'tee',
    topColor: '#d9a533',
    shirtColor: '#d9a533',
    sleeves: 'short',
    legs: 'pants',
    bottomColor: '#25304a',
    shoeColor: '#ecece6',
    accentColor: '#c4552f',
    accessories: ['bracelet'],
    trim: ['wrist', 'belt', 'headband'],
  },
  {
    id: 4,
    name: 'Enzo',
    description: 'Tall and heavy-set, curly brown hair, rust tunic, leather tool belt.',
    height: 1.88,
    shoulder: 1.18,
    hip: 1.12,
    limb: 1.2,
    bulk: 1.18,
    skin: '#c68e5f',
    hair: 'curly',
    hairColor: '#3b2416',
    top: 'tunic',
    topColor: '#a3452a',
    shirtColor: '#a3452a',
    sleeves: 'long',
    legs: 'pants',
    bottomColor: '#4a3626',
    shoeColor: '#2a1f18',
    accentColor: '#6b4a2f',
    accessories: ['toolbelt'],
    trim: ['belt', 'wrist'],
  },
  {
    id: 5,
    name: 'Faye',
    description: 'Petite, pale, long auburn hair, teal jacket, red boots and a mustard scarf.',
    height: 1.58,
    shoulder: 0.9,
    hip: 1.0,
    limb: 0.88,
    bulk: 0.9,
    skin: '#f1d3b3',
    hair: 'long',
    hairColor: '#8a3b1f',
    top: 'jacket',
    topColor: '#2f6f8f',
    shirtColor: '#e9e4d6',
    sleeves: 'long',
    legs: 'pants',
    bottomColor: '#55585f',
    shoeColor: '#a3282c',
    accentColor: '#d8a43a',
    accessories: ['scarf'],
    trim: ['headband', 'wrist', 'belt'],
  },
  {
    id: 6,
    name: 'Gil',
    description: 'Very tall and lanky, bald, grey shirt under a green vest, round glasses.',
    height: 1.92,
    shoulder: 1.0,
    hip: 0.9,
    limb: 0.86,
    bulk: 0.88,
    skin: '#e8c39e',
    hair: 'bald',
    hairColor: '#7a6a5c',
    top: 'vest',
    topColor: '#3d6b3a',
    shirtColor: '#c9ccd2',
    sleeves: 'long',
    legs: 'pants',
    bottomColor: '#8a7b5c',
    shoeColor: '#4d3520',
    accentColor: '#3d6b3a',
    accessories: ['glasses'],
    trim: ['belt', 'wrist', 'headband'],
  },
  {
    id: 7,
    name: 'Hex',
    description: 'Medium build, buzz cut, black tee, cargo greens, backpack and bracelet.',
    height: 1.66,
    shoulder: 1.0,
    hip: 0.94,
    limb: 1.0,
    bulk: 1.0,
    skin: '#7a4c2e',
    hair: 'buzz',
    hairColor: '#1b1613',
    top: 'tee',
    topColor: '#1d1f24',
    shirtColor: '#1d1f24',
    sleeves: 'short',
    legs: 'pants',
    bottomColor: '#4f5a3a',
    shoeColor: '#232326',
    accentColor: '#c7522e',
    accessories: ['backpack', 'bracelet'],
    trim: ['headband', 'belt', 'wrist'],
  },
  {
    id: 8,
    name: 'Ivo',
    description: 'Stocky, ruddy, blond under a green cap, denim jacket, boots and a tool belt.',
    height: 1.77,
    shoulder: 1.1,
    hip: 1.1,
    limb: 1.14,
    bulk: 1.14,
    skin: '#e6b9a0',
    hair: 'hat',
    hairColor: '#c9a35a',
    top: 'jacket',
    topColor: '#3a5a8c',
    shirtColor: '#d8d2c4',
    sleeves: 'long',
    legs: 'pants',
    bottomColor: '#5a3f2c',
    shoeColor: '#3b2a1c',
    accentColor: '#2f6b3f',
    accessories: ['toolbelt'],
    trim: ['belt', 'wrist'],
  },
  {
    id: 9,
    name: 'Juno',
    description: 'Athletic, warm brown skin, high ponytail, indigo tunic dress over leggings.',
    height: 1.71,
    shoulder: 0.98,
    hip: 0.96,
    limb: 0.96,
    bulk: 0.94,
    skin: '#b57a4c',
    hair: 'ponytail',
    hairColor: '#241812',
    top: 'dress',
    topColor: '#3b3f8f',
    shirtColor: '#3b3f8f',
    sleeves: 'long',
    legs: 'pants',
    bottomColor: '#1d1f2e',
    shoeColor: '#f0eee8',
    accentColor: '#d9c46a',
    accessories: ['necklace', 'bracelet'],
    trim: ['headband', 'belt', 'wrist'],
  },
]

/** The ten named demo agents map to presets by table; everyone else hashes. */
export const NAME_TABLE: ReadonlyMap<string, number> = new Map([
  ['ada', 0],
  ['bao', 1],
  ['cyra', 2],
  ['dev', 3],
  ['enzo', 4],
  ['faye', 5],
  ['gil', 6],
  ['hex', 7],
  ['ivo', 8],
  ['juno', 9],
])

/** FNV-1a over UTF-16 code units, folded to a preset index. */
export function hashName(name: string): number {
  let h = 0x811c9dc5
  for (let i = 0; i < name.length; i++) {
    h ^= name.charCodeAt(i)
    h = Math.imul(h, 0x01000193) >>> 0
  }
  return h % PRESETS.length
}

export function presetIndexFor(name: string): number {
  const key = name.trim().toLowerCase()
  const pinned = NAME_TABLE.get(key)
  if (pinned !== undefined) return pinned
  return hashName(key)
}

export function presetFor(name: string): Preset {
  return PRESETS[presetIndexFor(name)]!
}
