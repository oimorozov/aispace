import { useEffect, useMemo, useRef, useState } from 'react'
import { Background, BackgroundVariant, Controls, Handle, MarkerType, Position, ReactFlow, type Connection, type Node, type NodeProps, type ReactFlowInstance } from '@xyflow/react'
import { ArrowLeft, Check, ExternalLink, GitBranch, LoaderCircle, Plus, RefreshCw, Settings2, Trash2, X } from 'lucide-react'
import type { ImportClient, ImportDecision, ImportEdge, ImportGraph, ImportPayload, ImportPlan, ImportSelection, Settings, Workspace } from '../lib/types'
import { dependencyOrigins } from '../lib/types'
import { DirectoryField, DirectoryPicker } from './DirectoryPicker'
import './github-plan.css'

interface ImportDraft {
  selection: ImportSelection
  plan: ImportPlan | null
  edges: ImportEdge[]
  decisions: ImportDecision[]
  name: string
  directory: string | null
  payload: ImportPayload | null
  open?: boolean
}

const storageKey = 'aispace.github-import.v1'
const origins = dependencyOrigins
const pair = (edge: { source: number; target: number }) => `${edge.source}:${edge.target}`
const editableEdges = (edges: ImportEdge[]) => edges.map(({ source, target, explanation }) => ({ source, target, explanation }))
const messageOf = (error: unknown) => error instanceof Error ? error.message : 'Не удалось выполнить запрос.'
const record = (value: unknown): value is Record<string, unknown> => Boolean(value) && typeof value === 'object' && !Array.isArray(value)
const identifier = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value > 0
const textValue = (value: unknown): value is string => typeof value === 'string'
const githubUrl = (value: unknown) => { try { const url = new URL(String(value)); return url.protocol === 'https:' && url.hostname === 'github.com' && !url.username && !url.password } catch { return false } }
const boundedArray = (value: unknown, limit: number): value is unknown[] => Array.isArray(value) && value.length <= limit
const issueState = (value: unknown) => value === 'open' || value === 'closed'
const validBlockerIssue = (value: unknown) => record(value) && identifier(value.id) && identifier(value.number) && textValue(value.repository) && textValue(value.title) && issueState(value.state) && githubUrl(value.url)

function validSelection(value: unknown): value is ImportSelection {
  if (!record(value) || !textValue(value.id) || !value.id || !textValue(value.created_at) || !record(value.repository) || !identifier(value.repository.id) || !textValue(value.repository.full_name) || !githubUrl(value.repository.url) || typeof value.repository.private !== 'boolean' || !boundedArray(value.issues, 1000) || !value.issues.length) return false
  const ids = new Set<number>()
  return value.issues.every(issue => {
    if (!record(issue) || !identifier(issue.id) || ids.has(issue.id) || !identifier(issue.number) || !textValue(issue.title) || !textValue(issue.body) || !issueState(issue.state) || !githubUrl(issue.url) || !textValue(issue.updated_at) || !boundedArray(issue.labels, 1000) || !issue.labels.every(label => record(label) && textValue(label.name) && textValue(label.color)) || !record(issue.dependencies) || !['complete', 'unavailable'].includes(String(issue.dependencies.status)) || !boundedArray(issue.dependencies.blocked_by, 1000) || !issue.dependencies.blocked_by.every(validBlockerIssue)) return false
    ids.add(issue.id)
    return true
  })
}

function validEdges(value: unknown, selection: ImportSelection, withOrigin = true) {
  const ids = new Set(selection.issues.map(issue => issue.id))
  return boundedArray(value, 10000) && value.every(edge => record(edge) && identifier(edge.source) && identifier(edge.target) && ids.has(edge.source) && ids.has(edge.target) && textValue(edge.explanation) && edge.explanation.length <= 2000 && (!withOrigin || Object.hasOwn(origins, String(edge.origin))))
}

function validDecisions(value: unknown) {
  return boundedArray(value, 10000) && value.every(item => record(item) && ['external_completed', 'ignore_github'].includes(String(item.kind)) && identifier(item.source) && identifier(item.target) && textValue(item.reason) && item.reason.length <= 2000)
}

