import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

const backend = `${(process.env.AISPACE_E2E_API_URL || 'http://127.0.0.1:8000').replace(/\/$/, '')}/api`
const provider = 'http://127.0.0.1:8766'
const providerBaseUrl = process.env.AISPACE_E2E_PROVIDER_URL || `${provider}/v1`

type Tasklet = { id: string; title: string; prompt: string; status: string; last_output: string }
type Workspace = { tasklets: Tasklet[]; edges: { id: string; source: string; target: string; pass_context: boolean }[]; pipeline: { status: string } }
type AuditEvent = { kind: string; request_id: string; label: string; time: number; messages?: { role: string; content: string }[] }

async function workspace(request: APIRequestContext): Promise<Workspace> {
  const response = await request.get(`${backend}/workspace`)
  expect(response.ok()).toBeTruthy()
  return response.json()
}

async function audit(request: APIRequestContext): Promise<AuditEvent[]> {
  const response = await request.get(`${provider}/audit`)
  expect(response.ok()).toBeTruthy()
  return (await response.json()).events
}

async function createTasklet(request: APIRequestContext, title: string, label: string, delay = 0.7, x = 100, y = 100): Promise<Tasklet> {
  const response = await request.post(`${backend}/tasklets`, {
    data: { title, prompt: `Выполни задачу [[label:${label}]][[delay:${delay}]]`, position: { x, y } },
  })
  expect(response.ok()).toBeTruthy()
  return response.json()
}

async function connect(request: APIRequestContext, source: Tasklet, target: Tasklet) {
  const response = await request.post(`${backend}/edges`, { data: { source: source.id, target: target.id, pass_context: false } })
  expect(response.ok()).toBeTruthy()
}

async function createTaskletInUI(page: Page, title: string, prompt: string) {
  await page.getByRole('button', { name: 'Новый тасклет', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('textbox', { name: 'Название', exact: true }).fill(title)
  await dialog.getByRole('textbox', { name: 'Промпт', exact: true }).fill(prompt)
  await dialog.getByRole('button', { name: 'Создать тасклет', exact: true }).click()
  await expect(dialog).not.toBeVisible()
  await expect(page.locator('.react-flow__node').filter({ hasText: title })).toBeVisible()
}

test.beforeEach(async ({ request }) => {
  await request.post(`${backend}/pipeline/stop`)
  await expect.poll(async () => (await workspace(request)).pipeline.status).not.toMatch(/^(running|stopping)$/)
  for (const tasklet of (await workspace(request)).tasklets) {
    const response = await request.delete(`${backend}/tasklets/${tasklet.id}`)
    expect(response.ok()).toBeTruthy()
  }
  const response = await request.patch(`${backend}/settings`, {
    data: { execution_mode: 'api', working_directory: null, codex_sandbox: 'read-only', api_key: 'local-test-key', base_url: providerBaseUrl, model: 'test-model', max_parallel: 2, workspace_context: '' },
  })
  expect(response.ok()).toBeTruthy()
  await request.post(`${provider}/reset`)
})

test('settings save, mask the key, test the provider, and survive reload', async ({ page, request }, testInfo) => {
  expect((await request.patch(`${backend}/settings`, { data: { api_key: null } })).ok()).toBeTruthy()
  await page.goto('/')
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  const key = page.getByLabel('API-ключ', { exact: true })
  await expect(key).toHaveAttribute('type', 'password')
  await key.fill('local-test-key')
  await page.getByRole('textbox', { name: 'Модель по умолчанию', exact: true }).fill('test-model')
  await page.getByText('Дополнительные настройки', { exact: true }).click()
  await page.getByRole('textbox', { name: 'API Base URL', exact: true }).fill(providerBaseUrl)
  await page.getByRole('textbox', { name: 'Контекст пространства', exact: true }).fill('Общий контекст тестового проекта')
  await page.getByRole('button', { name: 'Сохранить настройки', exact: true }).click()
  await expect.poll(async () => (await (await request.get(`${backend}/settings`)).json()).workspace_context).toBe('Общий контекст тестового проекта')
  const settings = await (await request.get(`${backend}/settings`)).json()
  expect(settings.api_key_configured).toBe(true)
  expect(JSON.stringify(settings)).not.toContain('local-test-key')
  await page.reload()
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  await expect(page.getByLabel('API-ключ', { exact: true })).toHaveValue('')
  await expect(page.getByRole('textbox', { name: 'Модель по умолчанию', exact: true })).toHaveValue('test-model')
  await expect(page.getByRole('textbox', { name: 'Контекст пространства', exact: true })).toHaveValue('Общий контекст тестового проекта')
  await expect(page.getByText('local-test-key', { exact: true })).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('settings.png'), fullPage: true })
  const connectionPromise = page.waitForResponse(response => response.url().endsWith('/api/settings/test'))
  await page.getByRole('button', { name: 'Проверить подключение', exact: true }).click()
  const connection = await connectionPromise
  expect(connection.ok()).toBeTruthy()
  expect((await connection.json()).ok).toBe(true)
})

