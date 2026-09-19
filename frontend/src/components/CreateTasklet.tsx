import { useEffect, useRef, useState } from 'react'
import { ArrowUpRight, X } from 'lucide-react'
import { patchWorkspaceUi, useWorkspaceUi } from '../lib/uiState'

export function CreateTasklet({ workspaceId, onClose, onCreate }: { workspaceId: string; onClose: () => void; onCreate: (title: string, prompt: string) => Promise<void> }) {
  const { createDraft: { title, prompt } } = useWorkspaceUi(workspaceId)
  const setTitle = (title: string) => patchWorkspaceUi(workspaceId, ui => ({ ...ui, createDraft: { ...ui.createDraft, title } }))
  const setPrompt = (prompt: string) => patchWorkspaceUi(workspaceId, ui => ({ ...ui, createDraft: { ...ui.createDraft, prompt } }))
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const dialog = useRef<HTMLDialogElement>(null)
  useEffect(() => { dialog.current?.showModal() }, [])
  function close() {
    if (pending || ((title || prompt) && !window.confirm('Закрыть создание тасклета без сохранения черновика?'))) return
    patchWorkspaceUi(workspaceId, { creating: false, createDraft: { title: '', prompt: '' } })
    onClose()
  }
  return (
    <dialog ref={dialog} className="modal" onCancel={event => { event.preventDefault(); close() }} onClick={event => { if (event.target === dialog.current) close() }} aria-labelledby="create-title">
      <form onSubmit={async event => { event.preventDefault(); setPending(true); setError(''); try { await onCreate(title.trim(), prompt.trim()); patchWorkspaceUi(workspaceId, current => current.createDraft.title === title && current.createDraft.prompt === prompt ? { ...current, creating: false, createDraft: { title: '', prompt: '' } } : current) } catch (error) { setError((error as Error).message); setPending(false) } }}>
        <div className="modal-heading"><div><span className="eyebrow">НОВАЯ ЗАДАЧА</span><h2 id="create-title">Новый тасклет</h2></div><button type="button" className="icon-button" aria-label="Закрыть создание тасклета" onClick={close} disabled={pending}><X size={18} /></button></div>
        <label className="field">Название<input autoFocus required maxLength={200} placeholder="Например, продумать архитектуру" value={title} onChange={event => setTitle(event.target.value)} disabled={pending} /></label>
        <label className="field">Промпт<textarea aria-label="Промпт" rows={5} placeholder="Что нужно сделать? Опишите задачу и желаемый результат." value={prompt} onChange={event => setPrompt(event.target.value)} disabled={pending} /></label>
        <p className="field-help">Инструкцию и связи можно изменить в любой момент.</p>
        {error && <div className="notice error-notice" role="alert">{error}</div>}
        <div className="modal-actions"><button type="button" className="button button-quiet" onClick={close} disabled={pending}>Отмена</button><button className="button button-primary" disabled={pending || !title.trim()}>{pending ? 'Создаём…' : 'Создать тасклет'}<ArrowUpRight size={16} /></button></div>
      </form>
    </dialog>
  )
}
