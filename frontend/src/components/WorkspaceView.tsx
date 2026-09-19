import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Background, BackgroundVariant, MarkerType, ReactFlow, applyNodeChanges, type NodeChange, type ReactFlowInstance } from '@xyflow/react'
import { ArrowUpRight, Check, CirclePause, GitBranch, Link2, LoaderCircle, Maximize, Minus, Plus, Square, Trash2, X } from 'lucide-react'
import { api } from '../lib/api'
import type { Message, Settings, Workspace } from '../lib/types'
import { patchWorkspaceUi, useWorkspaceUi, workspaceUi } from '../lib/uiState'
import { TaskletNode, type TaskletFlowNode } from './TaskletNode'
import { CreateTasklet } from './CreateTasklet'
import { Inspector } from './Inspector'
import { SettingsPage } from './SettingsPage'

const nodeTypes = { tasklet: TaskletNode }
function mergeMessages(current: Message[], incoming: Message[]) {
  const merged = new Map(current.map(message => [message.id, message]))
  for (const message of incoming) {
    const previous = merged.get(message.id)
    if (!previous || message.content.length >= previous.content.length) merged.set(message.id, message)
  }
  return [...merged.values()].sort((first, second) => first.created_at.localeCompare(second.created_at))
}

export function WorkspaceView({ workspace, settings, anyRunning, messageEvents, reconnect, refresh, onUpdate, onSettingsUpdate, notify }: { workspace: Workspace; settings: Settings; anyRunning: boolean; messageEvents: { sequence: number; value: Message }[]; reconnect: number; refresh: (wid: string) => Promise<void>; onUpdate: (workspace: Workspace) => void | Promise<void>; onSettingsUpdate: (settings: Settings) => void | Promise<void>; notify: (text: string, error?: boolean) => void }) {
  const wid = workspace.id
  const ui = useWorkspaceUi(wid)
  const { page, selectedId, selectedEdgeId, creating } = ui
  const [nodes, setNodes] = useState<TaskletFlowNode[]>([])
  const [messages, setMessages] = useState<Record<string, Message[]>>({})
  const [messagesLoading, setMessagesLoading] = useState(true)
  const [actionPending, setActionPending] = useState(false)
  const actionInFlight = useRef(false)
  const restartAction = useRef<(id: string) => void>(() => {})
  const messageBuffer = useRef(new Map<string, { sequence: number; value: Message }>())
  const messageSequence = useRef(0)
  const conversations = useMemo(() => Object.fromEntries(workspace.tasklets.map(tasklet => [tasklet.id, tasklet.conversation_id])), [workspace.tasklets])
  const currentConversations = useRef(conversations)
  currentConversations.current = conversations
  const flow = useRef<ReactFlowInstance<TaskletFlowNode> | null>(null)
  const locked = workspace.pipeline.status === 'running' || workspace.pipeline.status === 'stopping'
  const selected = workspace.tasklets.find(item => item.id === selectedId)
  const selectedEdge = workspace.edges.find(item => item.id === selectedEdgeId)
  const inspectorDraft = selectedId ? ui.tasklets[selectedId]?.draft : undefined
  const inspectorDirty = Boolean(selected && inspectorDraft && Object.entries(inspectorDraft).some(([name, value]) => (name === 'model' ? value || null : value) !== selected[name as keyof typeof selected]))
  const settingsDirty = Object.entries(ui.settings.globalDraft).some(([name, value]) => value !== settings[name as keyof Settings]) || Object.entries(ui.settings.workspaceDraft).some(([name, value]) => value !== workspace[name as keyof Workspace])
  const restartHint = actionPending ? 'Запрос выполняется' : anyRunning ? (workspace.pipeline.status === 'stopping' ? 'Дождитесь завершения остановки' : 'Дождитесь завершения пайплайна') : inspectorDirty ? 'Сохраните изменения тасклета перед запуском' : settingsDirty ? 'Сохраните изменения настроек перед запуском' : ''
  const setSelectedId = (id: string | null) => patchWorkspaceUi(wid, { selectedId: id })
  const setSelectedEdgeId = (id: string | null) => patchWorkspaceUi(wid, { selectedEdgeId: id })
  useEffect(() => {
    setNodes(previous => workspace.tasklets.map(tasklet => {
      const existing = previous.find(node => node.id === tasklet.id)
      return { ...existing, id: tasklet.id, type: 'tasklet', position: existing?.dragging ? existing.position : tasklet.position, data: { tasklet, restartBlocked: Boolean(restartHint), restartHint, onRestart: () => restartAction.current(tasklet.id) }, selected: tasklet.id === selectedId }
    }))
  }, [workspace.tasklets, selectedId, restartHint])
  const edges = useMemo(() => workspace.edges.map(edge => ({ ...edge, type: 'smoothstep', selected: edge.id === selectedEdgeId, animated: workspace.tasklets.some(item => item.id === edge.target && item.status === 'running'), markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 }, className: edge.pass_context ? 'context-edge' : '', ariaLabel: `Связь: ${workspace.tasklets.find(item => item.id === edge.source)?.title} → ${workspace.tasklets.find(item => item.id === edge.target)?.title}` })), [workspace.edges, workspace.tasklets, selectedEdgeId])

  useEffect(() => {
    const events = messageEvents.filter(event => event.value.workspace_id === wid && event.value.conversation_id === conversations[event.value.tasklet_id])
    if (events.length) messageSequence.current = Math.max(messageSequence.current, events[events.length - 1].sequence)
    const values = events.map(event => event.value)
    for (const [id, event] of messageBuffer.current) {
      if (event.value.conversation_id !== conversations[event.value.tasklet_id]) messageBuffer.current.delete(id)
    }
    for (const event of events) messageBuffer.current.set(event.value.id, event)
    setMessages(previous => {
      const next = Object.fromEntries(Object.entries(previous).filter(([id]) => id in conversations).map(([id, items]) => [id, items.filter(item => item.conversation_id === conversations[id])]))
      for (const value of values) next[value.tasklet_id] = mergeMessages(next[value.tasklet_id] || [], [value])
      return next
    })
  }, [messageEvents, wid, conversations])

  const selectedConversation = selectedId ? conversations[selectedId] : undefined

  useEffect(() => {
    if (!selectedId) return
    let active = true
    const start = messageSequence.current
    const conversation = selectedConversation
    setMessagesLoading(true)
    api.messages(wid, selectedId).then(items => {
      if (!active || currentConversations.current[selectedId] !== conversation) return
      const updates = [...messageBuffer.current.values()].filter(event => event.sequence > start && event.value.tasklet_id === selectedId && event.value.conversation_id === conversation).map(event => event.value)
      setMessages(previous => ({ ...previous, [selectedId]: mergeMessages(items.filter(item => item.conversation_id === conversation), updates) }))
    }).catch(error => { if (active) notify(error.message, true) }).finally(() => { if (active) setMessagesLoading(false) })
    return () => { active = false }
  }, [wid, selectedId, selectedConversation, reconnect, workspace.pipeline.id, notify])

  const mutate = useCallback(async (action: () => Promise<unknown>) => {
    try { await action(); await refresh(wid) } catch (error) { notify((error as Error).message, true); throw error }
  }, [wid, refresh, notify])

  function requireConfiguration(ids?: string[]) {
    const tasks = ids ? workspace.tasklets.filter(tasklet => ids.includes(tasklet.id)) : workspace.tasklets
    if (settings.execution_mode === 'codex') {
      if (tasks.some(tasklet => !(tasklet.working_directory || workspace.working_directory))) {
        patchWorkspaceUi(wid, { page: 'settings' })
        notify('Выберите рабочую папку проекта или тасклета перед запуском Codex.', true)
        throw new Error('Сначала выберите рабочую папку')
      }
      return
    }
    if (!settings.api_key_configured || tasks.some(tasklet => !(tasklet.model || settings.model).trim())) {
      patchWorkspaceUi(wid, { page: 'settings' })
      notify('Добавьте API-ключ и модель в настройках перед запуском.', true)
      throw new Error('Сначала настройте подключение')
    }
  }

  async function run(ids?: string[], restart = false) {
    if (actionInFlight.current || anyRunning) return
    if (inspectorDirty || settingsDirty) {
      notify(restartHint, true)
      return
    }
    requireConfiguration(ids)
    actionInFlight.current = true
    setActionPending(true)
    try { await mutate(() => restart ? api.restart(wid, ids![0]) : api.start(wid, ids)) } finally { actionInFlight.current = false; setActionPending(false) }
  }

  restartAction.current = id => { void run([id], true).catch(() => {}) }

  async function stop() {
    setActionPending(true)
    try { await mutate(() => api.stop(wid)); notify('Пайплайн остановлен') } catch { return } finally { setActionPending(false) }
  }

  async function createTasklet(title: string, prompt: string) {
    try {
      const count = workspace.tasklets.length
      const tasklet = await api.createTasklet(wid, { title, prompt, position: { x: 100 + (count % 3) * 340, y: 100 + Math.floor(count / 3) * 270 } })
      await refresh(wid)
      patchWorkspaceUi(wid, { selectedId: tasklet.id, selectedEdgeId: null })
      if (!workspaceUi(wid).viewport) setTimeout(() => { void flow.current?.fitView({ padding: 0.3, duration: 350, maxZoom: 1 }) }, 100)
    } catch (error) { notify((error as Error).message, true); throw error }
  }

  async function deleteTasklet(id: string) {
    const tasklet = workspace.tasklets.find(item => item.id === id)
    if (!window.confirm(`Удалить тасклет «${tasklet?.title}» вместе с историей чата и связями?`)) return
    try { await mutate(() => api.deleteTasklet(wid, id)); setSelectedId(null); notify('Тасклет удалён') } catch { return }
  }

  function onNodesChange(changes: NodeChange<TaskletFlowNode>[]) { setNodes(previous => applyNodeChanges(changes, previous)) }
  function selectNode(id: string | null) { patchWorkspaceUi(wid, { selectedId: id, selectedEdgeId: null }); return true }
  function openCreate() { patchWorkspaceUi(wid, { creating: true }) }
  const pipelineLabel = workspace.pipeline.status === 'running' ? 'Выполняется' : workspace.pipeline.status === 'stopping' ? 'Останавливаем' : workspace.pipeline.status === 'completed' ? 'Пайплайн завершён' : workspace.pipeline.status === 'failed' ? 'Ошибка пайплайна' : workspace.pipeline.status === 'cancelled' ? 'Пайплайн остановлен' : 'Всё готово к работе'

  return <div className="workspace-view">
        {page === 'workspace' ? <><header className="workspace-header"><div className="header-title"><div className="page-title-row"><h1>Пространство</h1><span className="task-count">{workspace.tasklets.length}</span></div></div><div className="header-actions"><button className="button button-secondary" onClick={openCreate} disabled={locked}><Plus size={16} />Новый тасклет</button>{locked ? <button className="button button-stop" onClick={() => void stop()} disabled={actionPending || workspace.pipeline.status === 'stopping'}>{workspace.pipeline.status === 'stopping' ? <LoaderCircle size={14} className="spin" /> : <Square size={12} fill="currentColor" />}{workspace.pipeline.status === 'stopping' ? 'Останавливаем…' : 'Остановить всё'}</button> : <button className="button button-primary" onClick={() => { void run().catch(() => {}) }} disabled={!workspace.tasklets.length || actionPending || inspectorDirty || settingsDirty || anyRunning}>{actionPending ? <LoaderCircle className="spin" size={14} /> : <ArrowUpRight size={17} />}Запустить пайплайн</button>}</div></header>
        <div className="workspace-toolbar"><span><GitBranch size={14} />Граф задач<span className="toolbar-divider" />{workspace.edges.length} связей</span><span className={`pipeline-indicator pipeline-${workspace.pipeline.status}`}>{locked ? <LoaderCircle size={13} className="spin" /> : workspace.pipeline.status === 'completed' ? <Check size={13} /> : workspace.pipeline.status === 'cancelled' ? <CirclePause size={13} /> : <span className="status-dot" />}{pipelineLabel}{workspace.pipeline.total > 0 && <span className="pipeline-count">{workspace.pipeline.completed}/{workspace.pipeline.total}</span>}</span></div>
        {workspace.pipeline.error && <div className="pipeline-error" role="alert">{workspace.pipeline.error}</div>}
        <div className="workspace-content"><div className="graph-area" tabIndex={0} aria-label="Граф тасклетов" onKeyDown={event => { const target = event.target as HTMLElement; if (['BUTTON', 'A', 'INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName) || target.isContentEditable) return; const focusedNode = target.closest<HTMLElement>('.react-flow__node'); if ((event.key === 'Enter' || event.key === ' ') && focusedNode?.dataset.id) { event.preventDefault(); selectNode(focusedNode.dataset.id); return } if ((event.key === 'Delete' || event.key === 'Backspace') && !locked && selectedEdgeId) { event.preventDefault(); void mutate(() => api.deleteEdge(wid, selectedEdgeId)).then(() => setSelectedEdgeId(null)).catch(() => {}) } }}>
          <ReactFlow<TaskletFlowNode> nodes={nodes} edges={edges} nodeTypes={nodeTypes} onInit={instance => { flow.current = instance }} onNodesChange={onNodesChange} onNodeClick={(_event, node) => { selectNode(node.id) }} onPaneClick={() => { if (selectNode(null)) setSelectedEdgeId(null) }} onEdgeClick={(_event, edge) => { if (selectNode(null)) setSelectedEdgeId(edge.id) }} onConnect={connection => { if (connection.source && connection.target) void mutate(() => api.createEdge(wid, connection.source!, connection.target!)).catch(() => {}) }} onNodeDragStop={(_event, node) => { void mutate(() => api.updateTasklet(wid, node.id, { position: node.position })).catch(() => {}) }} nodesDraggable={!locked} nodesConnectable={!locked} edgesReconnectable={false} deleteKeyCode={null} minZoom={0.25} maxZoom={1.75} defaultViewport={ui.viewport || undefined} onMoveEnd={(_event, viewport) => patchWorkspaceUi(wid, { viewport })} fitView={!ui.viewport} fitViewOptions={{ padding: 0.25, maxZoom: 1 }} defaultEdgeOptions={{ style: { strokeWidth: 1.5, stroke: '#a5a1b1' }, interactionWidth: 24 }} proOptions={{ hideAttribution: true }}>
            <Background variant={BackgroundVariant.Dots} gap={22} size={1.1} color="#dcdcd9" />
          </ReactFlow>
          {!workspace.tasklets.length && <div className="canvas-state empty-state"><h2>Тасклетов пока нет</h2><button className="button button-primary" onClick={openCreate}><Plus size={16} />Создать тасклет</button></div>}
          <div className="canvas-bottom"><div className="canvas-controls"><button className="icon-button" aria-label="Уменьшить масштаб" onClick={() => { void flow.current?.zoomOut() }}><Minus size={16} /></button><button className="icon-button" aria-label="Увеличить масштаб" onClick={() => { void flow.current?.zoomIn() }}><Plus size={16} /></button><span /><button className="icon-button" aria-label="Уместить граф" onClick={() => { void flow.current?.fitView({ padding: 0.25, duration: 300, maxZoom: 1 }) }}><Maximize size={15} /></button></div></div>
          {selectedEdge && <div className="edge-popover"><div><Link2 size={15} /><strong>Связь задач</strong><button className="icon-button" aria-label="Закрыть настройки связи" onClick={() => setSelectedEdgeId(null)}><X size={14} /></button></div><label className="checkbox-field"><input type="checkbox" checked={selectedEdge.pass_context} onChange={event => { void mutate(() => api.updateEdge(wid, selectedEdge.id, event.target.checked)).catch(() => {}) }} disabled={locked} />Передавать результат в контекст</label><button className="text-button danger-text" onClick={() => { void mutate(() => api.deleteEdge(wid, selectedEdge.id)).then(() => setSelectedEdgeId(null)).catch(() => {}) }} disabled={locked}><Trash2 size={13} />Удалить связь</button></div>}
        </div>
        {selected && <Inspector workspaceId={wid} workingDirectory={workspace.working_directory} runBlocked={Boolean(restartHint)} runBlockedReason={restartHint} settings={settings} running={locked} stopping={actionPending || workspace.pipeline.status === 'stopping'} onStop={stop} key={selected.id} tasklet={selected} tasklets={workspace.tasklets} edges={workspace.edges} messages={(messages[selected.id] || []).filter(message => message.conversation_id === selectedConversation)} messagesLoading={messagesLoading} locked={locked || actionPending} onClose={() => setSelectedId(null)} onSave={async value => { await mutate(() => api.updateTasklet(wid, selected.id, value)); notify('Тасклет сохранён') }} onDelete={() => { void deleteTasklet(selected.id) }} onRun={() => run([selected.id], ['completed', 'failed', 'cancelled'].includes(selected.status))} onSend={async content => { if (anyRunning) return; requireConfiguration([selected.id]); await mutate(() => api.sendMessage(wid, selected.id, content)) }} onConnect={(source, target) => mutate(() => api.createEdge(wid, source, target))} onDeleteEdge={id => mutate(() => api.deleteEdge(wid, id))} onUpdateEdge={(id, pass) => mutate(() => api.updateEdge(wid, id, pass))} />}
        </div></> : <SettingsPage settings={settings} workspace={workspace} globalLocked={anyRunning} workspaceLocked={locked} onUpdate={onSettingsUpdate} onWorkspaceUpdate={onUpdate} notify={notify} />}
      {creating && <CreateTasklet workspaceId={wid} onClose={() => patchWorkspaceUi(wid, { creating: false })} onCreate={createTasklet} />}
    </div>
}
