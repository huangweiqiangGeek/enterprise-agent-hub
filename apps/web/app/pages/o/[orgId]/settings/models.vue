<script setup lang="ts">
import type { components } from '../../../../types/api'
definePageMeta({ layout: 'workspace' })
const orgId = String(useRoute().params.orgId)
const { $api } = useNuxtApp()
const { organization } = useIdentity()
const canManage = computed(
  () =>
    organization.value?.effectiveRole === 'owner' ||
    organization.value?.effectiveRole === 'admin',
)
const { data, status, error, refresh } = await useAsyncData(
  `model-settings:${orgId}`,
  () =>
    $api.request<components['schemas']['ModelSettingsView']>(
      '/settings/models',
      { organizationId: orgId },
    ),
)
const kinds = computed(() => [
  ...new Set(data.value?.options.map((item) => item.kind)),
])
function config(
  kind: string,
): components['schemas']['ModelConfig'] | undefined {
  const item = data.value?.items.find((item) => item.kind === kind)
  return item && 'revision' in item ? item : undefined
}
</script>
<template>
  <div>
    <div class="page-intro">
      <p class="small muted">团队与设置</p>
      <h1>模型配置</h1>
      <p class="muted">
        连接聊天、向量与重排服务。测试成功后才会替换当前生效配置。
      </p>
    </div>
    <p v-if="status === 'pending' && !data" role="status">正在读取配置…</p>
    <div v-else-if="error" class="error" role="alert">
      无法读取模型配置。<button class="text-button" @click="refresh()">
        重新读取
      </button>
    </div>
    <div v-else-if="canManage" class="settings-stack">
      <ModelConfigPanel
        v-for="kind in kinds"
        :key="`${orgId}:${kind}`"
        :kind="kind"
        :options="data?.options.filter((option) => option.kind === kind) ?? []"
        :config="config(kind)"
        :organization-id="orgId"
        @saved="refresh()"
      />
    </div>
    <section v-else class="panel">
      <h2>可用模型</h2>
      <p v-if="!data?.items.length">尚未配置可用模型，请联系组织管理员。</p>
      <ul v-else>
        <li v-for="model in data.items" :key="model.id">
          {{ model.name }} · {{ model.kind }}
        </li>
      </ul>
      <p class="muted">只有所有者和管理员可测试或更改模型连接。</p>
    </section>
  </div>
</template>
