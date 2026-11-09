<script setup lang="ts">
import type { components } from '../types/api'
import { roleLabel } from '../utils/api-error'
const { $api } = useNuxtApp()
const { session, refresh, logout } = useIdentity()
const { busy, message, fields, run } = useFormAction()
const route = useRoute()
const name = ref('')
const key = ref('')
const submittedName = ref('')
async function create() {
  await run(async () => {
    const trimmed = name.value.trim()
    if (!key.value || submittedName.value !== trimmed) {
      key.value = crypto.randomUUID()
      submittedName.value = trimmed
    }
    const org = await $api.request<components['schemas']['OrganizationDTO']>(
      '/organizations',
      { method: 'POST', key: key.value, body: { name: trimmed } },
    )
    await refresh()
    await navigateTo(`/o/${org.id}`)
  })
}
</script>
<template>
  <main class="onboarding">
    <header class="topbar">
      <NuxtLink class="brand" to="/onboarding">Enterprise Agent Hub</NuxtLink>
      <div class="inline">
        <NuxtLink to="/account">{{ session?.user?.displayName }}</NuxtLink
        ><button class="text-button" :disabled="busy" @click="run(logout)">
          退出
        </button>
      </div>
    </header>
    <div class="page-intro">
      <h1>选择你的团队空间</h1>
      <p class="muted">每个组织独立管理成员、资料与任务。</p>
    </div>
    <p v-if="route.query.notice === 'access'" role="status" class="notice">
      该组织已不可访问，请选择其他组织或联系管理员。
    </p>
    <div class="onboarding-columns">
      <section aria-labelledby="org-list-title">
        <h2 id="org-list-title">已加入的组织</h2>
        <ul v-if="session?.organizations.length" class="organization-list">
          <li v-for="org in session.organizations" :key="org.id">
            <NuxtLink :to="`/o/${org.id}`"
              ><span>{{ org.name }}</span
              ><span class="badge">{{
                roleLabel(org.effectiveRole)
              }}</span></NuxtLink
            >
          </li>
        </ul>
        <div v-else class="empty">
          <h3>还没有加入组织</h3>
          <p>创建一个团队空间，或将你的注册邮箱发给管理员，由对方添加。</p>
        </div>
        <button
          class="secondary"
          :disabled="busy"
          @click="
            run(async () => {
              await refresh()
            })
          "
        >
          刷新组织列表
        </button>
      </section>
      <section class="panel" aria-labelledby="create-org-title">
        <h2 id="create-org-title">创建组织</h2>
        <p class="muted">你将成为新组织的所有者。</p>
        <form @submit.prevent="create">
          <label
            >组织名称<input
              v-model="name"
              name="name"
              required
              maxlength="100"
              :aria-invalid="fields.includes('name')"
              placeholder="例如：产品研发团队" /></label
          ><button class="primary" :disabled="busy">
            {{ busy ? '正在处理…' : '创建并进入' }}
          </button>
        </form>
      </section>
    </div>
    <p v-if="message" class="error" role="alert" tabindex="-1">{{ message }}</p>
  </main>
</template>
