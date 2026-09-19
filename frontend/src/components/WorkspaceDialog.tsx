import { useEffect, useRef, useState } from 'react'
import { Check, LoaderCircle, Trash2, X } from 'lucide-react'
import type { WorkspaceSummary } from '../lib/types'

export function WorkspaceDialog({ mode, workspace, onClose, onSubmit }: { mode: 'create' | 'rename' | 'delete'; workspace?: WorkspaceSummary; onClose: () => void; onSubmit: (name: string) => Promise<void> }) {
  const [name, setName] = useState(workspace?.name || '')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const dialog = useRef<HTMLDialogElement>(null)
  const title = mode === 'create' ? 'Создать пространство' : mode === 'rename' ? 'Переименовать пространство' : 'Удалить пространство'
  useEffect(() => { dialog.current?.showModal() }, [])
  return <dialog ref={dialog} className="modal" aria-labelledby="workspace-dialog-title" onCancel={event => { if (pending) event.preventDefault(); else onClose() }} onClick={event => { if (event.target === dialog.current && !pending) onClose() }}>
    <form onSubmit={async event => { event.preventDefault(); setPending(true); setError(''); try { await onSubmit(name.trim()); onClose() } catch (reason) { setError((reason as Error).message); setPending(false) } }}>
      <div className="modal-heading"><div><h2 id="workspace-dialog-title">{title}</h2></div><button type="button" className="icon-button" aria-label="Закрыть управление пространством" onClick={onClose} disabled={pending}><X size={18} /></button></div>
      {mode === 'delete' ? <p className="workspace-delete-copy">Удалить «{workspace?.name}» вместе с тасклетами, связями, перепиской и историей запусков? Файлы проекта останутся на компьютере.</p> : <label className="field">Название пространства<input autoFocus required maxLength={200} value={name} onChange={event => setName(event.target.value)} disabled={pending} placeholder="Например, новый проект" /></label>}
      {error && <div className="notice error-notice" role="alert">{error}</div>}
      <div className="modal-actions"><button type="button" className="button button-quiet" disabled={pending} onClick={onClose}>Отмена</button><button className={`button ${mode === 'delete' ? 'button-stop' : 'button-primary'}`} disabled={pending || (mode !== 'delete' && !name.trim())}>{pending ? <LoaderCircle size={14} className="spin" /> : mode === 'delete' ? <Trash2 size={14} /> : <Check size={14} />}{pending ? 'Сохраняем…' : title}</button></div>
    </form>
  </dialog>
}
