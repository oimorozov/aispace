import { Handle, Position, type Node, type NodeProps } from '@xyflow/react'
import { ArrowUpRight, Check, Circle, LoaderCircle, MessageSquare, MoreHorizontal, OctagonX, RotateCcw } from 'lucide-react'
import type { Tasklet } from '../lib/types'
import { statusLabels } from '../lib/types'

export type TaskletFlowNode = Node<{ tasklet: Tasklet; restartBlocked: boolean; restartHint: string; onRestart: () => void }, 'tasklet'>

export function TaskletNode({ data, selected }: NodeProps<TaskletFlowNode>) {
  const { tasklet } = data
  const restartable = ['completed', 'failed', 'cancelled'].includes(tasklet.status)
  const StatusIcon = tasklet.status === 'running' ? LoaderCircle : tasklet.status === 'completed' ? Check : tasklet.status === 'failed' ? OctagonX : Circle
  return (
    <article className={`tasklet-node ${selected ? 'is-selected' : ''} status-${tasklet.status}`} aria-label={`Тасклет: ${tasklet.title}`}>
      <Handle type="target" position={Position.Left} aria-label={`Вход: ${tasklet.title}`} />
      <div className="node-topline"><span className="node-symbol"><MessageSquare size={15} strokeWidth={1.8} /></span><span className="node-kind">ТАСКЛЕТ</span><MoreHorizontal size={17} className="node-more" /></div>
      <h3>{tasklet.title}</h3>
      <p className={`node-prompt ${!tasklet.prompt ? 'is-placeholder' : ''}`}>{tasklet.prompt || 'Добавьте инструкцию для этой задачи'}</p>
      <div className="node-footer"><span className={`status-label status-${tasklet.status}`}><StatusIcon size={12} className={tasklet.status === 'running' ? 'spin' : ''} />{statusLabels[tasklet.status]}</span><ArrowUpRight size={14} /></div>
      {restartable && <div className="node-restart"><button type="button" className="button button-secondary nodrag nopan" aria-label={`Перезапустить тасклет: ${tasklet.title}`} title={data.restartHint || 'Запустится заново в новом чате'} disabled={data.restartBlocked} onPointerDown={event => event.stopPropagation()} onKeyDown={event => event.stopPropagation()} onClick={event => { event.stopPropagation(); data.onRestart() }}><RotateCcw size={12} />Перезапустить</button><span>{data.restartHint || 'Запустится заново в новом чате'}</span></div>}
      <Handle type="source" position={Position.Right} aria-label={`Выход: ${tasklet.title}`} />
    </article>
  )
}
