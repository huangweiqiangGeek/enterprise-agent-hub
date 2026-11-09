import withNuxt from './.nuxt/eslint.config.mjs'

export default withNuxt({
  ignores: ['app/types/api.d.ts', 'playwright-report/**', 'test-results/**'],
  rules: {
    'vue/html-self-closing': [
      'error',
      { html: { void: 'always', normal: 'never', component: 'always' } },
    ],
  },
})
