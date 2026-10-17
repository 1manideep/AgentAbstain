import { memo } from 'react'
import { Card } from '../Card'
import { TaskBoard } from '../TaskBoard'

export const TasksCard = memo(function TasksCard() {
  return (
    <Card id="tasks" title="Task board">
      <TaskBoard />
    </Card>
  )
})
