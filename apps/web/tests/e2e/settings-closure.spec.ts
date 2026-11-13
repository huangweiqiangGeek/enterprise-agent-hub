import { randomUUID } from 'node:crypto'
import { writeFile } from 'node:fs/promises'
import { expect, test, type Page } from '@playwright/test'

test.setTimeout(75000)
async function register(page: Page, name = '设置验收用户') {
  await page.goto('/register')
  const email = `${randomUUID()}@example.com`
  await page.getByLabel('显示名').fill(name)
  await page.getByLabel('邮箱', { exact: true }).fill(email)
  await page.locator('input[name="password"]').fill('Settings-browser-2026')
  await page.getByRole('button', { name: '注册并继续' }).click()
  await expect(page).toHaveURL(/\/onboarding$/)
  return email
}
async function prepare(page: Page) {
  await register(page)
  await page.getByLabel('组织名称').fill('设置验收团队')
  await page.getByRole('button', { name: '创建并进入' }).click()
  await expect(page.getByTestId('dashboard')).toBeVisible()
  const org = new URL(page.url()).pathname.split('/').at(-1) || ''
  const session = (
    await (await page.request.get('/api/v1/auth/session')).json()
  ).data
  const headers = {
    Origin: new URL(page.url()).origin,
    'X-CSRF-Token': session.csrfToken as string,
    'X-Organization-Id': org,
  }
  return { org, session, headers }
}

test('limits and account conflict keep drafts, require explicit reload and support keyboard', async ({
  page,
}, info) => {
  const { headers, session } = await prepare(page)
  await page.getByRole('link', { name: '组织限制', exact: true }).click()
  const current = (
    await (
      await page.request.get('/api/v1/settings/limits', { headers })
    ).json()
  ).data
  await page.getByLabel('每次运行模型步骤', { exact: true }).fill('4')
  expect(
    (
      await page.request.put('/api/v1/settings/limits', {
        headers,
        data: {
          limits: { ...current.effective, maxRunSteps: 3 },
          expectedRevision: current.revision,
        },
      })
    ).status(),
  ).toBe(200)
  await page.getByRole('button', { name: '保存组织限制' }).click()
  await expect(page.getByRole('alert')).toBeVisible()
  await expect(
    page.getByLabel('每次运行模型步骤', { exact: true }),
  ).toHaveValue('4')
  page.once('dialog', (d) => d.dismiss())
  await page.getByRole('button', { name: '重新读取生效限制' }).click()
  await expect(
    page.getByLabel('每次运行模型步骤', { exact: true }),
  ).toHaveValue('4')
  page.once('dialog', (d) => d.accept())
  await page.getByRole('button', { name: '重新读取生效限制' }).click()
  await expect(
    page.getByLabel('每次运行模型步骤', { exact: true }),
  ).toHaveValue('3')
  await page.getByLabel('每次运行模型步骤', { exact: true }).fill('4')
  await page.getByLabel('每次运行模型步骤', { exact: true }).press('Enter')
  await expect(page.getByRole('status')).toContainText('组织限制已保存')
  await expect(page.getByText('任务尚未开放', { exact: false })).toHaveCount(0)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({
    path: info.outputPath('limits-390.png'),
    fullPage: true,
  })
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true)
  await page.getByRole('link', { name: '账号设置', exact: true }).click()
  await page.getByLabel('显示名').fill('保留我的草稿')
  expect(
    (
      await page.request.patch('/api/v1/me', {
        headers,
        data: {
          displayName: '另一页面的新名字',
          expectedRevision: session.user.revision,
        },
      })
    ).status(),
  ).toBe(200)
  await page.getByLabel('显示名').press('Enter')
  await expect(page.getByRole('alert')).toBeVisible()
  await expect(page.getByLabel('显示名')).toHaveValue('保留我的草稿')
  page.once('dialog', (d) => d.accept())
  await page.getByRole('button', { name: '重新读取显示名' }).click()
  await expect(page.getByLabel('显示名')).toHaveValue('另一页面的新名字')
  await page.getByLabel('显示名').fill('核对后的新名字')
  await page.getByLabel('显示名').press('Enter')
  await expect(page.getByRole('status')).toHaveText('显示名已保存。')
})

