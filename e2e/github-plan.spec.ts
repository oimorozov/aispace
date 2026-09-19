import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

const backend = `${process.env.AISPACE_E2E_BACKEND_URL || 'http://127.0.0.1:8000'}/api`
const github = process.env.AISPACE_GITHUB_MOCK_URL
const provider = process.env.AISPACE_E2E_PROVIDER_URL
const planDialog = (page: Page) => page.getByRole('dialog', { name: 'Граф из GitHub Issues', exact: true })
const workspaceSelector = (page: Page) => page.getByRole('combobox', { name: 'Рабочее пространство', exact: true })
const scope = (id: string) => `${backend}/workspaces/${id}`
type Workspace = { id: string; name: string; tasklets: { id: string; title: string; prompt: string; status: string; position: { x: number; y: number }; source: { issue: { number: number; title: string; body: string; state: string; url: string }; repository: { full_name: string } } }[]; edges: { source: string; target: string; origin: string; explanation: string; pass_context: boolean }[]; pipeline: { status: string } }

test.skip(!github || !provider, 'Run with scripts/e2e_github.py and its isolated GitHub/AI fixtures')

async function workspaces(request: APIRequestContext): Promise<Workspace[]> {
  const values = await (await request.get(`${backend}/workspaces`)).json()
  return Promise.all(values.map(async (value: { id: string }) => (await request.get(scope(value.id))).json()))
}

async function fixture(request: APIRequestContext, value: Record<string, unknown> = {}) {
  expect((await request.post(`${github}/fixture`, { data: { planner: 'valid', ...value } })).ok()).toBeTruthy()
}

async function choose(page: Page, numbers = [1, 2, 3, 4]) {
  await page.getByRole('button', { name: 'Импортировать GitHub Issues', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'Импортировать GitHub Issues', exact: true })
  await dialog.getByRole('textbox', { name: 'Репозиторий GitHub', exact: true }).fill('demo/plan')
  await dialog.getByRole('button', { name: 'Загрузить issues', exact: true }).click()
  await dialog.getByRole('combobox', { name: 'Состояние issues', exact: true }).selectOption('all')
  await expect(dialog.getByRole('checkbox', { name: 'Выбрать issue #13', exact: true })).toHaveCount(0)
  for (const number of numbers) await dialog.getByRole('checkbox', { name: `Выбрать issue #${number}`, exact: true }).check()
  await dialog.getByRole('button', { name: 'Построить граф', exact: true }).click()
  await expect(planDialog(page)).toBeVisible()
}

async function ready(page: Page) {
  await expect(planDialog(page).getByRole('button', { name: 'Создать пространство', exact: true })).toBeEnabled()
}

async function create(page: Page, name: string) {
  await planDialog(page).getByRole('textbox', { name: 'Название пространства', exact: true }).fill(name)
  await planDialog(page).getByRole('button', { name: 'Создать пространство', exact: true }).click()
  await expect(planDialog(page)).toHaveCount(0)
  return workspaceSelector(page).inputValue()
}

test.beforeEach(async ({ request }) => {
  for (const item of await workspaces(request)) {
    await request.post(`${scope(item.id)}/pipeline/stop`)
    expect((await request.delete(scope(item.id))).ok()).toBeTruthy()
  }
  await fixture(request)
  expect((await request.patch(`${backend}/settings`, { data: { execution_mode: 'api', api_key: 'local-test-key', model: 'planner-fixture', base_url: `${provider}/v1`, max_parallel: 2 } })).ok()).toBeTruthy()
})

