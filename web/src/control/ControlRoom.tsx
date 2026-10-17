import { memo, useMemo, type ComponentType } from 'react'
import type { HelloConfig } from '../protocol'
import { useStore } from '../state/store'
import { AdoptionCard } from './cards/AdoptionCard'
import { AgentCard } from './cards/AgentCard'
import { ChronicleCard } from './cards/ChronicleCard'
import { ClaimsCard } from './cards/ClaimsCard'
import { ControlsCard } from './cards/ControlsCard'
import { DailyCard } from './cards/DailyCard'
import { DegenCard } from './cards/DegenCard'
import { DegradationCard } from './cards/DegradationCard'
import { EpochsCard } from './cards/EpochsCard'
import { FeedCard } from './cards/FeedCard'
import { GadgetsCard } from './cards/GadgetsCard'
import { GossipCard } from './cards/GossipCard'
import { LineageCard } from './cards/LineageCard'
import { MemoryCard } from './cards/MemoryCard'
import { PopulationCard } from './cards/PopulationCard'
import { SpendCard } from './cards/SpendCard'
import { TasksCard } from './cards/TasksCard'
import { TeffCard } from './cards/TeffCard'
import { VaultCard } from './cards/VaultCard'
import { WealthCard } from './cards/WealthCard'

export interface CardEntry {
  /** Matches the `id` the component passes to <Card>, which keys its open state in localStorage. */
  id: string
  Component: ComponentType
  /** Shown only when this returns true for the run's hello config (null before hello). Absent: always shown. */
  when?: (cfg: HelloConfig | null) => boolean
}

const memoryOn = (cfg: HelloConfig | null): boolean => cfg?.memory_layer?.enabled === true
const policyOn = (cfg: HelloConfig | null): boolean => cfg?.memory_layer?.policy === true

/** Every control-room card, in display order. Each card lives in its own file under ./cards. */
// oxlint-disable-next-line react/only-export-components
export const CARDS: readonly CardEntry[] = [
  { id: 'spend', Component: SpendCard },
  { id: 'agent', Component: AgentCard },
  { id: 'feed', Component: FeedCard },
  { id: 'wealth', Component: WealthCard },
  { id: 'teff', Component: TeffCard },
  { id: 'degradation', Component: DegradationCard },
  { id: 'degen', Component: DegenCard },
  { id: 'population', Component: PopulationCard },
  { id: 'claims', Component: ClaimsCard },
  { id: 'gossip', Component: GossipCard },
  { id: 'epochs', Component: EpochsCard },
  { id: 'chronicle', Component: ChronicleCard },
  { id: 'tasks', Component: TasksCard },
  { id: 'controls', Component: ControlsCard },
  { id: 'lineage', Component: LineageCard },
  { id: 'daily', Component: DailyCard },
  { id: 'memory', Component: MemoryCard, when: memoryOn },
  { id: 'vault', Component: VaultCard, when: memoryOn },
  { id: 'gadgets', Component: GadgetsCard },
  { id: 'adoption', Component: AdoptionCard, when: policyOn },
]

/** The cards to show for a run, in display order. */
// oxlint-disable-next-line react/only-export-components
export function visibleCards(cfg: HelloConfig | null, cards: readonly CardEntry[] = CARDS): CardEntry[] {
  return cards.filter((c) => c.when === undefined || c.when(cfg))
}

export const ControlRoom = memo(function ControlRoom() {
  const config = useStore((s) => s.config)
  const cards = useMemo(() => visibleCards(config), [config])
  return (
    <div className="control-room-inner">
      {cards.map(({ id, Component }) => (
        <Component key={id} />
      ))}
    </div>
  )
})
