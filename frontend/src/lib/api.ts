import type { ChatReset, CodexLogin, CodexStatus, Dependency, DirectoryCapabilities, DirectoryListing, Message, Pipeline, Settings, SettingsUpdate, Tasklet, Workspace, WorkspaceSummary } from './types'

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`/api${path}`, {
      ...options,
      headers: { 'Content-Type': 'application/json', ...options?.headers },
    })
  } catch {
    throw new Error('Нет соединения с сервером. Проверьте, что backend запущен.')
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    const detail = typeof body.detail === 'string' ? body.detail : 'Не удалось выполнить запрос'
    throw new ApiError(detail, response.status)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message) }
}

const body = (value: unknown) => JSON.stringify(value)

export const api = {
  workspaces: () => request<WorkspaceSummary[]>('/workspaces'),
  createWorkspace: (name: string) => request<Workspace>('/workspaces', { method: 'POST', body: body({ name }) }),
  workspace: (wid: string) => request<Workspace>(`/workspaces/${wid}`),
  updateWorkspace: (wid: string, value: Partial<Pick<Workspace, 'name' | 'workspace_context' | 'working_directory'>>) => request<Workspace>(`/workspaces/${wid}`, { method: 'PATCH', body: body(value) }),
  deleteWorkspace: (wid: string) => request<void>(`/workspaces/${wid}`, { method: 'DELETE' }),
  settings: () => request<Settings>('/settings'),
  updateSettings: (value: SettingsUpdate) => request<Settings>('/settings', { method: 'PATCH', body: body(value) }),
  testSettings: () => request<{ ok: boolean; message: string }>('/settings/test', { method: 'POST' }),
  codexStatus: () => request<CodexStatus>('/codex/status'),
  codexLogin: () => request<CodexLogin>('/codex/login', { method: 'POST' }),
  cancelCodexLogin: (login_id: string) => request<void>('/codex/login/cancel', { method: 'POST', body: body({ login_id }) }),
  codexLogout: () => request<void>('/codex/logout', { method: 'POST' }),
  directories: (path?: string) => request<DirectoryListing>(`/directories${path ? `?path=${encodeURIComponent(path)}` : ''}`),
  directoryCapabilities: () => request<DirectoryCapabilities>('/directories/capabilities'),
  chooseDirectory: (path?: string | null) => request<{ path: string | null }>('/directories/choose', { method: 'POST', body: body({ path: path || null }) }),
  createTasklet: (wid: string, value: Pick<Tasklet, 'title' | 'prompt' | 'position'>) => request<Tasklet>(`/workspaces/${wid}/tasklets`, { method: 'POST', body: body(value) }),
  updateTasklet: (wid: string, id: string, value: Partial<Pick<Tasklet, 'title' | 'prompt' | 'model' | 'position' | 'working_directory'>>) => request<Tasklet>(`/workspaces/${wid}/tasklets/${id}`, { method: 'PATCH', body: body(value) }),
  deleteTasklet: (wid: string, id: string) => request<void>(`/workspaces/${wid}/tasklets/${id}`, { method: 'DELETE' }),
  createEdge: (wid: string, source: string, target: string) => request<Dependency>(`/workspaces/${wid}/edges`, { method: 'POST', body: body({ source, target, pass_context: false }) }),
  updateEdge: (wid: string, id: string, pass_context: boolean) => request<Dependency>(`/workspaces/${wid}/edges/${id}`, { method: 'PATCH', body: body({ pass_context }) }),
  deleteEdge: (wid: string, id: string) => request<void>(`/workspaces/${wid}/edges/${id}`, { method: 'DELETE' }),
  messages: (wid: string, id: string) => request<Message[]>(`/workspaces/${wid}/tasklets/${id}/messages`),
  sendMessage: (wid: string, id: string, content: string) => request<Pipeline>(`/workspaces/${wid}/tasklets/${id}/messages`, { method: 'POST', body: body({ content }) }),
  start: (wid: string, ids?: string[]) => request<Pipeline>(`/workspaces/${wid}/pipeline/start`, { method: 'POST', body: body(ids ? { tasklet_ids: ids } : {}) }),
  stop: (wid: string) => request<Pipeline>(`/workspaces/${wid}/pipeline/stop`, { method: 'POST' }),
}

export function subscribe(handlers: {
  workspaces: (value: WorkspaceSummary[]) => void
  workspace: (value: Workspace) => void
  message: (value: Message) => void
  chatReset: (value: ChatReset) => void
  settings: (value: Settings) => void
  connection: (connected: boolean) => void
}) {
  const source = new EventSource('/api/events')
  source.onopen = () => handlers.connection(true)
  source.onerror = () => handlers.connection(false)
  source.addEventListener('workspaces', event => handlers.workspaces(JSON.parse(event.data)))
  source.addEventListener('workspace', event => handlers.workspace(JSON.parse(event.data)))
  source.addEventListener('message', event => handlers.message(JSON.parse(event.data)))
  source.addEventListener('chat_reset', event => handlers.chatReset(JSON.parse(event.data)))
  source.addEventListener('settings', event => handlers.settings(JSON.parse(event.data)))
  return () => source.close()
}
