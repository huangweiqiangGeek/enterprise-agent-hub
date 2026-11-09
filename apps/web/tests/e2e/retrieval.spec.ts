import { randomUUID } from 'node:crypto'
import { expect, test, type Page } from '@playwright/test'

async function account(page: Page) {
  const origin = process.env.EAH_WEB_URL || 'http://127.0.0.1:3300'
  await page.goto('/register')
  const session = await page.request.get('/api/v1/auth/session')
  const email = `${randomUUID()}@example.com`
  const registered = await page.request.post('/api/v1/auth/register', {
    headers: {
      Origin: origin,
      'X-CSRF-Token': (await session.json()).data.csrfToken,
    },
    data: {
      email,
      password: 'Retrieval-browser-2026',
      displayName: '检索验收用户',
    },
  })
  expect(registered.status()).toBe(201)
  const loggedIn = await page.request.get('/api/v1/auth/session')
  return {
    email,
    Origin: origin,
    'X-CSRF-Token': (await loggedIn.json()).data.csrfToken as string,
  }
}

test('retrieval preview empty, scoped evidence, error, cancellation and mobile', async ({
  page,
}, testInfo) => {
  test.skip(
    process.env.EAH_BROWSER_INGESTION !== '1',
    'Needs isolated ingestion fixture',
  )
  const headers = await account(page)
  const orgResponse = await page.request.post('/api/v1/organizations', {
    headers: { ...headers, 'Idempotency-Key': randomUUID() },
    data: { name: '检索验收组织' },
  })
  expect(orgResponse.status()).toBe(201)
  const org = (await orgResponse.json()).data.id as string
  const scoped = { ...headers, 'X-Organization-Id': org }
  for (const kind of ['embedding', 'rerank']) {
    const response = await page.request.put(`/api/v1/settings/models/${kind}`, {
      headers: scoped,
      data: {
        providerProtocol:
          kind === 'embedding' ? 'openai-embeddings' : 'cohere-rerank-v2',
        baseUrl:
          kind === 'embedding'
            ? 'https://api.openai.com/v1'
            : 'https://api.cohere.com/v2',
        modelName: `test-${kind}`,
        credential: 'fixture-only',
        expectedRevision: 0,
        ...(kind === 'embedding' ? { embeddingDimension: 3 } : {}),
      },
    })
    expect(response.status()).toBe(200)
  }
  const response = await page.request.post('/api/v1/knowledge-bases', {
    headers: { ...scoped, 'Idempotency-Key': randomUUID() },
    data: { name: '发布资料' },
  })
  const kb = (await response.json()).data.id as string
  const otherOrgResponse = await page.request.post('/api/v1/organizations', {
    headers: { ...headers, 'Idempotency-Key': randomUUID() },
    data: { name: '另一个试查组织' },
  })
  const otherOrg = (await otherOrgResponse.json()).data.id as string
  const url = `/o/${org}/knowledge/${kb}`
  await page.goto(url)
  await expect(page.getByLabel('试查问题')).not.toBeVisible()
  await page.getByText('检索试查 · 管理员', { exact: true }).click()
  await page.getByLabel('试查问题').fill('发布回滚')
  await page.getByRole('button', { name: '开始试查', exact: true }).click()
  await expect(
    page.getByText('本次没有检索到片段', { exact: false }),
  ).toBeVisible()
  await page.getByLabel('选择文件').setInputFiles({
    name: '发布手册.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from(
      '发布回滚：失败后恢复上一稳定版本，并检查错误率。'.repeat(3) +
        '<script>window.pwned=true</script>',
    ),
  })
  await expect(page.locator('.document-list .badge')).toHaveText('已就绪', {
    timeout: 30000,
  })
  await page.getByLabel('试查范围').selectOption({ label: '发布手册.txt' })
  await page.getByRole('button', { name: '开始试查', exact: true }).click()
  const results = page.getByRole('region', { name: '检索结果' })
  await expect(results.locator('article')).toHaveCount(1)
  await expect(results).toContainText('排序分数不是事实可信度')
  await expect(results).toContainText('覆盖限制')
  await expect(results.locator('pre')).toContainText(
    '<script>window.pwned=true</script>',
  )
  expect(await page.evaluate(() => 'pwned' in window)).toBe(false)
  await results.getByRole('button', { name: '查看文档记录' }).click()
  await expect(page.locator('.knowledge-detail')).toContainText('发布手册.txt')
  await results.getByText('本次模型用量', { exact: true }).click()
  await expect(results).toContainText('search_units')
  await page.setViewportSize({ width: 390, height: 844 })
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true)
  await page.screenshot({
    path: testInfo.outputPath('retrieval-mobile.png'),
    fullPage: true,
  })
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.screenshot({
    path: testInfo.outputPath('retrieval-desktop.png'),
    fullPage: true,
  })

  await page.route('**/search-preview', (route) =>
    route.fulfill({
      status: 503,
      json: {
        error: {
          code: 'MODEL_UNAVAILABLE',
          message: '检索模型调用失败，请稍后重试。',
        },
        requestId: 'fixture',
      },
    }),
  )
  await page.getByRole('button', { name: '开始试查', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('检索模型调用失败')
  await expect(results).not.toBeVisible()
  await expect(page.getByLabel('试查问题')).toHaveValue('发布回滚')
  await page.unroute('**/search-preview')
  let releaseSwitch = () => {}
  const switchGate = new Promise<void>((resolve) => {
    releaseSwitch = resolve
  })
  let markStarted = () => {}
  const started = new Promise<void>((resolve) => {
    markStarted = resolve
  })
  await page.route('**/search-preview', async (route) => {
    const response = await route.fetch()
    markStarted()
    await switchGate
    await route.fulfill({ response })
  })
  await page.getByRole('button', { name: '开始试查', exact: true }).click()
  await started
  await page.getByLabel('切换组织').selectOption(otherOrg)
  await expect(
    page.getByRole('heading', { name: '另一个试查组织', exact: true }),
  ).toBeVisible()
  releaseSwitch()
  await page.waitForLoadState('networkidle')
  await expect(results).not.toBeVisible()
  await page.unroute('**/search-preview')
  await page.goto(url)
  await page.getByText('检索试查 · 管理员', { exact: true }).click()
  await page.getByLabel('试查问题').fill('发布回滚')
  let release: (() => void) | undefined
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.route('**/search-preview', async (route) => {
    await gate
    await route.abort()
  })
  await page.getByRole('button', { name: '开始试查', exact: true }).click()
  await page.getByRole('button', { name: '取消等待' }).click()
  release?.()
  await expect(results).not.toBeVisible()
  await expect(
    page.getByRole('button', { name: '开始试查', exact: true }),
  ).toBeEnabled()
  await page.unroute('**/search-preview')
  await page.getByRole('button', { name: '开始试查', exact: true }).click()
  await expect(results.locator('article')).toHaveCount(1)
  page.once('dialog', (dialog) => dialog.accept())
  await page.getByRole('button', { name: '删除文档', exact: true }).click()
  await expect(results).not.toBeVisible()
})

