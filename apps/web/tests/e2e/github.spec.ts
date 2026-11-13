import { randomUUID } from 'node:crypto'
import { expect, test } from '@playwright/test'

test('GitHub explicit grants, secret clearing, removal and readonly tools', async ({
  page,
}, testInfo) => {
  test.skip(
    process.env.EAH_BROWSER_GITHUB !== '1',
    'Requires explicit synthetic MCP fixture',
  )
  const origin = new URL(String(testInfo.project.use.baseURL)).origin
  const session = (
    await (await page.request.get('/api/v1/auth/session')).json()
  ).data
  expect(
    (
      await page.request.post('/api/v1/auth/register', {
        headers: { Origin: origin, 'X-CSRF-Token': session.csrfToken },
        data: {
          email: `${randomUUID()}@example.com`,
          password: 'Github-browser-2026',
          displayName: '连接验收用户',
        },
      })
    ).status(),
  ).toBe(201)
  const auth = (await (await page.request.get('/api/v1/auth/session')).json())
    .data
  const headers = { Origin: origin, 'X-CSRF-Token': auth.csrfToken }
  const org = (
    await (
      await page.request.post('/api/v1/organizations', {
        headers: { ...headers, 'Idempotency-Key': randomUUID() },
        data: { name: 'GitHub验收组织' },
      })
    ).json()
  ).data.id as string
  await page.goto(`/o/${org}/connections`)
  await expect(
    page.getByRole('heading', { name: '连接与工具', exact: true }),
  ).toBeVisible()
  await expect(page.getByLabel('专用只读凭据')).toBeEnabled()
  await page.getByLabel('专用只读凭据').fill('synthetic-browser-secret')
  await page.getByLabel('启用连接', { exact: true }).check()
  page.once('dialog', (dialog) => dialog.dismiss())
  await page.getByRole('link', { name: '组织概览', exact: true }).click()
  await expect(page.getByLabel('专用只读凭据')).toHaveValue(
    'synthetic-browser-secret',
  )
  await page.getByRole('button', { name: '测试连接', exact: true }).click()
  await expect(page.getByText(/测试通过：/)).toBeVisible()
  await page
    .getByRole('button', { name: '测试并保存连接', exact: true })
    .click()
  await expect(page.getByLabel('专用只读凭据')).toHaveValue('')
  await expect(
    page.getByText('连接已测试并启用', { exact: true }),
  ).toBeVisible()
  expect(await page.locator('body').innerText()).not.toContain(
    'synthetic-browser-secret',
  )
  await page.getByLabel('专用只读凭据').fill('synthetic-invalid')
  await page.getByRole('button', { name: '测试连接', exact: true }).click()
  await expect(page.getByText(/测试未通过：/)).toBeVisible()
  await expect(
    page.getByText('连接已测试并启用', { exact: true }),
  ).toBeVisible()
  await page.getByLabel('专用只读凭据').fill('')
  await page.getByLabel('Owner', { exact: true }).fill('example')
  await page.getByLabel('仓库名', { exact: true }).fill('hub')
  await expect(
    page.getByRole('button', { name: '验证并添加仓库' }),
  ).toBeDisabled()
  await page.getByRole('checkbox', { name: /向本组织共享/ }).check()
  await page.getByRole('button', { name: '验证并添加仓库' }).click()
  await expect(
    page.getByRole('button', { name: '移除 example/hub' }),
  ).toBeVisible()
  await page.reload()
  await expect(
    page.getByRole('button', { name: '移除 example/hub' }),
  ).toBeVisible()
  page.once('dialog', (dialog) => dialog.dismiss())
  await page.getByRole('button', { name: '移除 example/hub' }).click()
  await expect(
    page.getByRole('button', { name: '移除 example/hub' }),
  ).toBeVisible()
  page.once('dialog', (dialog) => dialog.accept())
  await page.getByRole('button', { name: '移除 example/hub' }).click()
  await expect(
    page.getByRole('button', { name: '移除 example/hub' }),
  ).toHaveCount(0)
  page.once('dialog', (dialog) => dialog.accept())
  await page
    .getByRole('button', { name: '停用 查询 Issue', exact: true })
    .click()
  await expect(
    page.getByRole('button', { name: '启用 查询 Issue', exact: true }),
  ).toBeVisible()
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(
    page.getByRole('heading', { name: '固定只读工具' }),
  ).toBeVisible()
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy()
  await page.screenshot({
    path: testInfo.outputPath('github-mobile.png'),
    fullPage: true,
  })
})
