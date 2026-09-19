import type { CodexLogin, CodexStatus, Dependency, DirectoryCapabilities, DirectoryListing, Message, Pipeline, Settings, SettingsUpdate, Tasklet, Workspace } from './types'

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
    throw new Error(detail)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

const body = (value: unknown) => JSON.stringify(value)

export const api = {
  workspace: () => request<Workspace>('/workspace'),
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
  createTasklet: (value: Pick<Tasklet, 'title' | 'prompt' | 'position'>) => request<Tasklet>('/tasklets', { method: 'POST', body: body(value) }),
  updateTasklet: (id: string, value: Partial<Pick<Tasklet, 'title' | 'prompt' | 'model' | 'position' | 'working_directory'>>) => request<Tasklet>(`/tasklets/${id}`, { method: 'PATCH', body: body(value) }),
  deleteTasklet: (id: string) => request<void>(`/tasklets/${id}`, { method: 'DELETE' }),
  createEdge: (source: string, target: string) => request<Dependency>('/edges', { method: 'POST', body: body({ source, target, pass_context: false }) }),
  updateEdge: (id: string, pass_context: boolean) => request<Dependency>(`/edges/${id}`, { method: 'PATCH', body: body({ pass_context }) }),
  deleteEdge: (id: string) => request<void>(`/edges/${id}`, { method: 'DELETE' }),
  messages: (id: string) => request<Message[]>(`/tasklets/${id}/messages`),
  sendMessage: (id: string, content: string) => request<Pipeline>(`/tasklets/${id}/messages`, { method: 'POST', body: body({ content }) }),
  start: (ids?: string[]) => request<Pipeline>('/pipeline/start', { method: 'POST', body: body(ids ? { tasklet_ids: ids } : {}) }),
  stop: () => request<Pipeline>('/pipeline/stop', { method: 'POST' }),
}

export function subscribe(handlers: {
  workspace: (value: Workspace) => void
  message: (value: Message) => void
  settings: (value: Settings) => void
  connection: (connected: boolean) => void
}) {
  const source = new EventSource('/api/events')
  source.onopen = () => handlers.connection(true)
  source.onerror = () => handlers.connection(false)
  source.addEventListener('workspace', event => handlers.workspace(JSON.parse(event.data)))
  source.addEventListener('message', event => handlers.message(JSON.parse(event.data)))
  source.addEventListener('settings', event => handlers.settings(JSON.parse(event.data)))
  return () => source.close()
}
