import { memo } from 'react'
import { Card } from '../Card'
import { ControlForms } from '../ControlForms'

export const ControlsCard = memo(function ControlsCard() {
  return (
    <Card id="controls" title="Controls" defaultOpen={false}>
      <ControlForms />
    </Card>
  )
})