test('GitHub issues become an isolated executable DAG with original sources and parallel work', async ({ page, request }, testInfo) => {
  const original = await (await request.post(`${backend}/workspaces`, { data: { name: 'Исходный проект' } })).json()
  const oldTask = await (await request.post(`${scope(original.id)}/tasklets`, { data: { title: 'Существующая задача', prompt: '[[label:original]][[delay:0.05]]', position: { x: 80, y: 80 } } })).json()
  await request.post(`${scope(original.id)}/pipeline/start`)
  await expect.poll(async () => (await (await request.get(scope(original.id))).json()).pipeline.status).toBe('completed')
  const originalSnapshot = await (await request.get(scope(original.id))).json()
  const originalMessages = await (await request.get(`${scope(original.id)}/tasklets/${oldTask.id}/messages`)).json()
  await fixture(request)
  await page.goto('/')
  await choose(page)
  await ready(page)
  await expect(planDialog(page).locator('.react-flow__node')).toHaveCount(4)
  await expect(planDialog(page).locator('.import-edge-row')).toHaveCount(2)
  await planDialog(page).locator('.import-edge-row').filter({ hasText: '#1 Базовая схема' }).click()
  await expect(planDialog(page).getByRole('textbox', { name: 'Основание связи', exact: true })).toContainText('GitHub')
  await page.screenshot({ path: testInfo.outputPath('github-plan-desktop.png'), fullPage: true })
  const id = await create(page, 'Импортированный проект')
  const imported: Workspace = await (await request.get(scope(id))).json()
  expect(imported.tasklets).toHaveLength(4)
  expect(imported.tasklets.every(task => task.status === 'idle')).toBe(true)
  const byNumber = Object.fromEntries(imported.tasklets.map(task => [task.source.issue.number, task]))
  expect(imported.edges.map(edge => [edge.source, edge.target])).toEqual(expect.arrayContaining([[byNumber[1].id, byNumber[2].id], [byNumber[2].id, byNumber[3].id]]))
  expect(imported.edges.every(edge => !edge.pass_context && Boolean(edge.explanation))).toBe(true)
  expect(imported.edges.some(edge => edge.source === byNumber[4].id || edge.target === byNumber[4].id)).toBe(false)
  for (const task of imported.tasklets) {
    expect(task.prompt).toBe(task.source.issue.body || task.source.issue.title)
    expect(await (await request.get(`${scope(id)}/tasklets/${task.id}/messages`)).json()).toEqual([])
  }
  expect(byNumber[4].source.issue.state).toBe('closed')
  expect(byNumber[4].prompt).toBe('Документация')
  const audit = await (await request.get(`${github}/audit`)).json()
  expect(audit.requests.every((entry: { method: string }) => entry.method === 'GET')).toBe(true)
  expect(audit.planning).not.toHaveLength(0)
  expect(audit.executions).toHaveLength(0)
  await page.locator(`.react-flow__node[data-id="${byNumber[2].id}"]`).click()
  await expect(page.getByRole('link', { name: 'demo/plan#2', exact: true })).toHaveAttribute('href', 'https://github.com/demo/plan/issues/2')
  await expect(page.locator('.dependency-source')).toContainText('GitHub')
  await workspaceSelector(page).selectOption(original.id)
  await workspaceSelector(page).selectOption(id)
  await page.reload()
  await expect(workspaceSelector(page)).toHaveValue(id)
  await expect(page.getByRole('link', { name: 'demo/plan#2', exact: true })).toBeVisible()
  expect(await (await request.get(scope(original.id))).json()).toEqual(originalSnapshot)
  expect(await (await request.get(`${scope(original.id)}/tasklets/${oldTask.id}/messages`)).json()).toEqual(originalMessages)
  await page.getByRole('button', { name: 'Закрыть редактор', exact: true }).click()
  await page.getByRole('button', { name: 'Запустить пайплайн', exact: true }).click()
  await expect.poll(async () => (await (await request.get(scope(id))).json()).pipeline.status).toBe('completed')
  const events = (await (await request.get(`${provider}/audit`)).json()).events
  const time = (label: string, kind: string) => events.find((item: { label: string; kind: string }) => item.label === label && item.kind === kind)?.time
  expect(time('plan-b', 'start')).toBeGreaterThanOrEqual(time('plan-a', 'finish'))
  expect(time('plan-c', 'start')).toBeGreaterThanOrEqual(time('plan-b', 'finish'))
  expect(time('plan-d', 'start')).toBeLessThan(time('plan-a', 'finish'))
})

