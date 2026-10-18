import { memo, useMemo } from 'react'
import { selectTierColor, useStore } from '../../state/store'
import { Card } from '../Card'
import { fmtCompact, SERIES } from '../charts/chartTheme'
import { VaultChart, vaultPoints, type VaultLine, type VaultPoint } from '../charts/VaultChart'
import { EmptyState } from './EmptyState'

export interface VaultAgent {
  id: string
  name: string
  color: string
  /** The agent's own day figures (vault_by_agent), in day order. */
  points: VaultPoint[]
}

/** The card's body: the population's series, and the selected agent's against it (null when none is selected). */
export const VaultBody = memo(function VaultBody({ population, agent }: { population: VaultPoint[]; agent: VaultAgent | null }) {
  const popLines = useMemo((): VaultLine[] => [{ key: 'population', label: 'population', color: SERIES[0], points: population }], [population])
  const agentLines = useMemo(
    (): VaultLine[] =>
      agent === null
        ? []
        : [
            { key: 'agent', label: agent.name, color: agent.color, points: agent.points },
            { key: 'population', label: 'population', color: SERIES[0], points: population, reference: true },
          ],
    [agent, population],
  )
  if (population.length === 0) return <EmptyState />
  return (
    <>
      <div className="sub">Population</div>
      <VaultChart lines={popLines} />
      {agent === null ? (
        <div className="muted small" style={{ marginTop: 10 }}>
          Select an agent to compare its vault.
        </div>
      ) : (
        <>
          <div className="sub">{agent.name}</div>
          {agent.points.length > 0 ? <VaultChart lines={agentLines} /> : <EmptyState text={`no vault figures for ${agent.name} yet`} />}
        </>
      )}
    </>
  )
})

/**
 * Vault growth and structure from the day rows (MEMORY_EVOLUTION §5.10): the population's notes, bytes, links per
 * note, orphan rate and index coverage per day, and the selected agent's own series when one is selected.
 * Registered only on runs with the memory layer enabled.
 */
export const VaultCard = memo(function VaultCard() {
  const rev = useStore((s) => s.seriesRev)
  const series = useStore((s) => s.series)
  const selectedId = useStore((s) => s.selectedAgentId)
  const rosterById = useStore((s) => s.rosterById)
  const config = useStore((s) => s.config)
  const population = useMemo(() => {
    void rev
    return vaultPoints(series.daily)
  }, [rev, series])
  const record = selectedId === null ? undefined : rosterById.get(selectedId)
  const name = record?.name ?? selectedId ?? ''
  const tier = record?.tier ?? ''
  const agent = useMemo((): VaultAgent | null => {
    void rev
    if (selectedId === null) return null
    return { id: selectedId, name, color: selectTierColor(config, tier), points: vaultPoints(series.daily, selectedId) }
  }, [rev, series, selectedId, name, tier, config])
  const last = population.length ? population[population.length - 1]! : null
  const meta = last ? <span className="muted small tnum">{`day ${last.day}` + (last.notes !== null ? ` · ${fmtCompact(last.notes)} notes` : '')}</span> : undefined
  return (
    <Card id="vault" title="Vault" meta={meta}>
      <VaultBody population={population} agent={agent} />
    </Card>
  )
})
