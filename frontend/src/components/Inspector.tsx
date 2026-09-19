import { useLayoutEffect, useRef, useState } from 'react'
import { ArrowDownToLine, ArrowUp, Check, FolderOpen, Link2, LoaderCircle, MessageSquare, Play, Square, Trash2, X } from 'lucide-react'
import type { Dependency, Message, Settings, Tasklet } from '../lib/types'
import { statusLabels } from '../lib/types'
import { clearMatching, patchTaskletUi, patchWorkspaceUi, taskletUi, useWorkspaceUi, type TaskletDraft } from '../lib/uiState'
import { MessageContent } from './MessageContent'
import { DirectoryField, DirectoryPicker } from './DirectoryPicker'

interface Props {
  workspaceId: string
  workingDirectory: string | null
  runBlocked: boolean
  runBlockedReason: string
  tasklet: Tasklet
  tasklets: Tasklet[]
  edges: Dependency[]
  messages: Message[]
  messagesLoading: boolean
  locked: boolean
  running: boolean
  stopping: boolean
  settings: Settings | null
  onStop: () => Promise<void>
  onClose: () => void
  onSave: (value: { title: string; prompt: string; model: string | null; working_directory: string | null }) => Promise<void>
  onDelete: () => void
  onRun: () => Promise<void>
  onSend: (content: string) => Promise<void>
  onConnect: (source: string, target: string) => Promise<void>
  onDeleteEdge: (id: string) => Promise<void>
  onUpdateEdge: (id: string, pass: boolean) => Promise<void>
}

