<script setup lang="ts">
import type { components } from '../types/api'
import { ApiError } from '../utils/api-error'
definePageMeta({ layout: 'workspace' })
const { $api } = useNuxtApp()
const { session, refresh, clearOrganization } = useIdentity()
const { busy, message, fields, run } = useFormAction()
const displayName = ref(session.value?.user?.displayName || '')
const currentPassword = ref('')
const newPassword = ref('')
const confirmation = ref('')
const saved = ref('')
const dirty = computed(
  () =>
    displayName.value !== session.value?.user?.displayName ||
    !!currentPassword.value ||
    !!newPassword.value,
)
const passwordChanged = ref(false)
onBeforeRouteLeave(
  () =>
    passwordChanged.value ||
    !session.value?.user ||
    !dirty.value ||
    window.confirm('修改尚未保存，确定离开吗？'),
)
async function saveName() {
  saved.value = ''
  await run(async () => {
    const user = session.value?.user
    if (!user) return
    await $api.request<components['schemas']['UserDTO']>('/me', {
      method: 'PATCH',
      body: { displayName: displayName.value, expectedRevision: user.revision },
    })
    await refresh()
    displayName.value = session.value?.user?.displayName || ''
    saved.value = '显示名已保存。'
  })
}
async function changePassword() {
  saved.value = ''
  await run(async () => {
    if (newPassword.value !== confirmation.value)
      throw new ApiError('两次新密码不一致。', 422, 'CONFIRM_PASSWORD', [
        'confirmation',
      ])
    await $api.empty('/me/password', {
      method: 'POST',
      body: {
        currentPassword: currentPassword.value,
        newPassword: newPassword.value,
      },
    })
    currentPassword.value = newPassword.value = confirmation.value = ''
    passwordChanged.value = true
    clearOrganization()
    session.value = null
    await navigateTo('/login?notice=password')
  })
}
</script>
<template>
  <div class="account-page">
    <div class="page-intro">
      <h1>账号设置</h1>
      <p class="muted">{{ session?.user?.email }}</p>
    </div>
    <p v-if="saved" role="status" class="success">{{ saved }}</p>
    <p v-if="message" role="alert" tabindex="-1" class="error">{{ message }}</p>
    <section class="form-section">
      <h2>个人资料</h2>
      <form @submit.prevent="saveName">
        <label
          >显示名<input
            v-model="displayName"
            name="displayName"
            required
            maxlength="100"
            :aria-invalid="fields.includes('displayName')" /></label
        ><button class="primary" :disabled="busy">保存显示名</button>
      </form>
    </section>
    <section class="form-section">
      <h2>修改密码</h2>
      <p class="muted">修改后将退出此账号的全部登录会话，需要重新登录。</p>
      <form @submit.prevent="changePassword">
        <label
          >当前密码<input
            v-model="currentPassword"
            type="password"
            autocomplete="current-password"
            required
            minlength="8"
            maxlength="128"
            :aria-invalid="fields.includes('currentPassword')" /></label
        ><label
          >新密码<input
            v-model="newPassword"
            type="password"
            autocomplete="new-password"
            required
            minlength="8"
            maxlength="128"
            :aria-invalid="fields.includes('newPassword')" /></label
        ><label
          >再次输入新密码<input
            v-model="confirmation"
            type="password"
            autocomplete="new-password"
            required
            minlength="8"
            maxlength="128"
            :aria-invalid="fields.includes('confirmation')" /></label
        ><button class="primary" :disabled="busy">修改密码并退出</button>
      </form>
    </section>
  </div>
</template>
