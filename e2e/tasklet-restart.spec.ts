import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

const backend = `${(process.env.AISPACE_E2E_API_URL || 'http://127.0.0.1:8000').replace(/\/$/, '')}/api`
const provider = 'http://127.0.0.1:8766'
const providerBaseUrl = process.env.AISPACE_E2E_PROVIDER_URL || `${provider}/v1`
let wid: string
const scope = () => `${backend}/workspaces/${wid}`
const node = (page: Page, id: string) => page.locator(`.react-flow__node[data-id="${id}"]`)
const restartButton = (page: Page, id: string, title: string) => node(page, id).getByRole('button', { name: `Перезапустить тасклет: ${title}`, exact: true })

async function snapshot(request: APIRequestContext) { return (await request.get(scope())).json() }
async function messages(request: APIRequestContext, id: string) { return (await request.get(`${scope()}/tasklets/${id}/messages`)).json() }
async function starts(request: APIRequestContext) { return (await (await request.get(`${provider}/audit`)).json()).events.filter((event: { kind: string }) => event.kind === 'start') }

async function create(request: APIRequestContext, title: string, delay = 0.1, x = 100, y = 100) {
  const response = await request.post(`${scope()}/tasklets`, { data: { title, prompt: `[[label:${title}]][[delay:${delay}]]`, position: { x, y } } })
  expect(response.ok()).toBeTruthy()
  return response.json()
}

async function connect(request: APIRequestContext, source: string, target: string) {
  expect((await request.post(`${scope()}/edges`, { data: { source, target, pass_context: true } })).ok()).toBeTruthy()
}

async function run(request: APIRequestContext) {
  expect((await request.post(`${scope()}/pipeline/start`, { data: {} })).ok()).toBeTruthy()
  await expect.poll(async () => (await snapshot(request)).pipeline.status).toBe('completed')
}

test.beforeEach(async ({ request }) => {
  for (const item of await (await request.get(`${backend}/workspaces`)).json()) {
    expect((await request.post(`${backend}/workspaces/${item.id}/pipeline/stop`)).ok()).toBeTruthy()
    expect((await request.delete(`${backend}/workspaces/${item.id}`)).ok()).toBeTruthy()
  }
  wid = (await (await request.post(`${backend}/workspaces`, { data: { name: 'Точечный перезапуск' } })).json()).id
  expect((await request.patch(`${backend}/settings`, { data: { execution_mode: 'api', api_key: 'local-test-key', base_url: providerBaseUrl, model: 'test-model', max_parallel: 2 } })).ok()).toBeTruthy()
  await request.post(`${provider}/reset`)
})

