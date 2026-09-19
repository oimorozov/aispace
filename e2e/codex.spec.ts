import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

const backend = `${(process.env.AISPACE_E2E_API_URL || 'http://127.0.0.1:8000').replace(/\/$/, '')}/api`
let workspaceBase: string
const auditServer = 'http://127.0.0.1:8766'

type Tasklet = { id: string; title: string; status: string; working_directory: string | null }
type Workspace = { tasklets: Tasklet[]; pipeline: { status: string } }
type CodexEvent = { transport: string; kind: string; time: number; process_id: string; label?: string; input?: string; thread_id?: string; active_turns?: number; parameters?: { cwd?: string; sandbox?: string }; goal?: { objective: string; status: string } }

async function workspace(request: APIRequestContext): Promise<Workspace> {
  const response = await request.get(`${workspaceBase}`)
  expect(response.ok()).toBeTruthy()
  return response.json()
}

async function events(request: APIRequestContext): Promise<CodexEvent[]> {
  const response = await request.get(`${auditServer}/audit`)
  expect(response.ok()).toBeTruthy()
  return (await response.json()).events.filter((item: CodexEvent) => item.transport === 'codex')
}

async function createTasklet(request: APIRequestContext, title: string, label: string, delay = 0.1, y = 100): Promise<Tasklet> {
  const response = await request.post(`${workspaceBase}/tasklets`, { data: { title, prompt: `Выполни задачу [[label:${label}]][[delay:${delay}]]`, position: { x: 80, y } } })
  expect(response.ok()).toBeTruthy()
  return response.json()
}

async function selectFolder(page: Page, field: string, folders: string[]) {
  await page.getByRole('button', { name: `Выбрать: ${field}`, exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'Рабочая папка', exact: true })
  const path = dialog.getByRole('textbox', { name: 'Путь к папке', exact: true })
  for (const folder of folders) {
    await expect(dialog.getByRole('button', { name: folder, exact: true })).toBeVisible()
    const current = await path.inputValue()
    await dialog.getByRole('button', { name: folder, exact: true }).click()
    await expect(path).toHaveValue(`${current.replace(/\/$/, '')}/${folder}`)
  }
  const selected = await path.inputValue()
  await dialog.getByRole('button', { name: 'Выбрать папку', exact: true }).click()
  await expect(dialog).not.toBeVisible()
  return selected
}

test.beforeEach(async ({ page, request }) => {
  const existing = await (await request.get(`${backend}/workspaces`)).json()
  for (const item of existing) {
    expect((await request.post(`${backend}/workspaces/${item.id}/pipeline/stop`)).ok()).toBeTruthy()
    expect((await request.delete(`${backend}/workspaces/${item.id}`)).ok()).toBeTruthy()
  }
  const created = await request.post(`${backend}/workspaces`, { data: { name: 'Тестовое пространство' } })
  expect(created.ok()).toBeTruthy()
  workspaceBase = `${backend}/workspaces/${(await created.json()).id}`
  await page.route('**/api/directories/capabilities', route => route.fulfill({ json: { native_picker: false, platform: 'linux' } }))
  await page.route('**/api/directories/choose', route => route.fulfill({ status: 501, json: { detail: 'Системный выбор папки не включён в этом сценарии' } }))
  await request.post(`${workspaceBase}/pipeline/stop`)
  await expect.poll(async () => (await workspace(request)).pipeline.status).not.toMatch(/^(running|stopping)$/)
  for (const tasklet of (await workspace(request)).tasklets) {
    expect((await request.delete(`${workspaceBase}/tasklets/${tasklet.id}`)).ok()).toBeTruthy()
  }
  const listing = await request.get(`${backend}/directories`)
  expect(listing.ok()).toBeTruthy()
  const root = (await listing.json()).path
  expect((await request.patch(`${backend}/settings`, { data: { execution_mode: 'codex', api_key: null, model: '', codex_sandbox: 'read-only', max_parallel: 2 } })).ok()).toBeTruthy()
  expect((await request.patch(workspaceBase, { data: { working_directory: root } })).ok()).toBeTruthy()
  await request.post(`${auditServer}/reset`)
})