test('model refresh preserves dirty sibling; embedding lock allows credential rotation only', async ({
  page,
}, info) => {
  test.skip(
    process.env.EAH_BROWSER_MODEL_FIXTURE !== '1' ||
      process.env.EAH_BROWSER_STORAGE !== '1',
    'Requires controlled fixture and isolated storage',
  )
  const { org, headers } = await prepare(page)
  const embeddingBody = {
    providerProtocol: 'openai-embeddings',
    baseUrl: 'https://api.openai.com/v1',
    modelName: 'test-embedding',
    embeddingDimension: 3,
    credential: 'fixture-only',
    expectedRevision: 0,
  }
  const initial = await page.request.put('/api/v1/settings/models/embedding', {
    headers,
    data: embeddingBody,
  })
  expect(initial.status()).toBe(200)
  const embeddingConfig = (await initial.json()).data
  await page.getByRole('link', { name: '模型配置', exact: true }).click()
  const embedding = page.getByRole('region', {
    name: 'Embedding 模型',
    exact: true,
  })
  const chat = page.getByRole('region', { name: '聊天模型', exact: true })
  await embedding
    .getByLabel('模型名称', { exact: true })
    .fill('unsaved-local-model')
  expect(
    (
      await page.request.put('/api/v1/settings/models/embedding', {
        headers,
        data: { ...embeddingBody, expectedRevision: embeddingConfig.revision },
      })
    ).status(),
  ).toBe(200)
  await chat.getByLabel('API Key', { exact: true }).fill('fixture-only')
  await chat.getByRole('button', { name: '测试并保存', exact: true }).click()
  await expect(chat.getByRole('status')).toContainText('配置已测试通过并生效')
  await expect(
    embedding.getByText('配置已更新，当前输入已保留。', { exact: false }),
  ).toBeVisible()
  await expect(embedding.getByLabel('模型名称', { exact: true })).toHaveValue(
    'unsaved-local-model',
  )
  await expect(
    embedding.getByRole('button', { name: '测试并保存', exact: true }),
  ).toBeDisabled()
  page.once('dialog', (d) => d.accept())
  await embedding.getByRole('button', { name: '重新读取生效配置' }).click()
  await expect(embedding.getByLabel('模型名称', { exact: true })).toHaveValue(
    'test-embedding',
  )
  const kb = (
    await (
      await page.request.post('/api/v1/knowledge-bases', {
        headers: { ...headers, 'Idempotency-Key': randomUUID() },
        data: { name: '锁定验收' },
      })
    ).json()
  ).data.id
  expect(
    (
      await page.request.post(`/api/v1/knowledge-bases/${kb}/documents`, {
        headers: { ...headers, 'Idempotency-Key': randomUUID() },
        multipart: {
          file: {
            name: '锁定.md',
            mimeType: 'text/markdown',
            buffer: Buffer.from('# 已接受的资料'),
          },
        },
      })
    ).status(),
  ).toBe(202)
  // The page still has the pre-upload snapshot; the server must reject changed index semantics.
  await embedding.getByLabel('模型名称', { exact: true }).fill('changed-index')
  await embedding
    .getByRole('button', { name: '测试并保存', exact: true })
    .click()
  await expect(embedding.getByRole('alert')).toContainText('Embedding')
  await expect(embedding.getByLabel('模型名称', { exact: true })).toHaveValue(
    'changed-index',
  )
  page.once('dialog', (d) => d.accept())
  await embedding.getByRole('button', { name: '重新读取生效配置' }).click()
  await expect(embedding.getByLabel('模型名称', { exact: true })).toBeDisabled()
  await expect(embedding.getByLabel('向量维度（可选）')).toBeDisabled()
  await embedding
    .getByLabel('API Key', { exact: true })
    .fill('rotated-fixture-only')
  await embedding
    .getByRole('button', { name: '测试并保存', exact: true })
    .click()
  await expect(embedding.getByRole('status')).toContainText(
    '配置已测试通过并生效',
  )
  await expect(embedding.getByLabel('API Key', { exact: true })).toHaveValue('')
  expect(
    await (await page.request.get(`/o/${org}/settings/models`)).text(),
  ).not.toContain('rotated-fixture-only')
  for (const width of [1440, 768, 390]) {
    await page.setViewportSize({ width, height: 900 })
    await page.screenshot({
      path: info.outputPath(`settings-models-${width}.png`),
      fullPage: true,
    })
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true)
  }
  const html = await page.evaluate(() => {
    const clone = document.documentElement.cloneNode(true)
    if (!(clone instanceof HTMLElement)) throw new Error('Missing document')
    clone
      .querySelectorAll('script,link[rel="stylesheet"]')
      .forEach((el) => el.remove())
    clone
      .querySelectorAll('input[type="password"]')
      .forEach((el) => el.removeAttribute('value'))
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
    clone.querySelector('head')?.append(style)
    return '<!DOCTYPE html>' + clone.outerHTML
  })
  await writeFile(info.outputPath('settings-static.html'), html)
})

