import { randomUUID } from 'node:crypto'
import { expect, test, type Page } from '@playwright/test'

async function register(page: Page) {
  await page.goto('/register')
  await page.getByLabel('显示名').fill('模型验收用户')
  await page
    .getByLabel('邮箱', { exact: true })
    .fill(`${randomUUID()}@example.com`)
  await page.locator('input[name="password"]').fill('Model-browser-2026')
  await page.getByRole('button', { name: '注册并继续' }).click()
  await expect(page).toHaveURL(/\/onboarding$/)
  await page.getByLabel('组织名称').fill('模型验收组织')
  await page.getByRole('button', { name: '创建并进入' }).click()
  await expect(page).toHaveURL(/\/o\/[\w-]+$/)
}

test('organization limits persist, enforce bounds and work on mobile', async ({
  page,
}, testInfo) => {
  await register(page)
  await page.getByRole('link', { name: '组织限制', exact: true }).click()
  await page.getByLabel('每次运行模型步骤', { exact: true }).fill('4')
  await page.getByRole('button', { name: '保存组织限制' }).click()
  await expect(page.getByRole('status')).toContainText('组织限制已保存')
  await page.reload()
  await expect(
    page.getByLabel('每次运行模型步骤', { exact: true }),
  ).toHaveValue('4')
  await page.getByLabel('每次运行模型步骤', { exact: true }).fill('11')
  expect(
    await page
      .getByLabel('每次运行模型步骤', { exact: true })
      .evaluate(
        (el) => el instanceof HTMLInputElement && el.validity.rangeOverflow,
      ),
  ).toBe(true)
  await page.getByLabel('每次运行模型步骤', { exact: true }).fill('4')
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({
    path: testInfo.outputPath('limits-mobile.png'),
    fullPage: true,
  })
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true)
})

test('model candidates test and save against isolated protocol fixture, no credential in response or SSR', async ({
  page,
}, testInfo) => {
  test.skip(
    process.env.EAH_BROWSER_MODEL_FIXTURE !== '1',
    'Requires explicit isolated protocol fixture; never a real-provider claim',
  )
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await register(page)
  await page.getByRole('link', { name: '模型配置', exact: true }).click()
  const chat = page.getByRole('region', { name: '聊天模型', exact: true })
  await chat.getByLabel('API Key', { exact: true }).fill('browser-private-key')
  await chat.getByRole('button', { name: '仅测试', exact: true }).click()
  await expect(chat.getByRole('status')).toContainText('测试通过尚未保存')
  await expect(chat.getByLabel('API Key', { exact: true })).toHaveValue('')
  await chat.getByLabel('API Key', { exact: true }).fill('browser-private-key')
  const saved = page.waitForResponse(
    (response) =>
      response.url().includes('/settings/models/chat') &&
      response.request().method() === 'PUT',
  )
  await chat.getByRole('button', { name: '测试并保存', exact: true }).click()
  const response = await saved
  expect(response.status()).toBe(200)
  expect(await response.text()).not.toContain('browser-private-key')
  await expect(chat.getByRole('status')).toContainText('配置已测试通过并生效')
  await page.reload()
  await expect(chat.getByText('已生效', { exact: true })).toBeVisible()
  const html = await page.request.get(page.url())
  expect(await html.text()).not.toContain('browser-private-key')
  await chat.getByLabel('API Key', { exact: true }).fill('must-clear-on-switch')
  await chat
    .getByRole('combobox', { name: '服务协议', exact: true })
    .selectOption('dashscope-chat')
  await expect(chat.getByLabel('API Key', { exact: true })).toHaveValue('')
  await expect(
    chat.getByRole('combobox', { name: '模型名称', exact: true }),
  ).toHaveValue('qwen-plus')
  await chat.getByLabel('API Key', { exact: true }).fill('bailian-fixture-only')
  await chat.getByRole('button', { name: '测试并保存', exact: true }).click()
  await expect(chat.getByRole('status')).toContainText('配置已测试通过并生效')
  await page.reload()
  await expect(
    chat.getByRole('combobox', { name: '服务协议', exact: true }),
  ).toHaveValue('dashscope-chat')
  const rerank = page.getByRole('region', { name: 'Rerank 服务', exact: true })
  await rerank
    .getByRole('combobox', { name: '服务协议', exact: true })
    .selectOption('dashscope-rerank')
  await rerank.getByLabel('模型名称', { exact: true }).fill('gte-rerank-v2')
  await rerank
    .getByLabel('API Key', { exact: true })
    .fill('bailian-fixture-only')
  await rerank.getByRole('button', { name: '测试并保存', exact: true }).click()
  await expect(rerank.getByRole('status')).toContainText('配置已测试通过并生效')
  await page.screenshot({
    path: testInfo.outputPath('models-desktop.png'),
    fullPage: true,
  })
  const embedding = page.getByRole('region', {
    name: 'Embedding 模型',
    exact: true,
  })
  await embedding.getByLabel('模型名称', { exact: true }).fill('test-embedding')
  await embedding.getByLabel('向量维度（可选）').fill('3')
  await embedding
    .getByLabel('API Key', { exact: true })
    .fill('embedding-private-key')
  await embedding
    .getByRole('button', { name: '测试并保存', exact: true })
    .click()
  await expect(embedding.getByRole('status')).toContainText(
    '配置已测试通过并生效',
  )
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({
    path: testInfo.outputPath('models-mobile.png'),
    fullPage: true,
  })
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true)
  expect(errors).toEqual([])
})
