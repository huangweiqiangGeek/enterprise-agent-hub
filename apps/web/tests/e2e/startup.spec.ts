import { expect, test } from '@playwright/test'

test('renders the startup page and proxies the actual API', async ({
  page,
}) => {
  await page.goto('/status')
  await expect(page.getByRole('heading', { level: 1 })).toHaveText(
    '研发团队的知识与任务助手',
  )
  const response = await page.request.get('/api/v1/health/live')
  expect(response.ok()).toBe(true)
  expect((await response.json()).data.status).toBe('ok')
  const ready = await page.request.get('/api/v1/health/ready')
  expect([200, 503]).toContain(ready.status())
  const expected = ready.ok() ? '服务与数据库已就绪' : '服务尚未就绪'
  await expect(page.getByTestId('health-status')).toHaveText(expected)
  await page.getByRole('button', { name: '重新检查' }).click()
  await expect(page.getByTestId('health-status')).toHaveText(expected)
})
