/**
 * Sky dome, stars, sun/moon/hemisphere lights, fog, cloud shadow and rain,
 * all driven by the render clock's hour of day and the sampled weather.
 * Nothing here allocates per frame; colours are lerped into preallocated
 * objects and the rain buffer is fixed size.
 */
import { useThree } from '@react-three/fiber'
import { useEffect, useMemo, useRef } from 'react'
import {
  AmbientLight,
  BackSide,
  BufferAttribute,
  BufferGeometry,
  Color,
  DirectionalLight,
  FogExp2,
  HemisphereLight,
  LineBasicMaterial,
  LineSegments,
  Points,
  PointsMaterial,
  ShaderMaterial,
  SphereGeometry,
  Vector3,
} from 'three'
import { daylight as daylightOf, horizonWarmth, nightAmount, sunDirection } from './daycycle'
import { mulberry32, Noise2D } from './noise'
import { registerSystem, SYS_ATMOSPHERE, type FrameCtx } from './sceneState'
import { skyState } from './skyState'

const DOME_R = 480
const STARS = 1400
const RAIN = 1400
const RAIN_BOX = 44
const RAIN_H = 26

const skyVertex = /* glsl */ `
  varying vec3 vDir;
  void main() {
    vDir = normalize((modelMatrix * vec4(position, 1.0)).xyz);
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    gl_Position = projectionMatrix * mv;
    gl_Position.z = gl_Position.w * 0.99999; // just inside the far plane so the depth test keeps it behind everything
  }
`
const skyFragment = /* glsl */ `
  uniform vec3 uZenith;
  uniform vec3 uHorizon;
  uniform vec3 uGround;
  uniform vec3 uSunDir;
  uniform vec3 uSunColor;
  uniform float uSunGlow;
  uniform float uStorm;
  varying vec3 vDir;
  void main() {
    vec3 d = normalize(vDir);
    float h = d.y;
    vec3 sky = mix(uHorizon, uZenith, smoothstep(-0.02, 0.55, h));
    vec3 below = mix(uGround, uHorizon, smoothstep(-0.35, 0.0, h));
    vec3 col = h < 0.0 ? below : sky;
    float sd = max(dot(d, uSunDir), 0.0);
    float disc = smoothstep(0.9985, 0.9995, sd);
    float glow = pow(sd, 32.0) * 0.55 + pow(sd, 6.0) * 0.18;
    col += uSunColor * (glow * uSunGlow + disc * uSunGlow * 1.5) * (1.0 - 0.8 * uStorm);
    // stormy: flatten the gradient toward a grey murk
    col = mix(col, vec3(0.16, 0.18, 0.21) * (0.3 + 0.7 * max(uZenith.b, 0.2)), uStorm * 0.75);
    gl_FragColor = vec4(col, 1.0);
  }
`

const C = {
  zenithDay: new Color('#2b62d6'),
  zenithDusk: new Color('#1f2f6b'),
  zenithNight: new Color('#050914'),
  horizonDay: new Color('#a8c6f2'),
  horizonWarm: new Color('#f2a35e'),
  horizonNight: new Color('#0d1530'),
  groundDay: new Color('#3b4657'),
  groundNight: new Color('#05070d'),
  sunNoon: new Color('#fff4e0'),
  sunWarm: new Color('#ffb066'),
  moon: new Color('#8fa8e6'),
  hemiGround: new Color('#2b3524'),
  fogDay: new Color('#a8c6f2'),
  fogStorm: new Color('#30353d'),
}

