import { randomUUID } from 'node:crypto'
import { expect, test, type Page } from '@playwright/test'

async function prepare(page: Page) {
  await page.goto('/register')
  await page.getByLabel('显示名').fill('资料验收用户')
  await page
    .getByLabel('邮箱', { exact: true })
    .fill(`${randomUUID()}@example.com`)
  await page.locator('input[name="password"]').fill('Knowledge-browser-2026')
  await page.getByRole('button', { name: '注册并继续' }).click()
  await expect(page).toHaveURL(/\/onboarding$/)
  await page.getByLabel('组织名称').fill('资料验收组织')
  await page.getByRole('button', { name: '创建并进入' }).click()
  await expect(page).toHaveURL(/\/o\/[\w-]+$/)
  const org = new URL(page.url()).pathname.split('/').at(-1) || ''
  const cookies = await page.context().cookies()
  const result = await page.request.put('/api/v1/settings/models/embedding', {
    headers: {
      'X-Organization-Id': org,
      'X-CSRF-Token':
        cookies.find((cookie) => cookie.name === 'eah_csrf')?.value || '',
      Origin: new URL(page.url()).origin,
    },
    data: {
      providerProtocol: 'openai-embeddings',
      baseUrl: 'https://api.openai.com/v1',
      modelName: 'test-embedding',
      embeddingDimension: 3,
      credential: 'fixture-only',
      expectedRevision: 0,
    },
  })
  expect(result.status()).toBe(200)
  await page.getByRole('link', { name: '知识库', exact: true }).click()
  await page.getByRole('button', { name: '新建知识库', exact: true }).click()
  await page.getByLabel('知识库名称').fill('产品与技术方案')
  await page
    .getByLabel('说明', { exact: true })
    .fill('研发团队共同维护的项目资料')
  await page.getByRole('button', { name: '保存知识库' }).click()
  await page.getByRole('link', { name: '产品与技术方案', exact: true }).click()
  await expect(page.getByLabel('选择文件')).toBeEnabled()
}

test('knowledge multi-file acceptance, download, filtering, deletion and mobile', async ({
  page,
}, testInfo) => {
  test.skip(
    process.env.EAH_BROWSER_STORAGE !== '1' ||
      process.env.EAH_BROWSER_MODEL_FIXTURE !== '1',
    'Needs disposable S3 and explicit model fixture',
  )
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await prepare(page)
  await page.getByLabel('选择文件').setInputFiles([
    {
      name: '设计说明.md',
      mimeType: 'text/markdown',
      buffer: Buffer.from('# 设计\n团队知识库'),
    },
    { name: '空文件.txt', mimeType: 'text/plain', buffer: Buffer.alloc(0) },
    {
      name: '程序.exe',
      mimeType: 'application/octet-stream',
      buffer: Buffer.from('MZ'),
    },
    {
      name: '设计说明.md',
      mimeType: 'text/markdown',
      buffer: Buffer.from('# 更新\n同名独立保存'),
    },
  ])
  await expect(page.locator('.document-list > li')).toHaveCount(2)
  await expect(
    page.getByText('不能上传空文件。', { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText('仅支持 PDF、TXT 和 MD，文件类型必须匹配。', {
      exact: true,
    }),
  ).toBeVisible()
  await expect(
    page
      .locator('.upload-list')
      .getByText('已接受 · 待处理；尚不可检索。', { exact: true }),
  ).toHaveCount(2)
  if (process.env.EAH_BROWSER_INGESTION !== '1')
    await expect(page.locator('.document-list .badge')).toHaveText([
      '待处理',
      '待处理',
    ])
  const downloaded = page.waitForEvent('download')
  await page.getByRole('button', { name: '下载原文件' }).first().click()
  expect((await downloaded).suggestedFilename()).toBe('设计说明.md')
  await page.getByLabel('文件名', { exact: true }).fill('missing')
  await page.getByRole('button', { name: '筛选', exact: true }).click()
  await expect(page.getByText('没有符合筛选条件的文档。')).toBeVisible()
  await page.getByLabel('文件名', { exact: true }).fill('')
  await page.getByRole('button', { name: '筛选', exact: true }).click()
  await expect(page.locator('.document-list > li')).toHaveCount(2)
  await page.screenshot({
    path: testInfo.outputPath('knowledge-desktop.png'),
    fullPage: true,
  })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({
    path: testInfo.outputPath('knowledge-mobile.png'),
    fullPage: true,
  })
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true)
  page.once('dialog', (dialog) => dialog.accept())
  await page.getByRole('button', { name: '删除文档' }).first().click()
  await expect(page.locator('.document-list > li')).toHaveCount(1)
  expect(errors).toEqual([])
})

test('ingestion previews, failed retry history and mobile', async ({
  page,
}, testInfo) => {
  test.skip(
    process.env.EAH_BROWSER_INGESTION !== '1',
    'Needs explicit ingestion fixture',
  )
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await prepare(page)
  await page.getByLabel('选择文件').setInputFiles({
    name: '入库验收.md',
    mimeType: 'text/markdown',
    buffer: Buffer.from('# 技术方案\n\n研发资料与接口验收。'),
  })
  await expect(page.locator('.document-list .badge')).toHaveText(['已就绪'], {
    timeout: 15000,
  })
  await page.getByRole('button', { name: '查看记录' }).click()
  await page.getByRole('button', { name: '查看提取文本', exact: true }).click()
  await expect(page.getByRole('region', { name: '提取文本' })).toContainText(
    '研发资料与接口验收。',
  )
  await page.getByRole('button', { name: '查看切片', exact: true }).click()
  await expect(page.getByRole('region', { name: '文档切片' })).toContainText(
    '技术方案',
  )
  await page.screenshot({
    path: testInfo.outputPath('ingestion-desktop.png'),
    fullPage: true,
  })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({
    path: testInfo.outputPath('ingestion-mobile.png'),
    fullPage: true,
  })
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true)
  await page.getByRole('button', { name: '关闭记录' }).click()
  await page.getByLabel('选择文件').setInputFiles({
    name: '损坏.pdf',
    mimeType: 'application/pdf',
    buffer: Buffer.from('%PDF-1.7\nbroken'),
  })
  const failed = page
    .locator('.document-list > li')
    .filter({ hasText: '损坏.pdf' })
  await expect(failed).toContainText('处理失败', { timeout: 15000 })
  await failed.getByRole('button', { name: '重新处理' }).click()
  await expect(failed).toContainText('第 2 次处理', { timeout: 15000 })
  await expect(failed).toContainText('处理失败', { timeout: 15000 })
  await failed.getByRole('button', { name: '查看记录' }).click()
  await expect(page.locator('.knowledge-detail li')).toHaveCount(2)
  expect(errors).toEqual([])
})