test('manual cycle is rejected and removing a native edge requires a recorded explicit decision', async ({ page, request }, testInfo) => {
  await page.goto('/')
  await choose(page)
  await ready(page)
  const dialog = planDialog(page)
  await dialog.getByRole('combobox', { name: 'Предшествующая issue', exact: true }).selectOption('400003')
  await dialog.getByRole('combobox', { name: 'Зависимая issue', exact: true }).selectOption('400001')
  await dialog.getByRole('textbox', { name: 'Объяснение новой связи', exact: true }).fill('Проверка конфликтующего порядка')
  await dialog.getByRole('button', { name: 'Добавить связь', exact: true }).click()
  await expect(dialog.getByRole('alert')).toContainText('Цикл')
  await expect(dialog.getByRole('button', { name: 'Создать пространство', exact: true })).toBeDisabled()
  await dialog.getByRole('button', { name: 'Удалить связь', exact: true }).click()
  await ready(page)
  await dialog.locator('.import-edge-row').filter({ hasText: '#1 Базовая схема' }).click()
  await dialog.getByRole('button', { name: 'Исключить исходную зависимость', exact: true }).click()
  await expect(dialog).toContainText('Задача сможет запускаться без ожидания исходной зависимости GitHub')
  await dialog.getByRole('textbox', { name: 'Причина решения', exact: true }).fill('Схема уже развёрнута в рабочем проекте')
  await dialog.getByRole('button', { name: 'Подтвердить решение', exact: true }).click()
  await ready(page)
  await page.setViewportSize({ width: 390, height: 844 })
  await dialog.evaluate(element => { element.scrollTop = 0 })
  await expect.poll(async () => dialog.locator('.import-graph').evaluate(element => {
    const area = element.getBoundingClientRect()
    const nodes = [...element.querySelectorAll('.react-flow__node')]
    return nodes.length === 4 && nodes.every(node => {
      const box = node.getBoundingClientRect()
      return box.width > 0 && box.left >= area.left && box.right <= area.right && box.top >= area.top && box.bottom <= area.bottom
    })
  })).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('github-plan-mobile.png'), fullPage: true })
  expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
  const id = await create(page, 'С явным исключением')
  const imported = await (await request.get(scope(id))).json()
  expect(imported.edges).toHaveLength(1)
  expect(imported.import_metadata.decisions).toEqual([{ kind: 'ignore_github', source: 400001, target: 400002, reason: 'Схема уже развёрнута в рабочем проекте' }])
})

test('external blockers cannot be accepted silently and adding the missing issue replans the whole graph', async ({ page, request }) => {
  await page.goto('/')
  await choose(page, [2, 3, 4])
  const dialog = planDialog(page)
  await expect(dialog.getByRole('region', { name: 'Внешние зависимости', exact: true })).toBeVisible()
  await expect(dialog.getByRole('button', { name: 'Создать пространство', exact: true })).toBeDisabled()
  await dialog.getByRole('button', { name: 'Добавить issue #1', exact: true }).click()
  await ready(page)
  await expect(dialog.locator('.react-flow__node')).toHaveCount(4)
  await expect(dialog.getByRole('region', { name: 'Внешние зависимости', exact: true })).toHaveCount(0)
  await create(page, 'С включённой предпосылкой')
  expect((await workspaces(request))[0].tasklets).toHaveLength(4)
  await choose(page, [2, 3])
  await dialog.getByRole('button', { name: 'Отметить выполненной', exact: true }).click()
  await dialog.getByRole('textbox', { name: 'Причина решения', exact: true }).fill('Предпосылка выполнена и проверена отдельно')
  await dialog.getByRole('button', { name: 'Подтвердить решение', exact: true }).click()
  await ready(page)
  const id = await create(page, 'Предпосылка вне пространства')
  const value = await (await request.get(scope(id))).json()
  expect(value.tasklets).toHaveLength(2)
  expect(value.import_metadata.decisions[0].kind).toBe('external_completed')
})

test('invalid AI plans stay recoverable and never create partial workspaces', async ({ page, request }) => {
  await fixture(request, { planner: 'cycle' })
  await page.goto('/')
  await choose(page)
  const errors: Record<string, RegExp> = { cycle: /Цикл/, self: /не может зависеть от себя/, foreign: /вне выбранного/, reversed: /перевернул/, invalid: /некорректный JSON/, tool: /инструмент/ }
  for (const [mode, message] of Object.entries(errors)) {
    if (mode !== 'cycle') {
      await fixture(request, { planner: mode })
      await planDialog(page).getByRole('button', { name: 'Повторить анализ', exact: true }).click()
    }
    await expect(planDialog(page).getByRole('alert')).toContainText(message)
    await expect(planDialog(page).getByRole('button', { name: 'Создать пространство', exact: true })).toBeDisabled()
    expect(await workspaces(request)).toEqual([])
  }
  await planDialog(page).getByRole('button', { name: 'Использовать только известные зависимости', exact: true }).click()
  await ready(page)
  await expect(planDialog(page).locator('.import-edge-row')).toHaveCount(1)
})

