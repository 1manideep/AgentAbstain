import { memo, useMemo } from 'react'
import { useShallow } from 'zustand/react/shallow'
import { selectTierColor, useStore } from '../../state/store'
import { Card } from '../Card'
import { GossipGraph, type GossipEdgeView, type GossipNode } from '../charts/GossipGraph'

export const GossipCard = memo(function GossipCard() {
  const rev = useStore((s) => s.seriesRev)
  const series = useStore((s) => s.series)
  const rosterById = useStore((s) => s.rosterById)
  const config = useStore((s) => s.config)
  const { selectedId, select } = useStore(useShallow((s) => ({ selectedId: s.selectedAgentId, select: s.select })))
  const { nodes, edges } = useMemo(() => {
    void rev
    const ids = new Set<string>()
    const edges: GossipEdgeView[] = []
    for (const e of series.gossipEdges.values()) {
      ids.add(e.from)
      ids.add(e.to)
      edges.push({ from: e.from, to: e.to, count: e.count })
    }
    const nodes: GossipNode[] = Array.from(ids)
      .map((id) => {
        const a = rosterById.get(id)
        return { id, name: a?.name ?? id, color: selectTierColor(config, a?.tier ?? ''), alive: a?.status === 'alive' }
      })
      .sort((a, b) => a.name.localeCompare(b.name))
    return { nodes, edges }
  }, [rev, series, rosterById, config])
  return (
    <Card id="gossip" title="Gossip graph">
      <GossipGraph nodes={nodes} edges={edges} total={series.gossipTotal} selectedId={selectedId} onSelect={select} />
    </Card>
  )
})
