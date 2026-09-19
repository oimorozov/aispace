import { useEffect, useState } from 'react'
import { Check, Github, LoaderCircle } from 'lucide-react'
import { api } from '../lib/api'

export function GitHubConnection() {
  const [configured, setConfigured] = useState(false)
  const [token, setToken] = useState('')
  const [pending, setPending] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    void api.githubConnection().then(value => { if (active) setConfigured(value.token_configured) }).catch(reason => { if (active) setError(reason.message) })
    return () => { active = false }
  }, [])

  async function save(value: string | null) {
    setPending(true)
    setError('')
    setMessage('')
    try {
      const result = await api.updateGitHubConnection(value)
      setConfigured(result.token_configured)
      setToken('')
      setMessage(result.token_configured ? 'Токен GitHub сохранён. Доступ проверяется при загрузке репозитория.' : 'Токен GitHub удалён. Публичные репозитории доступны без входа.')
    } catch (reason) { setError((reason as Error).message) } finally { setPending(false) }
  }

  return <form className="settings-form github-settings" onSubmit={event => { event.preventDefault(); void save(token.trim()) }}>
    <section className="settings-section">
      <div className="section-heading"><span className="section-icon"><Github size={18} /></span><div><h2>GitHub</h2><p>Отдельное подключение для чтения issues. Общее для всех пространств.</p></div></div>
      <p className="field-help">Публичный репозиторий можно открыть без токена. Для private-репозитория создайте fine-grained personal access token с доступом к нужному репозиторию и разрешением Issues: read.</p>
      <p className="github-connection-status">{configured ? 'Токен GitHub настроен' : 'Без токена GitHub'}</p>
      <label className="field">Токен GitHub<input aria-label="Токен GitHub" type="password" autoComplete="off" value={token} onChange={event => setToken(event.target.value)} placeholder={configured ? 'Новый токен для замены' : 'github_pat_…'} disabled={pending} /></label>
      <p className="field-help">Секрет хранится только на backend. Несохранённый токен не восстанавливается после закрытия формы или обновления страницы.</p>
      <div className="connection-actions"><button className="button button-secondary" disabled={pending || !token.trim()}>{pending ? <LoaderCircle size={14} className="spin" /> : <Check size={14} />}Сохранить токен GitHub</button>{configured && <button type="button" className="text-button danger-text" disabled={pending} onClick={() => { if (window.confirm('Удалить сохранённый токен GitHub?')) void save(null) }}>Удалить токен GitHub</button>}</div>
      {error && <div className="notice error-notice" role="alert">{error}</div>}
      {message && <div className="notice small" role="status">{message}</div>}
    </section>
  </form>
}
