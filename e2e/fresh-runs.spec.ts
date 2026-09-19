import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

const backend = `${(process.env.AISPACE_E2E_API_URL || 'http://127.0.0.1:8000').replace(/\/$/, '')}/api`
const provider = 'http://127.0.0.1:8766'
const providerBaseUrl = process.env.AISPACE_E2E_PROVIDER_URL || `${provider}/v1`
let wid: string
const scope = () => `${backend}/workspaces/${wid}`
const chatPath = (id: string) => `${scope()}/tasklets/${id}/messages`
const node = (page: Page, id: string) => page.locator(`.react-flow__node[data-id="${id}"]`)

async function snapshot(request: APIRequestContext) {
  return (await request.get(scope())).json()
}

async function create(request: APIRequestContext, title: string, x = 100) {
  const response = await request.post(`${scope()}/tasklets`, { data: { title, prompt: `[[label:${title}]][[delay:0.15]]`, position: { x, y: 100 } } })
  expect(response.ok()).toBeTruthy()
  return response.json()
}

async function run(request: APIRequestContext, ids?: string[]) {
  expect((await request.post(`${scope()}/pipeline/start`, { data: ids ? { tasklet_ids: ids } : {} })).ok()).toBeTruthy()
  await expect.poll(async () => (await snapshot(request)).pipeline.status).toBe('completed')
}

async function chat(page: Page, id: string) {
  await node(page, id).click()
  await page.getByRole('tab', { name: /^Чат/ }).click()
}

async function changePrompt(request: APIRequestContext, id: string, label: string, delay = 0.15) {
  expect((await request.patch(`${scope()}/tasklets/${id}`, { data: { prompt: `[[label:${label}]][[delay:${delay}]]` } })).ok()).toBeTruthy()
}

test.beforeEach(async ({ request }) => {
  for (const item of await (await request.get(`${backend}/workspaces`)).json()) {
    expect((await request.post(`${backend}/workspaces/${item.id}/pipeline/stop`)).ok()).toBeTruthy()
    expect((await request.delete(`${backend}/workspaces/${item.id}`)).ok()).toBeTruthy()
  }
  const response = await request.post(`${backend}/workspaces`, { data: { name: 'Новые разговоры' } })
  wid = (await response.json()).id
  expect((await request.patch(`${backend}/settings`, { data: { execution_mode: 'api', api_key: 'local-test-key', base_url: providerBaseUrl, model: 'test-model', max_parallel: 2 } })).ok()).toBeTruthy()
  await request.post(`${provider}/reset`)
})

test('fresh run discards late old history and old SSE while preserving new streamed messages', async ({ page, request }) => {
  const task = await create(request, 'old-run')
  await run(request)
  expect((await request.post(chatPath(task.id), { data: { content: '[[label:old-follow-up]][[delay:0.1]]' } })).ok()).toBeTruthy()
  await expect.poll(async () => (await snapshot(request)).pipeline.status).toBe('completed')
  const oldMessages = await (await request.get(chatPath(task.id))).json()
  await page.addInitScript(() => {
    const Original = window.EventSource
    const sources: EventSource[] = []
    Object.assign(window, { testEventSources: sources })
    window.EventSource = class extends Original {
      constructor(url: string | URL, options?: EventSourceInit) { super(url, options); sources.push(this) }
    }
  })
  let release: () => void = () => {}
  const gate = new Promise<void>(resolve => { release = resolve })
  let captured = false
  let delivered = false
  await page.route(`**/api/workspaces/${wid}/tasklets/${task.id}/messages`, async route => {
    if (captured || route.request().method() !== 'GET') return route.continue()
    const response = await route.fetch()
    captured = true
    await gate
    await route.fulfill({ response })
    delivered = true
  })
  await page.goto('/')
  await chat(page, task.id)
  await expect.poll(() => captured).toBe(true)
  await changePrompt(request, task.id, 'new-run', 0.5)
  await page.getByRole('button', { name: 'Запустить пайплайн', exact: true }).click()
  await expect(page.getByText('Готово: new-run.', { exact: true })).toBeVisible()
  release()
  await expect.poll(() => delivered).toBe(true)
  await page.evaluate(messages => {
    const source = (window as unknown as { testEventSources: EventSource[] }).testEventSources.at(-1)!
    for (const message of messages) source.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(message) }))
  }, oldMessages)
  await expect(page.getByRole('log')).not.toContainText('old-run')
  await expect(page.getByRole('log')).not.toContainText('old-follow-up')
  await expect(page.locator('.chat-message')).toHaveCount(2)
  await expect(page.getByText('Готово: new-run.', { exact: true })).toBeVisible()
  const audit = (await (await request.get(`${provider}/audit`)).json()).events
  const latest = audit.filter((event: { kind: string }) => event.kind === 'start').at(-1)
  expect(latest.messages).toEqual([{ role: 'user', content: '[[label:new-run]][[delay:0.5]]' }])
  await page.reload()
  await expect(page.getByText('Готово: new-run.', { exact: true })).toBeVisible()
  await expect(page.locator('.chat-message')).toHaveCount(2)
})

