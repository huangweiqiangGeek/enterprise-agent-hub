<script setup lang="ts">
import type { components } from '../types/api'
import { healthLabel } from '../utils/health'

type HealthResponse = components['schemas']['HealthResponse']

const { data, error, status, refresh } = await useFetch<HealthResponse>(
  '/api/v1/health/ready',
  { retry: 0, timeout: 6000 },
)
const label = computed(() => healthLabel(status.value, data.value?.data.status))
</script>

<template>
  <main>
    <p class="eyebrow">ENTERPRISE AGENT HUB</p>
    <h1>研发团队的知识与任务助手</h1>
    <p class="intro">连接文档与项目进展，让每个结论都有来源。</p>
    <section aria-labelledby="setup-title">
      <h2 id="setup-title">工程启动检查</h2>
      <p>基础服务状态。<NuxtLink to="/login">进入工作空间</NuxtLink></p>
      <p role="status" data-testid="health-status">{{ label }}</p>
      <p v-if="error" class="hint">
        请按 README 检查数据库和初始化步骤后重试。
      </p>
      <button :disabled="status === 'pending'" @click="refresh()">
        重新检查
      </button>
    </section>
  </main>
</template>

<style scoped>
:root {
  font-family:
    system-ui,
    -apple-system,
    'Segoe UI',
    sans-serif;
  color: #183a36;
  background: #f5f5ee;
}
body {
  margin: 0;
}
main {
  max-width: 780px;
  padding: 80px 24px;
  margin: auto;
}
.eyebrow {
  font-size: 12px;
  letter-spacing: 0.12em;
}
h1 {
  font-size: clamp(28px, 5vw, 44px);
  line-height: 1.25;
}
.intro {
  line-height: 1.8;
}
section {
  margin-top: 48px;
  padding-top: 24px;
  border-top: 1px solid #a6b9b3;
}
h2 {
  font-size: 20px;
}
.hint {
  color: #78572b;
}
button {
  padding: 10px 18px;
  border: 1px solid #24584d;
  border-radius: 4px;
  color: #fff;
  background: #24584d;
  font: inherit;
  cursor: pointer;
}
button:focus-visible {
  outline: 3px solid #b18b46;
  outline-offset: 3px;
}
button:disabled {
  cursor: wait;
  opacity: 0.6;
}
</style>
