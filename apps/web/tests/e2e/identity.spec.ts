import { randomUUID } from 'node:crypto'
import { expect, test, type Page } from '@playwright/test'

const password = 'Browser-password-2026'

async function register(page: Page, displayName: string) {
  const email = `${randomUUID()}@example.com`
  await page.goto('/register')
  await page.getByLabel('显示名').fill(displayName)
  await page.getByLabel('邮箱', { exact: true }).fill(email)
  await page.locator('input[name="password"]').fill(password)
  await page.getByRole('button', { name: '注册并继续' }).click()
  await expect(page).toHaveURL(/\/onboarding$/)
  return email
}

async function createOrganization(page: Page, name: string) {
  await page.getByLabel('组织名称').fill(name)
  await page.getByRole('button', { name: '创建并进入' }).click()
  await expect(page.getByRole('heading', { name, exact: true })).toBeVisible()
  return new URL(page.url()).pathname
}

test('registration, SSR session, organization switching, account and password revocation', async ({
  page,
}, testInfo) => {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  const email = await register(page, '浏览器验收成员')
  await page.screenshot({
    path: testInfo.outputPath('organizations-desktop.png'),
    fullPage: true,
  })
  const first = await createOrganization(page, '验收团队甲')
  await page.reload()
  await expect(
    page.getByRole('heading', { name: '验收团队甲', exact: true }),
  ).toBeVisible()
  const serverPage = await page.request.get(first)
  expect(serverPage.headers()['cache-control']).toBe('no-store')
  const cookies = await page.context().cookies()
  expect(
    cookies.find((cookie) => cookie.name === 'eah_session')?.httpOnly,
  ).toBe(true)
  expect(await page.evaluate(() => document.cookie)).not.toContain(
    'eah_session',
  )
  await page.getByRole('link', { name: '所有组织', exact: true }).click()
  const second = await createOrganization(page, '验收团队乙')
  await page.getByLabel('切换组织').selectOption(first.split('/').at(-1) || '')
  await expect(page).toHaveURL(new RegExp(`${first}$`))
  await expect(
    page.getByRole('heading', { name: '验收团队乙', exact: true }),
  ).toHaveCount(0)
  await page.getByLabel('切换组织').selectOption(second.split('/').at(-1) || '')
  await page.getByRole('link', { name: '账号设置', exact: true }).click()
  await page.getByLabel('显示名').fill('修改后的姓名')
  await page.getByRole('button', { name: '保存显示名' }).click()
  await expect(page.getByRole('status')).toHaveText('显示名已保存。')
  await page.getByLabel('当前密码', { exact: true }).fill(password)
  await page.getByLabel('新密码', { exact: true }).fill('Changed-password-2026')
  await page.getByLabel('再次输入新密码').fill('wrong-confirmation')
  await page.getByRole('button', { name: '修改密码并退出' }).click()
  await expect(page.getByRole('alert')).toHaveText('两次新密码不一致。')
  await page.getByLabel('再次输入新密码').fill('Changed-password-2026')
  await page.getByRole('button', { name: '修改密码并退出' }).click()
  await expect(page).toHaveURL(/\/login\?notice=password$/)
  await page.getByLabel('邮箱', { exact: true }).fill(email)
  await page.locator('input[name="password"]').fill('Changed-password-2026')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page).toHaveURL(/\/onboarding$/)
  await page.getByRole('button', { name: '退出', exact: true }).click()
  await expect(page).toHaveURL(/\/login$/)
  await page.goto(first)
  await expect(page).toHaveURL(/\/login$/)
  expect(errors).toEqual([])
})

