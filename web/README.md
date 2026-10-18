# The Void — web

Live 3D view and control room for the simulation (DESIGN §16). Vite + React 19 +
TypeScript, react-three-fiber, drei, zustand; charts are inline SVG.

## Scripts

| script | what |
|---|---|
| `npm run dev` | dev server; `/api` and `/ws` are proxied to the Python server on `127.0.0.1:8000` |
| `VITE_FEED=fixture npm run dev` | fixture mode: replays `public/fixtures/mock.jsonl` and answers the control room's detail requests from `public/fixtures/api.json`; POSTs are logged no-ops |
| `npm run gen:fixture [-- --out DIR [--force]]` | a synthetic 300-tick fixture + `api.json`, written to a fresh temp dir (the path is printed) unless `--out DIR` (or `FIXTURE_OUT`) names one; writing into `public/fixtures`, which holds the committed recording, is refused without `--force` |
| `npm run typecheck` / `test` / `build` / `lint` | tsc -b, vitest, vite build, oxlint (run vitest as `npx vitest run --root .`) |
| `npm run screenshot` / `screenshot:landscape` | Playwright smoke shots + fps / draw-call measurement (dev server must be running) |
| `npm run screenshot:tour` | visual tour, 35 stills + captions in `screenshots/tour/` (fixture dev server, best with `VITE_FIXTURE_BURST=1000`) |

The committed fixture is a genuine recording, not the synthetic one: `void mock-feed` (from the repository root, `.venv/bin/void mock-feed`) runs `configs/demo_memory.yaml` for 240 ticks on scripted brains and writes `web/public/fixtures/mock.jsonl` and `api.json`. Those are its defaults, so a bare `void mock-feed` re-records the committed fixture in place; `--config`, `--ticks`, `--seed` and `--out` record something else (a config that could call a paid provider is refused).

Fixture pacing: `VITE_FIXTURE_PACE` (ts-delta multiplier, default 0.6), `VITE_FIXTURE_BURST` (ticks delivered at once on connect, default 40).

The screenshot scripts drive headless Chromium through Playwright. Install its browser once with `npx playwright install chromium` (on a bare Linux box `npx playwright install --with-deps chromium` also installs the system libraries); `screenshot:landscape`, `screenshot:humans` and `screenshot:behaviour` use it unless `VOID_CHROME` names a Chrome/Chromium executable. `screenshot` and `screenshot:tour` still default to a fixed Chromium path from the machine they were written on, so on any other machine set `VOID_CHROME` for them (for example to the `chrome` binary that `npx playwright install chromium` puts under `~/.cache/ms-playwright/`, or under `$PLAYWRIGHT_BROWSERS_PATH` when that is set). `VOID_URL` points any of them at another server.

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

## Characters

Without a `rig.glb`, agents are drawn as procedural stylised humans instead of
the instanced capsules (`src/scene/humans/`):