test('reconnect after a missed reset removes cached history and keeps untouched task chats', async ({ page, request }) => {
  const first = await create(request, 'first-old')
  const second = await create(request, 'untouched', 500)
  await run(request)
  await page.addInitScript(() => {
    const Original = window.EventSource
    window.EventSource = new Proxy(Original, {
      construct(Target, args) {
        const source = Reflect.construct(Target, args) as EventSource
        const listeners: Parameters<EventTarget['addEventListener']>[] = []
        const add = source.addEventListener.bind(source)
        const close = source.close.bind(source)
        let replacement: EventSource | undefined
        source.addEventListener = (...values: Parameters<EventTarget['addEventListener']>) => { listeners.push(values); add(...values) }
        source.close = () => { close(); replacement?.close() }
        Object.assign(window, { testStream: {
          disconnect: () => { source.close(); source.dispatchEvent(new Event('error')) },
          reconnect: () => {
            replacement = new Original(source.url)
            replacement.onopen = source.onopen
            replacement.onerror = source.onerror
            for (const listener of listeners) replacement.addEventListener(...listener)
          },
        } })
        return source
      },
    })
  })
  await page.goto('/')
  await chat(page, first.id)
  await expect(page.getByText('Готово: first-old.', { exact: true })).toBeVisible()
  await page.evaluate(() => (window as unknown as { testStream: { disconnect: () => void } }).testStream.disconnect())
  await expect(page.getByText('Нет соединения', { exact: true })).toBeVisible()
  await changePrompt(request, first.id, 'first-new')
  await run(request, [first.id])
  await expect(page.getByText('Готово: first-old.', { exact: true })).toBeVisible()
  await page.evaluate(() => (window as unknown as { testStream: { reconnect: () => void } }).testStream.reconnect())
  await expect(page.getByText('Готово: first-new.', { exact: true })).toBeVisible({ timeout: 15_000 })
  await expect(page.getByRole('log')).not.toContainText('first-old')
  await expect(page.locator('.chat-message')).toHaveCount(2)
  await chat(page, second.id)
  await expect(page.getByText('Готово: untouched.', { exact: true })).toBeVisible()
  await chat(page, first.id)
  await expect(page.getByText('Готово: first-new.', { exact: true })).toBeVisible()
  await expect(page.getByRole('log')).not.toContainText('first-old')
})

