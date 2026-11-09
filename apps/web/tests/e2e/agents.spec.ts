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
      password: 'Agent-browser-2026',
      displayName: 'Agent验收用户',
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

async function organization(page: Page) {
  const headers = await account(page)
  const response = await page.request.post('/api/v1/organizations', {
    headers: { ...headers, 'Idempotency-Key': randomUUID() },
    data: { name: 'Agent验收组织' },
  })
  expect(response.status()).toBe(201)
  const org = (await response.json()).data.id as string
  return { org, headers: { ...headers, 'X-Organization-Id': org } }
}

test('Agent drafts, templates, leave guard, conflict reload and independent clone', async ({
  page,
}, testInfo) => {
  const { org, headers } = await organization(page)
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.goto(`/o/${org}/agents`)
  await expect(
    page.getByText('暂无符合条件的Agent。', { exact: false }),
  ).toBeVisible()
  await page.getByRole('link', { name: '新建Agent', exact: true }).click()
  await page.getByLabel('预置模板').selectOption('compare')
  await expect(page.getByLabel('任务提示词')).toHaveValue(/对比/)
  await page.getByLabel('Agent名称').fill('方案比较助手')
  page.once('dialog', (dialog) => dialog.dismiss())
  await page.getByRole('link', { name: '组织概览', exact: true }).click()
  await expect(page.getByLabel('Agent名称')).toHaveValue('方案比较助手')
  await page.getByRole('button', { name: '保存停用配置', exact: true }).click()
  await expect(
    page.getByRole('heading', { name: '方案比较助手', exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole('combobox', { name: '任务类型', exact: true }),
  ).toBeDisabled()
  await expect(
    page.getByRole('heading', { name: '已保存的第 1 版 · 已停用' }),
  ).toBeVisible()
  await page.getByRole('button', { name: '启用Agent', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('请先完成启用条件')
  await page.getByLabel('任务提示词').fill('保留本地未保存内容')
  await page
    .getByRole('link', { name: '请至少选择一个知识库。', exact: true })
    .click()
  await expect(page.getByLabel('任务提示词')).toHaveValue('保留本地未保存内容')
  const id = new URL(page.url()).pathname.split('/').at(-1)
  const current = (
    await (await page.request.get(`/api/v1/agents/${id}`, { headers })).json()
  ).data
  const remote = await page.request.put(`/api/v1/agents/${id}`, {
    headers,
    data: {
      name: current.name,
      description: current.description,
      taskType: current.taskType,
      prompt: '其他管理员保存的版本',
      knowledgeBaseIds: [],
      toolKeys: current.toolKeys,
      chatConnectionId: null,
      enabled: false,
      expectedRevision: current.revision,
    },
  })
  expect(remote.status()).toBe(200)
  await page.getByRole('button', { name: '保存新版本', exact: true }).click()
  await expect(
    page.getByText('配置已被更新。当前输入已保留', { exact: false }),
  ).toBeVisible()
  await expect(page.getByLabel('任务提示词')).toHaveValue('保留本地未保存内容')
  page.once('dialog', (dialog) => dialog.accept())
  await page.getByRole('button', { name: '重新读取配置', exact: true }).click()
  await expect(page.getByLabel('任务提示词')).toHaveValue(
    '其他管理员保存的版本',
  )
  await page.getByRole('button', { name: '复制Agent', exact: true }).click()
  await page.getByLabel('副本名称').fill('方案比较助手副本')
  await page.getByRole('button', { name: '确认复制', exact: true }).click()
  await expect(
    page.getByRole('heading', { name: '方案比较助手副本', exact: true }),
  ).toBeVisible()
  expect(new URL(page.url()).pathname.split('/').at(-1)).not.toBe(id)
  await expect(
    page.getByRole('heading', { name: '已保存的第 1 版 · 已停用' }),
  ).toBeVisible()
  await expect(
    page.getByRole('button', { name: '试运行（尚未就绪）' }),
  ).toBeDisabled()
  await page.setViewportSize({ width: 390, height: 844 })
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true)
  await page.screenshot({
    path: testInfo.outputPath('agents-mobile.png'),
    fullPage: true,
  })
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.screenshot({
    path: testInfo.outputPath('agents-desktop.png'),
    fullPage: true,
  })
  expect(errors).toEqual([])
})

test('Agent enabled configuration, member projection and disabled deep link', async ({
  page,
  browser,
}) => {
  test.skip(
    process.env.EAH_BROWSER_INGESTION !== '1',
    'Requires isolated ingestion and model fixtures',
  )
  const { org, headers } = await organization(page)
  let chatId = ''
  for (const kind of ['chat', 'embedding', 'rerank']) {
    const response = await page.request.put(`/api/v1/settings/models/${kind}`, {
      headers,
      data: {
        providerProtocol: {
          chat: 'openai-chat',
          embedding: 'openai-embeddings',
          rerank: 'cohere-rerank-v2',
        }[kind],
        baseUrl:
          kind === 'rerank'
            ? 'https://api.cohere.com/v2'
            : 'https://api.openai.com/v1',
        modelName: kind === 'chat' ? 'gpt-4o-mini-2024-07-18' : `test-${kind}`,
        credential: 'fixture-only',
        expectedRevision: 0,
        ...(kind === 'embedding' ? { embeddingDimension: 3 } : {}),
      },
    })
    expect(response.status()).toBe(200)
    if (kind === 'chat') chatId = (await response.json()).data.id
  }
  const kbResponse = await page.request.post('/api/v1/knowledge-bases', {
    headers: { ...headers, 'Idempotency-Key': randomUUID() },
    data: { name: '发布资料' },
  })
  const kb = (await kbResponse.json()).data.id as string
  // Prepare real ready documents through the API; upload UI has its own D04/D05 suite.
  const upload = await page.request.post(
    `/api/v1/knowledge-bases/${kb}/documents`,
    {
      headers: { ...headers, 'Idempotency-Key': randomUUID() },
      multipart: {
        file: {
          name: '发布说明.txt',
          mimeType: 'text/plain',
          buffer: Buffer.from('发布必须经过评审并保留回滚记录。'.repeat(3)),
        },
      },
    },
  )
  expect(upload.status()).toBe(202)
  const documentId = (await upload.json()).data.id
  await expect
    .poll(
      async () => {
        const response = await page.request.get(
          `/api/v1/documents/${documentId}`,
          { headers },
        )
        return (await response.json()).data.document.status
      },
      { timeout: 15000 },
    )
    .toBe('ready')
  await page.goto(`/o/${org}/agents/new`)
  await page.getByLabel('预置模板').selectOption('qa')
  await page.getByLabel('Agent名称').fill('已配置的文档助手')
  await page.getByRole('checkbox', { name: '发布资料', exact: false }).check()
  await page.getByLabel('模型连接').selectOption(chatId)
  await page.getByRole('button', { name: '保存停用配置', exact: true }).click()
  await expect(page.getByText('配置条件已满足，可以启用。')).toBeVisible()
  await page.getByRole('button', { name: '启用Agent', exact: true }).click()
  await expect(
    page.getByRole('heading', { name: '已保存的第 1 版 · 已启用' }),
  ).toBeVisible()
  const agentUrl = page.url()
  const context = await browser.newContext({
    baseURL: new URL(page.url()).origin,
  })
  try {
    const member = await context.newPage()
    const memberHeaders = await account(member)
    expect(
      (
        await page.request.post('/api/v1/members', {
          headers: { ...headers, 'Idempotency-Key': randomUUID() },
          data: { email: memberHeaders.email },
        })
      ).status(),
    ).toBe(201)
    await member.goto(`/o/${org}/agents`)
    await expect(member.getByRole('link', { name: '新建Agent' })).toHaveCount(0)
    await member.getByRole('link', { name: '查看用途', exact: true }).click()
    await expect(
      member.getByRole('heading', { name: '已配置的文档助手' }),
    ).toBeVisible()
    await expect(member.getByLabel('任务提示词')).toHaveCount(0)
    await expect(
      member.getByRole('link', { name: '开始文档问答' }),
    ).toBeVisible()
    const html = await (await member.request.get(agentUrl)).text()
    expect(html).not.toContain('根据用户问题检索已授权资料')
    expect(html).not.toContain('fixture-only')
    page.once('dialog', (dialog) => dialog.accept())
    await page.getByRole('button', { name: '停用Agent', exact: true }).click()
    await expect(
      page.getByRole('heading', { name: '已保存的第 1 版 · 已停用' }),
    ).toBeVisible()
    await member.reload()
    await expect(
      member.getByRole('heading', { name: '已配置的文档助手' }),
    ).toHaveCount(0)
    await expect(member.getByRole('alert')).toBeVisible()
    await member.goto(`/o/${org}/agents`)
    await expect(
      member.getByText('暂无符合条件的Agent。', { exact: false }),
    ).toBeVisible()
  } finally {
    await context.close()
  }
})