test('lost upload response reconciles original identity without duplicate file', async ({
  page,
}) => {
  test.skip(
    process.env.EAH_BROWSER_STORAGE !== '1' ||
      process.env.EAH_BROWSER_MODEL_FIXTURE !== '1',
    'Needs disposable S3 and explicit model fixture',
  )
  await prepare(page)
  let uploads = 0
  await page.route('**/api/v1/knowledge-bases/*/documents', async (route) => {
    if (route.request().method() !== 'POST') return route.continue()
    uploads += 1
    const response = await route.fetch()
    expect(response.status()).toBe(202)
    await route.abort('failed')
  })
  await page.getByLabel('选择文件').setInputFiles({
    name: 'lost-response.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('accepted even without response'),
  })
  await expect(
    page
      .locator('.upload-list')
      .getByText('已接受 · 待处理；尚不可检索。', { exact: true }),
  ).toBeVisible()
  await expect(page.locator('.document-list > li')).toHaveCount(1)
  expect(uploads).toBe(1)
})

test('cancelled transfer keeps its identity for a user retry', async ({
  page,
}) => {
  test.skip(
    process.env.EAH_BROWSER_STORAGE !== '1' ||
      process.env.EAH_BROWSER_MODEL_FIXTURE !== '1',
    'Needs disposable S3 and explicit model fixture',
  )
  await prepare(page)
  const keys: string[] = []
  let release: () => void = () => {}
  const held = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.route('**/api/v1/knowledge-bases/*/documents', async (route) => {
    if (route.request().method() !== 'POST') return route.continue()
    keys.push(route.request().headers()['idempotency-key'] || '')
    if (keys.length === 1) {
      await held
      await route.abort('aborted')
    } else await route.continue()
  })
  await page.getByLabel('选择文件').setInputFiles({
    name: 'cancel.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('retry same file'),
  })
  await expect.poll(() => keys.length).toBe(1)
  await page.getByRole('button', { name: '取消传输', exact: true }).click()
  release()
  await expect(page.getByRole('button', { name: '用原标识重试' })).toBeEnabled()
  await page.getByRole('button', { name: '用原标识重试' }).click()
  await expect(page.locator('.document-list > li')).toHaveCount(1)
  expect(keys).toHaveLength(2)
  expect(keys[0]).toBeTruthy()
  expect(keys[1]).toBe(keys[0])
})

