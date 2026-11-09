export default defineNuxtConfig({
  compatibilityDate: '2026-11-06',
  devtools: { enabled: false },
  modules: ['@nuxt/eslint'],
  css: ['~/assets/main.css'],
  typescript: { strict: true },
  runtimeConfig: {
    apiBase: 'http://127.0.0.1:8300',
  },
  app: {
    head: {
      htmlAttrs: { lang: 'zh-CN' },
      title: 'Enterprise Agent Hub',
      meta: [{ name: 'description', content: '研发团队 AI Agent 工作平台' }],
    },
  },
})
