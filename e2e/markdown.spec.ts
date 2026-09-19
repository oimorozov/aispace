import { expect, test, type APIRequestContext, type Locator, type Page } from '@playwright/test'

const backend = `${(process.env.AISPACE_E2E_API_URL || 'http://127.0.0.1:8000').replace(/\/$/, '')}/api`
const provider = 'http://127.0.0.1:8766'
const providerBaseUrl = process.env.AISPACE_E2E_PROVIDER_URL || `${provider}/v1`
let workspaceBase: string

const code = 'function greet() {\n  const message = "hello"\n\n  return message\n}\n'
const rich = [
  '# Заголовок',
  '',
  'Первый абзац: **важно**, *курсив*, ~~удалено~~ и `inline()`.',
  'Вторая строка того же абзаца.',
  '',
  'Второй абзац.',
  '',
  '## Подзаголовок',
  '',
  '> Цитата',
  '',
  '- Первый пункт',
  '  - Вложенный пункт',
  '',
  '1. Первый шаг',
  '2. Второй шаг',
  '',
  '- [x] Сделано',
  '- [ ] Осталось',
  '',
  '| Имя | Значение |',
  '| --- | --- |',
  '| Проект | aispace |',
  '',
  '[Документация](https://example.test/markdown) и [почта](mailto:test@example.test).',
  '',
  '---',
  '',
  '```javascript',
  code.trimEnd(),
  '```',
].join('\n')

async function state(request: APIRequestContext) {
  return (await request.get(workspaceBase)).json()
}

async function configure(request: APIRequestContext, label: string, chunks: { content: string; delay?: number }[], error = false) {
  expect((await request.post(`${provider}/responses`, { data: { label, chunks, error } })).ok()).toBeTruthy()
}

async function create(request: APIRequestContext, label: string, prompt = `[[label:${label}]]`) {
  const response = await request.post(`${workspaceBase}/tasklets`, { data: { title: label, prompt, position: { x: 100, y: 100 } } })
  expect(response.ok()).toBeTruthy()
  return response.json()
}

async function openChat(page: Page, id: string) {
  await page.goto('/')
  await page.locator(`.react-flow__node[data-id="${id}"]`).click()
  await page.getByRole('tab', { name: /^Чат/ }).click()
}

async function messages(request: APIRequestContext, id: string): Promise<{ role: string; content: string }[]> {
  return (await request.get(`${workspaceBase}/tasklets/${id}/messages`)).json()
}

async function assertRich(message: Locator) {
  await expect(message.getByRole('heading', { name: 'Заголовок', exact: true })).toBeVisible()
  await expect(message.getByRole('heading', { name: 'Подзаголовок', exact: true })).toHaveCount(1)
  await expect(message.locator('strong')).toHaveText('важно')
  await expect(message.locator('em')).toHaveText('курсив')
  await expect(message.locator('del')).toHaveText('удалено')
  await expect(message.locator('blockquote')).toHaveText('Цитата')
  await expect(message.locator('ul ul li')).toHaveText('Вложенный пункт')
  await expect(message.locator('ol li')).toHaveText(['Первый шаг', 'Второй шаг'])
  await expect(message.locator('table th')).toHaveText(['Имя', 'Значение'])
  await expect(message.locator('table td')).toHaveText(['Проект', 'aispace'])
  await expect(message.locator('hr')).toHaveCount(1)
  await expect(message.locator('p').filter({ hasText: 'Первый абзац:' }).locator('br')).toHaveCount(1)
  await expect(message.locator('p').filter({ hasText: /^Второй абзац\.$/ })).toHaveCount(1)
  await expect(message.locator('p code')).toHaveText('inline()')
  expect(await message.locator('pre code').textContent()).toBe(code)
  await expect(message.getByRole('checkbox', { name: 'Выполнено', exact: true })).toBeChecked()
  await expect(message.getByRole('checkbox', { name: 'Не выполнено', exact: true })).not.toBeChecked()
  for (const checkbox of await message.getByRole('checkbox').all()) await expect(checkbox).toBeDisabled()
}

test.beforeEach(async ({ request }) => {
  for (const item of await (await request.get(`${backend}/workspaces`)).json()) {
    expect((await request.post(`${backend}/workspaces/${item.id}/pipeline/stop`)).ok()).toBeTruthy()
    expect((await request.delete(`${backend}/workspaces/${item.id}`)).ok()).toBeTruthy()
  }
  const response = await request.post(`${backend}/workspaces`, { data: { name: 'Markdown' } })
  expect(response.ok()).toBeTruthy()
  workspaceBase = `${backend}/workspaces/${(await response.json()).id}`
  expect((await request.patch(`${backend}/settings`, { data: { execution_mode: 'api', api_key: 'local-test-key', base_url: providerBaseUrl, model: 'test-model' } })).ok()).toBeTruthy()
  await request.post(`${provider}/reset`)
})

