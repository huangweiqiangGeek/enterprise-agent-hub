import { randomUUID } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { expect, test, type Page } from '@playwright/test'
test.setTimeout(75000)

async function account(page: Page) {
  await page.goto('/register')
  const initial = (
    await (await page.request.get('/api/v1/auth/session')).json()
  ).data
  const email = `${randomUUID()}@example.com`
  const origin = new URL(page.url()).origin
  expect(
    (
      await page.request.post('/api/v1/auth/register', {
        headers: { Origin: origin, 'X-CSRF-Token': initial.csrfToken },
        data: {
          email,
          password: 'Dialogue-test-2026',
          displayName: '对话验收',
        },
      })
    ).status(),
  ).toBe(201)
  const session = (
    await (await page.request.get('/api/v1/auth/session')).json()
  ).data
  return {
    email,
    userId: session.user.id,
    headers: { Origin: origin, 'X-CSRF-Token': session.csrfToken },
  }
}
async function prepare(page: Page) {
  const user = await account(page)
  const org = (
    await (
      await page.request.post('/api/v1/organizations', {
        headers: { ...user.headers, 'Idempotency-Key': randomUUID() },
        data: { name: '对话验收组织' },
      })
    ).json()
  ).data.id as string
  const headers = { ...user.headers, 'X-Organization-Id': org }
  let chat = ''
  for (const kind of ['chat', 'embedding', 'rerank']) {
    const result = await page.request.put(`/api/v1/settings/models/${kind}`, {
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
    expect(result.status()).toBe(200)
    if (kind === 'chat') chat = (await result.json()).data.id
  }
  const kb = (
    await (
      await page.request.post('/api/v1/knowledge-bases', {
        headers: { ...headers, 'Idempotency-Key': randomUUID() },
        data: { name: '发布规范' },
      })
    ).json()
  ).data.id as string
  const uploaded = await page.request.post(
    `/api/v1/knowledge-bases/${kb}/documents`,
    {
      headers: { ...headers, 'Idempotency-Key': randomUUID() },
      multipart: {
        file: {
          name: '发布规范.md',
          mimeType: 'text/markdown',
          buffer: Buffer.from(
            '# 发布流程\n\n发布必须经过评审。\n\n发布应保留回滚记录。',
          ),
        },
      },
    },
  )
  expect(uploaded.status()).toBe(202)
  const doc = (await uploaded.json()).data.id as string
  await expect
    .poll(
      async () =>
        (
          await (
            await page.request.get(`/api/v1/documents/${doc}`, { headers })
          ).json()
        ).data.document.status,
      { timeout: 15000 },
    )
    .toBe('ready')
  const created = await page.request.post('/api/v1/agents', {
    headers: { ...headers, 'Idempotency-Key': randomUUID() },
    data: {
      name: '文档助手',
      description: '文档问答',
      taskType: 'qa',
      prompt: '仅依据实际证据回答，引用来源。',
      knowledgeBaseIds: [kb],
      toolKeys: ['knowledge.search'],
      chatConnectionId: chat,
      enabled: true,
    },
  })
  expect(created.status()).toBe(201)
  return {
    org,
    headers,
    kb,
    doc,
    agent: (await created.json()).data.id as string,
  }
}

test.beforeEach(() => {
  test.skip(
    process.env.EAH_BROWSER_QA !== '1',
    'Requires explicit isolated QA fixture',
  )
})
test('lost acceptance, double submit, refresh/offline recovery, source versions, export and follow-up', async ({
  page,
  context,
}, info) => {
  const { org, headers, agent, doc } = await prepare(page)
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  await page.goto(`/o/${org}/chat`)
  await page.getByLabel('Agent', { exact: true }).selectOption(agent)
  await expect(
    page.getByRole('checkbox', { name: '发布规范', exact: true }),
  ).toBeChecked()
  await page.getByLabel('问题', { exact: true }).fill('发布需要什么？')
  let accepted = ''
  let posts = 0
  let expired = false
  await page.route('**/api/v1/runs/*/events', async (route) => {
    if (!expired) {
      expired = true
      await route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({
          error: { code: 'EVENT_CURSOR_EXPIRED', message: '请重新读取快照' },
          requestId: 'cursor-fixture',
        }),
      })
    } else await route.continue()
  })
  await page.route('**/api/v1/conversations/*/runs', async (route) => {
    posts++
    const response = await route.fetch()
    expect(response.status()).toBe(202)
    accepted = (await response.json()).data.runId
    await route.abort('failed')
  })
  await page
    .getByRole('button', { name: '发送问题', exact: true })
    .evaluate((button) => {
      button.click()
      button.click()
    })
  await expect(
    page.getByRole('button', { name: '核对原提交', exact: true }),
  ).toBeEnabled()
  await page.unroute('**/api/v1/conversations/*/runs')
  await page.getByRole('button', { name: '核对原提交', exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/o/${org}/chat/[^/]+$`))
  expect(posts).toBe(1)
  await expect(page.getByTestId('draft')).toContainText('评审', {
    timeout: 15000,
  })
  await page.reload()
  await expect(page.getByLabel('运行结果')).toContainText(accepted)
  await context.setOffline(true)
  await page.waitForTimeout(9000)
  await context.setOffline(false)
  await expect(page.getByTestId('final-answer')).toContainText('回滚', {
    timeout: 20000,
  })
  await expect(page.getByTestId('draft')).toHaveCount(0)
  expect(expired).toBe(true)
  expect(
    (await page.getByTestId('final-answer').innerText()).match(
      /发布必须经过评审。/g,
    ),
  ).toHaveLength(1)
  expect(
    (
      await (
        await page.request.get('/api/v1/conversations', { headers })
      ).json()
    ).data.items,
  ).toHaveLength(1)
  const snapshot = (
    await (
      await page.request.get(`/api/v1/runs/${accepted}`, { headers })
    ).json()
  ).data
  const citation = snapshot.citations[0]
  expect(citation.documentId).toBe(doc)
  const source = (
    await (
      await page.request.get(
        `/api/v1/runs/${accepted}/sources/${citation.sourceId}`,
        { headers },
      )
    ).json()
  ).data
  await page.getByRole('button', { name: /来源 1：/ }).click()
  await expect(page.getByRole('dialog')).toContainText(source.attemptId)
  await expect(page.getByRole('dialog')).toContainText(source.chunkId)
  await expect(page.getByRole('dialog')).toContainText('评审')
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).not.toBeVisible()
  await expect(page.getByRole('button', { name: /来源 1：/ })).toBeFocused()
  const download = page.waitForEvent('download')
  await page.getByRole('button', { name: '下载 Markdown' }).click()
  const file = await download
  const path = await file.path()
  expect(path).toBeTruthy()
  if (path) {
    const content = await readFile(path, 'utf8')
    for (const text of [
      'UTC',
      '## 范围',
      '## 来源',
      '## 覆盖与限制',
      source.attemptId,
      source.chunkId,
    ])
      expect(content).toContain(text)
  }
  await page.getByRole('button', { name: '查看简明步骤' }).click()
  await expect(
    page.locator('summary').filter({ hasText: '尝试 1' }).first(),
  ).toBeVisible()
  await page.getByLabel('会话标题').fill('发布决策记录')
  await page.getByRole('button', { name: '重命名', exact: true }).click()
  await page.getByLabel('搜索会话').fill('发布决策')
  await page.getByRole('button', { name: '搜索', exact: true }).click()
  await expect(
    page.getByRole('link', { name: '发布决策记录', exact: true }),
  ).toBeVisible()
  await page.getByRole('checkbox', { name: '发布规范', exact: true }).check()
  await page.getByLabel('问题', { exact: true }).fill('回滚记录呢？')
  await expect(
    page.getByRole('button', { name: '发送问题', exact: true }),
  ).toBeEnabled()
  const followAccepted = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' &&
      response.url().endsWith('/runs') &&
      response.status() === 202,
  )
  await page.getByRole('button', { name: '发送问题', exact: true }).click()
  const nextRun = (await (await followAccepted).json()).data.runId
  expect(nextRun).not.toBe(accepted)
  await expect(page.getByLabel('运行结果')).toContainText(nextRun)
  await expect(page.getByTestId('final-answer')).toContainText('回滚', {
    timeout: 20000,
  })
  const conv = new URL(page.url()).pathname.split('/').at(-1)
  const messages = (
    await (
      await page.request.get(`/api/v1/conversations/${conv}/messages`, {
        headers,
      })
    ).json()
  ).data.items
  expect(
    messages.filter((m: { role: string }) => m.role === 'user'),
  ).toHaveLength(2)
  await expect(
    page.getByText('当前会话正在执行', { exact: false }),
  ).toHaveCount(0)
  await page.setViewportSize({ width: 390, height: 844 })
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true)
  await page.screenshot({
    path: info.outputPath('dialogue-mobile.png'),
    fullPage: true,
  })
  expect(errors).toEqual([])
})

test('admin audit is read only; revoked member loses displayed result and stream', async ({
  page,
  browser,
}) => {
  const { org, headers, agent, kb } = await prepare(page)
  const context = await browser.newContext({
    baseURL: new URL(page.url()).origin,
  })
  try {
    const memberPage = await context.newPage()
    const member = await account(memberPage)
    expect(
      (
        await page.request.post('/api/v1/members', {
          headers: { ...headers, 'Idempotency-Key': randomUUID() },
          data: { email: member.email },
        })
      ).status(),
    ).toBe(201)
    const mh = { ...member.headers, 'X-Organization-Id': org }
    const cid = (
      await (
        await memberPage.request.post('/api/v1/conversations', {
          headers: { ...mh, 'Idempotency-Key': randomUUID() },
          data: { agentId: agent, taskType: 'qa' },
        })
      ).json()
    ).data.id
    const rid = (
      await (
        await memberPage.request.post(`/api/v1/conversations/${cid}/runs`, {
          headers: { ...mh, 'Idempotency-Key': randomUUID() },
          data: {
            agentId: agent,
            input: { question: '发布流程', knowledgeBaseIds: [kb] },
          },
        })
      ).json()
    ).data.runId
    await memberPage.goto(`/o/${org}/chat/${cid}`)
    await expect(memberPage.getByTestId('draft')).toContainText('评审', {
      timeout: 15000,
    })
    await page.goto(`/o/${org}/runs/${rid}`)
    await expect(
      page.getByRole('heading', { name: '运行只读详情' }),
    ).toBeVisible()
    await expect(page.getByLabel('问题', { exact: true })).toHaveCount(0)
    await expect(
      page.getByRole('button', { name: /发送|停止|代发/ }),
    ).toHaveCount(0)
    await expect(page.getByRole('link', { name: /返回本人对话/ })).toHaveCount(
      0,
    )
    await page.goto(`/o/${org}/runs`)
    await expect(page.getByRole('heading', { name: /运行 0 次/ })).toBeVisible()
    await page.getByLabel('查看范围').selectOption('organization')
    await page.getByRole('button', { name: '应用筛选' }).click()
    await expect(page.getByRole('heading', { name: /运行 1 次/ })).toBeVisible()
    await page.getByRole('link', { name: '查看详情', exact: true }).click()
    await expect(
      page.getByRole('heading', { name: '运行只读详情' }),
    ).toBeVisible()
    const members = (
      await (await page.request.get('/api/v1/members', { headers })).json()
    ).data.items
    const record = members.find(
      (m: { userId: string }) => m.userId === member.userId,
    )
    expect(
      (
        await page.request.delete(
          `/api/v1/members/${member.userId}?expectedRevision=${record.revision}`,
          { headers },
        )
      ).status(),
    ).toBe(204)
    await memberPage.evaluate(() => window.dispatchEvent(new Event('focus')))
    await expect(memberPage.getByTestId('draft')).toHaveCount(0, {
      timeout: 15000,
    })
    await expect(memberPage.getByTestId('final-answer')).toHaveCount(0)
    expect(
      (
        await memberPage.request.get(`/api/v1/runs/${rid}/events`, {
          headers: mh,
        })
      ).status(),
    ).toBe(404)
  } finally {
    await context.close()
  }
})

test('run records share filters and expose historical detail and separate usage', async ({
  page,
}) => {
  const { org, headers, agent, kb, doc } = await prepare(page)
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  const cid = (
    await (
      await page.request.post('/api/v1/conversations', {
        headers: { ...headers, 'Idempotency-Key': randomUUID() },
        data: { agentId: agent, taskType: 'qa' },
      })
    ).json()
  ).data.id as string
  const accepted = await page.request.post(
    `/api/v1/conversations/${cid}/runs`,
    {
      headers: { ...headers, 'Idempotency-Key': randomUUID() },
      data: {
        agentId: agent,
        input: { question: '发布流程', knowledgeBaseIds: [kb] },
      },
    },
  )
  const rid = (await accepted.json()).data.runId as string
  await expect
    .poll(
      async () =>
        (
          await (
            await page.request.get(`/api/v1/runs/${rid}`, { headers })
          ).json()
        ).data.status,
      { timeout: 25000 },
    )
    .toBe('succeeded')
  await page.goto(`/o/${org}/runs`)
  await expect(
    page.getByRole('heading', { name: '执行记录与用量', exact: true }),
  ).toBeVisible()
  await expect(page.getByLabel('运行统计')).toContainText(
    '运行 1 次 · 成功率 100.0%',
  )
  await expect(
    page.getByRole('row').filter({ hasText: '文档助手' }),
  ).toContainText('已完成')
  await page.getByLabel('运行状态').selectOption('failed')
  await page.getByRole('button', { name: '应用筛选' }).click()
  await expect(page.getByLabel('运行统计')).toContainText(
    '运行 0 次 · 成功率 暂无',
  )
  await expect(
    page.getByText('没有符合条件的运行。', { exact: false }),
  ).toBeVisible()
  await page.getByLabel('运行状态').selectOption('')
  await page.getByLabel('时区', { exact: true }).fill('Asia/Shanghai')
  await page.getByRole('button', { name: '应用筛选' }).click()
  await expect(
    page.getByRole('columnheader', { name: '创建时间（Asia/Shanghai）' }),
  ).toBeVisible()
  await page.getByRole('link', { name: '查看详情', exact: true }).click()
  await expect(
    page.getByRole('heading', { name: '文档助手 · 历史版本 1' }),
  ).toBeVisible()
  await expect(page.getByText('发起者：', { exact: false })).toContainText(
    '排队',
  )
  await page.getByText('历史配置与资料范围', { exact: true }).click()
  await expect(
    page.getByText('历史配置与资料范围', { exact: true }).locator('..'),
  ).toContainText('gpt-4o-mini')
  await expect(
    page.getByRole('link', { name: '返回本人对话修改输入或准备重试' }),
  ).toBeVisible()
  await page.getByRole('button', { name: '查看简明步骤' }).click()
  await page
    .getByText('knowledge.search · 尝试 1 · succeeded', { exact: true })
    .click()
  await page.getByRole('button', { name: '查看步骤来源' }).first().click()
  // Equal-score fixture chunks may appear in either order; verify document identity
  // and a complete source passage rather than assuming the first chunk is review.
  await expect(page.getByRole('dialog')).toContainText(doc)
  await expect(page.getByRole('dialog')).toContainText(
    /发布必须经过评审。|发布应保留回滚记录。/,
  )
  await page.getByRole('button', { name: '关闭来源' }).click()
  expect(
    (
      await page.request.delete(`/api/v1/documents/${doc}`, { headers })
    ).status(),
  ).toBe(204)
  await page.reload()
  await expect(
    page.getByText('部分来源已删除，历史答案保留，原文不可用。'),
  ).toBeVisible()
  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('link', { name: '返回执行记录', exact: true }).click()
  await expect(
    page.getByRole('heading', { name: '执行记录与用量', exact: true }),
  ).toBeVisible()
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth + 1,
    ),
  ).toBe(true)
  expect(errors).toEqual([])
})

test('cancel response loss, offline refresh and explicit linked retry', async ({
  page,
  context,
}) => {
  const { org, headers, agent } = await prepare(page)
  await page.goto(`/o/${org}/chat`)
  await page.getByLabel('Agent', { exact: true }).selectOption(agent)
  await page.getByLabel('问题', { exact: true }).fill('发布停止与重试')
  await page.getByRole('button', { name: '发送问题', exact: true }).click()
  await expect(page.getByTestId('draft')).toContainText('评审', {
    timeout: 15000,
  })
  let cancelledId = ''
  await page.route('**/api/v1/runs/*/cancel', async (route) => {
    const id = route.request().url().split('/').at(-2) || ''
    const response = await route.fetch()
    expect([200, 202]).toContain(response.status())
    await route.abort('failed')
    cancelledId = id
  })
  await page.getByRole('button', { name: '停止运行', exact: true }).click()
  await expect.poll(() => cancelledId).not.toBe('')
  await page.unroute('**/api/v1/runs/*/cancel')
  await context.setOffline(true)
  await page.waitForTimeout(6000)
  await context.setOffline(false)
  await page.reload()
  await expect(page.getByLabel('运行结果')).toContainText('已取消', {
    timeout: 15000,
  })
  await expect(page.getByTestId('final-answer')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '下载 Markdown' })).toHaveCount(
    0,
  )
  expect(
    (
      await page.request.post(`/api/v1/runs/${cancelledId}/cancel`, { headers })
    ).status(),
  ).toBe(200)
  const conv = new URL(page.url()).pathname.split('/').at(-1)
  const before = (
    await (
      await page.request.get(`/api/v1/conversations/${conv}/messages`, {
        headers,
      })
    ).json()
  ).data.items
  expect(
    before.filter((m: { role: string }) => m.role === 'user'),
  ).toHaveLength(1)
  await page.getByRole('button', { name: '准备重试', exact: true }).click()
  await expect(page.getByLabel('问题', { exact: true })).toHaveValue(
    '发布停止与重试',
  )
  await expect(
    page.getByText(`将创建关联 ${cancelledId}`, { exact: false }),
  ).toBeVisible()
  const accepted = page.waitForResponse(
    (r) =>
      r.request().method() === 'POST' &&
      r.url().endsWith('/runs') &&
      r.status() === 202,
  )
  await page.getByRole('button', { name: '创建新重试', exact: true }).dblclick()
  const newId = (await (await accepted).json()).data.runId
  expect(newId).not.toBe(cancelledId)
  await expect(page.getByLabel('运行结果')).toContainText(
    `重试自运行 ${cancelledId}`,
  )
  await expect(page.getByTestId('final-answer')).toContainText('回滚', {
    timeout: 20000,
  })
  const retried = (
    await (await page.request.get(`/api/v1/runs/${newId}`, { headers })).json()
  ).data
  expect(retried.retryOfRunId).toBe(cancelledId)
  const after = (
    await (
      await page.request.get(`/api/v1/conversations/${conv}/messages`, {
        headers,
      })
    ).json()
  ).data.items
  expect(after.filter((m: { role: string }) => m.role === 'user')).toHaveLength(
    2,
  )
})
