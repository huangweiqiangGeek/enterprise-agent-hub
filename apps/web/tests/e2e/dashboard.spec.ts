import { randomUUID } from 'node:crypto'
import { writeFile } from 'node:fs/promises'
import { expect, test, type Page } from '@playwright/test'
test.setTimeout(75000)

async function prepare(page: Page) {
  await page.goto('/register')
  await page.getByLabel('显示名').fill('首次任务验收')
  await page
    .getByLabel('邮箱', { exact: true })
    .fill(`${randomUUID()}@example.com`)
  await page.locator('input[name="password"]').fill('Dashboard-browser-2026')
  await page.getByRole('button', { name: '注册并继续' }).click()
  await page.getByLabel('组织名称').fill('首次任务团队')
  await page.getByRole('button', { name: '创建并进入' }).click()
  await expect(page.getByTestId('dashboard')).toBeVisible()
  const org = new URL(page.url()).pathname.split('/').at(-1)!
  const session = (
    await (await page.request.get('/api/v1/auth/session')).json()
  ).data
  const headers = {
    Origin: new URL(page.url()).origin,
    'X-CSRF-Token': session.csrfToken as string,
    'X-Organization-Id': org,
  }
  await page.getByTestId('readiness-qa').locator('summary').click()
  await page
    .getByTestId('readiness-qa')
    .locator('a[href$="/settings/models"]')
    .first()
    .click()
  for (const [kind, title] of [
    ['chat', '聊天模型'],
    ['embedding', 'Embedding 模型'],
    ['rerank', 'Rerank 服务'],
  ]) {
    const panel = page.getByRole('region', { name: title, exact: true })
    if (kind !== 'chat')
      await panel.getByLabel('模型名称', { exact: true }).fill(`test-${kind}`)
    if (kind === 'embedding')
      await panel.getByLabel('向量维度（可选）').fill('3')
    await panel.getByLabel('API Key', { exact: true }).fill('fixture-only')
    await panel.getByRole('button', { name: '测试并保存', exact: true }).click()
    await expect(panel.getByRole('status')).toContainText(
      '配置已测试通过并生效',
    )
  }
  await page.getByRole('link', { name: '组织概览', exact: true }).click()
  await page.getByTestId('readiness-qa').locator('summary').click()
  await page
    .getByTestId('readiness-qa')
    .locator('a[href$="/knowledge"]')
    .first()
    .click()
  await page.getByRole('button', { name: '新建知识库', exact: true }).click()
  await page.getByLabel('知识库名称').fill('发布规范')
  await page.getByRole('button', { name: '保存知识库' }).click()
  await page.getByRole('link', { name: '发布规范', exact: true }).click()
  await page.getByLabel('选择文件').setInputFiles({
    name: '发布规范.md',
    mimeType: 'text/markdown',
    buffer: Buffer.from(
      '# 发布流程\n\n发布必须经过评审。\n\n发布应保留回滚记录。',
    ),
  })
  await expect(page.locator('.document-list .badge')).toBeVisible({
    timeout: 20000,
  })
  await page.getByRole('link', { name: '组织概览', exact: true }).click()
  await page.getByTestId('readiness-qa').locator('summary').click()
  await page
    .getByTestId('readiness-qa')
    .locator('a[href*="/agents/new"]')
    .click()
  await expect(
    page.getByRole('combobox', { name: '预置模板', exact: true }),
  ).toHaveValue('qa')
  await page.getByLabel('Agent名称').fill('文档助手')
  await page.getByRole('checkbox', { name: /发布规范/ }).check()
  await page
    .getByRole('combobox', { name: '模型连接', exact: true })
    .selectOption({ index: 1 })
  await page.getByRole('button', { name: '保存停用配置', exact: true }).click()
  await expect(
    page.getByRole('heading', { name: '文档助手', exact: true }),
  ).toBeVisible()
  const agent = new URL(page.url()).pathname.split('/').at(-1)!
  await page.getByRole('button', { name: '启用Agent', exact: true }).click()
  await expect(
    page.getByRole('heading', { name: /已保存的第.*已启用/ }),
  ).toBeVisible()
  return { org, headers, agent }
}