test('tasklets can be created, edited, restored, and deleted', async ({ page, request }, testInfo) => {
  await page.goto('/')
  await expect(page.getByRole('button', { name: 'Создать первый тасклет', exact: true })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('empty-workspace.png'), fullPage: true })
  await createTaskletInUI(page, 'План проекта', 'Составь план [[label:plan]]')
  await page.locator('.react-flow__node').filter({ hasText: 'План проекта' }).click()
  await page.getByRole('textbox', { name: 'Название', exact: true }).fill('Уточнённый план')
  await page.getByRole('textbox', { name: 'Промпт', exact: true }).fill('Подготовь подробный план [[label:plan]]')
  await page.getByRole('button', { name: 'Сохранить изменения', exact: true }).click()
  await expect.poll(async () => (await workspace(request)).tasklets[0]?.title).toBe('Уточнённый план')
  await page.reload()
  const node = page.locator('.react-flow__node').filter({ hasText: 'Уточнённый план' })
  await expect(node).toBeVisible()
  await node.click()
  await expect(page.getByRole('textbox', { name: 'Промпт', exact: true })).toHaveValue('Подготовь подробный план [[label:plan]]')
  page.once('dialog', dialog => dialog.accept())
  await page.getByRole('button', { name: 'Удалить тасклет', exact: true }).click()
  await expect(node).toHaveCount(0)
  await page.reload()
  await expect.poll(async () => (await workspace(request)).tasklets.length).toBe(0)
})

test('dependency arrows can be created, configured, persisted, and removed', async ({ page, request }) => {
  const source = await createTasklet(request, 'Исходная задача', 'source', 0.1, 80, 160)
  const target = await createTasklet(request, 'Зависимая задача', 'target', 0.1, 500, 160)
  await page.goto('/')
  await page.locator(`.react-flow__node[data-id="${target.id}"]`).click()
  await page.getByLabel('Добавить зависимость', { exact: true }).selectOption(source.id)
  await page.getByRole('button', { name: 'Связать', exact: true }).click()
  await expect(page.locator('.react-flow__edge')).toHaveCount(1)
  await page.getByRole('checkbox', { name: 'Передавать результат: Исходная задача', exact: true }).click()
  await expect(page.getByRole('checkbox', { name: 'Передавать результат: Исходная задача', exact: true })).toBeChecked()
  await expect.poll(async () => (await workspace(request)).edges[0]?.pass_context).toBe(true)
  await page.reload()
  await expect(page.locator('.react-flow__edge')).toHaveCount(1)
  await page.locator(`.react-flow__node[data-id="${target.id}"]`).click()
  await expect(page.getByLabel('Передавать результат: Исходная задача', { exact: true })).toBeChecked()
  await page.getByRole('button', { name: 'Удалить зависимость: Исходная задача', exact: true }).click()
  await expect(page.locator('.react-flow__edge')).toHaveCount(0)
  await expect.poll(async () => (await workspace(request)).edges.length).toBe(0)
})

test('independent tasklets run concurrently and their dependent waits for both', async ({ page, request }, testInfo) => {
  const first = await createTasklet(request, 'Архитектура', 'first', 1.5, 80, 60)
  const second = await createTasklet(request, 'Интерфейс', 'second', 1.5, 80, 280)
  const dependent = await createTasklet(request, 'Общий план', 'dependent', 0.2, 500, 160)
  await connect(request, first, dependent)
  await connect(request, second, dependent)
  await page.goto('/')
  await expect(page.locator('.react-flow__edge')).toHaveCount(2)
  await page.getByRole('button', { name: 'Запустить пайплайн', exact: true }).click()
  await expect.poll(async () => (await workspace(request)).tasklets.filter(tasklet => tasklet.status === 'running').length).toBe(2)
  await expect.poll(async () => (await workspace(request)).pipeline.status).toBe('completed')
  const state = await workspace(request)
  expect(state.tasklets.every(tasklet => tasklet.status === 'completed')).toBe(true)
  expect(state.tasklets.find(tasklet => tasklet.id === dependent.id)?.last_output).toContain('Готово: dependent.')
  for (const tasklet of [first, second, dependent]) {
    await expect(page.locator(`.react-flow__node[data-id="${tasklet.id}"]`)).toBeVisible()
    await expect(page.locator(`.react-flow__node[data-id="${tasklet.id}"]`)).toContainText('Завершён')
  }
  const events = await audit(request)
  const event = (kind: string, label: string) => {
    const found = events.find(item => item.kind === kind && item.label === label)
    expect(found).toBeDefined()
    return found!
  }
  expect(Math.max(event('start', 'first').time, event('start', 'second').time)).toBeLessThan(Math.min(event('finish', 'first').time, event('finish', 'second').time))
  expect(event('start', 'dependent').time).toBeGreaterThanOrEqual(Math.max(event('finish', 'first').time, event('finish', 'second').time))
  expect(event('start', 'dependent').messages?.some(message => message.content.includes('Готово: first.'))).toBe(false)
  await page.screenshot({ path: testInfo.outputPath('workspace.png'), fullPage: true })
  await page.locator(`.react-flow__node[data-id="${dependent.id}"]`).click()
  await page.getByRole('tab', { name: /^Чат/ }).click()
  await expect(page.getByText('Готово: dependent.', { exact: true })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('tasklet-chat.png'), fullPage: true })
  await page.reload()
  await page.locator(`.react-flow__node[data-id="${dependent.id}"]`).click()
  await page.getByRole('tab', { name: /^Чат/ }).click()
  await expect(page.getByText('Готово: dependent.', { exact: true })).toBeVisible()
})

