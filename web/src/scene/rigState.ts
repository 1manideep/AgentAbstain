/** Slots currently drawn by a rig (GLB or procedural humanoid) instead of the instanced capsule. */
export const rigState = {
  riggedSlots: new Set<number>(),
  /** Cap on simultaneously rigged agents; the rest stay instanced capsules. */
  maxRigged: 16,
  /** True while a GLB rig (Rig.tsx) is mounted; the procedural characters then yield to it. */
  glbActive: false,
}
