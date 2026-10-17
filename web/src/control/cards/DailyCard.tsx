import { memo, useCallback, useMemo } from 'react'
import type { DayRow } from '../../protocol'
import { selectTierColor, useStore } from '../../state/store'
import { Card } from '../Card'
import { DailyCharts } from '../charts/DailyCharts'
import { EmptyState } from './EmptyState'

/** Stable ids for day-row objects, so a replaced row (a day re-sent) changes the signature below. */
const rowIds = new WeakMap<DayRow, number>()
let nextRowId = 0
function rowId(row: DayRow): number {
  let id = rowIds.get(row)
  if (id === undefined) {
    id = ++nextRowId
    rowIds.set(row, id)
  }
  return id
}

/** Changes only when a day row is added or replaced: `series.daily` is mutated in place. */
function dailySignature(daily: readonly DayRow[]): string {
  return daily.map(rowId).join(',')
}

/**
 * Per-day aggregates from the `day` rows (DESIGN §16: daily charts on the day message). The card re-reads the series
 * at the store's commit rate (<= 4 Hz) but rebuilds the charts only when a day row arrives.
 */
export const DailyCard = memo(function DailyCard() {
  const rev = useStore((s) => s.seriesRev)
  const series = useStore((s) => s.series)
  const config = useStore((s) => s.config)
  const signature = useMemo(() => {
    void rev
    return dailySignature(series.daily)
  }, [rev, series])
  const rows = useMemo(() => {
    void signature
    return [...series.daily].sort((a, b) => a.day - b.day)
  }, [signature, series])
  const tierColor = useCallback((tier: string) => selectTierColor(config, tier), [config])
  const tierOrder = useMemo(() => {
    const rank = (name: string) => {
      const r = config?.tiers[name]?.rank
      return typeof r === 'number' ? r : Number.POSITIVE_INFINITY
    }
    return Object.keys(config?.tiers ?? {}).sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))
  }, [config])
  const last = rows[rows.length - 1]
  return (
    <Card id="daily" title="Daily" meta={last ? <span className="muted small tnum">through day {last.day}</span> : undefined}>
      {rows.length ? <DailyCharts rows={rows} tierColor={tierColor} tierOrder={tierOrder} /> : <EmptyState />}
    </Card>
  )
})
