import { Handle, Position, type Node, type NodeProps } from '@xyflow/react'
import { ArrowUpRight, Check, Circle, LoaderCircle, MessageSquare, MoreHorizontal, OctagonX } from 'lucide-react'
import type { Tasklet } from '../lib/types'
import { statusLabels } from '../lib/types'

export type TaskletFlowNode = Node<{ tasklet: Tasklet }, 'tasklet'>

export function TaskletNode({ data, selected }: NodeProps<TaskletFlowNode>) {
  const { tasklet } = data
  const StatusIcon = tasklet.status === 'running' ? LoaderCircle : tasklet.status === 'completed' ? Check : tasklet.status === 'failed' ? OctagonX : Circle
  return (
    <article className={`tasklet-node ${selected ? 'is-selected' : ''} status-${tasklet.status}`} aria-label={`Тасклет: ${tasklet.title}`}>
      <Handle type="target" position={Position.Left} aria-label={`Вход: ${tasklet.title}`} />
      <div className="node-topline"><span className="node-symbol"><MessageSquare size={15} strokeWidth={1.8} /></span><span className="node-kind">ТАСКЛЕТ</span><MoreHorizontal size={17} className="node-more" /></div>
      <h3>{tasklet.title}</h3>
      <p className={`node-prompt ${!tasklet.prompt ? 'is-placeholder' : ''}`}>{tasklet.prompt || 'Добавьте инструкцию для этой задачи'}</p>
      <div className="node-footer"><span className={`status-label status-${tasklet.status}`}><StatusIcon size={12} className={tasklet.status === 'running' ? 'spin' : ''} />{statusLabels[tasklet.status]}</span><ArrowUpRight size={14} /></div>
      <Handle type="source" position={Position.Right} aria-label={`Выход: ${tasklet.title}`} />
    </article>
  )
}
