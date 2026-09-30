import type { RosterAgent, TreeNode } from '../protocol'

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
