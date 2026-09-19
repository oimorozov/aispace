import { useEffect, useRef, useState } from 'react'
import { ArrowRight, Check, ExternalLink, Github, LoaderCircle, X } from 'lucide-react'
import { api } from '../lib/api'
import type { GitHubIssue, GitHubRepository, GitHubSelection } from '../lib/types'

const pageSize = 20
const nameOf = (value: string) => value.trim().replace(/^https:\/\/github\.com\//i, '').replace(/\/$/, '').replace(/\.git$/, '').toLowerCase()

export function GitHubImport({ onClose, onPlan, selection, onBack }: { onClose: () => void; onPlan: (selection: GitHubSelection) => void; selection?: GitHubSelection; onBack: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const request = useRef(0)
  const [input, setInput] = useState('')
  const [repository, setRepository] = useState<GitHubRepository | null>(null)
  const [issues, setIssues] = useState<GitHubIssue[]>([])
  const [selected, setSelected] = useState<Record<number, GitHubIssue>>({})
  const [state, setState] = useState('open')
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(1)
  const [pending, setPending] = useState<'load' | 'selection' | null>(null)
  const [error, setError] = useState('')
  const [configured, setConfigured] = useState(false)
  const choices = Object.values(selected)
  const search = query.trim().replace(/^#/, '').toLocaleLowerCase()
  const filtered = issues.filter(issue => (state === 'all' || issue.state === state) && (!search || String(issue.number).includes(search) || issue.title.toLocaleLowerCase().includes(search)))
  const pages = Math.max(1, Math.ceil(filtered.length / pageSize))
  const currentPage = Math.min(page, pages)
  const shown = filtered.slice((currentPage - 1) * pageSize, currentPage * pageSize)

  useEffect(() => {
    let active = true
    dialog.current?.showModal()
    void api.githubConnection().then(value => { if (active) setConfigured(value.token_configured) }).catch(reason => { if (active) setError(reason.message) })
    return () => { active = false; request.current++ }
  }, [])

  async function load() {
    if (choices.length && repository && nameOf(input) !== repository.full_name.toLowerCase() && !window.confirm('Сменить репозиторий и сбросить выбранные issues? Текущий выбор сохранится, если загрузка завершится ошибкой.')) return
    const id = ++request.current
    setPending('load')
    setError('')
    try {
      const result = await api.githubRepository(input)
      if (id !== request.current) return
      if (repository?.id !== result.repository.id) setSelected({})
      setRepository(result.repository)
      setIssues(result.issues)
      setInput(result.repository.full_name)
      setPage(1)
      setQuery('')
    } catch (reason) { if (id === request.current) setError((reason as Error).message) }
    finally { if (id === request.current) setPending(null) }
  }

  async function prepare() {
    if (!repository || !choices.length) return
    const id = ++request.current
    setPending('selection')
    setError('')
    try {
      const result = await api.githubSelection(repository, choices)
      if (id === request.current) onPlan(result)
    } catch (reason) { if (id === request.current) setError((reason as Error).message) }
    finally { if (id === request.current) setPending(null) }
  }

  function toggle(issue: GitHubIssue) {
    setSelected(previous => {
      const next = { ...previous }
      if (next[issue.id]) delete next[issue.id]
      else next[issue.id] = issue
      return next
    })
  }

  return <dialog ref={dialog} className="modal github-import-modal" aria-labelledby="github-import-title" onCancel={onClose}>
    <div className="modal-heading"><div><span className="eyebrow">GITHUB ISSUES</span><h2 id="github-import-title">Импортировать GitHub Issues</h2></div><button className="icon-button" aria-label="Закрыть импорт GitHub" onClick={onClose}><X size={18} /></button></div>
    {selection ? <section className="github-selection-ready" aria-label="Выбор для планировщика">
      <h3><Check size={18} />Выбор готов к построению графа</h3><p>{selection.repository.full_name} · {selection.issues.length} issues</p>
      <p className="field-help">Полные описания и доступные зависимости загружены. Следующий шаг — построение графа и подтверждение нового пространства.</p>
      {selection.issues.map(issue => <div className="github-selected-item" key={issue.id}><strong>#{issue.number} {issue.title}</strong><span>{issue.dependencies.status === 'complete' ? `Зависимостей: ${issue.dependencies.blocked_by.length}` : 'Зависимости прочитать не удалось'}</span>{issue.dependencies.error && <p className="notice error-notice" role="alert">#{issue.number}: {issue.dependencies.error}</p>}</div>)}
      <button className="button button-secondary" onClick={onBack}>Вернуться к выбору</button>
    </section> : <>
      <p className="field-help">Выберите issues для нового пространства. Текущий граф останется без изменений.</p>
      <p className="github-connection-status">{configured ? 'Токен GitHub настроен' : 'Без токена · публичные репозитории'}<span>Подключение GitHub настраивается отдельно в общих настройках.</span></p>
      <form className="github-repository-form" onSubmit={event => { event.preventDefault(); void load() }}><label className="field">Репозиторий GitHub<input value={input} onChange={event => setInput(event.target.value)} placeholder="owner/repo или https://github.com/owner/repo" disabled={Boolean(pending)} required /></label><button className="button button-secondary" disabled={Boolean(pending) || !input.trim()}>{pending === 'load' ? <LoaderCircle size={15} className="spin" /> : <Github size={15} />}Загрузить issues</button></form>
      {error && <div className="notice error-notice" role="alert">{error}<p>Выбор сохранён. Исправьте подключение или повторите запрос.</p></div>}
      {repository && <>
        <div className="github-repository-heading"><a href={repository.url} target="_blank" rel="noreferrer">{repository.full_name}<ExternalLink size={13} /></a><span>{repository.private ? 'Private' : 'Public'} · Загружено issues: {issues.length}</span></div>
        <div className="github-filters"><label className="field">Состояние issues<select value={state} onChange={event => { setState(event.target.value); setPage(1) }}><option value="open">Открытые</option><option value="closed">Закрытые</option><option value="all">Все состояния</option></select></label><label className="field">Поиск по номеру или заголовку<input value={query} onChange={event => { setQuery(event.target.value); setPage(1) }} placeholder="По всем загруженным issues" /></label></div>
        <div className="github-list-toolbar"><span>Найдено: {filtered.length}</span><button className="text-button" disabled={!shown.length || Boolean(pending)} onClick={() => setSelected(previous => ({ ...previous, ...Object.fromEntries(shown.map(issue => [issue.id, issue])) }))}>Выбрать показанные ({shown.length})</button></div>
        <div className="github-issues" aria-label="Issues репозитория">{shown.length ? shown.map(issue => <article className="github-issue" key={issue.id}>
          <div className="github-issue-heading"><input type="checkbox" aria-label={`Выбрать issue #${issue.number}`} checked={Boolean(selected[issue.id])} disabled={Boolean(pending)} onChange={() => toggle(issue)} /><strong>#{issue.number} {issue.title}</strong><a href={issue.url} target="_blank" rel="noreferrer" aria-label={`Открыть issue #${issue.number} на GitHub`}><ExternalLink size={14} /></a></div>
          <div className="github-issue-meta"><span>{issue.state === 'open' ? 'Открыта' : 'Закрыта'}</span>{issue.labels.map(label => <span className="github-label" key={label.name} style={/^[0-9a-f]{6}$/i.test(label.color) ? { borderColor: `#${label.color}` } : undefined}>{label.name}</span>)}</div>
          <details><summary>Описание #{issue.number} (Markdown)</summary>{issue.body ? <pre className="github-issue-body">{issue.body}</pre> : <p className="field-help">Описание отсутствует.</p>}</details>
        </article>) : <p className="github-empty">Issues с этим фильтром не найдены.</p>}</div>
        <div className="github-pagination"><button className="button button-secondary" disabled={currentPage <= 1} onClick={() => setPage(currentPage - 1)}>Предыдущая страница</button><span>Страница {currentPage} из {pages}</span><button className="button button-secondary" disabled={currentPage >= pages} onClick={() => setPage(currentPage + 1)}>Следующая страница</button></div>
        <section className="github-selected" aria-label="Выбранные issues"><h3>Выбрано issues: {choices.length}</h3><p className="field-help">Включая задачи, скрытые фильтром или находящиеся на другой странице.</p>{choices.map(issue => <div className="github-selected-item" key={issue.id}><span>#{issue.number} {issue.title}</span><button className="icon-button" aria-label={`Убрать issue #${issue.number} из выбора`} disabled={Boolean(pending)} onClick={() => toggle(issue)}><X size={14} /></button></div>)}</section>
      </>}
      <div className="modal-actions"><button className="button button-secondary" onClick={onClose}>Закрыть</button><button className="button button-primary" disabled={!choices.length || Boolean(pending)} onClick={() => void prepare()}>{pending === 'selection' ? <LoaderCircle size={15} className="spin" /> : <ArrowRight size={15} />}Построить граф</button></div>
    </>}
  </dialog>
}
