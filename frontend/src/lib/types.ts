export type TaskletStatus = 'idle' | 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | 'blocked'

export interface Tasklet {
  id: string
  workspace_id: string
  conversation_id: string | null
  title: string
  prompt: string
  model: string | null
  working_directory: string | null
  status: TaskletStatus
  position: { x: number; y: number }
  created_at: string
  updated_at: string
  error: string | null
  last_output: string
  source?: { provider: 'github'; repository: GitHubRepository; issue: GitHubSelection['issues'][number]; imported_at: string } | null
}

export interface Dependency {
  id: string
  source: string
  target: string
  pass_context: boolean
  origin?: ImportEdge['origin'] | null
  explanation?: string | null
}

export interface Message {
  id: string
  workspace_id: string
  conversation_id: string | null
  tasklet_id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  created_at: string
  run_id: string | null
}

export interface ChatReset {
  workspace_id: string
  tasklet_ids: string[]
  conversation_id: string
}

export interface Pipeline {
  id: string | null
  status: 'idle' | 'running' | 'stopping' | 'completed' | 'failed' | 'cancelled'
  started_at: string | null
  finished_at: string | null
  total: number
  completed: number
  error: string | null
}

export interface WorkspaceSummary {
  id: string
  name: string
  created_at: string
  updated_at: string
  pipeline: Pipeline
}

export interface Workspace extends WorkspaceSummary {
  workspace_context: string
  working_directory: string | null
  tasklets: Tasklet[]
  edges: Dependency[]
}

export interface Settings {
  execution_mode: 'api' | 'codex'
  codex_sandbox: 'read-only' | 'workspace-write'
  api_key_configured: boolean
  base_url: string
  model: string
  max_parallel: number
}

export interface CodexStatus {
  available: boolean
  authenticated: boolean
  auth_type: string | null
  account_label: string | null
  message: string
  capabilities: { workspace: boolean; commands: string[] }
}

export interface CodexLogin {
  login_id: string
  auth_url: string
  user_code: string | null
}

export interface DirectoryListing {
  path: string
  parent: string | null
  entries: { name: string; path: string }[]
  roots: string[]
}

export interface DirectoryCapabilities {
  native_picker: boolean
  platform: string
}

export type SettingsUpdate = Partial<Omit<Settings, 'api_key_configured'>> & { api_key?: string | null }

export interface GitHubRepository {
  id: number
  full_name: string
  url: string
  private: boolean
}

export interface GitHubIssue {
  id: number
  number: number
  url: string
  title: string
  body: string
  state: 'open' | 'closed'
  state_reason: string | null
  labels: { name: string; color: string }[]
  updated_at: string
}

export interface GitHubDependency {
  id: number
  number: number
  repository: string
  url: string
  title: string
  state: 'open' | 'closed'
}

export interface GitHubSelection {
  id: string
  repository: GitHubRepository
  created_at: string
  issues: (GitHubIssue & { dependencies: { status: 'complete' | 'unavailable'; blocked_by: GitHubDependency[]; error: string | null } })[]
}

export const statusLabels: Record<TaskletStatus, string> = {
  idle: 'Готов к запуску',
  queued: 'В очереди',
  running: 'Выполняется',
  completed: 'Завершён',
  failed: 'Ошибка',
  cancelled: 'Остановлен',
  blocked: 'Заблокирован',
}

export type ImportSelection = GitHubSelection

export interface ImportEdge {
  source: number
  target: number
  origin: 'github' | 'description' | 'ai' | 'user'
  explanation: string
}

export interface ImportDecision {
  kind: 'external_completed' | 'ignore_github'
  source: number
  target: number
  reason: string
}

export interface ImportBlocker {
  source: number
  target: number
  issue: { id: number; number: number; repository: string; url: string; title: string; state: 'open' | 'closed' }
}

export interface ImportGraph {
  edges: ImportEdge[]
  external_blockers: ImportBlocker[]
  positions: Record<string, { x: number; y: number }>
}

export interface ImportPlan extends ImportGraph {
  id: string
  status: 'running' | 'completed' | 'failed' | 'cancelled'
  selection_id: string
  selection: ImportSelection
  error: string | null
}

export interface ImportPayload {
  plan_id: string
  operation_id: string
  name: string
  working_directory: string | null
  edges: Pick<ImportEdge, 'source' | 'target' | 'explanation'>[]
  decisions: ImportDecision[]
}

export interface ImportClient {
  createPlan: (selectionId: string, mode: 'ai' | 'known') => Promise<ImportPlan>
  getPlan: (id: string) => Promise<ImportPlan>
  cancelPlan: (id: string) => Promise<ImportPlan>
  validatePlan: (id: string, edges: ImportPayload['edges'], decisions: ImportDecision[]) => Promise<ImportGraph>
  createImport: (payload: ImportPayload) => Promise<Workspace>
  selectIssues: (selection: ImportSelection, issues: { id: number; number: number }[]) => Promise<ImportSelection>
}


export const dependencyOrigins = { github: 'GitHub', description: 'Описание issue', ai: 'Вывод AI', user: 'Пользователь' }