test('ChatGPT subscription runs without an API key and keeps a tasklet thread for follow-ups', async ({ page, request }, testInfo) => {
  expect((await request.patch(`${backend}/settings`, { data: { execution_mode: 'api' } })).ok()).toBeTruthy()
  const tasklet = await createTasklet(request, 'Чат по подписке', 'subscription')
  await page.goto('/')
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  await page.getByRole('button', { name: /^ChatGPT/ }).click()
  await expect(page.getByText('local-test@example.invalid', { exact: false })).toBeVisible()
  await expect(page.getByLabel('API-ключ', { exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: 'Сохранить настройки', exact: true }).click()
  await expect.poll(async () => (await (await request.get(`${backend}/settings`)).json()).execution_mode).toBe('codex')
  expect((await (await request.get(`${backend}/settings`)).json()).api_key_configured).toBe(false)
  await page.screenshot({ path: testInfo.outputPath('subscription-settings.png'), fullPage: true })
  await page.getByRole('combobox', { name: 'Доступ к файлам', exact: true }).selectOption('workspace-write')
  await page.screenshot({ path: testInfo.outputPath('subscription-write-settings.png'), fullPage: true })
  await page.getByRole('combobox', { name: 'Доступ к файлам', exact: true }).selectOption('read-only')
  await page.getByRole('button', { name: /^Пространство/ }).click()
  await page.getByRole('button', { name: 'Запустить пайплайн', exact: true }).click()
  await expect.poll(async () => (await workspace(request)).pipeline.status).toBe('completed')
  await page.locator(`.react-flow__node[data-id="${tasklet.id}"]`).click()
  await page.getByRole('tab', { name: /^Чат/ }).click()
  await expect(page.getByText('Готово: subscription.', { exact: true })).toBeVisible()
  await page.getByPlaceholder('Напишите сообщение…', { exact: true }).fill('Уточни ответ [[label:subscription-followup]]')
  await page.getByRole('button', { name: 'Отправить сообщение', exact: true }).click()
  await expect(page.getByText('Готово: subscription-followup.', { exact: true })).toBeVisible()
  const started = (await events(request)).filter(event => event.kind === 'start')
  expect(started.map(event => event.label)).toEqual(['subscription', 'subscription-followup'])
  expect(started[1].thread_id).toBe(started[0].thread_id)
  await page.reload()
  await page.locator(`.react-flow__node[data-id="${tasklet.id}"]`).click()
  await page.getByRole('tab', { name: /^Чат/ }).click()
  await expect(page.getByText('Готово: subscription-followup.', { exact: true })).toBeVisible()
})

test('working directories selected in settings and tasklets persist and reach the executor', async ({ page, request }, testInfo) => {
  const tasklet = await createTasklet(request, 'Компонент проекта', 'directory')
  await page.goto('/')
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  const project = await selectFolder(page, 'Рабочая папка проекта', ['demo'])
  await page.getByRole('button', { name: 'Сохранить пространство', exact: true }).click()
  await expect.poll(async () => (await (await request.get(workspaceBase)).json()).working_directory).toBe(project)
  await page.reload()
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Выбрать: Рабочая папка проекта', exact: true })).toContainText(project)
  await page.getByRole('button', { name: /^Пространство/ }).click()
  await page.locator(`.react-flow__node[data-id="${tasklet.id}"]`).click()
  const component = await selectFolder(page, 'Рабочая папка тасклета', ['component'])
  await page.getByRole('button', { name: 'Сохранить изменения', exact: true }).click()
  await expect.poll(async () => (await workspace(request)).tasklets[0]?.working_directory).toBe(component)
  await page.reload()
  await page.locator(`.react-flow__node[data-id="${tasklet.id}"]`).click()
  await expect(page.getByRole('button', { name: 'Выбрать: Рабочая папка тасклета', exact: true })).toContainText(component)
  await page.screenshot({ path: testInfo.outputPath('working-directory.png'), fullPage: true })
  await page.getByRole('button', { name: 'Запустить тасклет', exact: true }).click()
  await expect(page.getByText('Готово: directory.', { exact: true })).toBeVisible()
  const started = (await events(request)).find(event => event.kind === 'start')
  expect(started?.parameters?.cwd).toBe(component)
  expect(started?.parameters?.sandbox).toBe('read-only')
})

test('directory picker leaves a saved nested directory through the root and selects another branch', async ({ page, request }) => {
  const listing = await (await request.get(`${backend}/directories`)).json()
  const root = listing.roots[0] as string
  const base = root.replace(/\/$/, '')
  const nested = `${base}/demo/component`
  const other = `${base}/other`
  expect((await request.patch(workspaceBase, { data: { working_directory: nested } })).ok()).toBeTruthy()
  await page.goto('/')
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  await page.getByRole('button', { name: 'Выбрать: Рабочая папка проекта', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'Рабочая папка', exact: true })
  const path = dialog.getByRole('textbox', { name: 'Путь к папке', exact: true })
  await expect(path).toHaveValue(nested)
  await dialog.getByRole('button', { name: 'В корень', exact: true }).click()
  await expect(path).toHaveValue(root)
  await expect(dialog.getByRole('button', { name: 'В корень', exact: true })).toBeDisabled()
  await dialog.getByRole('button', { name: 'other', exact: true }).click()
  await expect(path).toHaveValue(other)
  await dialog.getByRole('button', { name: 'Выбрать папку', exact: true }).click()
  await expect(dialog).not.toBeVisible()
  await page.getByRole('button', { name: 'Сохранить пространство', exact: true }).click()
  await expect.poll(async () => (await (await request.get(workspaceBase)).json()).working_directory).toBe(other)
  await page.reload()
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Выбрать: Рабочая папка проекта', exact: true })).toContainText(other)
})

test('native directory picker opens only on click and saves the selected host path', async ({ page, request }, testInfo) => {
  const settings = await (await request.get(workspaceBase)).json()
  const selected = `${settings.working_directory.replace(/\/$/, '')}/other`
  const calls: { method: string; path: string | null }[] = []
  let finish: () => void = () => {}
  const result = new Promise<void>(resolve => { finish = resolve })
  await page.route('**/api/directories/capabilities', route => route.fulfill({ json: { native_picker: true, platform: 'darwin' } }))
  await page.route('**/api/directories/choose', async route => {
    calls.push({ method: route.request().method(), path: route.request().postDataJSON().path })
    await result
    await route.fulfill({ json: { path: selected } })
  })
  await page.goto('/')
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  await page.getByRole('button', { name: 'Выбрать: Рабочая папка проекта', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'Рабочая папка', exact: true })
  await expect(dialog.getByRole('button', { name: 'Выбрать в Finder…', exact: true })).toBeVisible()
  await expect(dialog.getByRole('textbox', { name: 'Путь к папке', exact: true })).toHaveValue(settings.working_directory)
  expect(calls).toEqual([])
  await page.screenshot({ path: testInfo.outputPath('native-directory-picker.png'), fullPage: true })
  await dialog.getByRole('button', { name: 'Выбрать в Finder…', exact: true }).click()
  await expect(dialog.getByRole('button', { name: 'Выберите папку в Finder…', exact: true })).toBeDisabled()
  await expect(dialog.getByRole('button', { name: 'Выбрать папку', exact: true })).toBeDisabled()
  await expect.poll(() => calls).toEqual([{ method: 'POST', path: settings.working_directory }])
  finish()
  await expect(dialog).not.toBeVisible()
  await expect(page.getByRole('button', { name: 'Выбрать: Рабочая папка проекта', exact: true })).toContainText(selected)
  await page.getByRole('button', { name: 'Сохранить пространство', exact: true }).click()
  await expect.poll(async () => (await (await request.get(workspaceBase)).json()).working_directory).toBe(selected)
})

test('cancelling the native directory picker preserves the current directory', async ({ page, request }) => {
  const settings = await (await request.get(workspaceBase)).json()
  await page.route('**/api/directories/capabilities', route => route.fulfill({ json: { native_picker: true, platform: 'darwin' } }))
  await page.route('**/api/directories/choose', route => route.fulfill({ json: { path: null } }))
  await page.goto('/')
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  await page.getByRole('button', { name: 'Выбрать: Рабочая папка проекта', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'Рабочая папка', exact: true })
  await dialog.getByRole('button', { name: 'Выбрать в Finder…', exact: true }).click()
  await expect(dialog.getByText('Выбор отменён. Можно выбрать папку в списке ниже.', { exact: true })).toBeVisible()
  await expect(dialog.getByRole('textbox', { name: 'Путь к папке', exact: true })).toHaveValue(settings.working_directory)
  await expect(dialog.getByRole('button', { name: 'Выбрать папку', exact: true })).toBeEnabled()
  await dialog.getByRole('button', { name: 'Закрыть выбор папки', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Выбрать: Рабочая папка проекта', exact: true })).toContainText(settings.working_directory)
  await expect(page.getByRole('button', { name: 'Сохранить пространство', exact: true })).toBeDisabled()
  expect((await (await request.get(workspaceBase)).json()).working_directory).toBe(settings.working_directory)
})

test('native directory picker errors keep the browser directory fallback usable', async ({ page, request }) => {
  const settings = await (await request.get(workspaceBase)).json()
  const selected = `${settings.working_directory.replace(/\/$/, '')}/demo`
  await page.route('**/api/directories/capabilities', route => route.fulfill({ json: { native_picker: true, platform: 'darwin' } }))
  await page.route('**/api/directories/choose', route => route.fulfill({ status: 503, json: { detail: 'Не удалось открыть Finder. Выберите папку в списке.' } }))
  await page.goto('/')
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  await page.getByRole('button', { name: 'Выбрать: Рабочая папка проекта', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'Рабочая папка', exact: true })
  await dialog.getByRole('button', { name: 'Выбрать в Finder…', exact: true }).click()
  await expect(dialog.getByRole('alert')).toHaveText('Не удалось открыть Finder. Выберите папку в списке.')
  await dialog.getByRole('button', { name: 'demo', exact: true }).click()
  await expect(dialog.getByRole('textbox', { name: 'Путь к папке', exact: true })).toHaveValue(selected)
  await dialog.getByRole('button', { name: 'Выбрать папку', exact: true }).click()
  await page.getByRole('button', { name: 'Сохранить пространство', exact: true }).click()
  await expect.poll(async () => (await (await request.get(workspaceBase)).json()).working_directory).toBe(selected)
})

test('stopping from a Codex chat interrupts both active turns and cancels queued dependencies', async ({ page, request }, testInfo) => {
  const first = await createTasklet(request, 'Долгий Codex A', 'codex-slow-a', 20, 60)
  expect((await request.patch(`${workspaceBase}/tasklets/${first.id}`, { data: { prompt: '/goal Выполни долгую задачу [[label:codex-slow-a]][[delay:20]]' } })).ok()).toBeTruthy()
  const second = await createTasklet(request, 'Долгий Codex B', 'codex-slow-b', 20, 280)
  const dependent = await createTasklet(request, 'Зависимый Codex', 'codex-never', 0.1, 500)
  for (const parent of [first, second]) {
    expect((await request.post(`${workspaceBase}/edges`, { data: { source: parent.id, target: dependent.id } })).ok()).toBeTruthy()
  }
  await page.goto('/')
  await page.locator(`.react-flow__node[data-id="${first.id}"]`).click()
  await page.getByRole('tab', { name: /^Чат/ }).click()
  await page.getByRole('button', { name: 'Запустить пайплайн', exact: true }).click()
  await expect.poll(async () => (await events(request)).filter(event => event.kind === 'start').length).toBe(2)
  await expect.poll(async () => (await events(request)).some(event => event.kind === 'goal_set' && event.goal?.status === 'active')).toBe(true)
  expect((await workspace(request)).tasklets.find(tasklet => tasklet.id === dependent.id)?.status).toBe('queued')
  const inspector = page.getByRole('complementary', { name: 'Редактор тасклета', exact: true })
  await inspector.getByRole('button', { name: 'Остановить всё', exact: true }).click()
  await expect.poll(async () => (await workspace(request)).pipeline.status).toBe('cancelled')
  expect((await workspace(request)).tasklets.every(tasklet => !['running', 'queued'].includes(tasklet.status))).toBe(true)
  await expect.poll(async () => (await events(request)).filter(event => event.kind === 'cancelled').length).toBe(2)
  const recorded = await events(request)
  const started = recorded.filter(event => event.kind === 'start')
  expect(started.map(event => event.label).sort()).toEqual(['codex-slow-a', 'codex-slow-b'])
  expect(recorded.filter(event => event.kind === 'interrupt')).toHaveLength(2)
  expect(recorded.filter(event => event.kind === 'finish')).toHaveLength(0)
  expect(recorded.filter(event => event.kind === 'goal_set').map(event => event.goal?.status)).toEqual(['paused', 'active', 'paused'])
  for (const active of started) {
    expect(recorded.some(event => event.kind === 'cleanup' && event.thread_id === active.thread_id)).toBe(true)
    await expect.poll(async () => (await events(request)).some(event => event.kind === 'process_exit' && event.process_id === active.process_id && event.active_turns === 0)).toBe(true)
  }
  await expect(inspector.getByText('Остановлен', { exact: true })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('codex-stopped.png'), fullPage: true })
})

test('unsupported harness commands are rejected before a model turn starts', async ({ page, request }) => {
  const tasklet = await createTasklet(request, 'Команды исполнителя', 'commands')
  await page.goto('/')
  await page.locator(`.react-flow__node[data-id="${tasklet.id}"]`).click()
  await page.getByRole('tab', { name: /^Чат/ }).click()
  await page.getByPlaceholder('Напишите сообщение…', { exact: true }).fill('/unknown execute this')
  const responsePromise = page.waitForResponse(response => response.url().endsWith(`/tasklets/${tasklet.id}/messages`) && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Отправить сообщение', exact: true }).click()
  const response = await responsePromise
  expect(response.status()).toBe(422)
  await expect(page.getByText(/Команда \/unknown пока не поддерживается/)).toBeVisible()
  expect((await events(request)).filter(event => event.kind === 'start')).toHaveLength(0)
  const messages = await (await request.get(`${workspaceBase}/tasklets/${tasklet.id}/messages`)).json()
  expect(messages).toEqual([])
})

test('goal commands use the harness goal protocol instead of sending a literal slash prompt', async ({ page, request }) => {
  const tasklet = await createTasklet(request, 'Цель проекта', 'goal')
  const objective = 'Составь план проекта [[label:goal-plan]]'
  await page.goto('/')
  await page.locator(`.react-flow__node[data-id="${tasklet.id}"]`).click()
  await page.getByRole('tab', { name: /^Чат/ }).click()
  await page.getByPlaceholder('Напишите сообщение…', { exact: true }).fill(`/goal ${objective}`)
  await page.getByRole('button', { name: 'Отправить сообщение', exact: true }).click()
  await expect.poll(async () => (await workspace(request)).pipeline.status).toBe('completed')
  const recorded = await events(request)
  expect(recorded.some(event => event.kind === 'goal_set' && event.goal?.objective === objective && event.goal.status === 'active')).toBe(true)
  const started = recorded.filter(event => event.kind === 'start')
  expect(started).toHaveLength(1)
  expect(started[0].input).not.toContain(`/goal ${objective}`)
  await expect(page.getByRole('log', { name: 'История чата', exact: true })).toContainText('Готово:')
  const inspected = recorded.filter(event => event.kind === 'goal_get').length
  await page.getByPlaceholder('Напишите сообщение…', { exact: true }).fill('/goal inspect')
  const inspectResponse = page.waitForResponse(response => response.url().endsWith(`/tasklets/${tasklet.id}/messages`) && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Отправить сообщение', exact: true }).click()
  expect((await inspectResponse).ok()).toBeTruthy()
  await expect.poll(async () => (await events(request)).filter(event => event.kind === 'goal_get').length).toBeGreaterThan(inspected)
  await expect(page.getByRole('log', { name: 'История чата', exact: true })).toContainText('Статус: выполнена')
  expect((await events(request)).filter(event => event.kind === 'start')).toHaveLength(1)
})