export function Sky() {
  const scene = useThree((s) => s.scene)
  const sun = useRef<DirectionalLight>(null)
  const moon = useRef<DirectionalLight>(null)
  const hemi = useRef<HemisphereLight>(null)
  const ambient = useRef<AmbientLight>(null)

  const dome = useMemo(() => {
    const geom = new SphereGeometry(DOME_R, 28, 14)
    const mat = new ShaderMaterial({
      vertexShader: skyVertex,
      fragmentShader: skyFragment,
      side: BackSide,
      depthWrite: false,
      depthTest: true,
      fog: false,
      uniforms: {
        uZenith: { value: new Color() },
        uHorizon: { value: new Color() },
        uGround: { value: new Color() },
        uSunDir: { value: new Vector3(0, 1, 0) },
        uSunColor: { value: new Color() },
        uSunGlow: { value: 1 },
        uStorm: { value: 0 },
      },
    })
    return { geom, mat }
  }, [])

  const stars = useMemo(() => {
    const rand = mulberry32(1234567)
    const pos = new Float32Array(STARS * 3)
    for (let i = 0; i < STARS; i++) {
      // uniform on the upper hemisphere, denser toward the zenith
      const u = rand()
      const v = rand()
      const theta = 2 * Math.PI * u
      const phi = Math.acos(1 - v) * 0.5
      const r = DOME_R * 0.96
      pos[i * 3] = r * Math.sin(phi) * Math.cos(theta)
      pos[i * 3 + 1] = r * Math.cos(phi)
      pos[i * 3 + 2] = r * Math.sin(phi) * Math.sin(theta)
    }
    const geom = new BufferGeometry()
    geom.setAttribute('position', new BufferAttribute(pos, 3))
    const mat = new PointsMaterial({ color: '#dfe8ff', size: 2.2, sizeAttenuation: false, transparent: true, opacity: 0, depthWrite: false, depthTest: true, fog: false })
    const points = new Points(geom, mat)
    points.frustumCulled = false
    return { geom, mat, points }
  }, [])

  const rain = useMemo(() => {
    const rand = mulberry32(777)
    const pos = new Float32Array(RAIN * 2 * 3)
    const seeds = new Float32Array(RAIN * 3) // x, z offsets and phase
    for (let i = 0; i < RAIN; i++) {
      seeds[i * 3] = (rand() - 0.5) * RAIN_BOX
      seeds[i * 3 + 1] = (rand() - 0.5) * RAIN_BOX
      seeds[i * 3 + 2] = rand() * RAIN_H
    }
    const geom = new BufferGeometry()
    const attr = new BufferAttribute(pos, 3)
    geom.setAttribute('position', attr)
    geom.setDrawRange(0, 0)
    const mat = new LineBasicMaterial({ color: '#b9c9dd', transparent: true, opacity: 0.0, depthWrite: false })
    const lines = new LineSegments(geom, mat)
    lines.frustumCulled = false
    return { geom, attr, pos, seeds, mat, lines }
  }, [])

  const fog = useMemo(() => new FogExp2(C.fogDay.clone(), 0.004), [])
  const bg = useMemo(() => C.horizonDay.clone(), [])
  useEffect(() => {
    scene.fog = fog
    scene.background = bg
    return () => {
      if (scene.fog === fog) scene.fog = null
      if (scene.background === bg) scene.background = null
    }
  }, [scene, fog, bg])

  useEffect(() => {
    const cloudNoise = new Noise2D(99)
    const sunDir = { x: 0, y: 1, z: 0 }
    const zenith = new Color()
    const horizon = new Color()
    const ground = new Color()
    const sunColor = new Color()
    const tmp = new Color()
    let weather = 0
    let rainAmt = 0
    const system = (ctx: FrameCtx) => {
      const { dt, now, hour } = ctx
      weather += (ctx.out.weather - weather) * Math.min(1, dt * 1.5)
      const storm = Math.max(0, -weather)
      const day = daylightOf(hour)
      const night = nightAmount(hour)
      const warm = horizonWarmth(hour)
      sunDirection(hour, sunDir)

      // colours
      zenith.copy(C.zenithNight).lerp(C.zenithDusk, Math.min(1, day * 2 + warm * 0.5)).lerp(C.zenithDay, day)
      horizon.copy(C.horizonNight).lerp(C.horizonDay, day)
      tmp.copy(C.horizonWarm)
      horizon.lerp(tmp, warm * Math.max(day, 0.15) * 0.85)
      ground.copy(C.groundNight).lerp(C.groundDay, day)
      sunColor.copy(C.sunNoon).lerp(C.sunWarm, warm)
      // cloud shadow: slow scrolling noise on the sun
      const n = cloudNoise.fbm(now * 0.045, 1.7, 3)
      const cloud = 1 - (0.12 + 0.6 * storm) * (0.5 + 0.5 * n)

      // publish for water/props
      skyState.sunDir.set(sunDir.x, sunDir.y, sunDir.z)
      skyState.sunColor.copy(sunColor)
      skyState.horizon.copy(horizon)
      skyState.zenith.copy(zenith)
      skyState.daylight = day
      skyState.night = night
      skyState.storm = storm
      skyState.cloud = cloud
      skyState.hour = hour

      // dome
      const u = dome.mat.uniforms
      ;(u.uZenith!.value as Color).copy(zenith)
      ;(u.uHorizon!.value as Color).copy(horizon)
      ;(u.uGround!.value as Color).copy(ground)
      ;(u.uSunDir!.value as Vector3).set(sunDir.x, sunDir.y, sunDir.z)
      ;(u.uSunColor!.value as Color).copy(sunColor)
      u.uSunGlow!.value = Math.max(0, sunDir.y + 0.08) * 1.2
      u.uStorm!.value = storm

      // fog and background: horizon colour blended toward storm murk
      bg.copy(horizon).lerp(C.fogStorm, storm * 0.8)
      fog.color.copy(bg)
      fog.density = 0.0034 + 0.016 * storm + 0.002 * night

      // lights
      if (sun.current) {
        sun.current.intensity = day * 2.1 * cloud * (1 - 0.55 * storm)
        sun.current.color.copy(sunColor)
        sun.current.position.set(sunDir.x * 120, Math.max(0.02, sunDir.y) * 120, sunDir.z * 120)
      }
      if (moon.current) {
        moon.current.intensity = night * 0.6 * (1 - 0.6 * storm)
        moon.current.position.set(-sunDir.x * 100, Math.max(0.25, -sunDir.y) * 100, -sunDir.z * 100 + 30)
      }
      if (hemi.current) {
        hemi.current.intensity = 0.42 + 0.7 * day * (1 - 0.4 * storm)
        hemi.current.color.copy(zenith).lerp(horizon, 0.5)
        hemi.current.groundColor.copy(C.hemiGround).lerp(C.groundNight, night)
      }
      if (ambient.current) ambient.current.intensity = 0.14 + 0.3 * night

      // stars
      stars.mat.opacity = night * (1 - 0.9 * storm) * 0.9
      stars.points.visible = stars.mat.opacity > 0.01
      stars.points.rotation.y = now * 0.004

      // rain (weather < -0.5): fixed buffer, positions wrap around the camera
      const rainTarget = weather < -0.5 ? Math.min(1, (-weather - 0.5) * 3) : 0
      rainAmt += (rainTarget - rainAmt) * Math.min(1, dt * 1.2)
      if (rainAmt > 0.01 && ctx.camera) {
        const cx = ctx.camera.position.x
        const cz = ctx.camera.position.z
        const p = rain.pos
        const sd = rain.seeds
        const fall = now * 22
        for (let i = 0; i < RAIN; i++) {
          const y = RAIN_H - ((sd[i * 3 + 2]! + fall) % RAIN_H)
          const x = cx + sd[i * 3]!
          const z = cz + sd[i * 3 + 1]!
          const b = i * 6
          p[b] = x
          p[b + 1] = y
          p[b + 2] = z
          p[b + 3] = x + 0.08
          p[b + 4] = y + 0.75
          p[b + 5] = z
        }
        rain.attr.needsUpdate = true
        rain.geom.setDrawRange(0, RAIN * 2)
        rain.mat.opacity = 0.42 * rainAmt
        rain.lines.visible = true
      } else {
        rain.lines.visible = false
      }
    }
    return registerSystem(SYS_ATMOSPHERE, system)
  }, [dome, stars, rain, fog, bg])

  useEffect(
    () => () => {
      dome.geom.dispose()
      dome.mat.dispose()
      stars.geom.dispose()
      stars.mat.dispose()
      rain.geom.dispose()
      rain.mat.dispose()
    },
    [dome, stars, rain],
  )

  return (
    <group>
      <mesh geometry={dome.geom} material={dome.mat} renderOrder={50} frustumCulled={false} />
      <primitive object={stars.points} renderOrder={51} />
      <primitive object={rain.lines} />
      <directionalLight ref={sun} position={[40, 80, 20]} intensity={2} color={C.sunNoon} />
      <directionalLight ref={moon} position={[-40, 60, 30]} intensity={0} color={C.moon} />
      <hemisphereLight ref={hemi} args={['#8fb4ff', '#2b3524', 0.9]} />
      <ambientLight ref={ambient} intensity={0.1} color="#7d8fd0" />
    </group>
  )
}
