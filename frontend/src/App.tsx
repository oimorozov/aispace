import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Background, BackgroundVariant, MarkerType, ReactFlow, applyNodeChanges, type NodeChange, type ReactFlowInstance } from '@xyflow/react'
import { ArrowDown, ArrowUpRight, Check, CircleHelp, CirclePause, GitBranch, Layers2, Link2, LoaderCircle, Maximize, Minus, Plus, Settings2, Square, Trash2, Workflow, X } from 'lucide-react'
import { api, subscribe } from './lib/api'
import type { Message, Settings, Workspace } from './lib/types'
import { TaskletNode, type TaskletFlowNode } from './components/TaskletNode'
import { CreateTasklet } from './components/CreateTasklet'
import { Inspector } from './components/Inspector'
import { SettingsPage } from './components/SettingsPage'

const nodeTypes = { tasklet: TaskletNode }
function mergeMessages(current: Message[], incoming: Message[]) {
  const merged = new Map(current.map(message => [message.id, message]))
  for (const message of incoming) {
    const previous = merged.get(message.id)
    if (!previous || message.content.length >= previous.content.length) merged.set(message.id, message)
  }
  return [...merged.values()].sort((first, second) => first.created_at.localeCompare(second.created_at))
}
const emptyWorkspace: Workspace = { tasklets: [], edges: [], pipeline: { id: null, status: 'idle', started_at: null, finished_at: null, total: 0, completed: 0, error: null } }

