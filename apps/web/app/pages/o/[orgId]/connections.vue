<script setup lang="ts">
import type { components } from '../../../types/api'
definePageMeta({ layout: 'workspace', key: (route) => route.path })
type Connection =
  | components['schemas']['GitHubConnectionView']
  | components['schemas']['GitHubSummary']
type Repo = components['schemas']['RepositoryView']
type Tool = components['schemas']['ToolView']
type TestResult = components['schemas']['GitHubTestResult']
const orgId = String(useRoute().params.orgId)
const { $api } = useNuxtApp()
const { organization, session } = useIdentity()
const admin = computed(() =>
  ['owner', 'admin'].includes(organization.value?.effectiveRole || ''),
)
const controller = new AbortController()
onBeforeUnmount(() => controller.abort())
const opts = {
  organizationId: orgId,
  signal: controller.signal,
  timeoutMs: 40000,
}
const { data, status, error, refresh } = await useAsyncData(
  `github:${orgId}`,
  async () => ({
    connection: await $api.request<Connection>('/connections/github', opts),
    repositories: await $api.request<Repo[]>(
      '/connections/github/repositories',
      opts,
    ),
    tools: await $api.request<Tool[]>('/tools', opts),
  }),
)
const config = computed(() =>
  data.value && 'revision' in data.value.connection
    ? data.value.connection
    : null,
)
const { busy, message, run } = useFormAction()
const credential = ref('')
const name = ref('GitHub')
const enabled = ref(true)
const owner = ref('')
const repo = ref('')
const share = ref(false)
const notice = ref('')
const interactive = ref(false)
onMounted(() => {
  interactive.value = true
})
watch(
  () => config.value?.revision,
  () => {
    if (config.value) {
      name.value = config.value.name
      enabled.value = config.value.enabled
    }
  },
  { immediate: true },
)
const dirty = computed(() =>
  Boolean(
    credential.value ||
    owner.value ||
    repo.value ||
    name.value !== (config.value?.name || 'GitHub') ||
    enabled.value !== (config.value?.enabled ?? true),
  ),
)
onBeforeRouteLeave(() => {
  if (
    session.value?.user &&
    organization.value?.id === orgId &&
    admin.value &&
    dirty.value &&
    !window.confirm('离开后将丢弃未保存的连接和仓库输入，继续吗？')
  )
    return false
})
async function test() {
  notice.value = ''
  await run(async () => {
    const result = await $api.request<TestResult>('/connections/github/test', {
      ...opts,
      method: 'POST',
      body: credential.value ? { credential: credential.value } : {},
    })
    notice.value = `${result.ok ? '测试通过' : '测试未通过'}：${result.message}（${result.code}）`
    await refresh()
  })
}
async function save() {
  if (
    !enabled.value &&
    config.value?.enabled &&
    !window.confirm('停用会阻止GitHub查询并使相关结果不可用，继续吗？')
  )
    return
  notice.value = ''
  await run(async () => {
    await $api.request<Connection>('/connections/github', {
      ...opts,
      method: 'PUT',
      body: {
        name: name.value,
        enabled: enabled.value,
        expectedRevision: config.value?.revision || 0,
        ...(credential.value ? { credential: credential.value } : {}),
      },
    })
    credential.value = ''
    notice.value = '连接已保存。凭据更换后请重新验证并共享原允许仓库。'
    await refresh()
  })
}
let pending: { key: string; owner: string; repo: string } | null = null
watch(
  () => [
    organization.value?.id,
    organization.value?.accessRevision,
    organization.value?.effectiveRole,
  ],
  () => {
    credential.value = ''
    owner.value = ''
    repo.value = ''
    share.value = false
    pending = null
    notice.value = ''
    name.value = config.value?.name || 'GitHub'
    enabled.value = config.value?.enabled ?? true
  },
  { flush: 'sync' },
)
async function add() {
  notice.value = ''
  await run(async () => {
    if (!share.value) return
    if (
      !pending ||
      pending.owner !== owner.value ||
      pending.repo !== repo.value
    )
      pending = {
        key: crypto.randomUUID(),
        owner: owner.value,
        repo: repo.value,
      }
    await $api.request<Repo>('/connections/github/repositories', {
      ...opts,
      method: 'POST',
      key: pending.key,
      body: {
        owner: pending.owner,
        repo: pending.repo,
        shareWithOrganization: true,
      },
    })
    pending = null
    owner.value = ''
    repo.value = ''
    share.value = false
    notice.value = '精确仓库验证通过，已共享给本组织。'
    await refresh()
  })
}
async function remove(item: Repo) {
  if (
    !window.confirm(
      `移除 ${item.owner}/${item.repo} 会撤销本组织查询范围，并影响相关运行及历史内容访问。继续吗？`,
    )
  )
    return
  await run(async () => {
    await $api.empty(`/connections/github/repositories/${item.id}`, {
      ...opts,
      method: 'DELETE',
    })
    await refresh()
  })
}
async function toggle(tool: Tool) {
  if (
    tool.enabled &&
    !window.confirm(
      `停用“${tool.name}”会阻止后续调用，相关Agent可能不再就绪。继续吗？`,
    )
  )
    return
  await run(async () => {
    await $api.request<Tool>(`/tools/${tool.key}`, {
      ...opts,
      method: 'PATCH',
      body: { enabled: !tool.enabled, expectedRevision: tool.revision },
    })
    await refresh()
  })
}
</script>
<template>
  <div>
    <div class="page-intro">
      <h1>连接与工具</h1>
      <p class="muted">
        只读 GitHub
        MCP。先测试连接，再明确共享仓库；不会自动共享凭据可访问的全部仓库。
      </p>
    </div>
    <p v-if="status === 'pending' && !data" role="status">正在读取连接…</p>
    <div v-else-if="error" class="error" role="alert">
      无法读取连接。<button @click="refresh()">重新读取</button>
    </div>
    <template v-else-if="data">
      <section class="panel">
        <h2>GitHub 连接</h2>
        <p role="status">
          {{
            data.connection.available
              ? '连接已测试并启用'
              : '连接未就绪或已停用'
          }}
        </p>
        <form v-if="admin && config" @submit.prevent="save">
          <label for="github-name">连接名称</label
          ><input
            id="github-name"
            v-model="name"
            required
            maxlength="100"
            :disabled="busy || !interactive"
          />
          <label for="github-token">专用只读凭据</label
          ><input
            id="github-token"
            v-model="credential"
            type="password"
            autocomplete="off"
            :disabled="busy || !interactive"
            :placeholder="
              config.hasCredential ? '已配置；留空保留原凭据' : '首次保存必填'
            "
          />
          <p class="small muted">
            只发送至固定官方 GitHub MCP
            服务。保存后不回显；更换凭据将停用已共享仓库，须重新验证。
          </p>
          <label
            ><input
              v-model="enabled"
              type="checkbox"
              :disabled="busy || !interactive"
            />启用连接</label
          >
          <p v-if="config.lastTestedAt" class="small">
            上次测试：{{ config.lastTestedAt }} ·
            {{ config.testResult?.message }}
          </p>
          <div class="inline">
            <button
              type="button"
              :disabled="busy || !interactive"
              @click="test"
            >
              测试连接</button
            ><button
              class="primary"
              type="submit"
              :disabled="busy || !interactive"
            >
              测试并保存连接
            </button>
          </div>
        </form>
        <p v-else class="muted">只有所有者和管理员可管理连接凭据。</p>
      </section>
      <section class="panel">
        <h2>允许仓库</h2>
        <p v-if="!data.repositories.length" class="muted">
          尚无可用仓库。搜索无结果不代表仓库验证成功。
        </p>
        <ul>
          <li v-for="item in data.repositories" :key="item.id">
            <strong>{{ item.owner }}/{{ item.repo }}</strong> ·
            {{ item.enabled ? '已共享' : '已撤销，重新添加可验证恢复' }}
            <span class="small">验证时间：{{ item.verifiedAt }}</span
            ><button
              v-if="admin && item.enabled"
              :disabled="busy || !interactive"
              @click="remove(item)"
            >
              移除 {{ item.owner }}/{{ item.repo }}
            </button>
          </li>
        </ul>
        <form v-if="admin" @submit.prevent="add">
          <div class="form-grid">
            <div>
              <label for="github-owner">Owner</label
              ><input
                id="github-owner"
                v-model="owner"
                pattern="[A-Za-z0-9][A-Za-z0-9_.-]{0,99}"
                required
                :disabled="busy || !interactive"
              />
            </div>
            <div>
              <label for="github-repo">仓库名</label
              ><input
                id="github-repo"
                v-model="repo"
                pattern="[A-Za-z0-9][A-Za-z0-9_.-]{0,99}"
                required
                :disabled="busy || !interactive"
              />
            </div>
          </div>
          <label
            ><input
              v-model="share"
              type="checkbox"
              required
              :disabled="busy || !interactive"
            />向本组织共享 {{ owner || 'owner' }}/{{
              repo || 'repo'
            }}
            的只读资料</label
          >
          <button
            class="primary"
            type="submit"
            :disabled="
              busy || !interactive || !share || !data.connection.available
            "
          >
            验证并添加仓库
          </button>
        </form>
      </section>
      <section class="panel">
        <h2>固定只读工具</h2>
        <ul>
          <li v-for="tool in data.tools" :key="tool.key">
            <strong>{{ tool.name }}</strong> · 只读 ·
            {{ tool.enabled ? '启用' : '停用' }}
            <p class="small muted">{{ tool.inputSummary }}</p>
            <button
              v-if="admin && tool.key !== 'knowledge.search'"
              :disabled="busy || !interactive"
              @click="toggle(tool)"
            >
              {{ tool.enabled ? '停用' : '启用' }} {{ tool.name }}
            </button>
          </li>
        </ul>
      </section>
    </template>
    <p v-if="message" class="error" role="alert" tabindex="-1">
      {{ message
      }}<button :disabled="busy" @click="refresh()">重新读取状态</button>
    </p>
    <p v-if="notice" role="status">{{ notice }}</p>
  </div>
</template>

<style scoped>
.panel + .panel {
  margin-top: 1.25rem;
}
input[type='checkbox'] {
  width: auto;
  margin-right: 0.5rem;
}
li + li {
  margin-top: 1rem;
}
</style>
