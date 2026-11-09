<script setup lang="ts">
import type { components } from '../../../../types/api'
import { apiError } from '../../../../utils/api-error'
definePageMeta({ layout: 'workspace', key: (route) => route.path })
const route = useRoute(),
  orgId = String(route.params.orgId),
  agentId = String(route.params.agentId)
const { $api } = useNuxtApp()
const { organization } = useIdentity()
const canManage = computed(() =>
  ['owner', 'admin'].includes(organization.value?.effectiveRole || ''),
)
const { data, status, error, refresh } = await useAsyncData(
  `agent:${orgId}:${agentId}`,
  () =>
    $api.request<
      | components['schemas']['AgentConfig']
      | components['schemas']['AgentSummary']
    >(`/agents/${agentId}`, { organizationId: orgId }),
)
</script>
<template>
  <div>
    <NuxtLink :to="`/o/${orgId}/agents`">← 所有Agent</NuxtLink>
    <p v-if="status === 'pending'" role="status">正在读取配置…</p>
    <div v-else-if="error" class="error" role="alert">
      {{ apiError(error).message
      }}<button class="text-button" @click="refresh()">重新读取</button>
    </div>
    <template v-else-if="data"
      ><header class="page-intro">
        <p class="small muted">Agent配置 · 第 {{ data.versionNumber }} 版</p>
        <h1>{{ data.name }}</h1>
      </header>
      <AgentEditor
        v-if="canManage && 'prompt' in data"
        :organization-id="orgId"
        :initial="data"
        @saved="data = $event"
      />
      <section v-else class="panel">
        <p>{{ data.description || '暂无说明' }}</p>
        <p v-for="issue in data.readiness" :key="issue.code" class="notice">
          {{ issue.message }}
        </p>
        <NuxtLink
          v-if="
            data.enabled && data.taskType === 'qa' && !data.readiness.length
          "
          :to="`/o/${orgId}/chat?agent=${agentId}`"
          >开始文档问答</NuxtLink
        >
        <button v-else class="secondary" disabled>开始任务（尚未就绪）</button>
        <p class="small muted">
          开始问答将使用已保存的第
          {{ data.versionNumber }} 版配置并计入用量。
        </p>
      </section></template
    >
  </div>
</template>
