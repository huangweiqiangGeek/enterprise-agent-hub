<script setup lang="ts">
import type { components } from '../../../../types/api'
definePageMeta({ layout: 'workspace' })
const orgId = String(useRoute().params.orgId)
const { $api } = useNuxtApp()
const { organization } = useIdentity()
const canManage = computed(() =>
  ['owner', 'admin'].includes(organization.value?.effectiveRole || ''),
)
const q = ref(''),
  task = ref(''),
  enabled = ref('')
const filter = ref(''),
  cursor = ref<string | null>(null)
const labels = {
  qa: '文档问答',
  compare: '需求与方案对比',
  progress: '进展与风险总结',
}
const { data, status, error, refresh } = await useAsyncData(
  `agents:${orgId}`,
  () =>
    $api.request<components['schemas']['Page_AgentSummary_']>(
      `/agents?${filter.value}${cursor.value ? `&cursor=${encodeURIComponent(cursor.value)}` : ''}`,
      { organizationId: orgId },
    ),
  { watch: [filter, cursor] },
)
function search() {
  cursor.value = null
  filter.value = new URLSearchParams({
    q: q.value,
    ...(task.value ? { taskType: task.value } : {}),
    ...(canManage.value && enabled.value ? { enabled: enabled.value } : {}),
  }).toString()
}
</script>
<template>
  <div>
    <header class="page-intro model-heading">
      <div>
        <p class="small muted">团队助手</p>
        <h1>Agent</h1>
        <p class="muted">
          保存用途、提示词与允许资源；每次编辑保留独立配置版本。
        </p>
      </div>
      <NuxtLink v-if="canManage" class="primary" :to="`/o/${orgId}/agents/new`"
        >新建Agent</NuxtLink
      >
    </header>
    <form class="inline-form knowledge-filter" @submit.prevent="search">
      <label>搜索Agent<input v-model="q" maxlength="200" /></label
      ><label
        >任务类型筛选<select v-model="task">
          <option value="">全部类型</option>
          <option v-for="(label, key) in labels" :key="key" :value="key">
            {{ label }}
          </option>
        </select></label
      ><label v-if="canManage"
        >启用状态<select v-model="enabled">
          <option value="">全部状态</option>
          <option value="true">已启用</option>
          <option value="false">已停用</option>
        </select></label
      ><button class="secondary">筛选</button>
    </form>
    <p v-if="status === 'pending'" role="status">正在读取Agent…</p>
    <p v-else-if="error" class="error" role="alert">
      Agent读取失败。<button class="text-button" @click="refresh()">
        重试
      </button>
    </p>
    <template v-else-if="data"
      ><p v-if="!data.items.length" class="empty">
        暂无符合条件的Agent。{{
          canManage
            ? '可以创建停用配置，逐步完善。'
            : '请联系管理员配置并启用。'
        }}
      </p>
      <ul v-else class="knowledge-list">
        <li v-for="agent in data.items" :key="agent.id">
          <div class="model-heading">
            <NuxtLink :to="`/o/${orgId}/agents/${agent.id}`"
              ><h2>{{ agent.name }}</h2></NuxtLink
            ><span class="badge">{{
              agent.enabled ? '已启用' : '已停用'
            }}</span>
          </div>
          <p>{{ agent.description || '暂无说明' }}</p>
          <p class="small muted">
            {{ labels[agent.taskType] }} · 第 {{ agent.versionNumber }} 版
          </p>
          <p v-if="agent.readiness.length" class="small">
            {{ agent.readiness.length }} 项配置条件待完成
          </p>
          <NuxtLink :to="`/o/${orgId}/agents/${agent.id}`">{{
            canManage ? '管理配置' : '查看用途'
          }}</NuxtLink>
        </li>
      </ul>
      <div class="inline pagination">
        <button v-if="cursor" class="secondary" @click="cursor = null">
          回到首页</button
        ><button
          v-if="data.nextCursor"
          class="secondary"
          @click="cursor = data.nextCursor || null"
        >
          下一页
        </button>
      </div>
    </template>
  </div>
</template>