test('Markdown renders both roles, keeps raw content and survives reopening and reload', async ({ page, context, request }, testInfo) => {
  const tasklet = await create(request, 'rich')
  await configure(request, 'rich', [{ content: rich }])
  await openChat(page, tasklet.id)
  await page.getByRole('textbox', { name: 'Сообщение', exact: true }).fill(rich)
  await page.getByRole('button', { name: 'Отправить сообщение', exact: true }).click()
  await expect.poll(async () => (await state(request)).pipeline.status).toBe('completed')
  for (const role of ['user', 'assistant']) await assertRich(page.locator(`.message-${role}`).last())
  const saved = await messages(request, tasklet.id)
  expect(saved.filter(item => item.role === 'user').at(-1)?.content).toBe(rich)
  expect(saved.filter(item => item.role === 'assistant').at(-1)?.content).toBe(rich)
  await page.locator('.message-user').last().getByRole('checkbox').first().evaluate(element => (element as HTMLInputElement).click())
  expect(await messages(request, tasklet.id)).toEqual(saved)
  await context.route('https://example.test/markdown', route => route.fulfill({ contentType: 'text/plain', body: 'Link destination' }))
  const popupPromise = page.waitForEvent('popup')
  await page.locator('.message-assistant').last().getByRole('link', { name: 'Документация', exact: true }).click()
  const popup = await popupPromise
  await popup.waitForLoadState()
  expect(await popup.evaluate(() => window.opener)).toBeNull()
  await popup.close()
  await expect(page.getByRole('tab', { name: /^Чат/ })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByRole('complementary', { name: 'Редактор тасклета' })).toContainText('rich')
  await page.getByRole('button', { name: 'Закрыть редактор', exact: true }).click()
  await page.locator(`.react-flow__node[data-id="${tasklet.id}"]`).click()
  await page.getByRole('tab', { name: /^Чат/ }).click()
  await assertRich(page.locator('.message-assistant').last())
  await page.reload()
  await assertRich(page.locator('.message-assistant').last())
  expect(await messages(request, tasklet.id)).toEqual(saved)
  await page.locator('.message-assistant').last().getByRole('heading', { name: 'Заголовок', exact: true }).scrollIntoViewIfNeeded()
  await page.screenshot({ path: testInfo.outputPath('markdown-desktop.png'), fullPage: true })
})

test('Markdown blocks script URLs, HTML execution and automatic image requests', async ({ page, request }) => {
  const unsafe = [
    '<script>window.markdownExecuted = true</script>',
    '',
    '<img src="https://example.test/raw-image" onerror="window.markdownExecuted = true">',
    '',
    '<a href="javascript:alert(1)" onclick="window.markdownExecuted = true">raw link</a>',
    '',
    '[javascript](javascript:alert%281%29) [encoded](java&#x73;cript:alert%281%29)',
    '[data](data:text/html,unsafe) [file](file:///tmp/private) [relative](./README.md) [protocol](//example.test/path)',
    '',
    '![Подпись картинки](https://example.test/markdown-image)',
    '',
    '[Разрешённая ссылка](https://example.test/safe) [HTTP](http://example.test/safe) [Почта](mailto:test@example.test)',
  ].join('\n')
  const unexpected: string[] = []
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('request', event => { if (event.url().startsWith('https://example.test/') || event.url().startsWith('http://example.test/')) unexpected.push(event.url()) })
  await page.route('https://example.test/**', route => route.abort())
  await page.route('http://example.test/**', route => route.abort())
  await page.addInitScript(() => { Object.assign(window, { markdownExecuted: false }) })
  const tasklet = await create(request, 'unsafe')
  await configure(request, 'unsafe', [{ content: unsafe }])
  await openChat(page, tasklet.id)
  await page.getByRole('textbox', { name: 'Сообщение', exact: true }).fill(unsafe)
  await page.getByRole('button', { name: 'Отправить сообщение', exact: true }).click()
  await expect.poll(async () => (await state(request)).pipeline.status).toBe('completed')
  for (const role of ['user', 'assistant']) {
    const message = page.locator(`.message-${role}`).last()
    await expect(message.locator('script, img, iframe, [onclick], [onerror]')).toHaveCount(0)
    await expect(message.locator('a')).toHaveCount(3)
    await expect(message.getByText('Подпись картинки', { exact: true })).toBeVisible()
    for (const label of ['javascript', 'encoded', 'data', 'file', 'relative', 'protocol']) await expect(message.getByRole('link', { name: label, exact: true })).toHaveCount(0)
  }
  expect(await page.evaluate(() => Reflect.get(window, 'markdownExecuted'))).toBe(false)
  expect(unexpected).toEqual([])
  expect(errors).toEqual([])
  expect((await messages(request, tasklet.id)).filter(item => item.role === 'assistant').at(-1)?.content).toBe(unsafe)
})