test('cancelled cards stay red after selection and reload; keyboard restart runs only one', async ({ page, request }, testInfo) => {
  const a = await create(request, 'A', 3, 80, 60)
  const b = await create(request, 'B', 3, 80, 380)
  const c = await create(request, 'C', 0.1, 500, 220)
  await connect(request, a.id, c.id)
  await connect(request, b.id, c.id)
  await page.goto('/')
  await page.getByRole('button', { name: 'Запустить пайплайн', exact: true }).click()
  await expect.poll(async () => (await snapshot(request)).tasklets.filter((task: { status: string }) => task.status === 'running').length).toBe(2)
  await page.getByRole('button', { name: 'Остановить всё', exact: true }).first().click()
  await expect.poll(async () => (await snapshot(request)).pipeline.status).toBe('cancelled')
  for (const item of [a, b, c]) {
    await expect(node(page, item.id)).toContainText('Остановлен')
    await expect(node(page, item.id).locator('.tasklet-node')).toHaveCSS('border-top-color', 'rgb(196, 83, 95)')
  }
  await node(page, a.id).getByRole('heading', { name: 'A', exact: true }).click()
  await page.reload()
  const selected = node(page, a.id).locator('.tasklet-node')
  await expect(selected).toHaveClass(/is-selected/)
  await expect(selected).toHaveCSS('border-top-color', 'rgb(196, 83, 95)')
  await page.keyboard.press('Tab')
  await node(page, a.id).focus()
  await expect(selected).toHaveCSS('outline-width', '3px')
  const before = await snapshot(request)
  const histories = await Promise.all([messages(request, b.id), messages(request, c.id)])
  await restartButton(page, c.id, 'C').click()
  await expect(page.getByRole('alert').filter({ hasText: 'Сначала завершите зависимости' })).toBeVisible()
  expect(await snapshot(request)).toEqual(before)
  await restartButton(page, a.id, 'A').focus()
  await page.keyboard.press('Space')
  await expect.poll(async () => (await snapshot(request)).pipeline.status).toBe('completed')
  const after = await snapshot(request)
  expect(after.tasklets.find((task: { id: string }) => task.id === a.id).position).toEqual(a.position)
  expect(after.tasklets.find((task: { id: string }) => task.id === b.id).status).toBe('cancelled')
  expect(after.tasklets.find((task: { id: string }) => task.id === c.id).status).toBe('idle')
  expect(await Promise.all([messages(request, b.id), messages(request, c.id)])).toEqual(histories)
  expect((await starts(request)).map((event: { label: string }) => event.label).sort()).toEqual(['A', 'A', 'B'])
  await expect(selected).not.toHaveCSS('border-top-color', 'rgb(196, 83, 95)')
  await page.screenshot({ path: testInfo.outputPath('cancelled-and-restarted.png'), fullPage: true })
})

test('completed middle task restarts alone and invalidates only descendant status', async ({ page, request }) => {
  const a = await create(request, 'A', 0.1, 80, 70)
  const b = await create(request, 'B', 0.1, 420, 70)
  const c = await create(request, 'C', 0.1, 760, 70)
  const d = await create(request, 'D', 0.1, 80, 390)
  await connect(request, a.id, b.id)
  await connect(request, b.id, c.id)
  await run(request)
  const histories = await Promise.all([messages(request, a.id), messages(request, c.id), messages(request, d.id)])
  const original = (await snapshot(request)).tasklets.find((task: { id: string }) => task.id === b.id).conversation_id
  await page.goto('/')
  await restartButton(page, b.id, 'B').focus()
  await page.keyboard.press('Enter')
  await expect.poll(async () => (await snapshot(request)).tasklets.find((task: { id: string }) => task.id === b.id).conversation_id).not.toBe(original)
  await expect.poll(async () => (await snapshot(request)).pipeline.status).toBe('completed')
  expect((await starts(request)).map((event: { label: string }) => event.label).sort()).toEqual(['A', 'B', 'B', 'C', 'D'])
  expect(await Promise.all([messages(request, a.id), messages(request, c.id), messages(request, d.id)])).toEqual(histories)
  const state = await snapshot(request)
  expect(state.tasklets.find((task: { id: string }) => task.id === c.id).status).toBe('idle')
  expect(await messages(request, b.id)).toHaveLength(2)
})

