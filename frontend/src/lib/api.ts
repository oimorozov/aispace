import type { ChatReset, CodexLogin, CodexStatus, Dependency, DirectoryCapabilities, DirectoryListing, GitHubIssue, GitHubRepository, GitHubSelection, ImportClient, ImportDecision, ImportGraph, ImportPayload, ImportPlan, Message, Pipeline, Settings, SettingsUpdate, Tasklet, Workspace, WorkspaceSummary } from './types'

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
  githubConnection: () => request<{ token_configured: boolean }>('/github/connection'),
  updateGitHubConnection: (token: string | null) => request<{ token_configured: boolean }>('/github/connection', { method: 'PATCH', body: body({ token }) }),
  githubRepository: (repository: string) => request<{ repository: GitHubRepository; issues: GitHubIssue[] }>('/github/repository', { method: 'POST', body: body({ repository }) }),
  githubSelection: (repository: GitHubRepository, issues: Pick<GitHubIssue, 'id' | 'number'>[]) => request<GitHubSelection>('/github/selections', { method: 'POST', body: body({ repository: repository.full_name, repository_id: repository.id, issues: issues.map(({ id, number }) => ({ id, number })) }) }),
  getGitHubSelection: (id: string) => request<GitHubSelection>(`/github/selections/${id}`),
  createGitHubPlan: (selection_id: string, mode: 'ai' | 'known') => request<ImportPlan>('/github/plans', { method: 'POST', body: body({ selection_id, mode }) }),
  githubPlan: (id: string) => request<ImportPlan>(`/github/plans/${id}`),
  cancelGitHubPlan: (id: string) => request<ImportPlan>(`/github/plans/${id}`, { method: 'DELETE' }),
  validateGitHubPlan: (id: string, edges: ImportPayload['edges'], decisions: ImportDecision[]) => request<ImportGraph>(`/github/plans/${id}/validate`, { method: 'POST', body: body({ edges, decisions }) }),
  importGitHubPlan: (payload: ImportPayload) => request<Workspace>('/github/imports', { method: 'POST', body: body(payload) }),
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
  restart: (wid: string, id: string) => request<Pipeline>(`/workspaces/${wid}/tasklets/${id}/restart`, { method: 'POST' }),
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

export const githubImportClient: ImportClient = {
  createPlan: api.createGitHubPlan,
  getPlan: api.githubPlan,
  cancelPlan: api.cancelGitHubPlan,
  validatePlan: api.validateGitHubPlan,
  createImport: api.importGitHubPlan,
  selectIssues: (selection, issues) => request<GitHubSelection>(`/github/selections/${selection.id}/revise`, { method: 'POST', body: body({ issues }) }),
}