test('a lost response survives refresh and retries the exact operation while a new intentional import remains possible', async ({ page, request }) => {
  await page.goto('/')
  await choose(page)
  await ready(page)
  let firstPayload: unknown
  await page.route('**/api/github/imports', async route => {
    firstPayload = route.request().postDataJSON()
    const response = await route.fetch()
    expect(response.status()).toBe(201)
    await route.abort('connectionreset')
  }, { times: 1 })
  await planDialog(page).getByRole('button', { name: 'Создать пространство', exact: true }).click()
  await expect(planDialog(page).getByRole('button', { name: 'Повторить создание', exact: true })).toBeEnabled()
  expect(await workspaces(request)).toHaveLength(1)
  await page.reload()
  await expect(planDialog(page).getByRole('button', { name: 'Повторить создание', exact: true })).toBeEnabled()
  await expect(planDialog(page).getByRole('textbox', { name: 'Название пространства', exact: true })).toBeDisabled()
  const retry = page.waitForRequest(value => value.url().endsWith('/api/github/imports'))
  await planDialog(page).getByRole('button', { name: 'Повторить создание', exact: true }).click()
  expect((await retry).postDataJSON()).toEqual(firstPayload)
  await expect(planDialog(page)).toHaveCount(0)
  expect(await workspaces(request)).toHaveLength(1)
  await choose(page)
  await ready(page)
  await create(page, 'Второй осознанный импорт')
  expect(await workspaces(request)).toHaveLength(2)
})

test('stale and unavailable GitHub snapshots require rereading without silently losing selected issues', async ({ page, request }) => {
  await page.goto('/')
  await choose(page)
  await ready(page)
  await fixture(request, { changed_issue: 3 })
  await planDialog(page).getByRole('button', { name: 'Создать пространство', exact: true }).click()
  await expect(planDialog(page).getByRole('alert')).toContainText('изменились')
  expect(await workspaces(request)).toEqual([])
  await planDialog(page).getByRole('button', { name: 'Перечитать issues', exact: true }).click()
  await ready(page)
  await expect(planDialog(page).locator('.react-flow__node')).toHaveCount(4)
  await fixture(request, { missing_issue: 3 })
  await planDialog(page).getByRole('button', { name: 'Создать пространство', exact: true }).click()
  await expect(planDialog(page).getByRole('alert')).toContainText('недоступны')
  expect(await workspaces(request)).toEqual([])
  await fixture(request, { dependencies_failure: true })
  await planDialog(page).getByRole('button', { name: 'Перечитать issues', exact: true }).click()
  await expect(planDialog(page).getByRole('alert')).toContainText('Не прочитаны зависимости')
  await expect(planDialog(page).getByRole('button', { name: 'Создать пространство', exact: true })).toBeDisabled()
  await fixture(request)
  await planDialog(page).getByRole('button', { name: 'Перечитать issues', exact: true }).click()
  await ready(page)
  await create(page, 'Актуальный снимок')
})