function validPlan(value: unknown, selection: ImportSelection) {
  return record(value) && textValue(value.id) && value.id && value.selection_id === selection.id && ['running', 'completed', 'failed', 'cancelled'].includes(String(value.status)) && validSelection(value.selection) && value.selection.id === selection.id && validEdges(value.edges, selection) && record(value.positions) && Object.values(value.positions).every(position => record(position) && typeof position.x === 'number' && Number.isFinite(position.x) && typeof position.y === 'number' && Number.isFinite(position.y)) && boundedArray(value.external_blockers, 10000) && value.external_blockers.every(blocker => record(blocker) && identifier(blocker.source) && identifier(blocker.target) && validBlockerIssue(blocker.issue)) && (value.error === null || textValue(value.error))
}

export function savedGitHubImport(): ImportDraft | null {
  try {
    const value: unknown = JSON.parse(sessionStorage.getItem(storageKey) || 'null')
    if (!record(value) || !validSelection(value.selection) || !validEdges(value.edges, value.selection) || !validDecisions(value.decisions) || !textValue(value.name) || value.name.length > 200 || !(value.directory === null || textValue(value.directory)) || !(value.plan === null || validPlan(value.plan, value.selection))) return null
    if (value.payload !== null) {
      const payload = value.payload
      if (!record(payload) || !record(value.plan) || payload.plan_id !== value.plan.id || !textValue(payload.operation_id) || payload.operation_id.length < 16 || !textValue(payload.name) || !(payload.working_directory === null || textValue(payload.working_directory)) || !validEdges(payload.edges, value.selection, false) || !validDecisions(payload.decisions)) return null
    }
    return value as unknown as ImportDraft
  } catch { return null }
}

function persist(draft: ImportDraft, required = false) {
  try { sessionStorage.setItem(storageKey, JSON.stringify(draft)) }
  catch { if (required) throw new Error('Не удалось сохранить ключ операции в браузере. Освободите хранилище вкладки и повторите создание.') }
}

function forgetImport() { try { sessionStorage.removeItem(storageKey) } catch { return } }

type IssueNode = Node<{ issue: ImportSelection['issues'][number] }, 'issue'>

function PlanIssue({ data }: NodeProps<IssueNode>) {
  return <div className="import-issue-node"><Handle type="target" position={Position.Left} /><span>#{data.issue.number} · {data.issue.state === 'open' ? 'Открыта' : 'Закрыта'}</span><strong title={data.issue.title}>{data.issue.title}</strong><a href={data.issue.url} target="_blank" rel="noopener noreferrer" className="nodrag nopan" aria-label={`Открыть исходную issue #${data.issue.number}`}>GitHub<ExternalLink size={12} /></a><Handle type="source" position={Position.Right} /></div>
}

const nodeTypes = { issue: PlanIssue }

