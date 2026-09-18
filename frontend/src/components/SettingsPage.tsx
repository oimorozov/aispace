import { useEffect, useState } from 'react'
import { Check, ChevronDown, ExternalLink, Eye, EyeOff, KeyRound, LoaderCircle, RefreshCw, ShieldCheck } from 'lucide-react'
import { api } from '../lib/api'
import type { CodexLogin, CodexStatus, Settings } from '../lib/types'
import { DirectoryField, DirectoryPicker } from './DirectoryPicker'

export function SettingsPage({ settings, locked, onUpdate, notify, onDirtyChange }: { settings: Settings; locked: boolean; onUpdate: (settings: Settings) => void; notify: (text: string, error?: boolean) => void; onDirtyChange: (dirty: boolean) => void }) {
  const [key, setKey] = useState('')
  const [mode, setMode] = useState(settings.execution_mode)
  const [model, setModel] = useState(settings.model)
  const [baseUrl, setBaseUrl] = useState(settings.base_url)
  const [parallel, setParallel] = useState(settings.max_parallel)
  const [context, setContext] = useState(settings.workspace_context)
  const [directory, setDirectory] = useState(settings.working_directory)
  const [sandbox, setSandbox] = useState(settings.codex_sandbox)
  const [choosingDirectory, setChoosingDirectory] = useState(false)
  const [visible, setVisible] = useState(false)
  const [pending, setPending] = useState<string | null>(null)
  const [codex, setCodex] = useState<CodexStatus | null>(null)
  const [codexError, setCodexError] = useState('')
  const [login, setLogin] = useState<CodexLogin | null>(null)
  const dirty = Boolean(key) || mode !== settings.execution_mode || model !== settings.model || baseUrl !== settings.base_url || parallel !== settings.max_parallel || context !== settings.workspace_context || directory !== settings.working_directory || sandbox !== settings.codex_sandbox
  const disabled = locked || Boolean(pending)

  useEffect(() => {
    onDirtyChange(dirty)
    const onUnload = (event: BeforeUnloadEvent) => { if (dirty) event.preventDefault() }
    window.addEventListener('beforeunload', onUnload)
    return () => { onDirtyChange(false); window.removeEventListener('beforeunload', onUnload) }
  }, [dirty, onDirtyChange])

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

  async function save() {
    setPending('save')
    try {
      const result = await api.updateSettings({ ...(key.trim() ? { api_key: key.trim() } : {}), execution_mode: mode, model: model.trim(), base_url: baseUrl.trim(), max_parallel: parallel, workspace_context: context, working_directory: directory, codex_sandbox: sandbox })
      onUpdate(result)
      setKey('')
      setMode(result.execution_mode)
      setModel(result.model)
      setBaseUrl(result.base_url)
      setParallel(result.max_parallel)
      setContext(result.workspace_context)
      setDirectory(result.working_directory)
      setSandbox(result.codex_sandbox)
      notify('Настройки сохранены')
    } catch (error) { notify((error as Error).message, true) } finally { setPending(null) }
  }

  async function test() {
    setPending('test')
    try { const result = await api.testSettings(); notify(result.message, !result.ok) } catch (error) { notify((error as Error).message, true) } finally { setPending(null) }
  }

  async function removeKey() {
    if (!window.confirm('Удалить сохранённый API-ключ?')) return
    setPending('remove')
    try { onUpdate(await api.updateSettings({ api_key: null })); setKey(''); notify('API-ключ удалён') } catch (error) { notify((error as Error).message, true) } finally { setPending(null) }
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

  return (
    <main className="settings-page">
      <div className="settings-intro"><span className="eyebrow">ВАШЕ ПРОСТРАНСТВО, ВАШИ ПРАВИЛА</span><h1>Настройки</h1><p>Подключите модель и настройте работу тасклетов.</p></div>
      {locked && <div className="notice">Остановите пайплайн, чтобы изменить настройки.</div>}
      <form onSubmit={event => { event.preventDefault(); void save() }} className="settings-form">
        <section className="settings-section">
          <div className="section-heading"><span className="section-icon"><KeyRound size={18} /></span><div><h2>Подключение к AI</h2><p>Выберите, как будут выполняться тасклеты.</p></div></div>
          <div className="connection-modes" role="group" aria-label="Способ подключения"><button type="button" aria-pressed={mode === 'codex'} disabled={disabled || Boolean(login)} onClick={() => { setMode('codex'); if (mode !== 'codex') setModel('') }}><strong>ChatGPT</strong><span>Подписка через Codex</span></button><button type="button" aria-pressed={mode === 'api'} disabled={disabled || Boolean(login)} onClick={() => { setMode('api'); if (mode !== 'api') setModel('') }}><strong>API-ключ</strong><span>OpenAI-совместимый API</span></button></div>
          {mode === 'api' ? <>
            {settings.api_key_configured && <div className="connection-saved"><span className="configured-badge"><Check size={12} />Ключ сохранён</span></div>}
            <label className="field" htmlFor="api-key">API-ключ</label><div className="password-field"><input id="api-key" type={visible ? 'text' : 'password'} autoComplete="off" value={key} onChange={event => setKey(event.target.value)} placeholder={settings.api_key_configured ? 'Новый ключ, если хотите заменить текущий' : 'Вставьте API-ключ'} disabled={disabled} /><button type="button" className="icon-button" aria-label={visible ? 'Скрыть API-ключ' : 'Показать API-ключ'} onClick={() => setVisible(!visible)}>{visible ? <EyeOff size={16} /> : <Eye size={16} />}</button></div>
            <p className="field-help"><ShieldCheck size={13} />Ключ хранится на локальном backend и не возвращается в браузер.</p>
          </> : <>
            <div className="codex-connection"><div className="codex-account"><span className={`connection-dot ${codex?.authenticated ? 'connected' : ''}`} /><div><strong>{codex?.authenticated ? 'ChatGPT подключён' : codex ? 'Вход через ChatGPT' : 'Проверяем подключение…'}</strong><p>{codex?.account_label || codex?.message || 'Подключаемся к локальному исполнителю Codex.'}</p></div><button type="button" className="icon-button" aria-label="Обновить статус ChatGPT" onClick={() => void authenticate('refresh')} disabled={disabled}><RefreshCw size={14} className={pending === 'refresh' ? 'spin' : ''} /></button></div>
              {login ? <div className="codex-login"><p>Откройте страницу входа и подтвердите подключение.</p>{login.user_code && <div className="login-code"><span>Код подтверждения</span><code>{login.user_code}</code></div>}<a className="button button-secondary" href={login.auth_url} target="_blank" rel="noreferrer">Открыть ChatGPT<ExternalLink size={14} /></a><div className="login-waiting"><LoaderCircle size={13} className="spin" /><span>Ожидаем подтверждения…</span><button type="button" className="text-button" onClick={() => void authenticate('cancel')} disabled={disabled}>Отменить</button></div></div> : <div className="connection-actions">{codex?.authenticated ? <button type="button" className="text-button danger-text" onClick={() => void authenticate('logout')} disabled={disabled}>Отключить ChatGPT</button> : <button type="button" className="button button-secondary" onClick={() => void authenticate('login')} disabled={disabled || !codex?.available}>{pending === 'login' && <LoaderCircle size={14} className="spin" />}Войти через ChatGPT</button>}</div>}
            </div>
            {codexError && <div className="notice error-notice" role="alert">{codexError}</div>}
            <p className="field-help">Подписка используется через Codex. API-ключ для этого режима не нужен.</p>
          </>}
          <label className="field">Модель по умолчанию<input value={model} onChange={event => setModel(event.target.value)} placeholder={mode === 'codex' ? 'Автоматически — модель Codex по умолчанию' : 'Идентификатор модели у вашего провайдера'} disabled={disabled} /></label>
          <p className="field-help">{mode === 'codex' ? 'Можно оставить пустым. Отдельный тасклет может выбрать свою модель.' : 'Можно выбрать другую модель в отдельном тасклете.'}</p>
          {mode === 'api' && <>
            <details className="advanced-settings"><summary>Дополнительные настройки<ChevronDown size={14} /></summary><label className="field">API Base URL<input type="url" value={baseUrl} onChange={event => setBaseUrl(event.target.value)} placeholder="https://api.openai.com/v1" required disabled={disabled} /></label><p className="field-help">Адрес OpenAI-совместимого API, включая /v1, если его требует провайдер.</p></details>
            <div className="connection-actions"><button type="button" className="button button-secondary" disabled={disabled || !settings.api_key_configured || !settings.model || dirty} onClick={() => void test()}>{pending === 'test' ? <LoaderCircle className="spin" size={14} /> : <span className="connection-test-dot" />}{pending === 'test' ? 'Проверяем…' : 'Проверить подключение'}</button>{settings.api_key_configured && <button type="button" className="text-button danger-text" onClick={() => void removeKey()} disabled={disabled}>Удалить ключ</button>}</div>
            {dirty && <p className="field-help">Сохраните изменения перед проверкой подключения.</p>}
          </>}
        </section>
        <section className="settings-section"><div className="section-heading"><div><h2>Контекст проекта</h2><p>Рабочая папка и общие инструкции для тасклетов.</p></div></div>
          <DirectoryField label="Рабочая папка проекта" value={directory} disabled={disabled} onOpen={() => setChoosingDirectory(true)} onClear={() => setDirectory(null)} />
          <p className="field-help">{mode === 'codex' ? 'Codex читает файлы в этой папке. В тасклете можно выбрать другую.' : 'Папки доступны исполнителю Codex. Режим API сам не читает локальные файлы.'}</p>
          {directory !== settings.working_directory && <p className="field-help">При смене папки контекст Codex начнётся заново. Переписка в aispace сохранится.</p>}
          {mode === 'codex' && <><label className="field">Доступ к файлам<select value={sandbox} onChange={event => setSandbox(event.target.value as Settings['codex_sandbox'])} disabled={disabled}><option value="read-only">Только чтение</option><option value="workspace-write">Чтение и изменение в рабочей папке</option></select></label><p className="field-help">{sandbox === 'read-only' ? 'Для изучения проекта и подготовки предложений.' : 'Тасклеты смогут изменять файлы и выполнять команды в рабочей папке.'}</p></>}
          <label className="field">Контекст пространства<textarea aria-label="Контекст пространства" rows={5} value={context} onChange={event => setContext(event.target.value)} placeholder="Опишите проект, цели и общие требования…" disabled={disabled} /></label>
          {mode === 'codex' && <p className="field-help command-help">{codex?.capabilities.commands.includes('/goal') ? '/goal <цель> — начать цель; /goal pause, resume, clear — управление. /stop — остановить всё.' : codex?.capabilities.commands.length ? `Команды исполнителя: ${codex.capabilities.commands.join(', ')}.` : 'Доступность команд зависит от подключённого исполнителя Codex.'}</p>}
        </section>
        <section className="settings-section"><div className="section-heading"><div><h2>Выполнение задач</h2><p>Задачи без зависимостей могут работать одновременно.</p></div></div><label className="field parallel-field">Параллельные задачи<select value={parallel} onChange={event => setParallel(Number(event.target.value))} disabled={disabled}>{[1, 2, 3, 4, 5, 6, 7, 8].map(value => <option key={value} value={value}>{value}</option>)}</select></label><p className="field-help">Лимит одновременно работающих тасклетов.</p></section>
        <div className="settings-footer"><span>{dirty ? 'Есть несохранённые изменения' : 'Все изменения сохранены'}</span><button className="button button-primary" disabled={disabled || !dirty}>{pending === 'save' ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />}{pending === 'save' ? 'Сохраняем…' : 'Сохранить настройки'}</button></div>
      </form>
      {choosingDirectory && <DirectoryPicker value={directory} onClose={() => setChoosingDirectory(false)} onSelect={value => { setDirectory(value); setChoosingDirectory(false) }} />}
    </main>
  )
}