test('analysis cancellation leaves no workspace and missing AI configuration preserves the selection', async ({ page, request }) => {
  await request.patch(`${backend}/settings`, { data: { api_key: null, model: '' } })
  await page.goto('/')
  await choose(page)
  await expect(planDialog(page).getByRole('alert')).toContainText('Настройте API-ключ и модель')
  await planDialog(page).getByRole('button', { name: 'Настроить подключение AI', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Настройки', exact: true })).toBeVisible()
  await request.patch(`${backend}/settings`, { data: { api_key: 'local-test-key', model: 'planner-fixture' } })
  await fixture(request, { planner: 'slow' })
  await page.getByRole('button', { name: 'Импортировать GitHub Issues', exact: true }).click()
  await expect(planDialog(page)).toContainText('4 issues')
  await expect(planDialog(page).getByText('Анализируем зависимости…', { exact: true })).toBeVisible()
  await planDialog(page).getByRole('button', { name: 'Отменить анализ', exact: true }).click()
  await expect(planDialog(page)).toHaveCount(0)
  expect(await workspaces(request)).toEqual([])
})

test('Codex subscription plans from the UI in an isolated temporary session and cancellation closes its process', async ({ page, request }) => {
  await page.goto('/')
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  await page.getByRole('button', { name: /^ChatGPT Подписка/ }).click()
  await expect(page.getByText('ChatGPT подключён', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Сохранить настройки', exact: true }).click()
  await expect.poll(async () => (await (await request.get(`${backend}/settings`)).json()).execution_mode).toBe('codex')
  await fixture(request)
  await choose(page)
  await ready(page)
  let audit = await (await request.get(`${github}/audit`)).json()
  const thread = audit.events.find((event: { kind: string }) => event.kind === 'planning_thread')
  expect(thread.parameters).toMatchObject({ ephemeral: true, sandbox: 'read-only', approvalPolicy: 'never', dynamicTools: [], selectedCapabilityRoots: [] })
  expect(audit.executions).toEqual([])
  expect(await workspaces(request)).toEqual([])
  expect(audit.events.some((event: { kind: string; process_id: string; active_turns: number }) => event.kind === 'process_exit' && event.process_id === thread.process_id && event.active_turns === 0)).toBe(true)
  const id = await create(page, 'План через подписку')
  const imported = await (await request.get(scope(id))).json()
  expect(imported.tasklets.every((task: { status: string }) => task.status === 'idle')).toBe(true)
  await fixture(request, { planner: 'slow' })
  await choose(page)
  await expect.poll(async () => (await (await request.get(`${github}/audit`)).json()).events.some((event: { kind: string }) => event.kind === 'planning_start')).toBe(true)
  await planDialog(page).getByRole('button', { name: 'Отменить анализ', exact: true }).click()
  await expect(planDialog(page)).toHaveCount(0)
  audit = await (await request.get(`${github}/audit`)).json()
  expect(audit.events.some((event: { kind: string }) => event.kind === 'planning_cancelled')).toBe(true)
  expect(audit.events.some((event: { kind: string }) => event.kind === 'planning_cleanup')).toBe(true)
  expect(audit.events.filter((event: { kind: string }) => event.kind === 'process_exit').every((event: { active_turns: number }) => event.active_turns === 0)).toBe(true)
  expect(await workspaces(request)).toHaveLength(1)
  await fixture(request, { planner: 'tool' })
  await choose(page)
  await expect(planDialog(page).getByRole('alert')).toContainText(/инструмент|выполнить действие/)
  await expect(planDialog(page).getByRole('button', { name: 'Создать пространство', exact: true })).toBeDisabled()
  expect((await (await request.get(`${github}/audit`)).json()).executions).toEqual([])
})

test('excluding a dependent issue records the decision and does not silently include another repository', async ({ page, request }) => {
  await fixture(request, { external_other: true })
  await page.goto('/')
  await choose(page)
  const dialog = planDialog(page)
  await expect(dialog.getByRole('region', { name: 'Внешние зависимости', exact: true })).toContainText('demo/other#5')
  await expect(dialog.getByRole('button', { name: 'Добавить issue #5', exact: true })).toHaveCount(0)
  await dialog.getByRole('button', { name: 'Исключить зависимую issue #3', exact: true }).click()
  await ready(page)
  const id = await create(page, 'Без внешне заблокированной задачи')
  const imported = await (await request.get(scope(id))).json()
  expect(imported.tasklets).toHaveLength(3)
  expect(imported.tasklets.some((item: { source: { issue: { number: number } } }) => item.source.issue.number === 3)).toBe(false)
  expect(imported.import_metadata.selection_changes).toContainEqual(expect.objectContaining({ kind: 'excluded_issue', issue_id: 400003, number: 3 }))
})

test('corrupted import drafts cannot crash or execute the restored page', async ({ page, request }) => {
  await page.goto('/')
  await page.evaluate(() => sessionStorage.setItem('aispace.github-import.v1', JSON.stringify({ selection: { id: 'broken', issues: [] }, edges: [], decisions: [], name: 'Broken' })))
  await page.reload()
  await expect(page.getByRole('button', { name: 'Импортировать GitHub Issues', exact: true })).toBeEnabled()
  await choose(page)
  await ready(page)
  const before = (await (await request.get(`${github}/audit`)).json()).planning.length
  await page.evaluate(() => {
    const value = JSON.parse(sessionStorage.getItem('aispace.github-import.v1')!)
    value.plan.positions = { malicious: null }
    value.edges = [{ source: 'bad', target: null, origin: 'other', explanation: {} }]
    sessionStorage.setItem('aispace.github-import.v1', JSON.stringify(value))
  })
  await page.reload()
  await expect(planDialog(page)).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Импортировать GitHub Issues', exact: true })).toBeEnabled()
  expect((await (await request.get(`${github}/audit`)).json()).planning).toHaveLength(before)
  expect(await workspaces(request)).toEqual([])
})

test('refresh retrieves AI edges that completed offscreen and preserves subsequent manual graph edits', async ({ page, request }) => {
  await fixture(request, { planner_delay: 0.3 })
  await page.goto('/')
  let runningSnapshot: Record<string, unknown> | undefined
  await page.route('**/api/github/plans/*', async route => {
    if (route.request().method() === 'GET' && runningSnapshot) await route.fulfill({ json: runningSnapshot })
    else await route.continue()
  })
  const started = page.waitForResponse(response => response.url().endsWith('/api/github/plans') && response.request().method() === 'POST')
  await choose(page)
  runningSnapshot = await (await started).json()
  await expect.poll(async () => (await (await request.get(`${backend}/github/plans/${runningSnapshot!.id}`)).json()).status).toBe('completed')
  await expect.poll(async () => page.evaluate(() => JSON.parse(sessionStorage.getItem('aispace.github-import.v1')!).plan.status)).toBe('running')
  await page.unroute('**/api/github/plans/*')
  await page.reload()
  await ready(page)
  await expect(planDialog(page).locator('.import-edge-row')).toHaveCount(2)
  await planDialog(page).locator('.import-edge-row').filter({ hasText: '#3 Интерфейс' }).click()
  await planDialog(page).getByRole('button', { name: 'Удалить связь', exact: true }).click()
  await ready(page)
  await page.reload()
  await ready(page)
  await expect(planDialog(page).locator('.import-edge-row')).toHaveCount(1)
  expect(await workspaces(request)).toEqual([])
})

test('a real write failure rolls back every imported row and retry keeps the same operation', async ({ page, request }) => {
  await page.goto('/')
  await choose(page)
  await ready(page)
  const before = await (await request.get(`${backend}/e2e/import-counts`)).json()
  expect((await request.post(`${backend}/e2e/import-failure`, { data: { enabled: true } })).ok()).toBe(true)
  let submitted: unknown
  try {
    const failed = page.waitForResponse(response => response.url().endsWith('/api/github/imports'))
    await planDialog(page).getByRole('button', { name: 'Создать пространство', exact: true }).click()
    const response = await failed
    expect(response.status()).toBe(500)
    submitted = response.request().postDataJSON()
    await expect(planDialog(page).getByRole('button', { name: 'Повторить создание', exact: true })).toBeEnabled()
    expect(await workspaces(request)).toEqual([])
    expect(await (await request.get(`${backend}/e2e/import-counts`)).json()).toEqual(before)
  } finally {
    expect((await request.post(`${backend}/e2e/import-failure`, { data: { enabled: false } })).ok()).toBe(true)
  }
  const retried = page.waitForRequest(value => value.url().endsWith('/api/github/imports'))
  await planDialog(page).getByRole('button', { name: 'Повторить создание', exact: true }).click()
  expect((await retried).postDataJSON()).toEqual(submitted)
  await expect(planDialog(page)).toHaveCount(0)
  const values = await workspaces(request)
  expect(values).toHaveLength(1)
  expect(values[0].tasklets).toHaveLength(4)
  expect(values[0].edges).toHaveLength(2)
})
