import { memo, useMemo } from 'react'
import { INK } from './chartTheme'
import { ChartTip, useChartTip } from './ChartTip'

export interface GossipNode {
  id: string
  name: string
  color: string
  alive: boolean
}
export interface GossipEdgeView {
  from: string
  to: string
  count: number
}

interface Props {
  nodes: GossipNode[]
  edges: GossipEdgeView[]
  total: number
  selectedId: string | null
  onSelect: (id: string) => void
}

const W = 360
const H = 260
const R = 96

/** Radial layout (no simulation): agents on a circle, edges weighted by transfer count. */
export const GossipGraph = memo(function GossipGraph({ nodes, edges, total, selectedId, onSelect }: Props) {
  const { tip, show, hide } = useChartTip()
  const pos = useMemo(() => {
    const m = new Map<string, { x: number; y: number }>()
    nodes.forEach((n, i) => {
      const a = (i / Math.max(1, nodes.length)) * Math.PI * 2 - Math.PI / 2
      m.set(n.id, { x: W / 2 + Math.cos(a) * R, y: H / 2 + Math.sin(a) * R })
    })
    return m
  }, [nodes])
  const maxCount = Math.max(1, ...edges.map((e) => e.count))
  if (nodes.length === 0) return <div className="muted small">No gossip transfers yet.</div>
  const byId = new Map(nodes.map((n) => [n.id, n]))
  return (
    <div className="chart">
      <div className="chart-sub">{total} transfers · edge weight = count</div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img" aria-label="gossip graph">
        {edges.map((e) => {
          const a = pos.get(e.from)
          const b = pos.get(e.to)
          if (!a || !b) return null
          const w = 1 + (e.count / maxCount) * 4
          const involved = selectedId === e.from || selectedId === e.to
          // slight curve so A→B and B→A do not overlap
          const mx = (a.x + b.x) / 2 + (b.y - a.y) * 0.12
          const my = (a.y + b.y) / 2 - (b.x - a.x) * 0.12
          return (
            <g key={e.from + '>' + e.to}>
              <path
                d={`M${a.x} ${a.y}Q${mx} ${my} ${b.x} ${b.y}`}
                fill="none"
                stroke={involved ? INK.primary : INK.secondary}
                strokeOpacity={involved ? 0.9 : 0.35}
                strokeWidth={w}
                strokeLinecap="round"
              />
              <path
                d={`M${a.x} ${a.y}Q${mx} ${my} ${b.x} ${b.y}`}
                fill="none"
                stroke="transparent"
                strokeWidth={Math.max(12, w + 8)}
                onPointerEnter={(ev) => {
                  const rect = (ev.currentTarget.ownerSVGElement as SVGSVGElement).getBoundingClientRect()
                  show((mx / W) * rect.width, (my / H) * rect.height, `${byId.get(e.from)?.name ?? e.from} → ${byId.get(e.to)?.name ?? e.to}`, [
                    { label: 'gossip transfers', value: String(e.count) },
                  ])
                }}
                onPointerLeave={hide}
              />
            </g>
          )
        })}
        {nodes.map((n) => {
          const p = pos.get(n.id)!
          const sel = n.id === selectedId
          const labelLeft = p.x < W / 2 - 4
          return (
            <g key={n.id} onClick={() => onSelect(n.id)} style={{ cursor: 'pointer' }}>
              <circle cx={p.x} cy={p.y} r={sel ? 9 : 7} fill={n.color} stroke={INK.surface} strokeWidth="2" opacity={n.alive ? 1 : 0.45} />
              {sel ? <circle cx={p.x} cy={p.y} r={13} fill="none" stroke={INK.primary} strokeWidth="1" /> : null}
              <text x={p.x + (labelLeft ? -12 : 12)} y={p.y + 4} fill={sel ? INK.primary : INK.secondary} fontSize="11" textAnchor={labelLeft ? 'end' : 'start'}>
                {n.name}
              </text>
            </g>
          )
        })}
      </svg>
      <ChartTip tip={tip} width={W} />
    </div>
  )
})
