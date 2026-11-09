<script setup lang="ts">
import type { components } from '../../../../types/api'
import { ApiError, apiError } from '../../../../utils/api-error'
definePageMeta({ layout: 'workspace' })
type Doc = components['schemas']['DocumentDTO']
type KB = components['schemas']['KnowledgeDTO']
type List = components['schemas']['Page_DocumentDTO_']
type Policy = components['schemas']['UploadPolicy']
const route = useRoute()
const orgId = String(route.params.orgId),
  kbId = String(route.params.kbId)
const { $api } = useNuxtApp()
const { organization } = useIdentity()
const canManage = computed(() =>
  ['owner', 'admin'].includes(organization.value?.effectiveRole || ''),
)
const {
  data: kb,
  error: baseError,
  refresh: refreshBase,
} = await useAsyncData(`kb:${orgId}:${kbId}`, () =>
  $api.request<KB>(`/knowledge-bases/${kbId}`, { organizationId: orgId }),
)
const { data: policy, refresh: refreshPolicy } = await useAsyncData(
  `upload-policy:${orgId}:${kbId}`,
  () =>
    $api.request<Policy>(`/knowledge-bases/${kbId}/upload-policy`, {
      organizationId: orgId,
    }),
)
const name = ref(''),
  q = ref(''),
  state = ref(''),
  filterState = ref('')
const cursor = ref<string | null>(null)
const { data, status, error, refresh } = await useAsyncData(
  `documents:${orgId}:${kbId}`,
  () =>
    $api.request<List>(
      `/knowledge-bases/${kbId}/documents?q=${encodeURIComponent(q.value)}${filterState.value ? `&status=${filterState.value}` : ''}${cursor.value ? `&cursor=${encodeURIComponent(cursor.value)}` : ''}`,
      { organizationId: orgId },
    ),
  { watch: [q, filterState, cursor] },
)
const { busy, message, run } = useFormAction()
const labels: Record<string, string> = {
  queued: '待处理',
  processing: '处理中',
  ready: '已就绪',
  failed: '处理失败',
}
const detail = ref<components['schemas']['DocumentDetail'] | null>(null)
const searchRevision = ref(0)
const retryKeys = new Map<string, string>()
let alive = true,
  polling = false
