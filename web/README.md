# The Void — web

Live 3D view and control room for the simulation (DESIGN §16). Vite + React 19 +
TypeScript, react-three-fiber, drei, zustand; charts are inline SVG.

## Scripts

| script | what |
|---|---|
| `npm run dev` | dev server; `/api` and `/ws` are proxied to the Python server on `127.0.0.1:8000` |
| `VITE_FEED=fixture npm run dev` | fixture mode: replays `public/fixtures/mock.jsonl`, POSTs are logged no-ops |
| `npm run gen:fixture [-- --out dir]` | synthetic 300-tick fixture + `api.json` (a genuine one: `void mock-feed --out web/public/fixtures/mock.jsonl`) |
| `npm run typecheck` / `test` / `build` / `lint` | tsc -b, vitest, vite build, oxlint |
| `npm run screenshot` / `screenshot:landscape` | Playwright smoke shots + fps / draw-call measurement (dev server must be running) |

Fixture pacing: `VITE_FIXTURE_PACE` (ts-delta multiplier, default 0.6), `VITE_FIXTURE_BURST` (ticks delivered at once on connect, default 40).

## Architecture in one paragraph

`src/state/history.ts` holds typed-array frames outside React with a virtual
timeline and the render clock; `src/scene/Driver.tsx` advances the clock,
samples once per frame into preallocated arrays and runs the scene systems in
order (terrain → agents → nodes → effects → atmosphere → camera → rig). Nothing
in the frame loop reads zustand; low-churn UI state lives in
`src/state/store.ts` and is committed at ≤ 4 Hz. Every agent-authored string is
rendered as React text or a drei `<Text>`; no HTML is ever built from data.

## Landscape

The terrain is a seeded heightmap (`hello.config.seed`, `src/scene/terrain.ts`)
with a flat basin around every resource node and flattened paths between them.
Agents, nodes, gadgets and props are placed with the same `heightAt(x, z)`. The
sky dome, sun/moon, stars, fog, cloud shadow and rain follow `tick_of_day` and
the snapshot's `weather`; scarcity turns the grass to straw. The HUD `⌗` button
toggles a subtle world-grid overlay on the terrain.

Rendering profile: on a hardware GPU the scene runs with MSAA, N8AO, bloom and
vignette at dpr 1–1.5; when a software rasteriser is detected (SwiftShader,
llvmpipe) MSAA/bloom/AO are off, dpr may drop to 0.75 and only the vignette
stays (`src/scene/gpu.ts`, `src/scene/Post.tsx`).

## Dropping in models (optional)

External CDNs are never used; everything is procedural unless you add files
under `public/models/`:

```
public/models/manifest.json
public/models/tree_pine.glb   public/models/tree_round.glb   public/models/tree_bush.glb
public/models/rock.glb        public/models/grass.glb        public/models/rig.glb
```

`manifest.json`:

```json
{
  "props": { "tree_pine": "tree_pine.glb", "tree_round": "tree_round.glb", "tree_bush": "tree_bush.glb", "rock": "rock.glb", "grass": "grass.glb" },
  "rig": "rig.glb"
}
```

* **Prop pack** (`useGLTFPack`, `src/scene/gltfPack.ts`): for each key present
  the instanced procedural geometry/material is replaced by the GLB's first
  mesh. Author props with the base on `y = 0`, roughly 1–3 units tall; the
  seeded scatter (positions, rotations, scale range, tint) is unchanged, so the
  draw-call budget stays at one call per species.
* **Character rig** (`useRig`, `src/scene/Rig.tsx`): a ReadyPlayerMe avatar with
  Mixamo clips exported as one GLB. Up to 16 visible agents get a
  `SkeletonUtils` clone with an `AnimationMixer`; the procedural weight vector
  `{idle, walk, sleep, degenerate, dead}` becomes the clip weights. Clips are
  matched by name: `idle|breath|stand`, `walk|run|jog`, `sleep|lie|rest`,
  `degenerat|twitch|dance|shake|glitch`, `dead|death|die|fall`. Agents beyond the
  cap keep the instanced capsule. Materials are tinted with the tier colour.
  Without `rig.glb` (or a manifest `rig`) the hook is a no-op.

The server's CSP allows same-origin assets and blob workers
(`worker-src 'self' blob:`), which is what the troika text workers and GLB
loading need.