test('team confirmation, roles, ownership, removal and two browser isolation', async ({
  page,
  browser,
  baseURL,
}, testInfo) => {
  await register(page, '所有者验收')
  const org = await createOrganization(page, '权限验收团队')
  const otherContext = await browser.newContext({ baseURL })
  try {
    const other = await otherContext.newPage()
    const email = await register(other, '成员验收')
    await other.goto(org)
    await expect(other).toHaveURL(/\/onboarding\?notice=access$/)
    await page.getByRole('link', { name: '团队成员', exact: true }).click()
    await page.getByLabel('成员邮箱').fill(email)
    await page.getByRole('button', { name: '核对账号' }).click()
    await expect(page.getByText('确认添加：')).toBeVisible()
    await page.getByRole('button', { name: '确认添加成员' }).click()
    const memberRow = page.getByRole('row').filter({ hasText: email })
    await expect(memberRow).toBeVisible()
    await other.goto(`${org}/settings/team`)
    await expect(other.getByRole('heading', { name: '团队成员' })).toBeVisible()
    await expect(other.getByRole('button', { name: '核对账号' })).toHaveCount(0)
    await memberRow.getByRole('button', { name: '设为管理员' }).click()
    await expect(
      memberRow.getByRole('button', { name: '设为成员', exact: true }),
    ).toBeVisible()
    await other.reload()
    await expect(other.getByRole('button', { name: '核对账号' })).toBeVisible()
    await expect(other.getByRole('button', { name: '转移所有权' })).toHaveCount(
      0,
    )
    page.once('dialog', (dialog) => dialog.accept())
    await memberRow.getByRole('button', { name: '转移所有权' }).click()
    await expect(page.getByRole('button', { name: '转移所有权' })).toHaveCount(
      0,
    )
    await other.reload()
    const oldOwner = other.getByRole('row').filter({ hasText: '所有者验收' })
    other.once('dialog', (dialog) => dialog.accept())
    await oldOwner.getByRole('button', { name: '移除', exact: true }).click()
    await expect(oldOwner).toHaveCount(0)
    await other.screenshot({
      path: testInfo.outputPath('team-desktop.png'),
      fullPage: true,
    })
    await page.reload()
    await expect(page).toHaveURL(/\/onboarding\?notice=access$/)
  } finally {
    await otherContext.close()
  }
})

test('mobile registration, keyboard form and error retention', async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/login?redirect=https://evil.example')
  await page.getByLabel('邮箱', { exact: true }).fill('missing@example.com')
  await page.locator('input[name="password"]').fill(password)
  await page.locator('input[name="password"]').press('Enter')
  await expect(page.getByRole('alert')).toHaveText('邮箱或密码不正确。')
  await expect(page.getByLabel('邮箱', { exact: true })).toHaveValue(
    'missing@example.com',
  )
  await page.screenshot({
    path: testInfo.outputPath('login-mobile.png'),
    fullPage: true,
  })
  await register(page, '移动端验收')
  await createOrganization(page, '移动端团队')
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true)
  await page.screenshot({
    path: testInfo.outputPath('organization-mobile.png'),
    fullPage: true,
  })
})

test('organization switch discards a delayed member response', async ({
  page,
}) => {
  await register(page, '延迟响应验收')
  const first = await createOrganization(page, '旧组织')
  await page.getByRole('link', { name: '所有组织', exact: true }).click()
  const second = await createOrganization(page, '新组织')
  await page.goto(`${first}/settings/team`)
  let release = () => {}
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  let intercepted = () => {}
  const started = new Promise<void>((resolve) => {
    intercepted = resolve
  })
  await page.route('**/api/v1/members', async (route) => {
    const response = await route.fetch()
    intercepted()
    await gate
    await route.fulfill({ response })
  })
  await page.getByRole('button', { name: '刷新 / 返回第一页' }).click()
  await started
  await page.getByLabel('切换组织').selectOption(second.split('/').at(-1) || '')
  await expect(
    page.getByRole('heading', { name: '新组织', exact: true }),
  ).toBeVisible()
  release()
  await page.waitForLoadState('networkidle')
  await expect(
    page.getByRole('heading', { name: '团队成员', exact: true }),
  ).toHaveCount(0)
  await expect(
    page.getByRole('heading', { name: '旧组织', exact: true }),
  ).toHaveCount(0)
})