let timer: ReturnType<typeof setInterval> | undefined
async function reload() {
  await Promise.all([refresh(), refreshBase(), refreshPolicy()])
}
function search() {
  cursor.value = null
  q.value = name.value.trim()
  filterState.value = state.value
}
async function poll() {
  if (
    document.hidden ||
    polling ||
    !data.value?.items.some((item) =>
      ['queued', 'processing'].includes(item.status),
    )
  )
    return
  polling = true
  try {
    await refresh()
    const selected = detail.value
    if (selected && alive) {
      const updated = await $api.request<
        components['schemas']['DocumentDetail']
      >(`/documents/${selected.document.id}`, { organizationId: orgId })
      if (alive && detail.value?.document.id === selected.document.id)
        detail.value = updated
    }
  } catch {
    // The next visible polling cycle retries; explicit actions show their own error.
  } finally {
    polling = false
  }
}
onMounted(() => {
  timer = setInterval(poll, 5000)
  window.addEventListener('focus', poll)
})
onUnmounted(() => {
  alive = false
  clearInterval(timer)
  window.removeEventListener('focus', poll)
})
async function inspect(row: Doc) {
  await run(async () => {
    const value = await $api.request<components['schemas']['DocumentDetail']>(
      `/documents/${row.id}`,
      {
        organizationId: orgId,
      },
    )
    if (alive) detail.value = value
  })
}
async function retry(row: Doc) {
  const key = retryKeys.get(row.currentAttemptId) || crypto.randomUUID()
  retryKeys.set(row.currentAttemptId, key)
  await run(async () => {
    await $api.request(`/documents/${row.id}/retry`, {
      method: 'POST',
      organizationId: orgId,
      key,
      body: { expectedAttemptId: row.currentAttemptId },
    })
    retryKeys.delete(row.currentAttemptId)
    await reload()
    if (detail.value?.document.id === row.id) detail.value = null
  })
}
async function inspectSource(item: components['schemas']['RetrievalItem']) {
  await run(async () => {
    const value = await $api.request<components['schemas']['DocumentDetail']>(
      `/documents/${item.documentId}`,
      { organizationId: orgId },
    )
    if (alive && value.document.currentAttemptId === item.attemptId)
      detail.value = value
    else if (alive) {
      searchRevision.value++
      throw new ApiError(
        '文档版本已变化，请重新试查。',
        409,
        'DOCUMENT_STATE_CONFLICT',
      )
    }
  })
}
async function remove(row: Doc) {
  if (
    !window.confirm(
      `删除“${row.fileName}”？资料将立即不可访问，历史来源标记删除，后台文件将稍后清理。`,
    )
  )
    return
  await run(async () => {
    await $api.empty(`/documents/${row.id}`, {
      method: 'DELETE',
      organizationId: orgId,
    })
    detail.value = null
    searchRevision.value++
    await reload()
  })
}
async function download(row: Doc) {
  await run(async () => {
    const response = await fetch(
      `/api/v1/documents/${row.id}/content?view=original&attemptId=${row.currentAttemptId}`,
      { headers: { 'X-Organization-Id': orgId } },
    )
    if (!response.ok)
      throw apiError({
        statusCode: response.status,
        data: await response.json(),
      })
    const blob = await response.blob()
    if (!alive) return
    const url = URL.createObjectURL(blob),
      link = document.createElement('a')
    link.href = url
    link.download = row.fileName
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  })
}
</script>
<template>
  <div>
    <NuxtLink :to="`/o/${orgId}/knowledge`">← 所有知识库</NuxtLink>
    <div v-if="baseError" class="error" role="alert">
      {{ apiError(baseError).message }}
      <button class="text-button" @click="reload()">重新读取</button>
    </div>
    <template v-else-if="kb">
      <header class="page-intro knowledge-title">
        <p class="small muted">团队资料 / 知识库</p>
        <h1>{{ kb.name }}</h1>
        <p class="muted">
          {{ kb.description || '在这里添加并核对团队资料。' }}
        </p>
      </header>
      <DocumentUpload
        v-if="canManage && policy"
        :organization-id="orgId"
        :knowledge-base-id="kbId"
        :policy="policy"
        @accepted="reload()"
      />
      <p v-else-if="!canManage" class="notice">
        你可以查看与下载资料；添加、删除由管理员操作。
      </p>
      <p v-if="message" class="error" role="alert">{{ message }}</p>
      <SearchPreview
        v-if="canManage"
        :organization-id="orgId"
        :knowledge-base-id="kbId"
        :documents="data?.items || []"
        :revision="searchRevision"
        @inspect="inspectSource"
      />
      <form class="inline-form knowledge-filter" @submit.prevent="search">
        <label
          >文件名<input
            v-model="name"
            maxlength="200"
            placeholder="搜索文件名" /></label
        ><label
          >处理状态<select v-model="state">
            <option value="">全部状态</option>
            <option value="queued">待处理</option>
            <option value="processing">处理中</option>
            <option value="ready">已就绪</option>
            <option value="failed">处理失败</option>
          </select></label
        ><button class="secondary">筛选</button>
      </form>
      <p v-if="status === 'pending' && !data" role="status">正在读取文档…</p>
      <div v-else-if="error" class="error" role="alert">
        读取文档失败。<button class="text-button" @click="refresh()">
          重新读取
        </button>
      </div>
      <template v-else-if="data">
        <p v-if="!data.items.length" class="empty">
          {{
            q || filterState
              ? '没有符合筛选条件的文档。'
              : '还没有文档。添加文件后，它们会显示在这里。'
          }}
        </p>
        <ul v-else class="document-list">
          <li v-for="row in data.items" :key="row.id">
            <div class="model-heading">
              <strong>{{ row.fileName }}</strong
              ><span class="badge">{{ labels[row.status] || row.status }}</span>
            </div>
            <p class="small muted">
              {{ row.sizeBytes }} 字节 ·
              {{
                new Date(row.createdAt).toLocaleString('zh-CN', {
                  timeZone: 'Asia/Shanghai',
                })
              }}
              · 第 {{ row.attemptNumber }} 次处理
            </p>
            <p v-if="row.status !== 'ready'" class="small muted">
              尚不可检索{{ row.errorMessage ? `：${row.errorMessage}` : '' }}
            </p>
            <div class="row-actions">
              <button
                v-if="canManage && row.status === 'failed'"
                class="secondary"
                :disabled="busy"
                @click="retry(row)"
              >
                重新处理
              </button>
              <button
                class="text-button"
                :disabled="busy"
                @click="inspect(row)"
              >
                查看记录</button
              ><button
                class="text-button"
                :disabled="busy"
                @click="download(row)"
              >
                下载原文件</button
              ><button
                v-if="canManage"
                class="text-button danger"
                :disabled="busy"
                @click="remove(row)"
              >
                删除文档
              </button>
            </div>
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
      <section v-if="detail" class="panel knowledge-detail">
        <div class="model-heading">
          <h2>{{ detail.document.fileName }}</h2>
          <button class="text-button" @click="detail = null">关闭记录</button>
        </div>
        <p>当前状态：{{ labels[detail.document.status] }}</p>
        <p v-if="detail.document.errorMessage" class="error">
          {{ detail.document.errorMessage }}
        </p>
        <DocumentPreview
          v-if="detail.document.status === 'ready'"
          :document="detail.document"
          :organization-id="orgId"
          :can-manage="canManage"
        />
        <p v-else class="small muted">
          处理完成后可预览提取文本；失败时管理员可以重新处理。
        </p>
        <ul>
          <li v-for="attempt in detail.attempts" :key="attempt.id">
            第 {{ attempt.attemptNumber }} 次 ·
            {{ labels[attempt.status] || attempt.status }}
            <span v-if="attempt.errorCode"> · {{ attempt.errorCode }}</span>
          </li>
        </ul>
      </section>
    </template>
  </div>
</template>