test('member cannot open preview or invoke its API', async ({
  page,
  browser,
}) => {
  test.skip(process.env.EAH_BROWSER_INGESTION !== '1', 'Needs isolated fixture')
  const headers = await account(page)
  const orgResponse = await page.request.post('/api/v1/organizations', {
    headers: { ...headers, 'Idempotency-Key': randomUUID() },
    data: { name: '成员只读验收' },
  })
  const org = (await orgResponse.json()).data.id as string
  const scoped = { ...headers, 'X-Organization-Id': org }
  const kbResponse = await page.request.post('/api/v1/knowledge-bases', {
    headers: { ...scoped, 'Idempotency-Key': randomUUID() },
    data: { name: '成员资料' },
  })
  const kb = (await kbResponse.json()).data.id as string
  const context = await browser.newContext({
    baseURL: new URL(page.url()).origin,
  })
  try {
    const member = await context.newPage()
    const memberHeaders = await account(member)
    expect(
      (
        await page.request.post('/api/v1/members', {
          headers: { ...scoped, 'Idempotency-Key': randomUUID() },
          data: { email: memberHeaders.email },
        })
      ).status(),
    ).toBe(201)
    await member.goto(`/o/${org}/knowledge/${kb}`)
    await expect(
      member.getByText('检索试查 · 管理员', { exact: true }),
    ).toHaveCount(0)
    expect(
      (
        await member.request.post(
          `/api/v1/knowledge-bases/${kb}/search-preview`,
          {
            headers: { ...memberHeaders, 'X-Organization-Id': org },
            data: { query: '发布回滚' },
          },
        )
      ).status(),
    ).toBe(403)
  } finally {
    await context.close()
  }
})
