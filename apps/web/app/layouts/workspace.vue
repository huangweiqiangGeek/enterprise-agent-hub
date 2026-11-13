<script setup lang="ts">
import type { components } from '../types/api'
import { apiError, roleLabel } from '../utils/api-error'
const { $api } = useNuxtApp()
const { session, organization, refresh, logout, clearOrganization } =
  useIdentity()
const { message, busy, run } = useFormAction()
const route = useRoute()
let timer: ReturnType<typeof setInterval> | undefined
let checking = false
async function switchOrganization(event: Event) {
  if (event.target instanceof HTMLSelectElement)
    await navigateTo(`/o/${event.target.value}`)
}
async function checkAccess() {
  const orgId = organization.value?.id
  if (!orgId || checking || document.hidden) return
  checking = true
  try {
    const current = await $api.request<
      components['schemas']['OrganizationDTO']
    >('/organizations/current', { organizationId: orgId })
    if (organization.value?.id !== orgId) return
    message.value = ''
    if (current.accessRevision !== organization.value.accessRevision) {
      clearOrganization()
      organization.value = current
      await refresh()
      await refreshNuxtData()
    }
  } catch (error) {
    const failure = apiError(error)
    if (failure.status === 401 || failure.status === 404) {
      clearOrganization()
      await refresh()
      await navigateTo('/onboarding?notice=access')
    } else if (failure.code !== 'ABORTED') message.value = failure.message
  } finally {
    checking = false
  }
}
onMounted(() => {
  timer = setInterval(checkAccess, 5000)
  window.addEventListener('focus', checkAccess)
})
onUnmounted(() => {
  clearInterval(timer)
  window.removeEventListener('focus', checkAccess)
})
</script>
<template>
  <div class="workspace">
    <header class="topbar">
      <NuxtLink class="brand" to="/onboarding">Enterprise Agent Hub</NuxtLink>
      <div class="inline">
        <label class="switcher"
          >组织<select
            :value="organization?.id || ''"
            aria-label="切换组织"
            @change="switchOrganization"
          >
            <option value="" disabled>选择组织</option>
            <option
              v-for="org in session?.organizations"
              :key="org.id"
              :value="org.id"
            >
              {{ org.name }}
            </option>
          </select></label
        ><NuxtLink to="/account">{{ session?.user?.displayName }}</NuxtLink
        ><button class="text-button" :disabled="busy" @click="run(logout)">
          退出
        </button>
      </div>
    </header>
    <div class="workspace-body">
      <aside class="sidebar">
        <p class="small muted">{{ organization?.name || '个人账号' }}</p>
        <p v-if="organization" class="badge">
          {{ roleLabel(organization.effectiveRole) }}
        </p>
        <nav aria-label="主要导航">
          <NuxtLink
            v-if="organization"
            :to="`/o/${organization.id}`"
            :aria-current="
              route.path === `/o/${organization.id}` ? 'page' : undefined
            "
            >组织概览</NuxtLink
          ><NuxtLink v-if="organization" :to="`/o/${organization.id}/knowledge`"
            >知识库</NuxtLink
          ><NuxtLink v-if="organization" :to="`/o/${organization.id}/chat`"
            >对话工作台</NuxtLink
          ><NuxtLink v-if="organization" :to="`/o/${organization.id}/runs`"
            >执行记录与用量</NuxtLink
          ><NuxtLink v-if="organization" :to="`/o/${organization.id}/agents`"
            >Agent</NuxtLink
          ><NuxtLink
            v-if="organization"
            :to="`/o/${organization.id}/settings/team`"
            >团队成员</NuxtLink
          ><NuxtLink
            v-if="organization"
            :to="`/o/${organization.id}/settings/models`"
            >模型配置</NuxtLink
          ><NuxtLink
            v-if="organization"
            :to="`/o/${organization.id}/settings/limits`"
            >组织限制</NuxtLink
          ><NuxtLink to="/account">账号设置</NuxtLink
          ><NuxtLink to="/onboarding">所有组织</NuxtLink>
        </nav>
      </aside>
      <main class="workspace-content">
        <p v-if="message" class="error" role="alert" tabindex="-1">
          {{ message }}
        </p>
        <slot />
      </main>
    </div>
  </div>
</template>