for (const mobile of [false, true]) {
  test(`dirty forms block card restart; chat draft survives inspector restart${mobile ? ' on mobile' : ''}`, async ({ page, request }, testInfo) => {
    if (mobile) await page.setViewportSize({ width: 390, height: 844 })
    const task = await create(request, 'Draft', 0.2)
    await run(request)
    await page.goto('/')
    await node(page, task.id).getByRole('heading', { name: 'Draft', exact: true }).click()
    const editor = page.getByRole('complementary', { name: 'Редактор тасклета', exact: true })
    await editor.getByRole('textbox', { name: 'Промпт', exact: true }).fill('Unsaved prompt')
    await expect(restartButton(page, task.id, 'Draft')).toBeDisabled()
    await expect(editor.getByRole('button', { name: 'Перезапустить тасклет: Draft', exact: true })).toBeDisabled()
    await editor.getByRole('textbox', { name: 'Промпт', exact: true }).fill(task.prompt)
    await expect(restartButton(page, task.id, 'Draft')).toBeEnabled()
    await page.getByRole('button', { name: 'Настройки', exact: true }).click()
    await page.getByRole('textbox', { name: 'Модель по умолчанию', exact: true }).fill('unsaved-model')
    await page.getByRole('button', { name: 'Пространство', exact: true }).click()
    await expect(restartButton(page, task.id, 'Draft')).toBeDisabled()
    await expect(node(page, task.id)).toContainText('Сохраните изменения настроек')
    await page.getByRole('button', { name: 'Настройки', exact: true }).click()
    await page.getByRole('textbox', { name: 'Модель по умолчанию', exact: true }).fill('test-model')
    await page.getByRole('button', { name: 'Пространство', exact: true }).click()
    await editor.getByRole('tab', { name: /^Чат/ }).click()
    await editor.getByPlaceholder('Напишите сообщение…', { exact: true }).fill('Keep this unsent draft')
    const button = editor.getByRole('button', { name: 'Перезапустить тасклет: Draft', exact: true })
    await expect(button).toBeVisible()
    const response = page.waitForResponse(value => value.url().endsWith(`/tasklets/${task.id}/restart`))
    await button.click()
    expect((await response).status()).toBe(202)
    await expect.poll(async () => (await snapshot(request)).pipeline.status).toBe('completed')
    await expect(editor.getByPlaceholder('Напишите сообщение…', { exact: true })).toHaveValue('Keep this unsent draft')
    await expect(editor.locator('.chat-message')).toHaveCount(2)
    expect(JSON.stringify(await starts(request))).not.toContain('Keep this unsent draft')
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    await page.screenshot({ path: testInfo.outputPath('restart-inspector.png'), fullPage: true })
  })
}

test('pending restart remains blocked after completion until its response arrives', async ({ page, request }) => {
  const a = await create(request, 'A', 0.8)
  const b = await create(request, 'B', 0.1, 500)
  await run(request)
  await page.goto('/')
  let intercepted = false
  let release: () => void = () => {}
  const gate = new Promise<void>(resolve => { release = resolve })
  await page.route(`**/api/workspaces/${wid}/tasklets/${a.id}/restart`, async route => {
    const response = await route.fetch()
    intercepted = true
    await gate
    await route.fulfill({ response })
  })
  await restartButton(page, a.id, 'A').click()
  await expect.poll(() => intercepted).toBe(true)
  expect((await request.post(`${scope()}/tasklets/${a.id}/restart`)).status()).toBe(409)
  await expect(restartButton(page, b.id, 'B')).toBeDisabled()
  await expect.poll(async () => (await snapshot(request)).pipeline.status).toBe('completed')
  await expect(restartButton(page, a.id, 'A')).toBeDisabled()
  release()
  await expect(restartButton(page, a.id, 'A')).toBeEnabled()
  expect((await starts(request)).map((event: { label: string }) => event.label).sort()).toEqual(['A', 'A', 'B'])
})

test('failed cards offer restart and the next attempt begins a new chat', async ({ page, request }) => {
  const task = await create(request, 'Failure')
  await run(request)
  expect((await request.patch(`${backend}/settings`, { data: { base_url: 'http://127.0.0.1:1/v1' } })).ok()).toBeTruthy()
  expect((await request.post(`${scope()}/tasklets/${task.id}/restart`)).ok()).toBeTruthy()
  await expect.poll(async () => (await snapshot(request)).pipeline.status).toBe('failed')
  await page.goto('/')
  await expect(node(page, task.id)).toContainText('Ошибка')
  await expect(restartButton(page, task.id, 'Failure')).toBeEnabled()
  expect((await request.patch(`${backend}/settings`, { data: { base_url: providerBaseUrl } })).ok()).toBeTruthy()
  await restartButton(page, task.id, 'Failure').click()
  await expect.poll(async () => (await snapshot(request)).pipeline.status).toBe('completed')
  expect(await messages(request, task.id)).toHaveLength(2)
})