test('transfer cancellation and lost response reconcile actual owner without a second mutation', async ({
  page,
  browser,
}, info) => {
  const { org } = await prepare(page)
  const context = await browser.newContext()
  const other = await context.newPage()
  try {
    const email = await register(other, '新所有者')
    await page.getByRole('link', { name: '团队成员', exact: true }).click()
    await page
      .getByLabel('成员邮箱')
      .fill(`missing-${randomUUID()}@example.com`)
    await page.getByLabel('成员邮箱').press('Enter')
    await expect(page.getByRole('alert')).toContainText('注册')
    await page.getByLabel('成员邮箱').fill(email)
    await page.getByLabel('成员邮箱').press('Enter')
    await expect(page.getByText('确认添加：', { exact: false })).toBeVisible()
    await page.getByRole('button', { name: '确认添加成员' }).click()
    const row = page.getByRole('row').filter({ hasText: email })
    await expect(row).toBeVisible()
    let writes = 0
    await page.route(
      '**/api/v1/organizations/current/transfer-owner',
      async (route) => {
        writes++
        const response = await route.fetch()
        expect(response.status()).toBe(200)
        await route.abort('failed')
      },
    )
    page.once('dialog', async (d) => {
      expect(d.message()).toContain(email)
      expect(d.message()).toContain('管理员')
      await d.dismiss()
    })
    await row.getByRole('button', { name: '转移所有权' }).click()
    expect(writes).toBe(0)
    page.once('dialog', (d) => d.accept())
    await row.getByRole('button', { name: '转移所有权' }).click()
    await expect(
      page.getByRole('region', { name: '核对所有权转移' }),
    ).toBeVisible()
    await page.getByRole('button', { name: '核对当前所有者' }).click()
    await expect(
      page.getByRole('status').filter({ hasText: '已核实：' }),
    ).toContainText('已核实：新所有者')
    expect(writes).toBe(1)
    await expect(row).toContainText('所有者')
    await expect(page.getByRole('button', { name: '转移所有权' })).toHaveCount(
      0,
    )
    await expect(
      row.getByRole('button', { name: '移除', exact: true }),
    ).toHaveCount(0)
    await page.setViewportSize({ width: 768, height: 900 })
    await page.screenshot({
      path: info.outputPath('team-768.png'),
      fullPage: true,
    })
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true)
    await other.goto(`/o/${org}/settings/team`)
    await expect(
      other.getByRole('button', { name: '转移所有权' }),
    ).toBeVisible()
  } finally {
    await context.close()
  }
})
