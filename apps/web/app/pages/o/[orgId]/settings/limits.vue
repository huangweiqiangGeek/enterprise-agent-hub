<script setup lang="ts">
import type { components } from '../../../../types/api'
definePageMeta({ layout: 'workspace', key: (route) => route.path })
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
const { busy, message, code, run } = useFormAction()
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
const draft = ref<Limits | null>(
  data.value ? { ...data.value.effective } : null,
)
const baseline = ref(JSON.stringify(draft.value))
const revision = ref(data.value?.revision ?? 0)
const notice = ref('')
const dirty = computed(() => JSON.stringify(draft.value) !== baseline.value)
const stale = computed(
  () => data.value && data.value.revision !== revision.value,
)
function apply(value: View) {
  draft.value = { ...value.effective }
  baseline.value = JSON.stringify(draft.value)
  revision.value = value.revision
}
watch(data, (value) => {
  if (value && !dirty.value) apply(value)
})
async function reload() {
  if (dirty.value && !window.confirm('重新读取会丢弃未保存的限制，继续吗？'))
    return
  await run(async () => {
    const value = await $api.request<View>('/settings/limits', {
      organizationId: orgId,
    })
    apply(value)
    data.value = value
    notice.value = '已读取当前生效限制。'
  })
}
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
    const value = await $api.request<View>('/settings/limits', {
      method: 'PUT',
      organizationId: orgId,
      body: { limits: draft.value, expectedRevision: revision.value },
    })
    apply(value)
    data.value = value
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
        上传与运行均在接受时核对当前限制；权限撤销仍立即生效。
        可将已降低的数值恢复至系统硬上限以内。
      </p>
      <p class="small muted">
        生效版本：{{ data.revision }}；当前编辑基于版本 {{ revision }}。
      </p>
      <p v-if="stale || code === 'VERSION_CONFLICT'" class="notice">
        限制已被更新，当前输入已保留。请先重新读取，再核对修改。
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
      <p v-if="message" class="error" role="alert" tabindex="-1">
        {{ message }}
      </p>
      <p v-if="notice" class="success" role="status">{{ notice }}</p>
      <button
        v-if="canManage"
        class="primary"
        type="submit"
        :disabled="busy || !interactive || !dirty || !!stale"
      >
        {{ busy ? '正在保存…' : '保存组织限制' }}
      </button>
      <p v-else class="muted">只有所有者和管理员可修改组织上限。</p>
      <button
        type="button"
        class="secondary"
        :disabled="busy || !interactive"
        @click="reload"
      >
        重新读取生效限制
      </button>
    </form>
  </div>
</template>
