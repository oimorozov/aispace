import { expect, test, type Page } from '@playwright/test'

const backend = `${(process.env.AISPACE_E2E_API_URL || 'http://127.0.0.1:8000').replace(/\/$/, '')}/api`
let scope: string
let taskletId: string
let edgeId: string

test.beforeEach(async ({ request }) => {
  for (const item of await (await request.get(`${backend}/workspaces`)).json()) {
    expect((await request.post(`${backend}/workspaces/${item.id}/pipeline/stop`)).ok()).toBeTruthy()
    expect((await request.delete(`${backend}/workspaces/${item.id}`)).ok()).toBeTruthy()
  }
  const created = await request.post(`${backend}/workspaces`, { data: { name: 'Проверка навигации' } })
  scope = `${backend}/workspaces/${(await created.json()).id}`
  const first = await (await request.post(`${scope}/tasklets`, { data: { title: 'Первая', prompt: 'Сохранённая инструкция', position: { x: 80, y: 100 } } })).json()
  const second = await (await request.post(`${scope}/tasklets`, { data: { title: 'Вторая', prompt: 'Другая инструкция', position: { x: 420, y: 280 } } })).json()
  taskletId = first.id
  const edge = await (await request.post(`${scope}/edges`, { data: { source: first.id, target: second.id } })).json()
  edgeId = edge.id
})

const toggle = (page: Page, collapsed: boolean) => page.getByRole('button', { name: collapsed ? 'Показать боковую панель' : 'Скрыть боковую панель', exact: true })

for (const width of [1440, 850, 390]) {
  test(`sidebar toggles by keyboard without resetting drafts or viewport at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 })
    const writes: string[] = []
    page.on('request', request => { if (request.url().includes('/api/') && request.method() !== 'GET') writes.push(request.method() + ' ' + request.url()) })
    await page.goto('/')
    await page.locator(`.react-flow__node[data-id="${taskletId}"]`).click()
    await page.getByRole('textbox', { name: 'Промпт', exact: true }).fill('Черновик инструкции')
    await toggle(page, false).focus()
    await page.keyboard.press('Enter')
    await expect(toggle(page, true)).toHaveAttribute('aria-expanded', 'false')
    await expect(toggle(page, true)).toBeFocused()
    await expect(page.locator('.sidebar')).toHaveCount(0)
    await expect(page.getByRole('textbox', { name: 'Промпт', exact: true })).toHaveValue('Черновик инструкции')
    await page.keyboard.press('Space')
    await expect(toggle(page, false)).toHaveAttribute('aria-expanded', 'true')
    await page.getByRole('textbox', { name: 'Промпт', exact: true }).fill('Сохранённая инструкция')
    await page.getByRole('tab', { name: /^Чат/ }).click()
    await page.getByPlaceholder('Напишите сообщение…', { exact: true }).fill('Черновик сообщения')
    if (width > 540) {
      await page.getByRole('button', { name: 'Уменьшить масштаб', exact: true }).click()
      const viewport = await page.locator('.react-flow__viewport').getAttribute('style')
      await toggle(page, false).click()
      await toggle(page, true).click()
      await expect(page.locator('.react-flow__viewport')).toHaveAttribute('style', viewport!)
    }
    await toggle(page, false).click()
    await page.reload()
    await expect(toggle(page, true)).toBeVisible()
    await expect(page.getByRole('tab', { name: /^Чат/ })).toHaveAttribute('aria-selected', 'true')
    await expect(page.getByPlaceholder('Напишите сообщение…', { exact: true })).toHaveValue('Черновик сообщения')
    await page.getByRole('button', { name: 'Настройки', exact: true }).click()
    await expect(toggle(page, true)).toBeVisible()
    await toggle(page, true).click()
    await expect(page.locator('.sidebar')).toBeVisible()
    await expect(page.getByRole('textbox', { name: 'Контекст пространства', exact: true })).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    expect(writes).toEqual([])
  })
}

test('hidden sidebar stays hidden across resizing and preserves selected dependency', async ({ page }) => {
  await page.goto('/')
  await page.locator(`.react-flow__edge[data-id="${edgeId}"]`).click()
  await expect(page.getByText('Связь задач', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Уменьшить масштаб', exact: true }).click()
  const viewport = await page.locator('.react-flow__viewport').getAttribute('style')
  await toggle(page, false).click()
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(toggle(page, true)).toBeVisible()
  await expect(page.locator('.sidebar')).toHaveCount(0)
  await page.setViewportSize({ width: 1440, height: 1000 })
  await expect(page.getByText('Связь задач', { exact: true })).toBeVisible()
  await expect(page.locator('.react-flow__viewport')).toHaveAttribute('style', viewport!)
  await toggle(page, true).click()
  await page.reload()
  await expect(toggle(page, false)).toHaveAttribute('aria-expanded', 'true')
  await expect(page.getByText('Связь задач', { exact: true })).toBeVisible()
})
