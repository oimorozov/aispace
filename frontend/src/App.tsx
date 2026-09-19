import { useCallback, useEffect, useRef, useState } from 'react'
import { Check, CircleHelp, Layers2, LoaderCircle, PanelLeftClose, PanelLeftOpen, Pencil, Plus, Settings2, Trash2, Workflow, X } from 'lucide-react'
import { api, ApiError, subscribe } from './lib/api'
import type { Message, Settings, Workspace, WorkspaceSummary } from './lib/types'
import { getUiState, patchWorkspaceUi, reconcileWorkspaceList, reconcileWorkspaceUi, uiStorageUnavailable, updateUi, useUiState } from './lib/uiState'
import { WorkspaceView } from './components/WorkspaceView'
import { WorkspaceDialog } from './components/WorkspaceDialog'
import { SettingsPage } from './components/SettingsPage'

const isRunning = (workspace: WorkspaceSummary) => ['running', 'stopping'].includes(workspace.pipeline.status)

export default function App() {
  const ui = useUiState()
  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[]>([])
  const [cache, setCache] = useState<Record<string, Workspace>>({})
  const [settings, setSettings] = useState<Settings | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [workspaceErrors, setWorkspaceErrors] = useState<Record<string, string>>({})
  const [connected, setConnected] = useState(false)
  const [reconnect, setReconnect] = useState(0)
  const [messageEvents, setMessageEvents] = useState<{ sequence: number; value: Message }[]>([])
  const [dialog, setDialog] = useState<{ mode: 'create' | 'rename' | 'delete'; workspace?: WorkspaceSummary } | null>(null)
  const [toast, setToast] = useState<{ text: string; error: boolean } | null>(null)
  const toastTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const alive = useRef(true)
  const knownIds = useRef<Set<string> | null>(null)
  const revisions = useRef<Record<string, number>>({})
  const requests = useRef<Record<string, number>>({})
  const listRevision = useRef(0)
  const listRequest = useRef(0)
  const settingsRequest = useRef(0)
  const settingsRevision = useRef(0)
  const messageSequence = useRef(0)
  const storageNotified = useRef(false)
  const activeId = ui.activeWorkspaceId
  const workspace = activeId ? cache[activeId] : undefined
  const summary = workspaces.find(item => item.id === activeId)
  const page = ui.workspaces[activeId || '__global__']?.page || 'workspace'
  const running = workspaces.find(isRunning)
  const activeRunning = Boolean(summary && isRunning(summary))

  const notify = useCallback((text: string, error = false) => {
    setToast({ text, error })
    clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToast(null), error ? 9000 : 4200)
  }, [])

  const acceptList = useCallback((items: WorkspaceSummary[]) => {
    if (!alive.current) return
    const ids = new Set(items.map(item => item.id))
    for (const id of knownIds.current || []) if (!ids.has(id)) revisions.current[id] = (revisions.current[id] || 0) + 1
    knownIds.current = ids
    setWorkspaces(items)
    setCache(previous => Object.fromEntries(Object.entries(previous).filter(([id]) => ids.has(id))))
    reconcileWorkspaceList(items.map(item => item.id))
    setLoaded(true)
    setLoadError('')
  }, [])

  const refreshList = useCallback(async () => {
    const revision = listRevision.current
    const request = ++listRequest.current
    const items = await api.workspaces()
    if (request === listRequest.current && revision === listRevision.current) acceptList(items)
  }, [acceptList])

  const acceptWorkspace = useCallback((value: Workspace) => {
    if (!alive.current || (knownIds.current && !knownIds.current.has(value.id))) return
    revisions.current[value.id] = (revisions.current[value.id] || 0) + 1
    setCache(previous => ({ ...previous, [value.id]: value }))
    setWorkspaces(previous => previous.map(item => item.id === value.id ? { id: value.id, name: value.name, created_at: value.created_at, updated_at: value.updated_at, pipeline: value.pipeline } : item))
    setWorkspaceErrors(previous => ({ ...previous, [value.id]: '' }))
    reconcileWorkspaceUi(value)
  }, [])

  const refreshWorkspace = useCallback(async (wid: string) => {
    const revision = revisions.current[wid] || 0
    const request = (requests.current[wid] || 0) + 1
    requests.current[wid] = request
    try {
      const value = await api.workspace(wid)
      if (requests.current[wid] === request && (revisions.current[wid] || 0) === revision) acceptWorkspace(value)
    } catch (error) {
      if (!alive.current || requests.current[wid] !== request || (revisions.current[wid] || 0) !== revision) return
      if (error instanceof ApiError && error.status === 404) {
        await refreshList()
        return
      }
      setWorkspaceErrors(previous => ({ ...previous, [wid]: (error as Error).message }))
      throw error
    }
  }, [acceptWorkspace, refreshList])

  const acceptSettings = useCallback((value: Settings) => {
    settingsRevision.current += 1
    if (alive.current) setSettings(value)
  }, [])

  const refreshSettings = useCallback(async () => {
    const revision = settingsRevision.current
    const request = ++settingsRequest.current
    const value = await api.settings()
    if (request === settingsRequest.current && revision === settingsRevision.current) acceptSettings(value)
  }, [acceptSettings])

  const initialize = useCallback(async () => {
    const results = await Promise.allSettled([refreshList(), refreshSettings()])
    const error = results.find(result => result.status === 'rejected')
    if (alive.current && error?.status === 'rejected') setLoadError((error.reason as Error).message)
  }, [refreshList, refreshSettings])

  useEffect(() => {
    alive.current = true
    void initialize()
    const unsubscribe = subscribe({
      workspaces: value => { listRevision.current += 1; acceptList(value) },
      workspace: acceptWorkspace,
      settings: acceptSettings,
      message: value => {
        if (knownIds.current && !knownIds.current.has(value.workspace_id)) return
        const event = { sequence: ++messageSequence.current, value }; setMessageEvents(previous => [...previous.filter(item => item.value.id !== value.id).slice(-255), event])
      },
      connection: value => {
        setConnected(value)
        if (value) {
          setReconnect(previous => previous + 1)
          void initialize()
          const wid = getUiState().activeWorkspaceId
          if (wid) void refreshWorkspace(wid).catch(() => {})
        }
      },
    })
    return () => { alive.current = false; unsubscribe(); clearTimeout(toastTimer.current) }
  }, [initialize, acceptList, acceptWorkspace, acceptSettings, refreshWorkspace])

  useEffect(() => {
    if (activeId && loaded) void refreshWorkspace(activeId).catch(() => {})
  }, [activeId, loaded, refreshWorkspace])

  useEffect(() => {
    if (uiStorageUnavailable() && !storageNotified.current) {
      storageNotified.current = true
      notify('Браузер не разрешил сохранить состояние. Черновики доступны до закрытия страницы.', true)
    }
  }, [ui, notify])

  const switchWorkspace = (id: string) => updateUi(current => ({ ...current, activeWorkspaceId: id }))
  const navigate = (next: 'workspace' | 'settings') => patchWorkspaceUi(activeId || '__global__', { page: next })

  async function submitWorkspace(name: string) {
    if (!dialog) return
    if (dialog.mode === 'create') {
      const value = await api.createWorkspace(name)
      listRevision.current += 1
      knownIds.current = new Set([...(knownIds.current || []), value.id])
      setWorkspaces(previous => [...previous.filter(item => item.id !== value.id), value])
      acceptWorkspace(value)
      switchWorkspace(value.id)
      notify('Пространство создано')
    } else if (dialog.workspace && dialog.mode === 'rename') {
      await api.updateWorkspace(dialog.workspace.id, { name })
      await refreshWorkspace(dialog.workspace.id)
      notify('Пространство переименовано')
    } else if (dialog.workspace) {
      await api.deleteWorkspace(dialog.workspace.id)
      listRevision.current += 1
      revisions.current[dialog.workspace.id] = (revisions.current[dialog.workspace.id] || 0) + 1
      knownIds.current?.delete(dialog.workspace.id)
      await refreshList()
      notify('Пространство удалено')
    }
  }

  return <div className={`app-shell ${ui.sidebarCollapsed ? 'sidebar-collapsed' : ''}`}>
    {!ui.sidebarCollapsed && <aside className="sidebar">
      <a className="brand" href="#" onClick={event => { event.preventDefault(); navigate('workspace') }} aria-label="aispace — главная"><span className="brand-symbol"><Layers2 size={21} strokeWidth={1.65} /></span><span>aispace<span className="brand-dot">.</span></span></a>
      <nav aria-label="Основная навигация"><button aria-label="Пространство" className={`nav-item ${page === 'workspace' ? 'active' : ''}`} onClick={() => navigate('workspace')}><Workflow size={18} /><span>Пространство</span><span className="nav-count">{workspace?.tasklets.length || 0}</span></button><button aria-label="Настройки" className={`nav-item ${page === 'settings' ? 'active' : ''}`} onClick={() => navigate('settings')}><Settings2 size={18} /><span>Настройки</span></button></nav>
      <div className="sidebar-bottom"><div className="connection-status"><span className={`connection-dot ${connected ? 'connected' : ''}`} /><div><strong>{connected ? 'Локальный сервер' : !loaded ? 'Подключаемся…' : 'Нет соединения'}</strong><span>{connected ? (settings?.execution_mode === 'codex' ? 'ChatGPT через Codex' : settings?.api_key_configured ? 'API-ключ сохранён' : 'Добавьте API-ключ') : 'Автоматическое переподключение'}</span></div></div><div className="sidebar-version"><span>aispace</span><span>v0.1</span></div></div>
    </aside>}
    <div className="main-area">
      <div className="workspace-switcher">
        <button className="icon-button" aria-label={ui.sidebarCollapsed ? 'Показать боковую панель' : 'Скрыть боковую панель'} aria-expanded={!ui.sidebarCollapsed} onClick={() => updateUi(current => ({ ...current, sidebarCollapsed: !current.sidebarCollapsed }))}>{ui.sidebarCollapsed ? <PanelLeftOpen size={17} /> : <PanelLeftClose size={17} />}</button>
        <select aria-label="Рабочее пространство" value={activeId || ''} disabled={!workspaces.length} onChange={event => switchWorkspace(event.target.value)}>{!workspaces.length && <option value="">Нет пространств</option>}{workspaces.map(item => <option key={item.id} value={item.id}>{item.name}{isRunning(item) ? ' · Выполняется' : ''}</option>)}</select>
        <button className="icon-button" aria-label="Создать пространство" title="Создать пространство" onClick={() => setDialog({ mode: 'create' })} disabled={!loaded}><Plus size={17} /></button>
        <button className="icon-button" aria-label="Переименовать пространство" title="Переименовать пространство" disabled={!summary || activeRunning} onClick={() => setDialog({ mode: 'rename', workspace: summary })}><Pencil size={15} /></button>
        <button className="icon-button" aria-label="Удалить пространство" title={activeRunning ? 'Остановите пайплайн перед удалением' : 'Удалить пространство'} disabled={!summary || activeRunning} onClick={() => setDialog({ mode: 'delete', workspace: summary })}><Trash2 size={15} /></button>
        {ui.sidebarCollapsed && <div className="compact-navigation"><button aria-label="Пространство" className={`icon-button ${page === 'workspace' ? 'active' : ''}`} onClick={() => navigate('workspace')}><Workflow size={17} /></button><button aria-label="Настройки" className={`icon-button ${page === 'settings' ? 'active' : ''}`} onClick={() => navigate('settings')}><Settings2 size={17} /></button></div>}
      </div>
      {running && running.id !== activeId && <div className="other-workspace-running" role="status"><span>Выполняется в пространстве «{running.name}». Новый запуск будет доступен после завершения.</span><button className="text-button" aria-label="Открыть выполняющееся пространство" onClick={() => switchWorkspace(running.id)}>Открыть пространство</button></div>}
      {workspace && settings ? <WorkspaceView key={workspace.id} workspace={workspace} settings={settings} anyRunning={Boolean(running)} messageEvents={messageEvents} reconnect={reconnect} refresh={refreshWorkspace} onUpdate={value => refreshWorkspace(value.id)} onSettingsUpdate={refreshSettings} notify={notify} /> : loaded && !workspaces.length && page === 'settings' && settings ? <SettingsPage key="global-settings" workspace={null} settings={settings} globalLocked={Boolean(running)} workspaceLocked={false} onUpdate={refreshSettings} onWorkspaceUpdate={value => refreshWorkspace(value.id)} notify={notify} /> : <div className="workspace-unavailable">{loadError || (activeId && workspaceErrors[activeId]) ? <><CircleHelp size={28} /><h1>Не удалось открыть пространство</h1><p>{loadError || (activeId && workspaceErrors[activeId])}</p><button className="button button-secondary" onClick={() => { void initialize(); if (activeId) void refreshWorkspace(activeId).catch(() => {}) }}>Попробовать снова</button></> : !loaded || workspaces.length ? <><LoaderCircle size={25} className="spin" /><p>Открываем пространство…</p></> : <><Layers2 size={35} strokeWidth={1.3} /><h1>Создайте рабочее пространство</h1><button className="button button-primary" onClick={() => setDialog({ mode: 'create' })}><Plus size={16} />Новое пространство</button></>}</div>}
    </div>
    {dialog && <WorkspaceDialog key={`${dialog.mode}:${dialog.workspace?.id || ''}`} mode={dialog.mode} workspace={dialog.workspace} onClose={() => setDialog(null)} onSubmit={submitWorkspace} />}
    {toast && <div className={`toast ${toast.error ? 'toast-error' : ''}`} role={toast.error ? 'alert' : 'status'}>{toast.error ? <CircleHelp size={16} /> : <Check size={16} />}<span>{toast.text}</span><button className="icon-button" aria-label="Закрыть уведомление" onClick={() => setToast(null)}><X size={15} /></button></div>}
  </div>
}