test('stop cancels every active tasklet and prevents downstream requests', async ({ page, request }) => {
  const first = await createTasklet(request, 'Долгая задача A', 'slow-a', 10, 80, 60)
  const second = await createTasklet(request, 'Долгая задача B', 'slow-b', 10, 80, 280)
  const dependent = await createTasklet(request, 'Ожидающая задача', 'never', 0.1, 500, 160)
  await connect(request, first, dependent)
  await connect(request, second, dependent)
  await page.goto('/')
  await page.getByRole('button', { name: 'Запустить пайплайн', exact: true }).click()
  await expect.poll(async () => (await audit(request)).filter(event => event.kind === 'start').length).toBe(2)
  await page.getByRole('button', { name: 'Остановить всё', exact: true }).click()
  await expect.poll(async () => (await workspace(request)).pipeline.status).toBe('cancelled')
  const state = await workspace(request)
  expect(state.tasklets.every(tasklet => !['running', 'queued'].includes(tasklet.status))).toBe(true)
  expect(state.tasklets.filter(tasklet => [first.id, second.id].includes(tasklet.id)).every(tasklet => tasklet.status === 'cancelled')).toBe(true)
  for (const tasklet of [first, second]) {
    await expect(page.locator(`.react-flow__node[data-id="${tasklet.id}"]`)).toBeVisible()
    await expect(page.locator(`.react-flow__node[data-id="${tasklet.id}"]`)).toContainText('Остановлен')
  }
  await expect.poll(async () => (await audit(request)).filter(event => event.kind === 'disconnect').length).toBe(2)
  expect((await audit(request)).filter(event => event.kind === 'start').map(event => event.label).sort()).toEqual(['slow-a', 'slow-b'])
  await expect(page.getByRole('button', { name: 'Запустить пайплайн', exact: true })).toBeEnabled()
})

test('a tasklet chat sends follow-ups with its prior conversation', async ({ page, request }) => {
  const tasklet = await createTasklet(request, 'Обсуждение', 'conversation', 0.1)
  await page.goto('/')
  await page.getByRole('button', { name: 'Запустить пайплайн', exact: true }).click()
  await expect.poll(async () => (await workspace(request)).pipeline.status).toBe('completed')
  await page.locator(`.react-flow__node[data-id="${tasklet.id}"]`).click()
  await page.getByRole('tab', { name: /^Чат/ }).click()
  await page.getByPlaceholder('Напишите сообщение…', { exact: true }).fill('Уточни ответ [[label:follow-up]]')
  await page.getByRole('button', { name: 'Отправить сообщение', exact: true }).click()
  await expect(page.getByText('Готово: follow-up.', { exact: true })).toBeVisible()
  const followup = (await audit(request)).find(event => event.kind === 'start' && event.label === 'follow-up')
  expect(followup).toBeDefined()
  expect(followup!.messages).toEqual(expect.arrayContaining([
    expect.objectContaining({ role: 'assistant', content: 'Готово: conversation.' }),
    expect.objectContaining({ role: 'user', content: 'Уточни ответ [[label:follow-up]]' }),
  ]))
})

test('navigation, settings, and tasklet creation remain usable on a narrow screen', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  await expect(page.getByLabel('API-ключ', { exact: true })).toBeVisible()
  const keyBounds = await page.getByLabel('API-ключ', { exact: true }).boundingBox()
  expect(keyBounds).not.toBeNull()
  expect(keyBounds!.x).toBeGreaterThanOrEqual(0)
  expect(keyBounds!.x + keyBounds!.width).toBeLessThanOrEqual(390)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('mobile-settings.png'), fullPage: true })
  await page.getByRole('button', { name: /^Пространство/ }).click()
  await createTaskletInUI(page, 'Мобильный тасклет', 'Подготовь план')
  await expect(page.getByRole('complementary', { name: 'Редактор тасклета', exact: true })).toBeVisible()
  await expect(page.getByRole('textbox', { name: 'Название', exact: true })).toHaveValue('Мобильный тасклет')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('mobile-tasklet.png'), fullPage: true })
})
