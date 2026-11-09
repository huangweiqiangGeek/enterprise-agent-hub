<script setup lang="ts">
import type { components } from '../../../../types/api'
definePageMeta({ layout: 'workspace' })
const orgId = String(useRoute().params.orgId)
const { organization } = useIdentity()
const canManage = computed(() =>
  ['owner', 'admin'].includes(organization.value?.effectiveRole || ''),
)
async function saved(config: components['schemas']['AgentConfig']) {
  await navigateTo(`/o/${orgId}/agents/${config.id}`)
}
</script>
<template>
  <div>
    <NuxtLink :to="`/o/${orgId}/agents`">← 所有Agent</NuxtLink>
    <header class="page-intro">
      <h1>新建Agent</h1>
      <p class="muted">从空白或预置模板开始，先保存停用配置。</p>
    </header>
    <AgentEditor v-if="canManage" :organization-id="orgId" @saved="saved" />
    <p v-else class="error" role="alert">只有管理员可以创建Agent。</p>
  </div>
</template>
