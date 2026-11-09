<script setup lang="ts">
import type { components } from '../../../../types/api'
import { roleLabel } from '../../../../utils/api-error'
definePageMeta({ layout: 'workspace' })
type Member = components['schemas']['MemberDTO']
type Account = components['schemas']['AccountDTO']
const route = useRoute()
const orgId = String(route.params.orgId)
const { $api } = useNuxtApp()
const { session, organization, refresh: refreshIdentity } = useIdentity()
const { busy, message, run } = useFormAction()
const email = ref('')
const resolved = ref<Account | null>(null)
const addKey = ref('')
const cursor = ref<string | null>(null)
const canManage = computed(
  () =>
    organization.value?.effectiveRole === 'owner' ||
    organization.value?.effectiveRole === 'admin',
)
const isOwner = computed(() => organization.value?.effectiveRole === 'owner')
const { data, status, error, refresh } = await useAsyncData(
  `members:${orgId}`,
  () =>
    $api.request<components['schemas']['Page_MemberDTO_']>(
      `/members${cursor.value ? `?cursor=${encodeURIComponent(cursor.value)}` : ''}`,
      { organizationId: orgId },
    ),
)
watch(email, () => {
  resolved.value = null
  addKey.value = ''
})
async function reload() {
  cursor.value = null
  organization.value = await $api.request<
    components['schemas']['OrganizationDTO']
  >('/organizations/current', { organizationId: orgId })
  await refreshIdentity()
  await refresh()
}
async function resolve() {
  resolved.value = null
  await run(async () => {
    resolved.value = await $api.request<Account>('/members/resolve', {
      method: 'POST',
      organizationId: orgId,
      body: { email: email.value },
    })
    addKey.value = crypto.randomUUID()
  })
}
async function add() {
  await run(async () => {
    if (!resolved.value) return
    await $api.request('/members', {
      method: 'POST',
      organizationId: orgId,
      key: addKey.value,
      body: { email: resolved.value.email },
    })
    email.value = ''
    resolved.value = null
    await reload()
  })
}
async function changeRole(member: Member) {
  await run(async () => {
    await $api.request(`/members/${member.userId}`, {
      method: 'PATCH',
      organizationId: orgId,
      body: {
        baseRole: member.effectiveRole === 'admin' ? 'member' : 'admin',
        expectedRevision: member.revision,
      },
    })
    await reload()
  })
}
async function remove(member: Member) {
  if (
    !window.confirm(
      `移除 ${member.displayName}（${member.email}）？移除后将立即失去组织访问权限。`,
    )
  )
    return
  await run(async () => {
    await $api.empty(`/members/${member.userId}`, {
      method: 'DELETE',
      organizationId: orgId,
    })
    await reload()
  })
}
async function transfer(member: Member) {
  if (
    !window.confirm(
      `将组织所有权转移给 ${member.displayName}（${member.email}）？你将成为管理员。`,
    )
  )
    return
  await run(async () => {
    await $api.request('/organizations/current/transfer-owner', {
      method: 'POST',
      organizationId: orgId,
      key: crypto.randomUUID(),
      body: {
        targetUserId: member.userId,
        expectedRevision: organization.value?.accessRevision,
      },
    })
    await reload()
  })
}
</script>
<template>
  <div>
    <div class="page-intro">
      <h1>团队成员</h1>
      <p class="muted">管理当前组织的协作成员与访问权限。</p>
    </div>
    <p v-if="message" class="error" role="alert" tabindex="-1">{{ message }}</p>
    <section v-if="canManage" class="panel member-add">
      <h2>添加已注册账号</h2>
      <form class="inline-form" @submit.prevent="resolve">
        <label
          >成员邮箱<input
            v-model="email"
            type="email"
            required
            autocomplete="off"
            placeholder="同事注册时使用的邮箱" /></label
        ><button class="secondary" :disabled="busy">核对账号</button>
      </form>
      <div v-if="resolved" class="notice">
        <p>
          确认添加：<strong>{{ resolved.displayName }}</strong
          >（{{ resolved.email }}）
        </p>
        <p class="small">加入后角色为普通成员。</p>
        <button class="primary" :disabled="busy" @click="add">
          确认添加成员
        </button>
      </div>
    </section>
    <p v-if="status === 'pending'" role="status">正在加载成员…</p>
    <div v-else-if="error" class="error" role="alert">
      成员列表加载失败。<button class="text-button" @click="reload">
        重新加载
      </button>
    </div>
    <div v-else-if="data" class="table-scroll">
      <table>
        <caption class="sr-only">
          当前组织成员
        </caption>
        <thead>
          <tr>
            <th>成员</th>
            <th>角色</th>
            <th>加入时间</th>
            <th v-if="canManage">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="member in data.items" :key="member.userId">
            <td>
              <strong>{{ member.displayName }}</strong
              ><span v-if="member.userId === session?.user?.id" class="muted"
                >（你）</span
              ><span class="cell-detail">{{ member.email }}</span>
            </td>
            <td>
              <span class="badge">{{ roleLabel(member.effectiveRole) }}</span>
            </td>
            <td>{{ member.joinedAt.slice(0, 10) }}</td>
            <td v-if="canManage">
              <div v-if="member.effectiveRole !== 'owner'" class="row-actions">
                <button
                  v-if="isOwner"
                  class="text-button"
                  :disabled="busy"
                  @click="changeRole(member)"
                >
                  {{
                    member.effectiveRole === 'admin' ? '设为成员' : '设为管理员'
                  }}</button
                ><button
                  v-if="isOwner"
                  class="text-button"
                  :disabled="busy"
                  @click="transfer(member)"
                >
                  转移所有权</button
                ><button
                  v-if="isOwner || member.effectiveRole === 'member'"
                  class="text-button danger"
                  :disabled="busy"
                  @click="remove(member)"
                >
                  移除
                </button>
              </div>
              <span v-else class="muted">所有者</span>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-if="!data.items.length" class="empty">
        没有更多成员。请返回第一页。
      </p>
    </div>
    <div class="inline pagination">
      <button
        class="secondary"
        :disabled="busy || status === 'pending'"
        @click="run(reload)"
      >
        刷新 / 返回第一页</button
      ><button
        v-if="data?.nextCursor"
        class="secondary"
        :disabled="busy"
        @click="
          run(async () => {
            cursor = data?.nextCursor || null
            await refresh()
          })
        "
      >
        下一页
      </button>
    </div>
  </div>
</template>