test('late current history cannot erase messages received during its request', async ({ page, request }) => {
  const task = await create(request, 'stream')
  await page.goto('/')
  expect((await request.post(chatPath(task.id), { data: { content: '[[label:streamed-follow-up]][[delay:1.5]]' } })).ok()).toBeTruthy()
  await expect(page.getByText('Выполняется', { exact: true }).first()).toBeVisible()
  let captured = false
  let release: () => void = () => {}
  const gate = new Promise<void>(resolve => { release = resolve })
  await page.route(`**/api/workspaces/${wid}/tasklets/${task.id}/messages`, async route => {
    if (captured) return route.continue()
    const response = await route.fetch()
    captured = true
    await gate
    await route.fulfill({ response })
  })
  await chat(page, task.id)
  await expect.poll(() => captured).toBe(true)
  await expect(page.getByText('Готово: streamed-follow-up.', { exact: true })).toBeVisible()
  release()
  await expect(page.getByRole('log')).not.toContainText('Загружаем историю')
  await expect(page.getByText('Готово: streamed-follow-up.', { exact: true })).toBeVisible()
  await expect(page.locator('.chat-message')).toHaveCount(2)
})

test('queued tasks lose their old chat and preview before execution, including after stop', async ({ page, request }) => {
  const source = await create(request, 'source')
  const target = await create(request, 'queued', 500)
  expect((await request.post(`${scope()}/edges`, { data: { source: source.id, target: target.id } })).ok()).toBeTruthy()
  await run(request)
  await page.goto('/')
  await chat(page, target.id)
  await expect(page.getByText('Готово: queued.', { exact: true })).toBeVisible()
  await changePrompt(request, source.id, 'slow-source', 20)
  await page.getByRole('button', { name: 'Запустить пайплайн', exact: true }).click()
  await expect.poll(async () => (await snapshot(request)).tasklets.find((task: { id: string }) => task.id === target.id).status).toBe('queued')
  await expect(page.locator('.chat-message')).toHaveCount(0)
  await expect(page.getByRole('tab', { name: 'Чат', exact: true })).toBeVisible()
  await page.getByRole('tab', { name: 'Задача', exact: true }).click()
  await expect(page.locator('.result-preview')).toHaveCount(0)
  await page.getByRole('button', { name: 'Остановить всё', exact: true }).first().click()
  await expect.poll(async () => (await snapshot(request)).pipeline.status).toBe('cancelled')
  await page.reload()
  await page.getByRole('tab', { name: /^Чат/ }).click()
  await expect(page.locator('.chat-message')).toHaveCount(0)
  await expect(page.getByText('Сообщений пока нет', { exact: true })).toBeVisible()
})

test('Codex fresh runs create distinct threads and follow-up resumes the latest', async ({ page, request }) => {
  const root = (await (await request.get(`${backend}/directories`)).json()).path
  expect((await request.patch(`${backend}/settings`, { data: { execution_mode: 'codex', api_key: null, model: '' } })).ok()).toBeTruthy()
  expect((await request.patch(scope(), { data: { working_directory: root } })).ok()).toBeTruthy()
  const task = await create(request, 'codex-old')
  await run(request)
  await changePrompt(request, task.id, 'codex-new')
  await run(request)
  await page.goto('/')
  await chat(page, task.id)
  await expect(page.getByText('Готово: codex-new.', { exact: true })).toBeVisible()
  await expect(page.getByRole('log')).not.toContainText('codex-old')
  await page.getByPlaceholder('Напишите сообщение…', { exact: true }).fill('[[label:codex-follow-up]]')
  await page.getByRole('button', { name: 'Отправить сообщение', exact: true }).click()
  await expect(page.getByText('Готово: codex-follow-up.', { exact: true })).toBeVisible()
  const events = (await (await request.get(`${provider}/audit`)).json()).events.filter((event: { transport: string; kind: string }) => event.transport === 'codex' && event.kind === 'start')
  expect(events).toHaveLength(3)
  expect(events[0].thread_id).not.toBe(events[1].thread_id)
  expect(events[2].thread_id).toBe(events[1].thread_id)
  expect(JSON.stringify(events[1])).not.toContain('codex-old')
  await expect(page.locator('.chat-message')).toHaveCount(4)
})
