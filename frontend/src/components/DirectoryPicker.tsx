import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowUp, Check, ChevronRight, Folder, FolderOpen, HardDrive, LoaderCircle, X } from 'lucide-react'
import { api } from '../lib/api'
import type { DirectoryListing } from '../lib/types'

export function DirectoryField({ label, value, inherited, disabled, onOpen, onClear }: { label: string; value: string | null; inherited?: string | null; disabled: boolean; onOpen: () => void; onClear: () => void }) {
  return <div className="directory-field"><span className="field directory-label">{label}</span><div className="directory-control"><button type="button" className="directory-value" onClick={onOpen} disabled={disabled} aria-label={`Выбрать: ${label}`}><FolderOpen size={15} /><span>{value || (inherited ? `Из настроек: ${inherited}` : 'Выбрать папку…')}</span></button>{value && <button type="button" className="icon-button" aria-label={`Очистить: ${label}`} onClick={onClear} disabled={disabled}><X size={14} /></button>}</div></div>
}

export function DirectoryPicker({ value, onSelect, onClose }: { value: string | null; onSelect: (path: string | null) => void; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const requestId = useRef(0)
  const [listing, setListing] = useState<DirectoryListing | null>(null)
  const [path, setPath] = useState(value || '')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [nativeAvailable, setNativeAvailable] = useState(false)
  const [nativePending, setNativePending] = useState(false)
  const [nativeMessage, setNativeMessage] = useState<{ text: string; error: boolean } | null>(null)
  const navigate = useCallback(async (next?: string) => {
    const id = ++requestId.current
    setLoading(true)
    setError('')
    try {
      const result = await api.directories(next)
      if (id !== requestId.current) return
      setListing(result)
      setPath(result.path)
    } catch (reason) { if (id === requestId.current) setError((reason as Error).message) }
    finally { if (id === requestId.current) setLoading(false) }
  }, [])
  useEffect(() => {
    let active = true
    dialog.current?.showModal()
    void navigate(value || undefined)
    void api.directoryCapabilities().then(result => { if (active) setNativeAvailable(result.native_picker) }).catch(() => {})
    return () => { active = false; requestId.current++ }
  }, [navigate, value])

  async function chooseNative() {
    setNativePending(true)
    setNativeMessage(null)
    try {
      const result = await api.chooseDirectory(listing?.path || value)
      if (result.path) onSelect(result.path)
      else setNativeMessage({ text: 'Выбор отменён. Можно выбрать папку в списке ниже.', error: false })
    } catch (reason) { setNativeMessage({ text: (reason as Error).message, error: true }) }
    finally { setNativePending(false) }
  }

  return <dialog ref={dialog} className="modal directory-modal" aria-labelledby="directory-title" onCancel={event => { if (nativePending) event.preventDefault(); else onClose() }} onClick={event => { if (event.target === dialog.current && !nativePending) onClose() }}>
    <div className="modal-heading"><div><span className="eyebrow">КОНТЕКСТ ПРОЕКТА</span><h2 id="directory-title">Рабочая папка</h2></div><button className="icon-button" aria-label="Закрыть выбор папки" onClick={onClose} disabled={nativePending}><X size={18} /></button></div>
    <p className="directory-description">Выберите папку на компьютере, где запущен aispace.</p>
    {nativeAvailable && <button type="button" className="button button-primary full-width directory-native" onClick={() => void chooseNative()} disabled={nativePending}>{nativePending ? <LoaderCircle size={15} className="spin" /> : <FolderOpen size={15} />}{nativePending ? 'Выберите папку в Finder…' : 'Выбрать в Finder…'}</button>}
    {nativeMessage && <div className={`notice small ${nativeMessage.error ? 'error-notice' : ''}`} role={nativeMessage.error ? 'alert' : 'status'}>{nativeMessage.text}</div>}
    {listing && listing.roots.length > 1 && <label className="field">Доступные каталоги<select aria-label="Доступные каталоги" value={listing.roots.find(root => listing.path === root || listing.path.startsWith(`${root}/`)) || ''} onChange={event => void navigate(event.target.value)} disabled={loading || nativePending}>{listing.roots.map(root => <option value={root} key={root}>{root}</option>)}</select></label>}
    <form className="directory-path-form" onSubmit={event => { event.preventDefault(); if (!nativePending) void navigate(path.trim() || undefined) }}><input aria-label="Путь к папке" value={path} onChange={event => setPath(event.target.value)} placeholder="Путь к папке" disabled={nativePending} /><button className="button button-secondary" disabled={loading || nativePending}>Открыть</button></form>
    {error && <div className="notice error-notice" role="alert"><p>{error}</p><button type="button" className="text-button" onClick={() => void navigate()} disabled={loading || nativePending}>Доступные папки</button></div>}
    <div className="directory-list" aria-label="Папки" aria-busy={loading}>
      {loading ? <div className="directory-empty"><LoaderCircle className="spin" size={20} />Загружаем папки…</div> : listing ? <><button type="button" className="directory-entry directory-parent" disabled={nativePending || !listing.roots[0] || listing.path === listing.roots[0]} onClick={() => void navigate(listing.roots[0])}><HardDrive size={16} /><span>В корень</span></button>{listing.parent && <button className="directory-entry directory-parent" onClick={() => void navigate(listing.parent!)} disabled={nativePending}><ArrowUp size={16} /><span>На уровень выше</span></button>}{listing.entries.map(entry => <button className="directory-entry" key={entry.path} onClick={() => void navigate(entry.path)} disabled={nativePending}><Folder size={16} /><span>{entry.name}</span><ChevronRight size={14} /></button>)}{listing.entries.length === 0 && <div className="directory-empty"><FolderOpen size={22} />Вложенных папок нет</div>}</> : <div className="directory-empty">Откройте доступный каталог.</div>}
    </div>
    <div className="modal-actions directory-actions"><button className="text-button" onClick={() => onSelect(null)} disabled={nativePending}>Без отдельной папки</button><button className="button button-primary" onClick={() => listing && onSelect(listing.path)} disabled={loading || nativePending || !listing || Boolean(error)}><Check size={15} />Выбрать папку</button></div>
  </dialog>
}