test('external deletion clears open previews and cancels a late original download', async ({
  page,
}) => {
  test.skip(
    process.env.EAH_BROWSER_INGESTION !== '1',
    'Needs isolated ingestion fixture',
  )
  await prepare(page)
  await page.getByLabel('选择文件').setInputFiles({
    name: '撤销.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('撤销后不应继续显示的正文'),
  })
  await expect(page.locator('.document-list .badge')).toHaveText(['已就绪'], {
    timeout: 15000,
  })
  await page.getByRole('button', { name: '查看记录' }).click()
  await page.getByRole('button', { name: '查看提取文本', exact: true }).click()
  await expect(page.getByRole('region', { name: '提取文本' })).toContainText(
    '撤销后不应继续显示的正文',
  )
  let release: () => void = () => {}
  const held = new Promise<void>((resolve) => {
    release = resolve
  })
  let documentId = ''
  let accepted = false
  let delivered = false
  let downloads = 0
  page.on('download', () => downloads++)
  await page.route(
    '**/api/v1/documents/*/content?view=original&*',
    async (route) => {
      documentId = new URL(route.request().url()).pathname.split('/')[4] || ''
      const response = await route.fetch()
      expect(response.status()).toBe(200)
      accepted = true
      await held
      await route.fulfill({ response }).catch(() => {})
      delivered = true
    },
  )
  await page.getByRole('button', { name: '下载原文件' }).click()
  await expect.poll(() => accepted).toBe(true)
  const org = new URL(page.url()).pathname.split('/')[2] || ''
  const csrf =
    (await page.context().cookies()).find((c) => c.name === 'eah_csrf')
      ?.value || ''
  const removed = await page.request.delete(`/api/v1/documents/${documentId}`, {
    headers: {
      'X-Organization-Id': org,
      'X-CSRF-Token': csrf,
      Origin: new URL(page.url()).origin,
    },
  })
  expect(removed.status()).toBe(204)
  await page.evaluate(() => window.dispatchEvent(new Event('focus')))
  await expect(page.getByRole('region', { name: '提取文本' })).toHaveCount(0)
  await expect(page.locator('.document-list > li')).toHaveCount(0)
  release()
  await expect.poll(() => delivered).toBe(true)
  await expect(
    page.getByText('还没有文档。添加文件后，它们会显示在这里。'),
  ).toBeVisible()
  expect(downloads).toBe(0)
  const old = await page.request.get(
    `/api/v1/documents/${documentId}/content?view=original`,
    { headers: { 'X-Organization-Id': org } },
  )
  expect([404, 410]).toContain(old.status())
})

test('known download 401 clears an open source immediately without permission polling', async ({
  page,
}) => {
  test.skip(
    process.env.EAH_BROWSER_INGESTION !== '1',
    'Needs isolated ingestion fixture',
  )
  await prepare(page)
  await page.getByLabel('选择文件').setInputFiles({
    name: 'session.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('会话保护正文'),
  })
  await expect(page.locator('.document-list .badge')).toHaveText(['已就绪'], {
    timeout: 15000,
  })
  await page.getByRole('button', { name: '查看记录' }).click()
  await page.getByRole('button', { name: '查看提取文本', exact: true }).click()
  await expect(page.getByRole('region', { name: '提取文本' })).toContainText(
    '会话保护正文',
  )
  await page.route('**/api/v1/organizations/current', (route) => route.abort())
  await page.route('**/api/v1/documents/*/content?view=original&*', (route) =>
    route.fulfill({
      status: 401,
      body: '',
    }),
  )
  await page.getByRole('button', { name: '下载原文件' }).click()
  await expect(page.getByRole('region', { name: '提取文本' })).toHaveCount(0)
  await expect(
    page.getByRole('link', { name: '团队成员', exact: true }),
  ).toHaveCount(0)
})

test('known preview HTML 401 clears an open source immediately without permission polling', async ({
  page,
}) => {
  test.skip(
    process.env.EAH_BROWSER_INGESTION !== '1',
    'Needs isolated ingestion fixture',
  )
  await prepare(page)
  await page.getByLabel('选择文件').setInputFiles({
    name: 'session.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('会话保护正文'),
  })
  await expect(page.locator('.document-list .badge')).toHaveText(['已就绪'], {
    timeout: 15000,
  })
  await page.getByRole('button', { name: '查看记录' }).click()
  await page.getByRole('button', { name: '查看提取文本', exact: true }).click()
  await expect(page.getByRole('region', { name: '提取文本' })).toContainText(
    '会话保护正文',
  )
  await page.route('**/api/v1/organizations/current', (route) => route.abort())
  await page.route('**/api/v1/documents/*/chunks?*', (route) =>
    route.fulfill({
      status: 401,
      body: '<html>Unauthorized</html>',
      contentType: 'text/html',
    }),
  )
  await page.getByRole('button', { name: '查看切片', exact: true }).click()
  await expect(page.getByRole('region', { name: '提取文本' })).toHaveCount(0)
  await expect(
    page.getByRole('link', { name: '团队成员', exact: true }),
  ).toHaveCount(0)
})