test('new account task entrances are read-only, recover from failure and resize', async ({
  page,
}, info) => {
  await page.goto('/register')
  await page.getByLabel('显示名').fill('工作台验收')
  await page
    .getByLabel('邮箱', { exact: true })
    .fill(`${randomUUID()}@example.com`)
  await page.locator('input[name="password"]').fill('Dashboard-browser-2026')
  await page.getByRole('button', { name: '注册并继续' }).click()
  await page.getByLabel('组织名称').fill('新团队工作台')
  await page.getByRole('button', { name: '创建并进入' }).click()
  await expect(page.getByTestId('dashboard')).toBeVisible()
  const home = new URL(page.url()).pathname
  let writes = 0
  page.on('request', (request) => {
    if (
      request.method() === 'POST' &&
      /\/(runs|conversations)(\/|$)/.test(new URL(request.url()).pathname)
    )
      writes++
  })
  await expect(page.getByTestId('recent-conversations')).toContainText(
    '还没有会话',
  )
  for (const type of ['qa', 'compare', 'progress']) {
    await page.getByTestId(`task-${type}`).getByRole('link').click()
    await expect(
      page.getByRole('combobox', { name: '任务类型', exact: true }),
    ).toHaveValue(type)
    await page.getByRole('link', { name: '组织概览', exact: true }).click()
  }
  await page.getByTestId('readiness-qa').locator('summary').click()
  const newAgent = page
    .getByTestId('readiness-qa')
    .locator('a[href*="/agents/new"]')
  await newAgent.click()
  await expect(
    page.getByRole('combobox', { name: '预置模板', exact: true }),
  ).toHaveValue('qa')
  await page.goto(home)
  await page.route('**/api/v1/dashboard', (route) =>
    route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({
        error: { code: 'UNAVAILABLE', message: '暂不可用' },
        requestId: 'dashboard-fixture',
      }),
    }),
  )
  await page.getByRole('button', { name: '刷新工作台', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('暂不可确认')
  await expect(page.getByTestId('dashboard-readiness')).toContainText(
    '准备情况未知',
  )
  await expect(page.getByTestId('recent-conversations')).not.toContainText(
    '还没有会话',
  )
  await page.unroute('**/api/v1/dashboard')
  await page.getByRole('button', { name: '重试读取', exact: true }).click()
  await expect(page.getByTestId('readiness-qa')).toBeVisible()
  expect(writes).toBe(0)
  for (const width of [1440, 768, 390]) {
    await page.setViewportSize({ width, height: 900 })
    await page.screenshot({
      path: info.outputPath(`dashboard-${width}.png`),
      fullPage: true,
    })
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true)
  }
  const html = await page.evaluate(() => {
    const clone = document.documentElement.cloneNode(true) as HTMLElement
    clone
      .querySelectorAll('script,link[rel="stylesheet"]')
      .forEach((el) => el.remove())
    const style = document.createElement('style')
    style.textContent = Array.from(document.styleSheets)
      .flatMap((sheet) => {
        try {
          return Array.from(sheet.cssRules).map((rule) => rule.cssText)
        } catch {
          return []
        }
      })
      .join('\n')
    clone.querySelector('head')!.append(style)
    return '<!DOCTYPE html>' + clone.outerHTML
  })
  await writeFile(info.outputPath('dashboard-static.html'), html)
})

test('first controlled task appears in personal recents; switching organizations clears late data', async ({
  page,
}) => {
  test.skip(
    process.env.EAH_BROWSER_QA !== '1',
    'Requires explicit controlled model fixture',
  )
  const { org, headers, agent } = await prepare(page)
  await page.goto(`/o/${org}`)
  await expect(page.getByTestId('readiness-qa')).toContainText('可以填写任务')
  await expect(page.getByTestId('readiness-progress')).not.toContainText(
    '可以填写任务',
  )
  await page.getByTestId('task-qa').getByRole('link').click()
  await page.getByLabel('Agent', { exact: true }).selectOption(agent)
  await page.getByLabel('问题', { exact: true }).fill('发布需要什么？')
  await page.getByRole('button', { name: '发送问题', exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/o/${org}/chat/[^/]+$`))
  const conversation = new URL(page.url()).pathname
  await expect(page.getByText('已完成', { exact: true }).first()).toBeVisible({
    timeout: 20000,
  })
  await page.getByRole('link', { name: '组织概览', exact: true }).click()
  await expect(
    page
      .getByTestId('recent-conversations')
      .locator(`a[href="${conversation}"]`),
  ).toBeVisible()
  const other = (
    await (
      await page.request.post('/api/v1/organizations', {
        headers: { ...headers, 'Idempotency-Key': randomUUID() },
        data: { name: '空白隔离团队' },
      })
    ).json()
  ).data.id as string
  // Refresh identity list before delaying the old organization's dashboard response.
  await page.reload()
  let release!: () => void
  const held = new Promise<void>((resolve) => {
    release = resolve
  })
  let started!: () => void
  const seen = new Promise<void>((resolve) => {
    started = resolve
  })
  await page.route('**/api/v1/dashboard', async (route) => {
    if (route.request().headers()['x-organization-id'] === org) {
      const response = await route.fetch()
      started()
      await held
      await route.fulfill({ response }).catch(() => {})
    } else await route.continue()
  })
  await page.getByRole('button', { name: '刷新工作台', exact: true }).click()
  await seen
  await page.getByLabel('切换组织').selectOption(other)
  await expect(page).toHaveURL(new RegExp(`/o/${other}$`))
  release()
  await expect(
    page.getByRole('heading', { name: '空白隔离团队', exact: true }),
  ).toBeVisible()
  await expect(page.getByTestId('recent-conversations')).toContainText(
    '还没有会话',
  )
  await expect(page.locator(`a[href="${conversation}"]`)).toHaveCount(0)
  await expect(page.getByTestId('readiness-qa')).not.toContainText(
    '可以填写任务',
  )
})
