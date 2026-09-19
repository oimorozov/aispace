import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

const backend = `${(process.env.AISPACE_E2E_API_URL || 'http://127.0.0.1:8000').replace(/\/$/, '')}/api`
const provider = 'http://127.0.0.1:8766'
let wid: string
let base: string
let reloadDialogs: string[]

async function task(request: APIRequestContext, title: string, prompt = '[[label:refresh]][[delay:0.1]]', x = 80) {
  const response = await request.post(`${base}/tasklets`, { data: { title, prompt, position: { x, y: 100 } } })
  expect(response.ok()).toBeTruthy()
  return response.json()
}

async function select(page: Page, id: string) {
  await page.locator(`.react-flow__node[data-id="${id}"]`).click()
}

async function snapshot(page: Page) {
  return page.evaluate(() => JSON.parse(sessionStorage.getItem('aispace.ui.v1')!))
}

function writes(page: Page) {
  const result: string[] = []
  page.on('request', request => {
    if (request.url().includes('/api/') && !['GET', 'HEAD', 'OPTIONS'].includes(request.method())) result.push(`${request.method()} ${request.url()}`)
  })
  return result
}

test.beforeEach(async ({ page, request }) => {
  reloadDialogs = []
  page.on('dialog', async dialog => {
    if (dialog.type() === 'beforeunload') reloadDialogs.push(dialog.message())
    await dialog.dismiss()
  })
  for (const item of await (await request.get(`${backend}/workspaces`)).json()) {
    expect((await request.post(`${backend}/workspaces/${item.id}/pipeline/stop`)).ok()).toBeTruthy()
    expect((await request.delete(`${backend}/workspaces/${item.id}`)).ok()).toBeTruthy()
  }
  wid = (await (await request.post(`${backend}/workspaces`, { data: { name: 'Восстановление' } })).json()).id
  base = `${backend}/workspaces/${wid}`
  expect((await request.patch(`${backend}/settings`, { data: { execution_mode: 'api', api_key: 'local-test-key', base_url: `${provider}/v1`, model: 'test-model', max_parallel: 2 } })).ok()).toBeTruthy()
  await request.post(`${provider}/reset`)
})

test.afterEach(() => expect(reloadDialogs).toEqual([]))

test('restores settings drafts, expanded controls and scroll while excluding unsaved secrets', async ({ page, request }) => {
  await page.setViewportSize({ width: 390, height: 700 })
  await page.goto('/')
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  await page.getByLabel('API-ключ', { exact: true }).fill('private-unsaved-key')
  await page.getByLabel('Контекст пространства', { exact: true }).fill('Последний символ Я')
  await page.getByLabel('Модель по умолчанию', { exact: true }).fill('draft-model')
  await page.getByText('Дополнительные настройки', { exact: true }).click()
  await page.getByLabel('API Base URL', { exact: true }).fill('https://example.invalid/v1')
  await page.locator('.parallel-field select').selectOption('4')
  await page.locator('.settings-page').evaluate(element => { element.scrollTop = 250 })
  await expect.poll(async () => (await snapshot(page)).workspaces[wid].settings.scroll).toBe(250)
  const mutations = writes(page)
  await page.reload()
  await expect(page.locator('.settings-page')).toBeVisible()
  await expect.poll(() => page.locator('.settings-page').evaluate(element => element.scrollTop)).toBe(250)
  await expect(page.getByLabel('API-ключ', { exact: true })).toHaveValue('')
  await expect(page.getByLabel('Контекст пространства', { exact: true })).toHaveValue('Последний символ Я')
  await expect(page.getByLabel('Модель по умолчанию', { exact: true })).toHaveValue('draft-model')
  await expect(page.getByLabel('API Base URL', { exact: true })).toHaveValue('https://example.invalid/v1')
  await expect(page.locator('.advanced-settings')).toHaveAttribute('open', '')
  await expect(page.locator('.parallel-field select')).toHaveValue('4')
  const saved = await (await request.get(`${backend}/settings`)).json()
  expect(saved.model).toBe('test-model')
  expect(saved.api_key_configured).toBe(true)
  expect(JSON.stringify(await snapshot(page))).not.toContain('private-unsaved-key')
  expect(mutations).toEqual([])
})

