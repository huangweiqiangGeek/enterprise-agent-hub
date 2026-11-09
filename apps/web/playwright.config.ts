import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests/e2e',
  use: { baseURL: process.env.EAH_WEB_URL || 'http://127.0.0.1:3300' },
  reporter: 'list',
  retries: 0,
})