export function GitHubPlan({ selection, settings, client, onSelection, onCreated, onConfigure, onBack, onClose }: { selection: ImportSelection; settings: Settings; client: ImportClient; onSelection: (selection: ImportSelection) => void; onCreated: (workspace: Workspace) => void; onConfigure: () => void; onBack: () => void; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const graphElement = useRef<HTMLDivElement>(null)
  const flow = useRef<ReactFlowInstance<IssueNode> | null>(null)
  const [initial] = useState(() => { const saved = savedGitHubImport(); return saved?.selection.id === selection.id ? saved : null })
  const [plan, setPlan] = useState<ImportPlan | null>(initial?.plan || null)
  const [edges, setEdges] = useState<ImportEdge[]>(initial?.edges || [])
  const [decisions, setDecisions] = useState<ImportDecision[]>(initial?.decisions || [])
  const [name, setName] = useState(initial?.name ?? selection.repository.full_name)
  const [directory, setDirectory] = useState<string | null>(initial?.directory || null)
  const [payload, setPayload] = useState<ImportPayload | null>(initial?.payload || null)
  const [positions, setPositions] = useState<ImportGraph['positions']>(initial?.plan?.positions || {})
  const [pending, setPending] = useState<'start' | 'cancel' | 'selection' | 'import' | null>(null)
  const [validation, setValidation] = useState<'pending' | 'valid' | 'invalid'>('pending')
  const [error, setError] = useState('')
  const [validationError, setValidationError] = useState('')
  const [fresh, setFresh] = useState(!initial?.plan)
  const [picker, setPicker] = useState(false)
  const [selectedEdge, setSelectedEdge] = useState<string | null>(null)
  const [source, setSource] = useState('')
  const [target, setTarget] = useState('')
  const [explanation, setExplanation] = useState('')
  const [decision, setDecision] = useState<Omit<ImportDecision, 'reason'> | null>(null)
  const [reason, setReason] = useState('')
  const alive = useRef(false)
  const started = useRef(false)
  const generation = useRef(0)
  const starting = useRef<Promise<ImportPlan> | null>(null)
  const importing = useRef(false)
  const cancelled = useRef(false)
  const state = useRef<ImportDraft>({ selection, plan, edges, decisions, name, directory, payload })
  const clientRef = useRef(client)
  clientRef.current = client
  state.current = { selection, plan, edges, decisions, name, directory, payload }
  const running = plan?.status === 'running' || pending === 'start'
  const frozen = Boolean(payload) || Boolean(pending) || running || !fresh
  const selected = edges.find(edge => pair(edge) === selectedEdge)
  const unresolved = (plan?.external_blockers || []).filter(blocker => !decisions.some(item => item.kind === 'external_completed' && pair(item) === pair(blocker)))
  const issueName = (id: number) => { const issue = selection.issues.find(item => item.id === id); return issue ? `#${issue.number} ${issue.title}` : `Issue ${id}` }

  function acceptPlan(value: ImportPlan, replace = true) {
    setPlan(value)
    setPositions(value.positions)
    if (replace) { setEdges(value.edges); setDecisions([]) }
    setFresh(true)
    setError(value.error || '')
  }

  async function analyze(mode: 'ai' | 'known' = 'ai') {
    if (state.current.payload || starting.current) return
    if (mode === 'ai' && settings.execution_mode === 'api' && (!settings.api_key_configured || !settings.model.trim())) {
      setError('Настройте API-ключ и модель перед анализом. Выбранные issues сохранены.')
      return
    }
    const id = ++generation.current
    cancelled.current = false
    setPending('start')
    setError('')
    setValidationError('')
    setPlan(null)
    setSelectedEdge(null)
    setDecision(null)
    setReason('')
    const request = clientRef.current.createPlan(selection.id, mode)
    starting.current = request
    try {
      const value = await request
      if (!alive.current || id !== generation.current || cancelled.current) return
      acceptPlan(value)
    } catch (failure) { if (alive.current && id === generation.current && !cancelled.current) setError(messageOf(failure)) }
    finally { if (starting.current === request) starting.current = null; if (alive.current && id === generation.current && !cancelled.current) setPending(null) }
  }

  useEffect(() => {
    alive.current = true
    dialog.current?.showModal()
    if (!started.current) {
      started.current = true
      if (initial?.plan) {
        const id = ++generation.current
        void clientRef.current.getPlan(initial.plan.id).then(value => { if (alive.current && generation.current === id) acceptPlan(value, initial.plan?.status !== 'completed') }).catch(failure => { if (alive.current && generation.current === id) { setError(messageOf(failure)); setFresh(true) } })
      } else if (!initial?.payload) void analyze()
    }
    return () => { alive.current = false }
  }, [])

  useEffect(() => { persist({ selection, plan, edges, decisions, name, directory, payload }) }, [selection, plan, edges, decisions, name, directory, payload])

  useEffect(() => {
    if (plan?.status !== 'running' || pending === 'cancel') return
    let active = true
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      try {
        const value = await clientRef.current.getPlan(plan!.id)
        if (!active) return
        acceptPlan(value)
        if (value.status === 'running') timer = setTimeout(() => void poll(), 500)
      } catch (failure) { if (active) { setError(messageOf(failure)); timer = setTimeout(() => void poll(), 1500) } }
    }
    timer = setTimeout(() => void poll(), 300)
    return () => { active = false; clearTimeout(timer) }
  }, [plan?.id, plan?.status, pending])

  useEffect(() => {
    if (plan?.status !== 'completed' || !fresh || payload) return
    let active = true
    setValidation('pending')
    setValidationError('')
    const timer = setTimeout(() => {
      void clientRef.current.validatePlan(plan.id, editableEdges(edges), decisions).then(graph => {
        if (!active) return
        setPositions(graph.positions)
        setValidation('valid')
      }).catch(failure => { if (active) { setValidation('invalid'); setValidationError(messageOf(failure)) } })
    }, 200)
    return () => { active = false; clearTimeout(timer) }
  }, [plan?.id, plan?.status, edges, decisions, fresh, payload])

  const layoutKey = JSON.stringify(positions)
  useEffect(() => {
    if (!graphElement.current) return
    let frame = 0
    const fit = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(() => { void flow.current?.fitView({ padding: 0.2, maxZoom: 1 }) }) }
    const observer = new ResizeObserver(fit)
    observer.observe(graphElement.current)
    fit()
    return () => { observer.disconnect(); cancelAnimationFrame(frame) }
  }, [plan?.id, plan?.status, layoutKey])

  async function dismiss(next: () => void, discard: boolean) {
    if (pending === 'import' || pending === 'selection' || pending === 'cancel') return
    if (running) {
      cancelled.current = true
      setPending('cancel')
      try {
        const active = starting.current ? await starting.current : plan
        if (active) await clientRef.current.cancelPlan(active.id)
      } catch (failure) { if (alive.current) { setError(messageOf(failure)); setPending(null) }; return }
      if (!alive.current) return
    }
    if (discard && !state.current.payload) forgetImport()
    else persist({ ...state.current, open: false })
    next()
  }

  async function changeSelection(issues = selection.issues.map(({ id, number }) => ({ id, number }))) {
    setPending('selection')
    setError('')
    try {
      const value = await clientRef.current.selectIssues(selection, issues)
      if (!alive.current) return
      persist({ selection: value, plan: null, edges: [], decisions: [], name, directory, payload: null })
      onSelection(value)
    } catch (failure) { if (alive.current) setError(messageOf(failure)) }
    finally { if (alive.current) setPending(null) }
  }

  function editEdges(next: ImportEdge[]) { setValidation('pending'); setEdges(next) }

  function addEdge(connection: Connection | { source: string; target: string }) {
    if (frozen || !connection.source || !connection.target) return
    const next = { source: Number(connection.source), target: Number(connection.target), origin: 'user' as const, explanation: explanation.trim() || 'Связь добавлена пользователем.' }
    if (edges.some(edge => pair(edge) === pair(next))) { setError('Такая связь уже есть.'); return }
    setError('')
    editEdges([...edges, next])
    setSelectedEdge(pair(next))
    setExplanation('')
  }

  function removeEdge(edge: ImportEdge) {
    if (edge.origin === 'github') { setDecision({ kind: 'ignore_github', source: edge.source, target: edge.target }); setReason(''); return }
    editEdges(edges.filter(item => pair(item) !== pair(edge)))
    setSelectedEdge(null)
  }

  function confirmDecision() {
    if (!decision || !reason.trim()) return
    setValidation('pending')
    setDecisions([...decisions.filter(item => !(item.kind === decision.kind && pair(item) === pair(decision))), { ...decision, reason: reason.trim() }])
    if (decision.kind === 'ignore_github') editEdges(edges.filter(edge => pair(edge) !== pair(decision)))
    setDecision(null)
    setReason('')
    setSelectedEdge(null)
  }

  async function create() {
    if (pending || importing.current) return
    importing.current = true
    setPending('import')
    setError('')
    let submitted = payload
    try {
      if (!submitted) {
        if (!plan || plan.status !== 'completed' || !name.trim()) return
        const graph = await clientRef.current.validatePlan(plan.id, editableEdges(edges), decisions)
        if (graph.external_blockers.length) throw new Error('Сначала разрешите внешние зависимости.')
        submitted = { plan_id: plan.id, operation_id: crypto.randomUUID(), name: name.trim(), working_directory: directory, edges: editableEdges(edges), decisions }
        persist({ ...state.current, payload: submitted }, true)
        setPayload(submitted)
        state.current = { ...state.current, payload: submitted }
      }
      const workspace = await clientRef.current.createImport(submitted)
      forgetImport()
      if (alive.current) onCreated(workspace)
    } catch (failure) {
      if (!alive.current) return
      const status = (failure as { status?: number }).status
      if (status && status < 500) {
        setPayload(null)
        persist({ ...state.current, payload: null })
        state.current = { ...state.current, payload: null }
      }
      setError(messageOf(failure))
    } finally { importing.current = false; if (alive.current) setPending(null) }
  }

  const nodes = useMemo<IssueNode[]>(() => selection.issues.map((issue, index) => ({ id: String(issue.id), type: 'issue', position: positions[String(issue.id)] || { x: 80, y: 80 + index * 220 }, data: { issue }, ariaLabel: `Issue #${issue.number}: ${issue.title}` })), [selection.issues, positions])
  const flowEdges = useMemo(() => edges.map(edge => ({ id: pair(edge), source: String(edge.source), target: String(edge.target), type: 'smoothstep', selected: selectedEdge === pair(edge), markerEnd: { type: MarkerType.ArrowClosed }, ariaLabel: `${issueName(edge.source)} → ${issueName(edge.target)} · ${origins[edge.origin]}`, style: { stroke: edge.origin === 'github' ? '#7465a5' : '#aaa0b8', strokeWidth: edge.origin === 'github' ? 2 : 1.5 } })), [edges, selectedEdge, selection.issues])

  return <dialog ref={dialog} className="modal github-plan-modal" aria-labelledby="github-plan-title" onCancel={event => { event.preventDefault(); void dismiss(onClose, true) }}>
    <div className="modal-heading"><div><h2 id="github-plan-title">Граф из GitHub Issues</h2><p className="import-plan-repository">{selection.repository.full_name} · {selection.issues.length} issues</p></div><button className="icon-button" aria-label="Закрыть построение графа" disabled={pending === 'import' || pending === 'selection' || pending === 'cancel'} onClick={() => void dismiss(onClose, true)}><X size={18} /></button></div>
    {error && <div className="notice error-notice" role="alert">{error}</div>}
    {payload && <div className="notice" role="status">{pending === 'import' ? 'Создаём пространство…' : 'Результат создания пока не подтверждён. Повторите тот же запрос: второе пространство не появится.'}</div>}
    {running || pending === 'cancel' ? <div className="import-analysis" role="status"><LoaderCircle size={25} className="spin" /><h3>{pending === 'cancel' ? 'Отменяем анализ…' : 'Анализируем зависимости…'}</h3><button className="button button-secondary" disabled={pending === 'cancel'} onClick={() => void dismiss(onClose, true)}>Отменить анализ</button></div> : plan?.status !== 'completed' ? <div className="import-plan-recovery"><button className="button button-primary" onClick={() => void analyze()} disabled={Boolean(pending) || Boolean(payload)}><RefreshCw size={14} />Повторить анализ</button><button className="button button-secondary" onClick={() => void analyze('known')} disabled={Boolean(pending) || Boolean(payload)}>Использовать только известные зависимости</button><button className="text-button" onClick={() => void dismiss(onConfigure, false)} disabled={Boolean(payload)}><Settings2 size={14} />Настроить подключение AI</button></div> : <>
      <div ref={graphElement} className="import-graph" aria-label="Предпросмотр графа GitHub"><ReactFlow<IssueNode> key={plan.id} onInit={instance => { flow.current = instance }} nodes={nodes} edges={flowEdges} nodeTypes={nodeTypes} onEdgeClick={(_event, edge) => setSelectedEdge(edge.id)} onPaneClick={() => setSelectedEdge(null)} onConnect={addEdge} nodesDraggable={false} nodesConnectable={!frozen} edgesReconnectable={false} deleteKeyCode={null} ariaLabelConfig={{ 'controls.zoomIn.ariaLabel': 'Увеличить масштаб', 'controls.zoomOut.ariaLabel': 'Уменьшить масштаб', 'controls.fitView.ariaLabel': 'Уместить граф' }} fitView fitViewOptions={{ padding: 0.2, maxZoom: 1 }} minZoom={0.15} maxZoom={1.5} proOptions={{ hideAttribution: true }}><Background variant={BackgroundVariant.Dots} gap={22} size={1} color="#ddd9e2" /><Controls showInteractive={false} /></ReactFlow></div>
      <div className="import-plan-columns"><section className="import-edges" aria-label="Связи плана"><h3><GitBranch size={16} />Связи · {edges.length}</h3>{edges.length ? <div className="import-edge-list">{edges.map(edge => <button key={pair(edge)} className={`import-edge-row ${selectedEdge === pair(edge) ? 'selected' : ''}`} onClick={() => setSelectedEdge(pair(edge))}><span>{issueName(edge.source)} → {issueName(edge.target)}</span><small>{origins[edge.origin]}</small></button>)}</div> : <p className="field-help">Задачи независимы.</p>}
        {selected && <div className="import-edge-details"><strong>{origins[selected.origin]}</strong><label className="field">Основание связи<textarea value={selected.explanation} maxLength={2000} disabled={frozen || selected.origin === 'github'} onChange={event => editEdges(edges.map(edge => pair(edge) === pair(selected) ? { ...edge, origin: 'user', explanation: event.target.value } : edge))} /></label><button className="text-button danger-text" disabled={frozen} onClick={() => removeEdge(selected)}><Trash2 size={13} />{selected.origin === 'github' ? 'Исключить исходную зависимость' : 'Удалить связь'}</button></div>}
        <form className="import-add-edge" onSubmit={event => { event.preventDefault(); addEdge({ source, target }) }}><label className="field">Предшествующая issue<select value={source} onChange={event => setSource(event.target.value)} disabled={frozen}><option value="">Выбрать issue</option>{selection.issues.map(issue => <option key={issue.id} value={issue.id}>#{issue.number} {issue.title}</option>)}</select></label><label className="field">Зависимая issue<select value={target} onChange={event => setTarget(event.target.value)} disabled={frozen}><option value="">Выбрать issue</option>{selection.issues.map(issue => <option key={issue.id} value={issue.id}>#{issue.number} {issue.title}</option>)}</select></label><label className="field">Объяснение новой связи<input value={explanation} onChange={event => setExplanation(event.target.value)} maxLength={2000} disabled={frozen} /></label><button className="button button-secondary" disabled={frozen || !source || !target}><Plus size={14} />Добавить связь</button></form>
      </section><section className="import-destination" aria-label="Новое пространство"><h3>Новое пространство</h3><label className="field">Название пространства<input value={name} maxLength={200} onChange={event => setName(event.target.value)} disabled={frozen} /></label><DirectoryField label="Рабочая папка нового пространства" value={directory} disabled={frozen} onOpen={() => setPicker(true)} onClear={() => setDirectory(null)} /><p className="field-help">Тасклеты создаются с пустыми чатами. Запуск — отдельным действием.</p>{validation === 'pending' && !payload && <p className="import-validation" role="status"><LoaderCircle size={13} className="spin" />Проверяем граф…</p>}{validationError && <div className="notice error-notice" role="alert">{validationError}</div>}</section></div>
      {unresolved.length > 0 && <section className="import-blockers" aria-label="Внешние зависимости"><h3>Внешние зависимости · {unresolved.length}</h3>{unresolved.map(blocker => <article key={pair(blocker)}><strong>{issueName(blocker.target)}</strong><p>Ожидает <a href={blocker.issue.url} target="_blank" rel="noopener noreferrer">{blocker.issue.repository}#{blocker.issue.number}: {blocker.issue.title}</a> · {blocker.issue.state === 'open' ? 'Открыта' : 'Закрыта'}</p><div className="import-blocker-actions">{blocker.issue.repository.toLowerCase() === selection.repository.full_name.toLowerCase() && <button className="button button-secondary" disabled={frozen} onClick={() => void changeSelection([...selection.issues.map(({ id, number }) => ({ id, number })), { id: blocker.issue.id, number: blocker.issue.number }])}>Добавить issue #{blocker.issue.number}</button>}<button className="button button-secondary" disabled={frozen} onClick={() => { setDecision({ kind: 'external_completed', source: blocker.source, target: blocker.target }); setReason('') }}>Отметить выполненной</button><button className="text-button danger-text" disabled={frozen || selection.issues.length === 1} onClick={() => void changeSelection(selection.issues.filter(issue => issue.id !== blocker.target).map(({ id, number }) => ({ id, number })))}>Исключить зависимую issue #{selection.issues.find(issue => issue.id === blocker.target)?.number}</button></div>{selection.issues.length === 1 && <p>Последнюю issue исключить нельзя. Вернитесь к выбору, чтобы изменить набор.</p>}</article>)}</section>}
      {decisions.length > 0 && <section className="import-decisions" aria-label="Решения о зависимостях"><h3>Решения о зависимостях</h3>{decisions.map(item => <div key={`${item.kind}:${pair(item)}`}><p><strong>{item.kind === 'external_completed' ? 'Предпосылка выполнена вне пространства' : 'Исходная зависимость исключена'}</strong><br />{item.reason}</p><button className="text-button" disabled={frozen} onClick={() => { setValidation('pending'); setDecisions(decisions.filter(value => value !== item)); const original = plan.edges.find(edge => pair(edge) === pair(item)); if (item.kind === 'ignore_github' && original) editEdges([...edges, original]) }}>Отменить решение</button></div>)}</section>}
      {decision && <form className="import-decision-confirmation" onSubmit={event => { event.preventDefault(); confirmDecision() }}><h3>{decision.kind === 'ignore_github' ? 'Исключить исходную зависимость?' : 'Подтвердить выполнение предпосылки?'}</h3><p>{decision.kind === 'ignore_github' ? 'Задача сможет запускаться без ожидания исходной зависимости GitHub. Сама issue в GitHub не изменится.' : 'Подтвердите, что блокирующая задача выполнена вне этого пространства. Закрытое состояние GitHub само по себе не подтверждает выполнение.'}</p><label className="field">Причина решения<textarea autoFocus value={reason} onChange={event => setReason(event.target.value)} required maxLength={2000} /></label><div><button type="button" className="button button-secondary" onClick={() => setDecision(null)}>Отмена</button><button className="button button-primary" disabled={!reason.trim()}><Check size={14} />Подтвердить решение</button></div></form>}
    </>}
    <div className="modal-actions import-plan-actions"><button className="button button-secondary" disabled={Boolean(pending) || Boolean(payload)} onClick={() => void dismiss(onBack, true)}><ArrowLeft size={14} />Вернуться к выбору</button><button className="text-button" disabled={frozen} onClick={() => void changeSelection()}><RefreshCw size={13} />Перечитать issues</button>{plan?.status === 'completed' && !payload && <button className="text-button" disabled={frozen} onClick={() => void analyze()}>Повторить анализ</button>}<button className="button button-primary" onClick={() => void create()} disabled={Boolean(pending) || (!payload && (plan?.status !== 'completed' || validation !== 'valid' || Boolean(unresolved.length) || !name.trim() || !fresh || Boolean(decision)))}>{pending === 'import' ? <LoaderCircle size={14} className="spin" /> : <Check size={14} />}{payload && pending !== 'import' ? 'Повторить создание' : 'Создать пространство'}</button></div>
    {picker && <DirectoryPicker value={directory} onClose={() => setPicker(false)} onSelect={path => { setDirectory(path); setPicker(false) }} />}
  </dialog>
}
