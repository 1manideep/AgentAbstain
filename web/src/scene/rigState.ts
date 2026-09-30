/** Slots currently drawn by a GLB rig instead of the instanced capsule. */
export const rigState = {
  riggedSlots: new Set<number>(),
  /** Cap on simultaneously rigged agents; the rest stay instanced capsules. */
  maxRigged: 16,
}
