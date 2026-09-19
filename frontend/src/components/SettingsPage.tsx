import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Check, ChevronDown, ExternalLink, Eye, EyeOff, KeyRound, LoaderCircle, RefreshCw } from 'lucide-react'
import { api } from '../lib/api'
import type { CodexLogin, CodexStatus, Settings, Workspace } from '../lib/types'
import { clearMatching, patchSettingsUi, patchWorkspaceUi, useWorkspaceUi, type GlobalDraft, type WorkspaceDraft } from '../lib/uiState'
import { DirectoryField, DirectoryPicker } from './DirectoryPicker'
import { GitHubConnection } from './GitHubConnection'

export function SettingsPage({ settings, workspace, globalLocked, workspaceLocked, onUpdate, onWorkspaceUpdate, notify }: { settings: Settings; workspace: Workspace | null; globalLocked: boolean; workspaceLocked: boolean; onUpdate: (settings: Settings) => void | Promise<void>; onWorkspaceUpdate: (workspace: Workspace) => void | Promise<void>; notify: (text: string, error?: boolean) => void }) {
  const wid = workspace?.id || '__global__'
  const ui = useWorkspaceUi(wid).settings
  const [key, setKey] = useState('')
  const [visible, setVisible] = useState(false)
  const [pending, setPending] = useState<string | null>(null)
  const [codex, setCodex] = useState<CodexStatus | null>(null)
  const [codexError, setCodexError] = useState('')
  const [login, setLogin] = useState<CodexLogin | null>(null)
  const container = useRef<HTMLElement>(null)
  const values = { ...settings, ...ui.globalDraft }
  const { execution_mode: mode, model, base_url: baseUrl, max_parallel: parallel, codex_sandbox: sandbox } = values
  const context = ui.workspaceDraft.workspace_context ?? workspace?.workspace_context ?? ''
  const directory = 'working_directory' in ui.workspaceDraft ? ui.workspaceDraft.working_directory! : workspace?.working_directory || null
  const globalDirty = Boolean(key) || Object.entries(ui.globalDraft).some(([name, value]) => value !== settings[name as keyof Settings])
  const workspaceDirty = Boolean(workspace) && (context !== workspace?.workspace_context || directory !== workspace?.working_directory)
  const disabled = globalLocked || Boolean(pending)
  const workspaceDisabled = workspaceLocked || Boolean(pending)
  const setGlobal = (change: GlobalDraft) => patchWorkspaceUi(wid, current => ({ ...current, settings: { ...current.settings, globalDraft: { ...current.settings.globalDraft, ...change } } }))
  const setWorkspace = (change: WorkspaceDraft) => patchWorkspaceUi(wid, current => ({ ...current, settings: { ...current.settings, workspaceDraft: { ...current.settings.workspaceDraft, ...change } } }))
  useLayoutEffect(() => { if (container.current) container.current.scrollTop = ui.scroll }, [])

  useEffect(() => {
    if (mode !== 'codex') return
    let active = true
    let inFlight = false
    async function refresh() {
      if (inFlight) return
      inFlight = true
      try {
        const result = await api.codexStatus()
        if (!active) return
        setCodex(result)
        setCodexError('')
        if (login && result.authenticated) { setLogin(null); notify('ChatGPT подключён') }
      } catch (error) { if (active) setCodexError((error as Error).message) }
      finally { inFlight = false }
    }
    void refresh()
    const timer = login ? setInterval(() => void refresh(), 2000) : undefined
    return () => { active = false; clearInterval(timer) }
  }, [mode, login, notify])

  async function saveGlobal() {
    setPending('global')
    try {
      const result = await api.updateSettings({ ...ui.globalDraft, ...(key.trim() ? { api_key: key.trim() } : {}) })
      await onUpdate(result)
      setKey('')
      patchWorkspaceUi(wid, current => ({ ...current, settings: { ...current.settings, globalDraft: clearMatching(current.settings.globalDraft, ui.globalDraft) } }))
      notify('Настройки сохранены')
    } catch (error) { notify((error as Error).message, true) } finally { setPending(null) }
  }

  async function saveWorkspace() {
    if (!workspace) return
    setPending('workspace')
    try {
      const result = await api.updateWorkspace(workspace.id, { workspace_context: context, working_directory: directory })
      await onWorkspaceUpdate(result)
      patchWorkspaceUi(wid, current => ({ ...current, settings: { ...current.settings, workspaceDraft: clearMatching(current.settings.workspaceDraft, ui.workspaceDraft) } }))
      notify('Настройки пространства сохранены')
    } catch (error) { notify((error as Error).message, true) } finally { setPending(null) }
  }

  async function test() {
    setPending('test')
    try { const result = await api.testSettings(); notify(result.message, !result.ok) } catch (error) { notify((error as Error).message, true) } finally { setPending(null) }
  }

  async function removeKey() {
    if (!window.confirm('Удалить сохранённый API-ключ?')) return
    setPending('remove')
    try { await onUpdate(await api.updateSettings({ api_key: null })); setKey(''); notify('API-ключ удалён') } catch (error) { notify((error as Error).message, true) } finally { setPending(null) }
  }

  async function authenticate(action: 'login' | 'cancel' | 'logout' | 'refresh') {
    setPending(action)
    setCodexError('')
    try {
      if (action === 'login') setLogin(await api.codexLogin())
      if (action === 'cancel' && login) { await api.cancelCodexLogin(login.login_id); setLogin(null) }
      if (action === 'logout') { await api.codexLogout(); setLogin(null) }
      setCodex(await api.codexStatus())
    } catch (error) { setCodexError((error as Error).message) } finally { setPending(null) }
  }

  return <main ref={container} className="settings-page" onScroll={event => patchSettingsUi(wid, { scroll: event.currentTarget.scrollTop })}>
    <div className="settings-intro"><h1>Настройки</h1><p>{workspace ? workspace.name : 'Общие настройки aispace'}</p></div>
    {workspace && <form className="settings-form workspace-settings-form" onSubmit={event => { event.preventDefault(); void saveWorkspace() }}>
      <section className="settings-section"><div className="section-heading"><div><h2>Настройки пространства</h2></div></div>
        {workspaceLocked && <div className="notice small">Остановите пайплайн этого пространства, чтобы изменить контекст.</div>}
        <DirectoryField label="Рабочая папка проекта" value={directory} disabled={workspaceDisabled} onOpen={() => patchSettingsUi(wid, { picker: { currentPath: directory, pathInput: directory || '' } })} onClear={() => setWorkspace({ working_directory: null })} />
        {mode === 'api' && <p className="field-help">Папка используется только Codex</p>}
        {directory !== workspace.working_directory && <p className="field-help">При смене папки контекст Codex начнётся заново. Переписка в aispace сохранится.</p>}
        <label className="field">Контекст пространства<textarea aria-label="Контекст пространства" rows={5} value={context} onChange={event => setWorkspace({ workspace_context: event.target.value })} placeholder="Опишите проект, цели и общие требования…" disabled={workspaceDisabled} /></label>
        <div className="settings-footer inline-settings-footer">{workspaceDirty && <span>Есть несохранённые изменения</span>}<button className="button button-primary" disabled={workspaceDisabled || !workspaceDirty}>{pending === 'workspace' ? <LoaderCircle size={14} className="spin" /> : <Check size={14} />}Сохранить пространство</button></div>
      </section>
    </form>}
    <form onSubmit={event => { event.preventDefault(); void saveGlobal() }} className="settings-form" aria-labelledby="global-settings-title">
      <h2 id="global-settings-title" className="settings-group-title">Общие настройки</h2>
      {globalLocked && <div className="notice">Общие настройки доступны после остановки всех выполняющихся задач.</div>}
      <section className="settings-section">
        <div className="section-heading"><span className="section-icon"><KeyRound size={18} /></span><div><h2>Подключение к AI</h2></div></div>
        <div className="connection-modes" role="group" aria-label="Способ подключения"><button type="button" aria-pressed={mode === 'codex'} disabled={disabled || Boolean(login)} onClick={() => setGlobal({ execution_mode: 'codex', ...(mode !== 'codex' ? { model: '' } : {}) })}><strong>ChatGPT</strong><span>Подписка через Codex</span></button><button type="button" aria-pressed={mode === 'api'} disabled={disabled || Boolean(login)} onClick={() => setGlobal({ execution_mode: 'api', ...(mode !== 'api' ? { model: '' } : {}) })}><strong>API-ключ</strong><span>OpenAI-совместимый API</span></button></div>
        {mode === 'api' ? <>
          {settings.api_key_configured && <div className="connection-saved"><span className="configured-badge"><Check size={12} />Ключ сохранён</span></div>}
          <label className="field" htmlFor="api-key">API-ключ</label><div className="password-field"><input id="api-key" type={visible ? 'text' : 'password'} autoComplete="off" value={key} onChange={event => setKey(event.target.value)} placeholder={settings.api_key_configured ? 'Новый API-ключ' : 'Вставьте API-ключ'} disabled={disabled} /><button type="button" className="icon-button" aria-label={visible ? 'Скрыть API-ключ' : 'Показать API-ключ'} onClick={() => setVisible(!visible)}>{visible ? <EyeOff size={16} /> : <Eye size={16} />}</button></div>
          <p className="field-help">Несохранённый API-ключ потребуется ввести заново после обновления страницы.</p>
        </> : <>
          <div className="codex-connection"><div className="codex-account"><span className={`connection-dot ${codex?.authenticated ? 'connected' : ''}`} /><div><strong>{codex?.authenticated ? 'ChatGPT подключён' : codex ? 'Вход через ChatGPT' : 'Проверяем подключение…'}</strong><p>{codex?.account_label || codex?.message || 'Подключаемся к локальному исполнителю Codex.'}</p></div><button type="button" className="icon-button" aria-label="Обновить статус ChatGPT" onClick={() => void authenticate('refresh')} disabled={Boolean(pending)}><RefreshCw size={14} className={pending === 'refresh' ? 'spin' : ''} /></button></div>
            {login ? <div className="codex-login"><p>Откройте страницу входа и подтвердите подключение.</p>{login.user_code && <div className="login-code"><span>Код подтверждения</span><code>{login.user_code}</code></div>}<a className="button button-secondary" href={login.auth_url} target="_blank" rel="noreferrer">Открыть ChatGPT<ExternalLink size={14} /></a><div className="login-waiting"><LoaderCircle size={13} className="spin" /><span>Ожидаем подтверждения…</span><button type="button" className="text-button" onClick={() => void authenticate('cancel')} disabled={Boolean(pending)}>Отменить</button></div></div> : <div className="connection-actions">{codex?.authenticated ? <button type="button" className="text-button danger-text" onClick={() => void authenticate('logout')} disabled={disabled}>Отключить ChatGPT</button> : <button type="button" className="button button-secondary" onClick={() => void authenticate('login')} disabled={disabled || !codex?.available}>{pending === 'login' && <LoaderCircle size={14} className="spin" />}Войти через ChatGPT</button>}</div>}
          </div>
          {codexError && <div className="notice error-notice" role="alert">{codexError}</div>}
        </>}
        <label className="field">Модель по умолчанию<input value={model} onChange={event => setGlobal({ model: event.target.value })} placeholder={mode === 'codex' ? 'По умолчанию Codex' : 'Идентификатор модели у вашего провайдера'} disabled={disabled} /></label>
        {mode === 'api' && <>
          <details className="advanced-settings" open={ui.advancedOpen} onToggle={event => patchSettingsUi(wid, { advancedOpen: event.currentTarget.open })}><summary>Дополнительные настройки<ChevronDown size={14} /></summary><label className="field">API Base URL<input type="url" value={baseUrl} onChange={event => setGlobal({ base_url: event.target.value })} placeholder="https://api.openai.com/v1" required disabled={disabled} /></label></details>
          <div className="connection-actions"><button type="button" className="button button-secondary" disabled={disabled || !settings.api_key_configured || !settings.model || globalDirty} onClick={() => void test()}>{pending === 'test' ? <LoaderCircle className="spin" size={14} /> : <span className="connection-test-dot" />}{pending === 'test' ? 'Проверяем…' : 'Проверить подключение'}</button>{settings.api_key_configured && <button type="button" className="text-button danger-text" onClick={() => void removeKey()} disabled={disabled}>Удалить ключ</button>}</div>
          {globalDirty && <p className="field-help">Сохраните изменения перед проверкой подключения.</p>}
        </>}
      </section>
      <section className="settings-section"><div className="section-heading"><div><h2>Выполнение задач</h2></div></div>
        {mode === 'codex' && <><label className="field">Доступ к файлам<select value={sandbox} onChange={event => setGlobal({ codex_sandbox: event.target.value as Settings['codex_sandbox'] })} disabled={disabled}><option value="read-only">Только чтение</option><option value="workspace-write">Чтение и изменение в рабочей папке</option></select></label></>}
        <label className="field parallel-field">Максимум параллельных задач<select value={parallel} onChange={event => setGlobal({ max_parallel: Number(event.target.value) })} disabled={disabled}>{[1, 2, 3, 4, 5, 6, 7, 8].map(value => <option key={value} value={value}>{value}</option>)}</select></label>
      </section>
      <div className="settings-footer">{globalDirty && <span>Есть несохранённые изменения</span>}<button className="button button-primary" disabled={disabled || !globalDirty}>{pending === 'global' ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />}{pending === 'global' ? 'Сохраняем…' : 'Сохранить настройки'}</button></div>
    </form>
    {ui.picker && workspace && <DirectoryPicker value={directory} state={ui.picker} onStateChange={picker => patchSettingsUi(wid, { picker })} onClose={() => patchSettingsUi(wid, { picker: null })} onSelect={value => { setWorkspace({ working_directory: value }); patchSettingsUi(wid, { picker: null }) }} />}
    <GitHubConnection />
  </main>
}