| file | what |
|---|---|
| `humanoid.ts` | `buildHumanoid(preset)`: a 19-bone hierarchy (hips, spine, chest, neck, head, shoulder/upperArm/forearm/hand L+R, upperLeg/lowerLeg/foot L+R) and two skinned meshes — the body (skin, hair, clothes, shoes, accessories as vertex colours; one draw call) and the tier-tinted trim (headband / belt / wrist band; one draw call). Revolved profiles with per-vertex skin weights that blend within a small radius of each joint; ≤ 4k triangles; faces +Z, feet on y = 0, 7.5-head proportions. |
| `presets.ts` | ten presets (0..9: Ada, Bao, Cyra, Dev, Enzo, Faye, Gil, Hex, Ivo, Juno) varying height 1.55–1.92, shoulder/hip width, limb thickness, skin tone, hair (short, curly, bun, ponytail, bald, undercut, long, braid, buzz, hat), top cut (tee, tunic, jacket, vest, dress), colours, shoes and accessories (glasses, scarf, backpack, tool belt, bracelet, necklace). `presetFor(name)`: the ten names map by table (case-insensitive); any other name (arrivals like Kai, children) hashes (FNV-1a) to a stable preset. |
| `clips.ts` | `buildClips(bones, preset)` authors seven full-body clips in code: `idle` (breathing, weight shift, head turns; 6 s loop), `walk` (contralateral swing, knee flex, hip sway and bob; 1 s loop), `sleep` (curled on the side, breathing; loop), `degenerate` (arms out, twitching limbs and head jerks; loop), `dead` (knees buckle, falls on the back; clamps), `talk` (raised hand wave + nod; 1.2 s one-shot), `forage` (crouch and reach down; 1.5 s loop). |
| `CharacterSystem.tsx` | the scene system (mounted in `Scene.tsx` next to `<Rig />`): up to `rigState.maxRigged` visible agents get a puppet with an `AnimationMixer`; the capsule path's per-slot weights `{idle, walk, sleep, degenerate, dead}` drive the clip weights, `talk` fires from talk events at the render tick, `forage` loops while the agent forages standing still. Position/heading/birth scale/alpha, degeneration jitter and the selected/hovered tint follow the capsule rules; rigged slots are marked in `rigState.riggedSlots` so `Agents.tsx` skips their capsule (the shadow blob stays). Puppets are picked by hover/click/double-click like capsules. |
| `Gallery.tsx` | `#/characters`: the ten characters in a row, labelled, cycling through the clips. |

Fallback rules: characters are off on a software rasteriser (`gpuProfile.software`)
and with `?rig=0`; `?rig=1` forces them on (used for SwiftShader screenshots).
A loaded `rig.glb` always wins: while it is mounted the procedural puppets are hidden.

Gallery URL parameters (before or after the hash): `?clip=walk` forces a clip,
`?still=1` freezes at t = 0.4 s (`?t=1.2` picks the time), `?moves=1&preset=3`
shows one preset seven times, one per clip, each frozen mid-clip, `?tier=4fd1c5`
sets the trim colour.

Screenshots (`screenshots/humans-contact.png`, `humans-moves.png`, `humans-world.png` daytime close-up, `humans-world-night.png` a group of walkers in the open):

```
npm run build && npx vite preview --port 4173      # gallery shots are taken from dist
VITE_FEED=fixture npm run dev -- --port 5173       # world shot uses fixture mode with ?rig=1
npm run screenshot:humans                          # VOID_SHOTS=contact,moves,world,world-night · VOID_MOVES_PRESET=1
npm run screenshot:behaviour                       # screenshots/behaviour-world.png (fixture mode, dev server on 5173)
npm run screenshot:tour                            # screenshots/tour/*.png + README.md captions (same server; VOID_TOUR=01,16 picks shots)
```

The tour (`scripts/screenshot-tour.mjs`) picks every tick and group from the
recording, frames each view by raycasting candidate cameras against the scene
(nothing between the camera and the subject, no clutter at the lens), and
replays the recorded events of that tick so bubbles and bursts are on screen.
It imports the store from the dev server's module graph, so it needs `vite`
rather than `vite preview`; start the server with `VITE_FIXTURE_BURST=1000` so
the whole recording arrives at once.

`behaviour-world.png` freezes a daytime group and injects four events through
`window.__void.inject` (the same path the socket takes): a talk whose claim is
false, the listener's claim feedback (red X on the side away from the
speaker, head shake), a remember (note card stacked above the speech bubble,
writing gesture) and a nightly maintenance pass.

The server sends this Content-Security-Policy on `/`, `/api/*` and every HTML
response (`void/server/auth.py`, `CSP`):

```
default-src 'self'; script-src 'self' blob:; worker-src 'self' blob:; img-src 'self' data: blob:; connect-src 'self' ws: wss: blob:; object-src 'none'; base-uri 'none'; form-action 'none'
```

`blob:` in `script-src` and `worker-src` is for the troika text workers; `blob:`
in `img-src` and `connect-src` is for GLB loading, since GLTFLoader turns the
textures embedded in a GLB into `blob:` URLs and loads them through `<img>` or
`fetch`. Nothing is loaded from another origin, so fonts, models and textures
must be served by the app itself (`public/`).
