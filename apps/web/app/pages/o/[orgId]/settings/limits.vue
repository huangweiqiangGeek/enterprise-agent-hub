<script setup lang="ts">
import type { components } from '../../../../types/api'
definePageMeta({ layout: 'workspace' })
type Limits = components['schemas']['Limits']
type View = components['schemas']['LimitsView']
const orgId = String(useRoute().params.orgId)
const { $api } = useNuxtApp()
const { session, organization } = useIdentity()
const canManage = computed(
  () =>
    organization.value?.effectiveRole === 'owner' ||
    organization.value?.effectiveRole === 'admin',
)
const { data, status, error, refresh } = await useAsyncData(
  `limits:${orgId}`,
  () => $api.request<View>('/settings/limits', { organizationId: orgId }),
)
const { busy, message, run } = useFormAction()
const interactive = ref(false)
onMounted(() => {
  interactive.value = true
})
const fields: { key: keyof Limits; label: string; unit: string }[] = [
  { key: 'maxRunSteps', label: '每次运行模型步骤', unit: '步' },
  { key: 'maxToolAttempts', label: '每次运行工具尝试', unit: '次（含重试）' },
  { key: 'runDeadlineSeconds', label: '运行时限', unit: '秒' },
  {
    key: 'maxRunTokens',
    label: '运行聊天 Token 预算',
    unit: '输入与输出 Token',
  },
  {
    key: 'maxUploadBytes',
    label: '单文件上传大小',
    unit: '字节（1 MiB = 1048576）',
  },
  { key: 'maxActiveRunsPerOrganization', label: '组织同时运行数', unit: '个' },
  { key: 'maxQueuedRunsPerOrganization', label: '组织排队运行数', unit: '个' },
]
const draft = ref<Limits | null>(null)
const notice = ref('')
watch(
  () => data.value?.revision,
  () => {
    const value = data.value
    draft.value = value ? { ...value.effective } : null
  },
  { immediate: true },
)
const dirty = computed(
  () =>
    JSON.stringify(draft.value) !==
    JSON.stringify(data.value?.effective ?? null),
)
onBeforeRouteLeave(() => {
  if (
    session.value?.user &&
    dirty.value &&
    !window.confirm('离开后将丢弃未保存的限制，继续吗？')
  )
    return false
})
async function save() {
  notice.value = ''
  await run(async () => {
    if (!data.value || !draft.value) return
    data.value = await $api.request<View>('/settings/limits', {
      method: 'PUT',
      organizationId: orgId,
      body: { limits: draft.value, expectedRevision: data.value.revision },
    })
    notice.value = '组织限制已保存，仅影响新接受的任务。'
  })
}
</script>
<template>
  <div>
    <div class="page-intro">
      <p class="small muted">团队与设置</p>
      <h1>组织限制</h1>
      <p class="muted">
        组织上限可以低于系统硬上限。变更只影响新接受的任务，进行中的任务保持原有限制。
      </p>
    </div>
    <p v-if="status === 'pending' && !data" role="status">正在读取限制…</p>
    <div v-else-if="error" role="alert" class="error">
      无法读取组织限制。<button class="text-button" @click="refresh()">
        重新读取
      </button>
    </div>
    <form v-else-if="data && draft" class="panel" @submit.prevent="save">
      <p class="small muted">
        单文件上传大小限制已生效；Agent
        任务尚未开放，其接受流程将在后续任务使用运行限制。
      </p>
      <div class="limits-grid">
        <div v-for="field in fields" :key="field.key" class="limit-field">
          <label :for="field.key">{{ field.label }}</label>
          <input
            v-if="canManage"
            :id="field.key"
            v-model.number="draft[field.key]"
            type="number"
            min="1"
            :max="data.system[field.key]"
            step="1"
            required
            :disabled="busy || !interactive"
          />
          <p v-else>{{ data.effective[field.key] }}</p>
          <p class="small muted">
            {{ field.unit }} · 系统硬上限 {{ data.system[field.key] }}
          </p>
        </div>
      </div>
      <p v-if="message" class="error" role="alert">{{ message }}</p>
      <p v-if="notice" class="success" role="status">{{ notice }}</p>
      <button
        v-if="canManage"
        class="primary"
        type="submit"
        :disabled="busy || !dirty"
      >
        {{ busy ? '正在保存…' : '保存组织限制' }}
      </button>
      <p v-else class="muted">只有所有者和管理员可修改组织上限。</p>
    </form>
  </div>
</template>
