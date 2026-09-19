import { useSyncExternalStore } from 'react'
import type { Settings, Tasklet, Workspace } from './types'

export type GlobalDraft = Partial<Omit<Settings, 'api_key_configured'>>
export type WorkspaceDraft = Partial<Pick<Workspace, 'workspace_context' | 'working_directory'>>
export type TaskletDraft = Partial<Pick<Tasklet, 'title' | 'prompt' | 'model' | 'working_directory'>>
export interface DirectoryUi { currentPath: string | null; pathInput: string }
export interface TaskletUi {
  tab: 'task' | 'chat'
  draft: TaskletDraft
  message: string
  source: string
  taskScroll: number
  chatScroll: number
  chatAtBottom: boolean
  picker: DirectoryUi | null
}
export interface SettingsUi {
  globalDraft: GlobalDraft
  workspaceDraft: WorkspaceDraft
  advancedOpen: boolean
  scroll: number
  picker: DirectoryUi | null
}
export interface WorkspaceUi {
  page: 'workspace' | 'settings'
  selectedId: string | null
  selectedEdgeId: string | null
  viewport: { x: number; y: number; zoom: number } | null
  creating: boolean
  createDraft: { title: string; prompt: string }
  tasklets: Record<string, TaskletUi>
  settings: SettingsUi
}
interface UiState {
  version: 1
  activeWorkspaceId: string | null
  sidebarCollapsed: boolean
  workspaces: Record<string, WorkspaceUi>
}

