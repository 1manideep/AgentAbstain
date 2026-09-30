import { describe, expect, it } from 'vitest'
import { dayPhase, daylight, hourOfDay, nightAmount, sunElevation } from './daycycle'
import { Noise2D } from './noise'
import { BASIN_LEVEL, MAX_HILL, MIN_HEIGHT, Terrain, WATER_LEVEL } from './terrain'

const NODES = [
  { id: 'n1', x: 19, y: 1.6 },
  { id: 'n2', x: 6.8, y: 18 },
  { id: 'n3', x: -12, y: 15 },
  { id: 'n4', x: -19, y: -2.5 },
  { id: 'n5', x: -11.4, y: -15.4 },
  { id: 'n6', x: 12.5, y: -14.5 },
]

describe('terrain', () => {
  it('is deterministic for a seed and differs across seeds', () => {
    const a = new Terrain(42, 60, NODES)
    const b = new Terrain(42, 60, NODES)
    const c = new Terrain(43, 60, NODES)
    let diff = 0
    for (let i = 0; i < 200; i++) {
      const x = -33 + (i * 7919) % 66
      const z = -33 + (i * 104729) % 66
      expect(a.heightAt(x, z)).toBe(b.heightAt(x, z))
      if (Math.abs(a.heightAt(x, z) - c.heightAt(x, z)) > 1e-6) diff++
    }
    expect(diff).toBeGreaterThan(100)
    const n = new Noise2D(7)
    expect(n.noise2(1.5, 2.5)).toBe(new Noise2D(7).noise2(1.5, 2.5))
  })

  it('keeps heights in range with gentle hills', () => {
    const t = new Terrain(42, 60, NODES)
    let min = Infinity
    let max = -Infinity
    for (let z = -33; z <= 33; z += 1.5) {
      for (let x = -33; x <= 33; x += 1.5) {
        const h = t.heightAt(x, z)
        expect(Number.isFinite(h)).toBe(true)
        if (h < min) min = h
        if (h > max) max = h
      }
    }
    expect(min).toBeGreaterThanOrEqual(MIN_HEIGHT - 1e-6)
    expect(max).toBeLessThanOrEqual(MAX_HILL + 1e-6)
    expect(max).toBeGreaterThan(1.2)
    expect(min).toBeLessThan(WATER_LEVEL) // a lake exists somewhere
  })

  it('flattens node basins and the paths between them to the basin level', () => {
    const t = new Terrain(42, 60, NODES)
    for (const n of NODES) {
      expect(Math.abs(t.heightAt(n.x, n.y) - BASIN_LEVEL)).toBeLessThan(0.12)
      expect(Math.abs(t.heightAt(n.x + 2, n.y - 1.5) - BASIN_LEVEL)).toBeLessThan(0.12)
    }
    // midpoints of every path segment are flat too, and the surface is smooth along the path
    for (const s of t.segments) {
      const mx = (s.ax + s.bx) / 2
      const mz = (s.az + s.bz) / 2
      expect(Math.abs(t.heightAt(mx, mz) - BASIN_LEVEL)).toBeLessThan(0.12)
      let prev = t.heightAt(s.ax, s.az)
      for (let k = 0.05; k <= 1; k += 0.05) {
        const h = t.heightAt(s.ax + (s.bx - s.ax) * k, s.az + (s.bz - s.az) * k)
        expect(Math.abs(h - prev)).toBeLessThan(0.08)
        prev = h
      }
    }
    expect(t.isReserved(NODES[0]!.x, NODES[0]!.y)).toBe(true)
  })

  it('builds a watertight grid with colours and a grass mask', () => {
    const t = new Terrain(42, 60, NODES)
    const g = t.buildGeometry(32)
    expect(g.getAttribute('position').count).toBe(33 * 33)
    expect(g.getIndex()!.count).toBe(32 * 32 * 6)
    expect(g.getAttribute('color').count).toBe(33 * 33)
    const grass = g.getAttribute('aGrass')
    let grassy = 0
    for (let i = 0; i < grass.count; i++) if (grass.getX(i) > 0.5) grassy++
    expect(grassy).toBeGreaterThan(50)
  })

  it('scatters props outside basins, paths and water, deterministically', () => {
    const t = new Terrain(42, 60, NODES)
    const a = t.scatter({ spacing: 4, salt: 1 })
    const b = t.scatter({ spacing: 4, salt: 1 })
    expect(a.length).toBeGreaterThan(20)
    expect(a).toEqual(b)
    for (const p of a) {
      expect(t.isReserved(p.x, p.z, 0.8)).toBe(false)
      expect(p.y).toBeGreaterThan(WATER_LEVEL)
    }
    expect(t.scatter({ spacing: 4, salt: 2 })).not.toEqual(a)
  })
})

describe('day cycle', () => {
  it('maps tick_of_day to hours and sun elevation', () => {
    expect(hourOfDay(0, 0, 24)).toBe(0)
    expect(hourOfDay(6, 0, 24)).toBe(6)
    expect(hourOfDay(6, 0.5, 24)).toBeCloseTo(6.5, 6)
    expect(hourOfDay(23, 0.99, 24)).toBeLessThan(24)
    expect(hourOfDay(3, 0, 12)).toBe(6) // 12 ticks per day → 2 h per tick
    expect(sunElevation(6)).toBeCloseTo(0, 6)
    expect(sunElevation(12)).toBeCloseTo(1, 6)
    expect(sunElevation(18)).toBeCloseTo(0, 6)
    expect(sunElevation(0)).toBeCloseTo(-1, 6)
    expect(sunElevation(22)).toBeLessThan(-0.5)
  })

  it('classifies phases and light levels', () => {
    expect(dayPhase(6)).toBe('dawn')
    expect(dayPhase(12)).toBe('day')
    expect(dayPhase(18)).toBe('dusk')
    expect(dayPhase(22)).toBe('night')
    expect(dayPhase(3)).toBe('night')
    expect(daylight(12)).toBe(1)
    expect(daylight(0)).toBe(0)
    expect(daylight(6.5)).toBeGreaterThan(0)
    expect(daylight(6.5)).toBeLessThan(1)
    expect(nightAmount(0)).toBe(1)
    expect(nightAmount(12)).toBe(0)
  })
})