export default function App() {
  const [workspace, setWorkspace] = useState<Workspace>(emptyWorkspace)
  const [nodes, setNodes] = useState<TaskletFlowNode[]>([])
  const [settings, setSettings] = useState<Settings | null>(null)
  const [page, setPage] = useState<'workspace' | 'settings'>('workspace')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [connected, setConnected] = useState(false)
  const [messages, setMessages] = useState<Record<string, Message[]>>({})
  const [messagesLoading, setMessagesLoading] = useState(false)
  const [actionPending, setActionPending] = useState(false)
  const [inspectorDirty, setInspectorDirty] = useState(false)
  const [settingsDirty, setSettingsDirty] = useState(false)
  const selectedRef = useRef<string | null>(null)
  selectedRef.current = selectedId
  const [toast, setToast] = useState<{ text: string; error: boolean } | null>(null)
  const flow = useRef<ReactFlowInstance<TaskletFlowNode> | null>(null)
  const toastTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const locked = workspace.pipeline.status === 'running' || workspace.pipeline.status === 'stopping'
  const selected = workspace.tasklets.find(item => item.id === selectedId)
  const selectedEdge = workspace.edges.find(item => item.id === selectedEdgeId)
  useEffect(() => {
    setNodes(previous => workspace.tasklets.map(tasklet => {
      const existing = previous.find(node => node.id === tasklet.id)
      return { ...existing, id: tasklet.id, type: 'tasklet', position: existing?.dragging ? existing.position : tasklet.position, data: { tasklet }, selected: tasklet.id === selectedId }
    }))
  }, [workspace.tasklets, selectedId])
  const edges = useMemo(() => workspace.edges.map(edge => ({ ...edge, type: 'smoothstep', selected: edge.id === selectedEdgeId, animated: workspace.tasklets.some(item => item.id === edge.target && item.status === 'running'), markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 }, className: edge.pass_context ? 'context-edge' : '', ariaLabel: `Связь: ${workspace.tasklets.find(item => item.id === edge.source)?.title} → ${workspace.tasklets.find(item => item.id === edge.target)?.title}` })), [workspace.edges, workspace.tasklets, selectedEdgeId])

  const notify = useCallback((text: string, error = false) => {
    setToast({ text, error })
    clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToast(null), error ? 9000 : 4200)
  }, [])

  const refresh = useCallback(async () => { setWorkspace(await api.workspace()) }, [])

  const initialize = useCallback(async () => {
    setLoading(true)
    try {
      const [nextWorkspace, nextSettings] = await Promise.all([api.workspace(), api.settings()])
      setWorkspace(nextWorkspace)
      setSettings(nextSettings)
      setLoadError('')
    } catch (error) { setLoadError((error as Error).message) } finally { setLoading(false) }
  }, [])

  useEffect(() => {
    void initialize()
    const unsubscribe = subscribe({
      workspace: setWorkspace,
      settings: setSettings,
      connection: value => {
        setConnected(value)
        if (value) {
          void api.settings().then(setSettings).catch(() => {})
          const id = selectedRef.current
          if (id) void api.messages(id).then(items => setMessages(previous => ({ ...previous, [id]: mergeMessages(previous[id] || [], items) }))).catch(() => {})
        }
      },
      message: message => setMessages(previous => {
        const items = previous[message.tasklet_id] || []
        return { ...previous, [message.tasklet_id]: mergeMessages(items, [message]) }
      }),
    })
    return () => { unsubscribe(); clearTimeout(toastTimer.current) }
  }, [initialize])

  useEffect(() => {
    if (!selectedId) return
    let active = true
    setMessagesLoading(true)
    api.messages(selectedId).then(items => { if (active) setMessages(previous => ({ ...previous, [selectedId]: mergeMessages(previous[selectedId] || [], items) })) }).catch(error => { if (active) notify(error.message, true) }).finally(() => { if (active) setMessagesLoading(false) })
    return () => { active = false }
  }, [selectedId, notify])

  async function mutate(action: () => Promise<unknown>) {
    try { await action(); await refresh() } catch (error) { notify((error as Error).message, true); throw error }
  }

  function requireConfiguration(ids?: string[]) {
    const tasks = ids ? workspace.tasklets.filter(tasklet => ids.includes(tasklet.id)) : workspace.tasklets
    if (settings?.execution_mode === 'codex') {
      if (tasks.some(tasklet => !(tasklet.working_directory || settings.working_directory))) {
        setPage('settings')
        notify('Выберите рабочую папку проекта или тасклета перед запуском Codex.', true)
        throw new Error('Сначала выберите рабочую папку')
      }
      return
    }
    if (!settings?.api_key_configured || tasks.some(tasklet => !(tasklet.model || settings.model).trim())) {
      setPage('settings')
      notify('Добавьте API-ключ и модель в настройках перед запуском.', true)
      throw new Error('Сначала настройте подключение')
    }
  }

  async function run(ids?: string[]) {
    requireConfiguration(ids)
    setActionPending(true)
    try { await mutate(() => api.start(ids)) } finally { setActionPending(false) }
  }

  async function stop() {
    setActionPending(true)
    try { await mutate(() => api.stop()); notify('Пайплайн остановлен') } catch { return } finally { setActionPending(false) }
  }

  async function createTasklet(title: string, prompt: string) {
    try {
      const count = workspace.tasklets.length
      const tasklet = await api.createTasklet({ title, prompt, position: { x: 100 + (count % 3) * 340, y: 100 + Math.floor(count / 3) * 270 } })
      await refresh()
      setSelectedId(tasklet.id)
      setSelectedEdgeId(null)
      setTimeout(() => { void flow.current?.fitView({ padding: 0.3, duration: 350, maxZoom: 1 }) }, 100)
    } catch (error) { notify((error as Error).message, true); throw error }
  }

  async function deleteTasklet(id: string) {
    const tasklet = workspace.tasklets.find(item => item.id === id)
    if (!window.confirm(`Удалить тасклет «${tasklet?.title}» вместе с историей чата и связями?`)) return
    try { await mutate(() => api.deleteTasklet(id)); setSelectedId(null); notify('Тасклет удалён') } catch { return }
  }

  function onNodesChange(changes: NodeChange<TaskletFlowNode>[]) {
    setNodes(previous => applyNodeChanges(changes, previous))
  }

  function navigate(next: 'workspace' | 'settings') {
    if (page === next) return
    if ((inspectorDirty || settingsDirty) && !window.confirm('Перейти без сохранения изменений?')) return
    setPage(next)
  }

  function selectNode(id: string | null) {
    if (id === selectedId) return true
    if (inspectorDirty && !window.confirm('Переключиться без сохранения изменений тасклета?')) return false
    setSelectedId(id)
    setSelectedEdgeId(null)
    return true
  }

  function openCreate() {
    if (inspectorDirty && !window.confirm('Создать новый тасклет без сохранения текущих изменений?')) return
    setCreating(true)
  }

  const pipelineLabel = workspace.pipeline.status === 'running' ? 'Выполняется' : workspace.pipeline.status === 'stopping' ? 'Останавливаем' : workspace.pipeline.status === 'completed' ? 'Пайплайн завершён' : workspace.pipeline.status === 'failed' ? 'Ошибка пайплайна' : workspace.pipeline.status === 'cancelled' ? 'Пайплайн остановлен' : 'Всё готово к работе'

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="#" onClick={event => { event.preventDefault(); navigate('workspace') }} aria-label="aispace — главная"><span className="brand-symbol"><Layers2 size={21} strokeWidth={1.65} /></span><span>aispace<span className="brand-dot">.</span></span></a>
        <div className="sidebar-section-label">ЛИЧНОЕ ПРОСТРАНСТВО</div>
        <nav aria-label="Основная навигация"><button aria-label="Пространство" className={`nav-item ${page === 'workspace' ? 'active' : ''}`} onClick={() => navigate('workspace')}><Workflow size={18} /><span>Пространство</span><span className="nav-count">{workspace.tasklets.length}</span></button><button aria-label="Настройки" className={`nav-item ${page === 'settings' ? 'active' : ''}`} onClick={() => navigate('settings')}><Settings2 size={18} /><span>Настройки</span></button></nav>
        <div className="sidebar-note"><div className="note-orbit"><span /><span /><span /></div><p>Большие идеи.<br />Небольшие шаги.</p><span>Дайте каждой задаче<br />своё пространство.</span></div>
        <div className="sidebar-bottom"><div className="connection-status"><span className={`connection-dot ${connected ? 'connected' : ''}`} /><div><strong>{connected ? 'Локальный сервер' : loading ? 'Подключаемся…' : 'Нет соединения'}</strong><span>{connected ? (settings?.execution_mode === 'codex' ? 'ChatGPT через Codex' : settings?.api_key_configured ? 'API-ключ сохранён' : 'Добавьте API-ключ') : 'Автоматическое переподключение'}</span></div></div><div className="sidebar-version"><span>aispace</span><span>v0.1</span></div></div>
      </aside>
      <div className="main-area">
        {page === 'workspace' ? <><header className="workspace-header"><div className="header-title"><div className="breadcrumb"><span className="workspace-initial">A</span>Моё пространство<span>/</span><span>Обзор</span></div><div className="page-title-row"><h1>Пространство</h1><span className="task-count">{workspace.tasklets.length}</span></div><p>Связывайте идеи. Запускайте задачи.</p></div><div className="header-actions"><button className="button button-secondary" onClick={openCreate} disabled={locked || loading || Boolean(loadError)}><Plus size={16} />Новый тасклет</button>{locked ? <button className="button button-stop" onClick={() => void stop()} disabled={actionPending || workspace.pipeline.status === 'stopping'}>{workspace.pipeline.status === 'stopping' ? <LoaderCircle size={14} className="spin" /> : <Square size={12} fill="currentColor" />}{workspace.pipeline.status === 'stopping' ? 'Останавливаем…' : 'Остановить всё'}</button> : <button className="button button-primary" onClick={() => { void run().catch(() => {}) }} disabled={!workspace.tasklets.length || loading || actionPending || inspectorDirty || Boolean(loadError)}>{actionPending ? <LoaderCircle className="spin" size={14} /> : <ArrowUpRight size={17} />}Запустить пайплайн</button>}</div></header>
        <div className="workspace-toolbar"><span><GitBranch size={14} />Граф задач<span className="toolbar-divider" />{workspace.edges.length} связей</span><span className={`pipeline-indicator pipeline-${workspace.pipeline.status}`}>{locked ? <LoaderCircle size={13} className="spin" /> : workspace.pipeline.status === 'completed' ? <Check size={13} /> : workspace.pipeline.status === 'cancelled' ? <CirclePause size={13} /> : <span className="status-dot" />}{pipelineLabel}{workspace.pipeline.total > 0 && <span className="pipeline-count">{workspace.pipeline.completed}/{workspace.pipeline.total}</span>}</span></div>
        {workspace.pipeline.error && <div className="pipeline-error" role="alert">{workspace.pipeline.error}</div>}
        <div className="workspace-content"><div className="graph-area" tabIndex={0} aria-label="Граф тасклетов" onKeyDown={event => { const target = event.target as HTMLElement; if (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName) || target.isContentEditable) return; const focusedNode = target.closest<HTMLElement>('.react-flow__node'); if ((event.key === 'Enter' || event.key === ' ') && focusedNode?.dataset.id) { event.preventDefault(); selectNode(focusedNode.dataset.id); return } if ((event.key === 'Delete' || event.key === 'Backspace') && !locked && selectedEdgeId) { event.preventDefault(); void mutate(() => api.deleteEdge(selectedEdgeId)).then(() => setSelectedEdgeId(null)).catch(() => {}) } }}>
          <ReactFlow<TaskletFlowNode> nodes={nodes} edges={edges} nodeTypes={nodeTypes} onInit={instance => { flow.current = instance }} onNodesChange={onNodesChange} onNodeClick={(_event, node) => { selectNode(node.id) }} onPaneClick={() => { if (selectNode(null)) setSelectedEdgeId(null) }} onEdgeClick={(_event, edge) => { if (selectNode(null)) setSelectedEdgeId(edge.id) }} onConnect={connection => { if (connection.source && connection.target) void mutate(() => api.createEdge(connection.source!, connection.target!)).catch(() => {}) }} onNodeDragStop={(_event, node) => { void mutate(() => api.updateTasklet(node.id, { position: node.position })).catch(() => {}) }} nodesConnectable={!locked} edgesReconnectable={false} deleteKeyCode={null} minZoom={0.25} maxZoom={1.75} fitView fitViewOptions={{ padding: 0.25, maxZoom: 1 }} defaultEdgeOptions={{ style: { strokeWidth: 1.5, stroke: '#a5a1b1' }, interactionWidth: 24 }} proOptions={{ hideAttribution: true }}>
            <Background variant={BackgroundVariant.Dots} gap={22} size={1.1} color="#dcdcd9" />
          </ReactFlow>
          {loading ? <div className="canvas-state"><LoaderCircle size={24} className="spin" /><p>Открываем пространство…</p></div> : loadError ? <div className="canvas-state"><CircleHelp size={30} strokeWidth={1.3} /><h2>Не удалось подключиться</h2><p>{loadError}</p><button className="button button-secondary" onClick={() => void initialize()}>Попробовать снова</button></div> : !workspace.tasklets.length && <div className="canvas-state empty-state"><div className="empty-illustration" aria-hidden="true"><div className="empty-mini-node"><span /><i /><i /></div><div className="empty-node-connector"><span /><ArrowDown size={14} /></div><div className="empty-main-node"><span className="empty-node-icon"><Layers2 size={18} strokeWidth={1.6} /></span><div><span /><i /></div><span className="empty-node-dot" /></div><div className="empty-floating-dot" /></div><span className="eyebrow">МЕСТО ДЛЯ СЛЕДУЮЩЕЙ ИДЕИ</span><h2>Всё начинается<br />с одного тасклета.</h2><p>Опишите задачу, соедините её с другими<br className="desktop-break" /> и дайте идеям двигаться дальше.</p><button className="button button-primary" onClick={openCreate}><Plus size={16} />Создать первый тасклет</button><span className="empty-hint">Один тасклет — одна задача и свой чат</span></div>}
          <div className="canvas-bottom"><div className="canvas-controls"><button className="icon-button" aria-label="Уменьшить масштаб" onClick={() => { void flow.current?.zoomOut() }}><Minus size={16} /></button><button className="icon-button" aria-label="Увеличить масштаб" onClick={() => { void flow.current?.zoomIn() }}><Plus size={16} /></button><span /><button className="icon-button" aria-label="Уместить граф" onClick={() => { void flow.current?.fitView({ padding: 0.25, duration: 300, maxZoom: 1 }) }}><Maximize size={15} /></button></div><span className="canvas-tip"><Link2 size={12} />Потяните от точки на карточке, чтобы создать связь</span></div>
          {selectedEdge && <div className="edge-popover"><div><Link2 size={15} /><strong>Связь задач</strong><button className="icon-button" aria-label="Закрыть настройки связи" onClick={() => setSelectedEdgeId(null)}><X size={14} /></button></div><label className="checkbox-field"><input type="checkbox" checked={selectedEdge.pass_context} onChange={event => { void mutate(() => api.updateEdge(selectedEdge.id, event.target.checked)).catch(() => {}) }} disabled={locked} />Передавать результат в контекст</label><button className="text-button danger-text" onClick={() => { void mutate(() => api.deleteEdge(selectedEdge.id)).then(() => setSelectedEdgeId(null)).catch(() => {}) }} disabled={locked}><Trash2 size={13} />Удалить связь</button></div>}
        </div>
        {selected && <Inspector settings={settings} running={locked} stopping={actionPending || workspace.pipeline.status === 'stopping'} onStop={stop} onDirtyChange={setInspectorDirty} key={selected.id} tasklet={selected} tasklets={workspace.tasklets} edges={workspace.edges} messages={messages[selected.id] || []} messagesLoading={messagesLoading} locked={locked || actionPending} onClose={() => setSelectedId(null)} onSave={async value => { await mutate(() => api.updateTasklet(selected.id, value)); notify('Тасклет сохранён') }} onDelete={() => { void deleteTasklet(selected.id) }} onRun={() => run([selected.id])} onSend={async content => { requireConfiguration([selected.id]); await mutate(() => api.sendMessage(selected.id, content)) }} onConnect={(source, target) => mutate(() => api.createEdge(source, target))} onDeleteEdge={id => mutate(() => api.deleteEdge(id))} onUpdateEdge={(id, pass) => mutate(() => api.updateEdge(id, pass))} />}
        </div></> : settings ? <SettingsPage onDirtyChange={setSettingsDirty} settings={settings} locked={locked} onUpdate={setSettings} notify={notify} /> : <div className="settings-unavailable"><h1>Настройки недоступны</h1><p>{loadError || 'Подключаемся к серверу…'}</p><button className="button button-secondary" onClick={() => void initialize()}>Попробовать снова</button></div>}
      </div>
      {creating && <CreateTasklet onClose={() => setCreating(false)} onCreate={createTasklet} />}
      {toast && <div className={`toast ${toast.error ? 'toast-error' : ''}`} role={toast.error ? 'alert' : 'status'}>{toast.error ? <CircleHelp size={16} /> : <Check size={16} />}<span>{toast.text}</span><button className="icon-button" aria-label="Закрыть уведомление" onClick={() => setToast(null)}><X size={15} /></button></div>}
    </div>
  )
}
