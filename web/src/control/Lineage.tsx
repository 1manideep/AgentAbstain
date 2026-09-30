import { memo, useEffect, useMemo, useState } from 'react'
import type { RosterAgent, TreeNode, TreeResponse } from '../protocol'
import { apiGet, FIXTURE_MODE } from '../net/api'
import { selectTierColor, useStore } from '../state/store'

/** Builds the lineage tree from the roster (fixture mode, or when /api/tree fails). */
export function treeFromRoster(roster: RosterAgent[]): TreeNode[] {
  const nodes = new Map<string, TreeNode>()
  for (const a of roster) {
    nodes.set(a.id, { id: a.id, name: a.name, tier: a.tier, generation: a.generation, status: a.status, born_tick: a.born_tick, died_tick: a.died_tick, children: [] })
  }
  const roots: TreeNode[] = []
  for (const a of roster) {
    const n = nodes.get(a.id)!
    const parent = a.parent_id ? nodes.get(a.parent_id) : undefined
    if (parent) parent.children.push(n)
    else roots.push(n)
  }
  const sort = (list: TreeNode[]) => {
    list.sort((x, y) => x.born_tick - y.born_tick || x.name.localeCompare(y.name))
    for (const n of list) sort(n.children)
  }
  sort(roots)
  return roots
}

const Node = memo(function Node({ n, depth, onSelect, selectedId, color }: { n: TreeNode; depth: number; onSelect: (id: string) => void; selectedId: string | null; color: (tier: string) => string }) {
  const dead = n.status !== 'alive'
  return (
    <>
      <div className={'tree-row' + (n.id === selectedId ? ' selected' : '') + (dead ? ' dead' : '')} style={{ paddingLeft: 8 + depth * 16 }} onClick={() => onSelect(n.id)}>
        <span className="tree-branch" aria-hidden="true">
          {depth > 0 ? '└' : ''}
        </span>
        <span className="tree-dot" style={{ background: color(n.tier) }} />
        <span className="tree-name">{n.name}</span>
        <span className="muted small">
          g{n.generation} · {n.tier}
        </span>
        <span className={'tree-state ' + (dead ? 'dead' : 'alive')}>{dead ? `✕ ${n.died_tick ?? ''}` : '● alive'}</span>
      </div>
      {n.children.map((c) => (
        <Node key={c.id} n={c} depth={depth + 1} onSelect={onSelect} selectedId={selectedId} color={color} />
      ))}
    </>
  )
})

export const Lineage = memo(function Lineage() {
  const roster = useStore((s) => s.roster)
  const rosterRev = useStore((s) => s.rosterRev)
  const config = useStore((s) => s.config)
  const selectedId = useStore((s) => s.selectedAgentId)
  const select = useStore((s) => s.select)
  const [remote, setRemote] = useState<TreeNode[] | null>(null)

  useEffect(() => {
    if (FIXTURE_MODE) return
    let cancelled = false
    const t = setTimeout(() => {
      apiGet<TreeResponse>('/api/tree')
        .then((r) => {
          if (!cancelled && Array.isArray(r.roots)) setRemote(r.roots)
        })
        .catch(() => {
          if (!cancelled) setRemote(null)
        })
    }, 800)
    return () => {
      cancelled = true
      clearTimeout(t)
    }
  }, [rosterRev])

  const roots = useMemo(() => remote ?? treeFromRoster(roster), [remote, roster])
  const color = useMemo(() => (tier: string) => selectTierColor(config, tier), [config])
  if (roots.length === 0) return <div className="muted small">No agents yet.</div>
  return (
    <div className="tree">
      {roots.map((n) => (
        <Node key={n.id} n={n} depth={0} onSelect={select} selectedId={selectedId} color={color} />
      ))}
    </div>
  )
})