test('Markdown preserves streamed split fences and retains unfinished code after stop or error', async ({ page, request }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  const tasklet = await create(request, 'split')
  const chunks = [{ content: '# Поток\n\n`' }, { content: '``python\n  print("часть")', delay: 0.2 }, { content: '\n``', delay: 1 }, { content: '`\n\n**Готово**', delay: 1 }]
  await configure(request, 'split', chunks)
  await openChat(page, tasklet.id)
  await page.getByRole('button', { name: 'Запустить пайплайн', exact: true }).click()
  await expect(page.locator('.message-assistant pre code')).toHaveText('  print("часть")\n')
  await expect.poll(async () => (await state(request)).pipeline.status).toBe('running')
  await expect(page.locator('.message-assistant pre code')).toContainText('``')
  await expect.poll(async () => (await state(request)).pipeline.status).toBe('completed')
  await expect(page.locator('.message-assistant strong')).toHaveText('Готово')
  expect(await page.locator('.message-assistant pre code').textContent()).toBe('  print("часть")\n')
  expect((await messages(request, tasklet.id)).filter(item => item.role === 'assistant').at(-1)?.content).toBe(chunks.map(chunk => chunk.content).join(''))
  for (const ending of ['stop', 'error']) {
    const partial = '```python\n  print("сохранить")\n'
    await configure(request, ending, [{ content: partial }, ...(ending === 'stop' ? [{ content: 'никогда', delay: 20 }] : [])], ending === 'error')
    await page.getByRole('textbox', { name: 'Сообщение', exact: true }).fill(`[[label:${ending}]]`)
    await page.getByRole('button', { name: 'Отправить сообщение', exact: true }).click()
    await expect(page.locator('.message-assistant').last().locator('pre code')).toHaveText('  print("сохранить")\n')
    if (ending === 'stop') await page.getByRole('complementary', { name: 'Редактор тасклета' }).getByRole('button', { name: 'Остановить всё', exact: true }).click()
    await expect.poll(async () => (await state(request)).pipeline.status).toBe(ending === 'stop' ? 'cancelled' : 'failed')
    expect((await messages(request, tasklet.id)).filter(item => item.role === 'assistant').at(-1)?.content).toBe(partial)
    await page.reload()
    expect(await page.locator('.message-assistant').last().locator('pre code').textContent()).toBe('  print("сохранить")\n')
  }
  expect(errors).toEqual([])
})

test('Markdown code and tables scroll inside messages on desktop and mobile', async ({ page, request }, testInfo) => {
  const long = 'very_long_identifier_'.repeat(30)
  const columns = Array.from({ length: 10 }, (_, index) => `Колонка ${index}`)
  const content = `ДлинноеСлово${'безпробелов'.repeat(60)}\n\nhttps://example.test/${'long-path-'.repeat(60)}\n\n\`\`\`text\n${long}\n\`\`\`\n\n| ${columns.join(' | ')} |\n| ${columns.map(() => '---').join(' | ')} |\n| ${columns.map(() => long).join(' | ')} |`
  const tasklet = await create(request, 'wide')
  await configure(request, 'wide', [{ content }])
  await openChat(page, tasklet.id)
  await page.getByRole('button', { name: 'Запустить пайплайн', exact: true }).click()
  await expect.poll(async () => (await state(request)).pipeline.status).toBe('completed')
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 })
    const message = page.locator('.message-assistant').last()
    await expect(message.locator('pre')).toBeVisible()
    for (const region of [message.locator('pre'), message.getByRole('region', { name: 'Таблица', exact: true })]) {
      expect(await region.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true)
      expect(await region.evaluate(element => { element.scrollLeft = 120; return element.scrollLeft })).toBeGreaterThan(0)
      expect(await region.evaluate(element => element.getBoundingClientRect().right <= document.documentElement.clientWidth)).toBe(true)
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    expect(await page.getByRole('log', { name: 'История чата' }).evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
    await message.locator('pre').scrollIntoViewIfNeeded()
    await page.screenshot({ path: testInfo.outputPath(`markdown-width-${width}.png`), fullPage: true })
  }
})

test('Codex messages use the same Markdown renderer', async ({ page, request }) => {
  const root = (await (await request.get(`${backend}/directories`)).json()).path
  expect((await request.patch(`${backend}/settings`, { data: { execution_mode: 'codex', api_key: null, model: '' } })).ok()).toBeTruthy()
  expect((await request.patch(workspaceBase, { data: { working_directory: root } })).ok()).toBeTruthy()
  const tasklet = await create(request, '**Codex** `code`')
  await openChat(page, tasklet.id)
  await page.getByRole('button', { name: 'Запустить пайплайн', exact: true }).click()
  await expect.poll(async () => (await state(request)).pipeline.status).toBe('completed')
  await expect(page.locator('.message-assistant strong')).toHaveText('Codex')
  await expect(page.locator('.message-assistant code')).toHaveText('code')
  expect((await messages(request, tasklet.id)).filter(item => item.role === 'assistant').at(-1)?.content).toBe('Готово: **Codex** `code`.')
  await page.reload()
  await expect(page.locator('.message-assistant strong')).toHaveText('Codex')
})