const storageKey = 'aispace.ui.v1'
const listeners = new Set<() => void>()
let storageError = false
const blankTasklet: TaskletUi = { tab: 'task', draft: {}, message: '', source: '', taskScroll: 0, chatScroll: 0, chatAtBottom: true, picker: null }
const blankSettings: SettingsUi = { globalDraft: {}, workspaceDraft: {}, advancedOpen: false, scroll: 0, picker: null }
export const blankWorkspaceUi: WorkspaceUi = { page: 'workspace', selectedId: null, selectedEdgeId: null, viewport: null, creating: false, createDraft: { title: '', prompt: '' }, tasklets: {}, settings: blankSettings }
const object = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const string = (value: unknown, fallback = '') => typeof value === 'string' ? value : fallback
const nullable = (value: unknown) => typeof value === 'string' && value ? value : null
const number = (value: unknown, fallback = 0) => typeof value === 'number' && Number.isFinite(value) ? value : fallback
function directory(value: unknown): DirectoryUi | null {
  if (!value || typeof value !== 'object') return null
  const item = object(value)
  return { currentPath: nullable(item.currentPath), pathInput: string(item.pathInput) }
}
function textFields(value: unknown, names: string[], nullables: string[] = []) {
  const result: Record<string, string | null> = {}
  const data = object(value)
  for (const name of names) if (typeof data[name] === 'string' || (data[name] === null && nullables.includes(name))) result[name] = data[name] as string | null
  return result
}
function cleanWorkspace(value: unknown): WorkspaceUi {
  const item = object(value)
  const settings = object(item.settings)
  const global = object(settings.globalDraft)
  const globalDraft: GlobalDraft = textFields(global, ['model', 'base_url'])
  if (global.execution_mode === 'api' || global.execution_mode === 'codex') globalDraft.execution_mode = global.execution_mode
  if (global.codex_sandbox === 'read-only' || global.codex_sandbox === 'workspace-write') globalDraft.codex_sandbox = global.codex_sandbox
  if (typeof global.max_parallel === 'number' && Number.isInteger(global.max_parallel) && global.max_parallel >= 1 && global.max_parallel <= 8) globalDraft.max_parallel = global.max_parallel
  const tasklets: Record<string, TaskletUi> = {}
  for (const [id, value] of Object.entries(object(item.tasklets))) {
    const tasklet = object(value)
    tasklets[id] = { ...blankTasklet, tab: tasklet.tab === 'chat' ? 'chat' : 'task', draft: textFields(tasklet.draft, ['title', 'prompt', 'model', 'working_directory'], ['model', 'working_directory']), message: string(tasklet.message), source: string(tasklet.source), taskScroll: Math.max(0, number(tasklet.taskScroll)), chatScroll: Math.max(0, number(tasklet.chatScroll)), chatAtBottom: tasklet.chatAtBottom !== false, picker: directory(tasklet.picker) }
  }
  const viewport = object(item.viewport)
  const zoom = number(viewport.zoom)
  const create = object(item.createDraft)
  return { ...blankWorkspaceUi, page: item.page === 'settings' ? 'settings' : 'workspace', selectedId: nullable(item.selectedId), selectedEdgeId: nullable(item.selectedEdgeId), viewport: zoom >= 0.25 && zoom <= 1.75 ? { x: number(viewport.x), y: number(viewport.y), zoom } : null, creating: item.creating === true, createDraft: { title: string(create.title), prompt: string(create.prompt) }, tasklets, settings: { globalDraft, workspaceDraft: textFields(settings.workspaceDraft, ['workspace_context', 'working_directory'], ['working_directory']), advancedOpen: settings.advancedOpen === true, scroll: Math.max(0, number(settings.scroll)), picker: directory(settings.picker) } }
}
function read(): UiState {
  const fallback: UiState = { version: 1, activeWorkspaceId: null, sidebarCollapsed: false, workspaces: {} }
  try {
    const raw = sessionStorage.getItem(storageKey)
    if (!raw) return fallback
    let parsed: Record<string, unknown>
    try { parsed = object(JSON.parse(raw)) } catch { return fallback }
    if (parsed.version !== 1) return fallback
    return { version: 1, activeWorkspaceId: nullable(parsed.activeWorkspaceId), sidebarCollapsed: parsed.sidebarCollapsed === true, workspaces: Object.fromEntries(Object.entries(object(parsed.workspaces)).map(([id, item]) => [id, cleanWorkspace(item)])) }
  } catch { storageError = true; return fallback }
}
let state = read()
export function getUiState() { return state }
export function uiStorageUnavailable() { return storageError }
export function updateUi(change: (current: UiState) => UiState) {
  const next = change(state)
  if (next === state) return
  state = next
  try { sessionStorage.setItem(storageKey, JSON.stringify(state)) } catch { storageError = true }
  for (const listener of listeners) listener()
}
export function useUiState() {
  return useSyncExternalStore(listener => { listeners.add(listener); return () => { listeners.delete(listener) } }, getUiState)
}
export function useWorkspaceUi(wid: string) { return useUiState().workspaces[wid] || blankWorkspaceUi }
export function workspaceUi(wid: string) { return state.workspaces[wid] || blankWorkspaceUi }
export function patchWorkspaceUi(wid: string, change: Partial<WorkspaceUi> | ((current: WorkspaceUi) => WorkspaceUi)) {
  updateUi(current => {
    const previous = current.workspaces[wid] || blankWorkspaceUi
    const next = typeof change === 'function' ? change(previous) : { ...previous, ...change }
    return { ...current, workspaces: { ...current.workspaces, [wid]: next } }
  })
}
export function taskletUi(wid: string, id: string) { return workspaceUi(wid).tasklets[id] || blankTasklet }
export function patchTaskletUi(wid: string, id: string, change: Partial<TaskletUi> | ((current: TaskletUi) => TaskletUi)) {
  patchWorkspaceUi(wid, current => {
    const previous = current.tasklets[id] || blankTasklet
    const next = typeof change === 'function' ? change(previous) : { ...previous, ...change }
    return { ...current, tasklets: { ...current.tasklets, [id]: next } }
  })
}
export function patchSettingsUi(wid: string, change: Partial<SettingsUi>) {
  patchWorkspaceUi(wid, current => ({ ...current, settings: { ...current.settings, ...change } }))
}
export function reconcileWorkspaceUi(workspace: Workspace) {
  const ui = state.workspaces[workspace.id]
  if (!ui) return
  const ids = new Set(workspace.tasklets.map(tasklet => tasklet.id))
  const selectedId = ui.selectedId && ids.has(ui.selectedId) ? ui.selectedId : null
  const selectedEdgeId = ui.selectedEdgeId && workspace.edges.some(edge => edge.id === ui.selectedEdgeId) ? ui.selectedEdgeId : null
  const tasklets = Object.fromEntries(Object.entries(ui.tasklets).filter(([id]) => ids.has(id)))
  if (selectedId !== ui.selectedId || selectedEdgeId !== ui.selectedEdgeId || Object.keys(tasklets).length !== Object.keys(ui.tasklets).length) patchWorkspaceUi(workspace.id, { selectedId, selectedEdgeId, tasklets })
}
export function reconcileWorkspaceList(ids: string[]) {
  const allowed = new Set([...ids, '__global__'])
  updateUi(current => {
    const activeWorkspaceId = current.activeWorkspaceId && ids.includes(current.activeWorkspaceId) ? current.activeWorkspaceId : ids[0] || null
    const workspaces = Object.fromEntries(Object.entries(current.workspaces).filter(([id]) => allowed.has(id)))
    if (activeWorkspaceId === current.activeWorkspaceId && Object.keys(workspaces).length === Object.keys(current.workspaces).length) return current
    return { ...current, activeWorkspaceId, workspaces }
  })
}

export function clearMatching<T extends object>(current: T, submitted: T): T {
  return Object.fromEntries(Object.entries(current).filter(([name, value]) => !(name in submitted) || value !== submitted[name as keyof T])) as T
}