test('restores every tasklet field and dependency choice, clearing drafts only after confirmed saves', async ({ page, request }) => {
  const a = await task(request, 'Основа')
  const b = await task(request, 'Редактор', undefined, 430)
  await page.goto('/')
  await select(page, b.id)
  await page.getByLabel('Название', { exact: true }).fill('Новое название')
  await page.getByLabel('Промпт', { exact: true }).fill('Последняя инструкция')
  await page.getByLabel('Модель', { exact: true }).fill('draft-model')
  await page.getByLabel('Добавить зависимость', { exact: true }).selectOption(a.id)
  await page.getByRole('button', { name: 'Выбрать: Рабочая папка тасклета', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Выбрать папку', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: 'Выбрать папку', exact: true }).click()
  await page.locator('.inspector-body').evaluate(element => { element.scrollTop = 160 })
  await expect.poll(async () => (await snapshot(page)).workspaces[wid].tasklets[b.id].taskScroll).toBe(160)
  const before = await snapshot(page)
  const mutations = writes(page)
  await page.reload()
  await expect(page.getByLabel('Название', { exact: true })).toHaveValue('Новое название')
  await expect(page.getByLabel('Промпт', { exact: true })).toHaveValue('Последняя инструкция')
  await expect(page.getByLabel('Модель', { exact: true })).toHaveValue('draft-model')
  await expect(page.getByLabel('Добавить зависимость', { exact: true })).toHaveValue(a.id)
  expect((await snapshot(page)).workspaces[wid].tasklets[b.id].draft).toEqual(before.workspaces[wid].tasklets[b.id].draft)
  await expect.poll(() => page.locator('.inspector-body').evaluate(element => element.scrollTop)).toBe(160)
  expect(mutations).toEqual([])
  await page.route(`**/api/workspaces/${wid}/tasklets/${b.id}`, route => route.request().method() === 'PATCH' ? route.fulfill({ status: 503, json: { detail: 'Временная ошибка' } }) : route.continue())
  await page.getByRole('button', { name: 'Сохранить изменения', exact: true }).click()
  await expect(page.getByText('Временная ошибка', { exact: true })).toBeVisible()
  await page.reload()
  await expect(page.getByLabel('Промпт', { exact: true })).toHaveValue('Последняя инструкция')
  await page.unroute(`**/api/workspaces/${wid}/tasklets/${b.id}`)
  await page.getByRole('button', { name: 'Сохранить изменения', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Сохранить изменения', exact: true })).toBeDisabled()
  await page.reload()
  await expect(page.getByLabel('Название', { exact: true })).toHaveValue('Новое название')
  await expect(page.getByRole('button', { name: 'Сохранить изменения', exact: true })).toBeDisabled()
  expect((await snapshot(page)).workspaces[wid].tasklets[b.id].draft).toEqual({})
})

test('restores a directory browser and typed path without choosing it or invoking Finder', async ({ page, request }) => {
  const item = await task(request, 'Папка')
  await page.goto('/')
  await select(page, item.id)
  await page.getByRole('button', { name: 'Выбрать: Рабочая папка тасклета', exact: true }).click()
  await page.getByRole('button', { name: 'demo', exact: true }).click()
  await expect(page.getByRole('button', { name: 'component', exact: true })).toBeVisible()
  await page.getByLabel('Путь к папке', { exact: true }).fill('/ещё/не/выбранная/папка')
  const before = (await snapshot(page)).workspaces[wid].tasklets[item.id].picker
  const mutations = writes(page)
  await page.route('**/api/directories/choose', route => route.fulfill({ json: { path: null } }))
  await page.reload()
  await expect(page.getByRole('dialog', { name: 'Рабочая папка', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'component', exact: true })).toBeVisible()
  await expect(page.getByLabel('Путь к папке', { exact: true })).toHaveValue('/ещё/не/выбранная/папка')
  expect((await snapshot(page)).workspaces[wid].tasklets[item.id].picker).toEqual(before)
  expect((await (await request.get(base)).json()).tasklets[0].working_directory).toBeNull()
  expect(mutations).toEqual([])
})

test('restores chat reading position and unsent text without serializing history or sending again', async ({ page, request }) => {
  const item = await task(request, 'История')
  for (let index = 0; index < 7; index++) {
    expect((await request.post(`${base}/tasklets/${item.id}/messages`, { data: { content: `Сообщение ${index}: ${'подробности '.repeat(45)} [[delay:0.01]]` } })).ok()).toBeTruthy()
    await expect.poll(async () => (await (await request.get(base)).json()).pipeline.status).toBe('completed')
  }
  await page.goto('/')
  await select(page, item.id)
  await page.getByRole('tab', { name: /^Чат/ }).click()
  await expect(page.locator('.chat-message')).toHaveCount(14)
  await page.getByLabel('Сообщение', { exact: true }).fill('Неотправленный вопрос')
  await page.locator('.chat-history').evaluate(element => { element.scrollTop = 120 })
  await expect.poll(async () => (await snapshot(page)).workspaces[wid].tasklets[item.id].chatScroll).toBe(120)
  const mutations = writes(page)
  await page.reload()
  await expect(page.locator('.chat-message')).toHaveCount(14)
  await expect(page.getByLabel('Сообщение', { exact: true })).toHaveValue('Неотправленный вопрос')
  await expect.poll(() => page.locator('.chat-history').evaluate(element => element.scrollTop)).toBe(120)
  expect(JSON.stringify(await snapshot(page))).not.toContain('подробности')
  expect(mutations).toEqual([])
  await page.getByRole('button', { name: 'Отправить сообщение', exact: true }).click()
  await expect(page.getByLabel('Сообщение', { exact: true })).toHaveValue('')
  await expect.poll(async () => (await (await request.get(base)).json()).pipeline.status).toBe('completed')
  await page.reload()
  await expect(page.getByLabel('Сообщение', { exact: true })).toHaveValue('')
  await expect(page.locator('.chat-message')).toHaveCount(16)
  expect(mutations).toHaveLength(1)
})

test('refresh reconnects to a running pipeline without repeating or stopping it', async ({ page, request }) => {
  const item = await task(request, 'Долгая задача', '[[label:once]][[delay:15]]')
  await page.goto('/')
  await select(page, item.id)
  await page.getByRole('button', { name: 'Запустить тасклет', exact: true }).click()
  await expect(page.getByRole('tab', { name: /^Чат/ })).toHaveAttribute('aria-selected', 'true')
  await expect.poll(async () => (await (await request.get(`${provider}/audit`)).json()).events.filter((event: { kind: string }) => event.kind === 'start').length).toBe(1)
  const mutations = writes(page)
  await page.reload()
  await expect(page.getByRole('tab', { name: /^Чат/ })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByText('Тасклет работает', { exact: true })).toBeVisible()
  expect(mutations).toEqual([])
  expect((await (await request.get(`${provider}/audit`)).json()).events.filter((event: { kind: string }) => event.kind === 'start')).toHaveLength(1)
  await page.getByRole('button', { name: 'Остановить всё', exact: true }).first().click()
  await expect.poll(async () => (await (await request.get(base)).json()).pipeline.status).toBe('cancelled')
  expect(mutations).toHaveLength(1)
})

test('drops deleted selections and invalid dependency choices but preserves unrelated drafts', async ({ page, request }) => {
  const a = await task(request, 'Удаляемый источник')
  const b = await task(request, 'Черновик', undefined, 430)
  await task(request, 'Оставшийся источник', undefined, 800)
  await page.goto('/')
  await select(page, b.id)
  await page.getByLabel('Промпт', { exact: true }).fill('Сохранить этот ввод')
  await page.getByLabel('Добавить зависимость', { exact: true }).selectOption(a.id)
  expect((await request.delete(`${base}/tasklets/${a.id}`)).ok()).toBeTruthy()
  await page.reload()
  await expect(page.getByLabel('Промпт', { exact: true })).toHaveValue('Сохранить этот ввод')
  await expect(page.getByLabel('Добавить зависимость', { exact: true })).toHaveValue('')
  await expect(page.getByRole('button', { name: 'Связать', exact: true })).toBeDisabled()
  expect((await request.delete(`${base}/tasklets/${b.id}`)).ok()).toBeTruthy()
  await page.reload()
  await expect(page.getByRole('complementary', { name: 'Редактор тасклета', exact: true })).toHaveCount(0)
  expect((await snapshot(page)).workspaces[wid].tasklets[b.id]).toBeUndefined()
})

test('temporary server failure preserves the selected tasklet and its draft until reliable data arrives', async ({ page, request }) => {
  const item = await task(request, 'Недоступный сервер')
  await page.goto('/')
  await select(page, item.id)
  await page.getByLabel('Промпт', { exact: true }).fill('Не терять при ошибке')
  await page.route('**/api/events', route => route.abort())
  await page.route(`**/api/workspaces/${wid}`, route => route.fulfill({ status: 503, json: { detail: 'Сервер временно недоступен' } }))
  const mutations = writes(page)
  await page.reload()
  await expect(page.getByRole('heading', { name: 'Не удалось открыть пространство', exact: true })).toBeVisible()
  expect((await snapshot(page)).workspaces[wid].tasklets[item.id].draft.prompt).toBe('Не терять при ошибке')
  await page.unroute(`**/api/workspaces/${wid}`)
  await page.getByRole('button', { name: 'Попробовать снова', exact: true }).click()
  await expect(page.getByLabel('Промпт', { exact: true })).toHaveValue('Не терять при ошибке')
  expect(mutations).toEqual([])
})

test('malformed snapshots and inaccessible storage leave the app usable without server mutations', async ({ page }) => {
  await page.goto('/')
  for (const value of ['{bad json', '{"version":999}', JSON.stringify({ version: 1, workspaces: { [wid]: { selectedId: 15, tasklets: [], viewport: { zoom: 'bad' }, settings: { globalDraft: { max_parallel: -20, api_key: 'must-not-survive' } } } } })]) {
    await page.evaluate(raw => sessionStorage.setItem('aispace.ui.v1', raw), value)
    await page.reload()
    await expect(page.getByRole('button', { name: 'Новый тасклет', exact: true })).toBeVisible()
    expect(JSON.stringify(await snapshot(page))).not.toContain('must-not-survive')
  }
  await page.addInitScript(() => {
    Object.defineProperty(window, 'sessionStorage', { get() { throw new DOMException('Storage denied', 'SecurityError') } })
  })
  const mutations = writes(page)
  await page.reload()
  await expect(page.getByText('Браузер не разрешил сохранить состояние. Черновики доступны до закрытия страницы.', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  await expect(page.getByLabel('Модель по умолчанию', { exact: true })).toHaveValue('test-model')
  expect(mutations).toEqual([])
})

test('refresh never resumes pending device login and excludes login data from storage', async ({ page }, testInfo) => {
  await page.route('**/api/codex/status', route => route.fulfill({ json: { available: true, authenticated: false, auth_type: null, account_label: null, message: 'Выполните вход', capabilities: { workspace: true, commands: [] } } }))
  await page.route('**/api/codex/login', route => route.fulfill({ json: { login_id: 'private-login-id', auth_url: 'https://example.invalid/login', user_code: 'PRIVATE-CODE' } }))
  await page.goto('/')
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  await page.getByRole('button', { name: 'ChatGPT Подписка через Codex', exact: true }).click()
  await page.getByRole('button', { name: 'Войти через ChatGPT', exact: true }).click()
  await expect(page.getByText('PRIVATE-CODE', { exact: true })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('device-login.png'), fullPage: true })
  const mutations = writes(page)
  await page.reload()
  await expect(page.getByRole('button', { name: 'Войти через ChatGPT', exact: true })).toBeVisible()
  await expect(page.getByText('PRIVATE-CODE', { exact: true })).toHaveCount(0)
  const stored = JSON.stringify(await snapshot(page))
  expect(stored).not.toContain('PRIVATE-CODE')
  expect(stored).not.toContain('private-login-id')
  expect(mutations).toEqual([])
})

test('explicitly discarded drafts stay discarded while cancelled confirmations retain them', async ({ page, request }) => {
  const item = await task(request, 'Отказ от черновика')
  await page.goto('/')
  await select(page, item.id)
  await page.getByLabel('Промпт', { exact: true }).fill('Важный черновик')
  await page.getByRole('button', { name: 'Закрыть редактор', exact: true }).click()
  await page.reload()
  await expect(page.getByLabel('Промпт', { exact: true })).toHaveValue('Важный черновик')
  page.removeAllListeners('dialog')
  page.on('dialog', async dialog => {
    if (dialog.type() === 'beforeunload') { reloadDialogs.push(dialog.message()); await dialog.dismiss() }
    else await dialog.accept()
  })
  await page.getByRole('button', { name: 'Закрыть редактор', exact: true }).click()
  await page.reload()
  await select(page, item.id)
  await expect(page.getByLabel('Промпт', { exact: true })).toHaveValue(item.prompt)
  await page.getByRole('button', { name: 'Новый тасклет', exact: true }).click()
  await page.getByRole('dialog').getByLabel('Название', { exact: true }).fill('Отменённое создание')
  await page.getByRole('button', { name: 'Закрыть создание тасклета', exact: true }).click()
  await page.reload()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  expect((await snapshot(page)).workspaces[wid].createDraft).toEqual({ title: '', prompt: '' })
  expect((await (await request.get(base)).json()).tasklets).toHaveLength(1)
})
