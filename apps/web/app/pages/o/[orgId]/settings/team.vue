<script setup lang="ts">
import type { components } from '../../../../types/api'
import { ApiError, apiError, roleLabel } from '../../../../utils/api-error'
definePageMeta({ layout: 'workspace', key: (route) => route.path })
type Member = components['schemas']['MemberDTO']
type Account = components['schemas']['AccountDTO']
const route = useRoute()
const orgId = String(route.params.orgId)
const { $api } = useNuxtApp()
const { session, organization, refresh: refreshIdentity } = useIdentity()
const { busy, message, code, run } = useFormAction()
const interactive = ref(false)
onMounted(() => {
  interactive.value = true
})
const email = ref('')
const resolved = ref<Account | null>(null)
const addKey = ref('')
const notice = ref('')
const pendingTransfer = ref<{
  target: Member
  revision: number
  key: string
} | null>(null)
const transferRetryAllowed = ref(false)
const controller = new AbortController()
onBeforeUnmount(() => controller.abort())
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
    notice.value = '成员已加入；重复添加不会创建重复记录。'
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
    notice.value = `${member.displayName} 的角色已更新。`
  })
}
async function remove(member: Member) {
  if (
    !window.confirm(
      `移除 ${member.displayName}（${member.email}）？移除后将立即失去组织历史、事件和下载等全部访问权限。`,
    )
  )
    return
  await run(async () => {
    await $api.empty(`/members/${member.userId}`, {
      method: 'DELETE',
      organizationId: orgId,
    })
    await reload()
    notice.value = `${member.displayName} 已移出当前组织。`
  })
}
async function transfer(member: Member) {
  if (pendingTransfer.value || !organization.value) return
  const revision = organization.value.accessRevision
  if (
    !window.confirm(
      `将组织所有权转移给 ${member.displayName}（${member.email}）？你将成为管理员，目标将获得所有者权限。`,
    )
  )
    return
  pendingTransfer.value = { target: member, revision, key: crypto.randomUUID() }
  await submitTransfer()
}
async function submitTransfer() {
  const pending = pendingTransfer.value
  if (!pending) return
  transferRetryAllowed.value = false
  notice.value = ''
  await run(async () => {
    try {
      await $api.request('/organizations/current/transfer-owner', {
        method: 'POST',
        organizationId: orgId,
        key: pending.key,
        body: {
          targetUserId: pending.target.userId,
          expectedRevision: pending.revision,
        },
      })
    } catch (error) {
      const failure = apiError(error)
      if (failure.status === 0) {
        throw new ApiError(
          '转移结果尚未确认。请先核对当前所有者，不要再次选择其他目标。',
          0,
          'TRANSFER_UNCONFIRMED',
        )
      }
      pendingTransfer.value = null
      throw error
    }
    pendingTransfer.value = null
    notice.value = `所有权已转移给 ${pending.target.displayName}，你现在是管理员。`
    await reload()
  })
}
async function checkTransfer() {
  const pending = pendingTransfer.value
  if (!pending) return
  await run(async () => {
    const current = await $api.request<
      components['schemas']['OrganizationDTO']
    >('/organizations/current', {
      organizationId: orgId,
      signal: controller.signal,
    })
    let owner: Member | undefined
    let next: string | null = null
    // Bounded read-only reconciliation. Never infer the target merely from our new role.
    for (let page = 0; page < 10; page++) {
      const members: components['schemas']['Page_MemberDTO_'] =
        await $api.request(
          `/members?limit=100${next ? `&cursor=${encodeURIComponent(next)}` : ''}`,
          { organizationId: orgId, signal: controller.signal },
        )
      owner = members.items.find((member) => member.effectiveRole === 'owner')
      if (owner || !members.nextCursor) break
      next = members.nextCursor
    }
    transferRetryAllowed.value = false
    const verified = await $api.request<
      components['schemas']['OrganizationDTO']
    >('/organizations/current', {
      organizationId: orgId,
      signal: controller.signal,
    })
    if (verified.accessRevision !== current.accessRevision) {
      notice.value = '核对期间权限发生变化，请再次核对；不会自动重试转移。'
      await reload()
      return
    }
    if (
      owner?.userId === pending.target.userId &&
      current.effectiveRole === 'admin'
    ) {
      pendingTransfer.value = null
      notice.value = `已核实：${pending.target.displayName} 是当前所有者，你现在是管理员。`
    } else if (
      owner?.userId === session.value?.user?.id &&
      current.accessRevision === pending.revision
    ) {
      transferRetryAllowed.value = true
      notice.value =
        '当前仍由你担任所有者，权限版本未变化。可重试原目标；仍使用同一提交标识。'
    } else {
      notice.value =
        '当前权限或所有者已变化，无法确认原转移结果。请核对成员列表；不会自动重试或改变目标。'
    }
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
    <p
      v-if="notice"
      :class="pendingTransfer ? 'notice' : 'success'"
      role="status"
    >
      {{ notice }}
    </p>
    <p
      v-if="code === 'VERSION_CONFLICT' || code === 'NOT_FOUND'"
      class="notice"
    >
      成员或权限已变化，输入保留。请刷新列表后重新核对目标。
    </p>
    <section v-if="pendingTransfer" class="panel" aria-label="核对所有权转移">
      <h2>转移结果待核对</h2>
      <p>
        原目标：{{ pendingTransfer.target.displayName }}（{{
          pendingTransfer.target.email
        }}）。核对只读取，不重新提交转移。
      </p>
      <button
        class="secondary"
        :disabled="busy || !interactive"
        @click="checkTransfer"
      >
        核对当前所有者
      </button>
      <button
        v-if="transferRetryAllowed && isOwner"
        class="secondary"
        :disabled="busy || !interactive"
        @click="submitTransfer"
      >
        重试原转移
      </button>
    </section>
    <section v-if="canManage" class="panel member-add">
      <h2>添加已注册账号</h2>
      <form class="inline-form" @submit.prevent="resolve">
        <label
          >成员邮箱<input
            v-model="email"
            :disabled="busy || !interactive || !!pendingTransfer"
            type="email"
            required
            autocomplete="off"
            placeholder="同事注册时使用的邮箱" /></label
        ><button
          class="secondary"
          :disabled="busy || !interactive || !!pendingTransfer"
        >
          核对账号
        </button>
      </form>
      <div v-if="resolved" class="notice">
        <p>
          确认添加：<strong>{{ resolved.displayName }}</strong
          >（{{ resolved.email }}）
        </p>
        <p class="small">加入后角色为普通成员。</p>
        <button
          class="primary"
          :disabled="busy || !interactive || !!pendingTransfer"
          @click="add"
        >
          确认添加成员
        </button>
      </div>
    </section>
    <p v-if="status === 'pending'" role="status">正在加载成员…</p>
    <div v-else-if="error" class="error" role="alert">
      成员列表加载失败。<button class="text-button" @click="run(reload)">
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
                  :disabled="busy || !interactive || !!pendingTransfer"
                  @click="changeRole(member)"
                >
                  {{
                    member.effectiveRole === 'admin' ? '设为成员' : '设为管理员'
                  }}</button
                ><button
                  v-if="isOwner"
                  class="text-button"
                  :disabled="busy || !interactive || !!pendingTransfer"
                  @click="transfer(member)"
                >
                  转移所有权</button
                ><button
                  v-if="isOwner || member.effectiveRole === 'member'"
                  class="text-button danger"
                  :disabled="busy || !interactive || !!pendingTransfer"
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
        :disabled="busy || !interactive || status === 'pending'"
        @click="run(reload)"
      >
        刷新 / 返回第一页</button
      ><button
        v-if="data?.nextCursor"
        class="secondary"
        :disabled="busy || !interactive || !!pendingTransfer"
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