export function Inspector({ workspaceId, workingDirectory, runBlocked, runBlockedReason, tasklet, tasklets, edges, messages, messagesLoading, locked, running, stopping, settings, onStop, onClose, onSave, onDelete, onRun, onSend, onConnect, onDeleteEdge, onUpdateEdge }: Props) {
  useWorkspaceUi(workspaceId)
  const ui = taskletUi(workspaceId, tasklet.id)
  const { tab, message, source } = ui
  const title = ui.draft.title ?? tasklet.title
  const prompt = ui.draft.prompt ?? tasklet.prompt
  const model = ('model' in ui.draft ? ui.draft.model : tasklet.model) || ''
  const directory = 'working_directory' in ui.draft ? ui.draft.working_directory! : tasklet.working_directory
  const patch = (change: Parameters<typeof patchTaskletUi>[2]) => patchTaskletUi(workspaceId, tasklet.id, change)
  const draft = (change: TaskletDraft) => patch(current => ({ ...current, draft: { ...current.draft, ...change } }))
  const setTab = (value: 'task' | 'chat') => patch({ tab: value })
  const setMessage = (value: string) => patch({ message: value })
  const setSource = (value: string) => patch({ source: value })
  const [pending, setPending] = useState(false)
  const chatHistory = useRef<HTMLDivElement>(null)
  const taskBody = useRef<HTMLDivElement>(null)
  const dirty = title !== tasklet.title || prompt !== tasklet.prompt || model !== (tasklet.model || '') || directory !== tasklet.working_directory
  const restartable = ['completed', 'failed', 'cancelled'].includes(tasklet.status)
  const effectiveDirectory = tasklet.working_directory || workingDirectory
  const codexMode = settings?.execution_mode === 'codex'
  const incoming = edges.filter(edge => edge.target === tasklet.id)
  const available = tasklets.filter(item => item.id !== tasklet.id && !incoming.some(edge => edge.source === item.id))
  useLayoutEffect(() => {
    if (tab === 'task' && taskBody.current) taskBody.current.scrollTop = taskletUi(workspaceId, tasklet.id).taskScroll
    if (tab === 'chat' && chatHistory.current && !messagesLoading) {
      const saved = taskletUi(workspaceId, tasklet.id)
      chatHistory.current.scrollTop = saved.chatAtBottom ? chatHistory.current.scrollHeight : saved.chatScroll
    }
  }, [tab, messagesLoading, workspaceId, tasklet.id])
  useLayoutEffect(() => {
    if (tab === 'chat' && !messagesLoading && taskletUi(workspaceId, tasklet.id).chatAtBottom && chatHistory.current) chatHistory.current.scrollTop = chatHistory.current.scrollHeight
  }, [messages, tab, messagesLoading, workspaceId, tasklet.id])

  async function perform(action: () => Promise<void>) {
    setPending(true)
    try { await action() } catch { return } finally { setPending(false) }
  }

  function close() {
    if ((dirty || message.trim()) && !window.confirm('Закрыть тасклет без сохранения черновика?')) return
    patchWorkspaceUi(workspaceId, current => ({ ...current, tasklets: Object.fromEntries(Object.entries(current.tasklets).filter(([id]) => id !== tasklet.id)) }))
    onClose()
  }

  return (
    <aside className="inspector" aria-label="Редактор тасклета">
      <div className="inspector-heading"><span className="eyebrow">ТАСКЛЕТ</span><div className="inspector-heading-actions"><button className="icon-button delete-button" aria-label="Удалить тасклет" onClick={onDelete} disabled={locked || pending}><Trash2 size={16} /></button><button className="icon-button" aria-label="Закрыть редактор" onClick={close}><X size={18} /></button></div></div>
      <h2 className="inspector-title">{tasklet.title}</h2>
      <div className="inspector-status"><span className={`status-label status-${tasklet.status}`}><span className="status-dot" />{statusLabels[tasklet.status]}</span></div>
      <div className="inspector-tabs" role="tablist" aria-label="Тасклет"><button role="tab" aria-selected={tab === 'task'} onClick={() => setTab('task')}>Задача</button><button role="tab" aria-selected={tab === 'chat'} onClick={() => setTab('chat')}>Чат{messages.length > 0 && <span className="tab-count">{messages.filter(item => item.role !== 'system').length}</span>}</button></div>
      {tab === 'task' ? <div className="inspector-body" ref={taskBody} onScroll={event => patch({ taskScroll: event.currentTarget.scrollTop })}>
        {locked && <div className="notice small">Пайплайн выполняется. Редактирование будет доступно после остановки.</div>}
        {tasklet.error && <div className="notice error-notice" role="alert">{tasklet.error}</div>}
        <form onSubmit={event => { event.preventDefault(); void perform(async () => { await onSave({ title: title.trim(), prompt, model: model.trim() || null, working_directory: directory }); patch(current => ({ ...current, draft: clearMatching(current.draft, ui.draft) })) }) }}>
          <label className="field">Название<input value={title} onChange={event => draft({ title: event.target.value })} maxLength={200} required disabled={locked || pending} /></label>
          <label className="field">Промпт<textarea aria-label="Промпт" rows={7} value={prompt} onChange={event => draft({ prompt: event.target.value })} placeholder="Опишите задачу для модели…" disabled={locked || pending} /></label>
          <label className="field">Модель<input value={model} onChange={event => draft({ model: event.target.value })} placeholder="По умолчанию из настроек" disabled={locked || pending} /></label>
          <DirectoryField label="Рабочая папка тасклета" value={directory} inherited={workingDirectory} disabled={locked || pending} onOpen={() => patch({ picker: { currentPath: directory || workingDirectory, pathInput: directory || workingDirectory || '' } })} onClear={() => draft({ working_directory: null })} />
          {!codexMode && <p className="field-help">Папка используется только Codex</p>}
          {directory !== (tasklet.working_directory || null) && <p className="field-help">Контекст Codex начнётся заново. Переписка в aispace сохранится.</p>}
          <button className="button button-secondary full-width save-tasklet" disabled={locked || pending || !dirty || !title.trim()}><Check size={15} />{pending ? 'Сохраняем…' : 'Сохранить изменения'}</button>
        </form>
        <section className="dependencies-section"><div className="subsection-heading"><h3><Link2 size={14} />Зависимости</h3><span>{incoming.length}</span></div>
          {incoming.map(edge => { const parent = tasklets.find(item => item.id === edge.source); return <div className="dependency-item" key={edge.id}><div className="dependency-title"><ArrowDownToLine size={13} /><span>{parent?.title || 'Тасклет'}</span><button className="icon-button" aria-label={`Удалить зависимость: ${parent?.title}`} onClick={() => void perform(() => onDeleteEdge(edge.id))} disabled={locked || pending}><X size={13} /></button></div><label className="checkbox-field"><input type="checkbox" checked={edge.pass_context} aria-label={`Передавать результат: ${parent?.title}`} onChange={event => void perform(() => onUpdateEdge(edge.id, event.target.checked))} disabled={locked || pending} />Передавать результат в контекст</label></div> })}
          {available.length > 0 && <div className="add-dependency"><select aria-label="Добавить зависимость" value={source} onChange={event => setSource(event.target.value)} disabled={locked || pending}><option value="">Выбрать тасклет…</option>{available.map(item => <option value={item.id} key={item.id}>{item.title}</option>)}</select><button className="button button-secondary" disabled={!source || locked || pending} onClick={() => void perform(async () => { await onConnect(source, tasklet.id); setSource('') })}>Связать</button></div>}
          {incoming.length === 0 && <div className="dependency-empty">Нет зависимостей</div>}
        </section>
        {tasklet.last_output && <section className="result-preview"><h3>Последний результат</h3><p>{tasklet.last_output}</p><button className="text-button" onClick={() => setTab('chat')}>Открыть в чате <MessageSquare size={13} /></button></section>}
      </div> : <div className="chat-panel">{codexMode && <div className="chat-context"><FolderOpen size={13} /><span title={effectiveDirectory || undefined}>{effectiveDirectory || 'Рабочая папка не выбрана'}</span></div>}<div className="chat-history" ref={chatHistory} onScroll={event => { const element = event.currentTarget; if (!messagesLoading) patch({ chatScroll: element.scrollTop, chatAtBottom: element.scrollHeight - element.scrollTop - element.clientHeight < 40 }) }} role="log" aria-label="История чата" aria-live="polite">{messagesLoading && <div className="chat-loading"><LoaderCircle size={18} className="spin" />Загружаем историю…</div>}{!messagesLoading && messages.filter(item => item.role !== 'system').length === 0 && <p className="chat-empty">Сообщений пока нет</p>}{messages.filter(item => item.role !== 'system').map(item => <article className={`chat-message message-${item.role}`} key={item.id}><span className="message-author">{item.role === 'user' ? 'Вы' : 'AI'}</span>{item.content ? <MessageContent content={item.content} /> : <div className="thinking">Думает…</div>}</article>)}{tasklet.status === 'running' && <div className="chat-working"><LoaderCircle size={12} className="spin" />Тасклет работает</div>}</div>{tasklet.error && <div className="notice error-notice chat-error" role="alert">{tasklet.error}</div>}{dirty && <div className="notice chat-error">Сохраните изменения на вкладке «Задача» перед отправкой сообщения.</div>}<form className="chat-composer" onSubmit={event => { event.preventDefault(); if (!message.trim() || runBlocked || locked || pending || dirty) return; void perform(async () => { await onSend(message.trim()); patch(current => ({ ...current, message: current.message === message ? '' : current.message })) }) }}><textarea aria-label="Сообщение" placeholder="Напишите сообщение…" rows={3} value={message} onChange={event => setMessage(event.target.value)} disabled={locked || pending || dirty} onKeyDown={event => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) { event.preventDefault(); event.currentTarget.form?.requestSubmit() } }} /><div className="composer-footer"><span>{runBlocked || locked ? 'Дождитесь завершения пайплайна' : '⌘ / Ctrl + Enter — отправить'}</span><div className="composer-actions">{running && <button type="button" className="button button-stop" onClick={() => void onStop()} disabled={stopping}>{stopping ? <LoaderCircle size={13} className="spin" /> : <Square size={10} fill="currentColor" />}{stopping ? 'Останавливаем…' : 'Остановить всё'}</button>}{!running && <button type="submit" className="send-button" aria-label="Отправить сообщение" disabled={runBlocked || locked || pending || dirty || !message.trim()}><ArrowUp size={17} /></button>}</div></div></form></div>}
      {(tab === 'task' || restartable) && <div className="inspector-footer">{running ? <button className="button button-stop full-width" onClick={() => void onStop()} disabled={stopping}>{stopping ? <LoaderCircle size={14} className="spin" /> : <Square size={12} fill="currentColor" />}{stopping ? 'Останавливаем…' : 'Остановить всё'}</button> : <button className="button button-primary full-width" aria-label={restartable ? `Перезапустить тасклет: ${tasklet.title}` : 'Запустить тасклет'} disabled={runBlocked || locked || pending || dirty || !tasklet.prompt.trim()} onClick={() => void perform(async () => { await onRun(); setTab('chat') })}><Play size={14} fill="currentColor" />{restartable ? 'Перезапустить тасклет' : 'Запустить тасклет'}</button>}{(dirty || runBlockedReason || restartable) && <span className="field-help">{dirty ? 'Сохраните изменения перед запуском' : runBlockedReason || 'Запустится заново в новом чате'}</span>}</div>}
      {ui.picker && <DirectoryPicker value={directory || workingDirectory} state={ui.picker} onStateChange={picker => patch({ picker })} onClose={() => patch({ picker: null })} onSelect={value => { draft({ working_directory: value }); patch({ picker: null }) }} />}
    </aside>
  )
}
