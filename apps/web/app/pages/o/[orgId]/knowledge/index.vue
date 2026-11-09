<script setup lang="ts">
import type { components } from '../../../../types/api'
definePageMeta({ layout: 'workspace' })
type KB = components['schemas']['KnowledgeDTO']
type List = components['schemas']['Page_KnowledgeDTO_']
const orgId = String(useRoute().params.orgId)
const { $api } = useNuxtApp()
const { organization } = useIdentity()
const canManage = computed(() =>
  ['owner', 'admin'].includes(organization.value?.effectiveRole || ''),
)
const q = ref('')
const filter = ref('')
const cursor = ref<string | null>(null)
const { data, status, error, refresh } = await useAsyncData(
  `knowledge:${orgId}`,
  () =>
    $api.request<List>(
      `/knowledge-bases?q=${encodeURIComponent(filter.value)}${cursor.value ? `&cursor=${encodeURIComponent(cursor.value)}` : ''}`,
      { organizationId: orgId },
    ),
  { watch: [filter, cursor] },
)
const form = ref(false)
const editing = ref<KB | null>(null)
const name = ref('')
const description = ref('')
const interactive = ref(false)
let key = ''
const { busy, message, run } = useFormAction()
onMounted(() => {
  interactive.value = true
})
function open(row?: KB) {
  editing.value = row || null
  name.value = row?.name || ''
  description.value = row?.description || ''
  key = crypto.randomUUID()
  message.value = ''
  form.value = true
}
function search() {
  cursor.value = null
  filter.value = q.value.trim()
}
async function save() {
  await run(async () => {
    await $api.request<KB>(
      editing.value
        ? `/knowledge-bases/${editing.value.id}`
        : '/knowledge-bases',
      {
        method: editing.value ? 'PATCH' : 'POST',
        organizationId: orgId,
        key,
        body: {
          name: name.value,
          description: description.value,
          ...(editing.value
            ? { expectedRevision: editing.value.revision }
            : {}),
        },
      },
    )
    form.value = false
    await refresh()
  })
}
async function remove(row: KB) {
  if (
    !window.confirm(
      `删除“${row.name}”及其中资料？资料将立即不可访问，历史来源会标记删除；被 Agent 绑定时无法删除。`,
    )
  )
    return
  await run(async () => {
    await $api.empty(`/knowledge-bases/${row.id}`, {
      method: 'DELETE',
      organizationId: orgId,
    })
    await refresh()
  })
}
</script>
<template>
  <div>
    <header class="page-intro model-heading">
      <div>
        <p class="small muted">团队资料</p>
        <h1>知识库</h1>
        <p class="muted">按主题整理资料，保留每次上传的独立记录。</p>
      </div>
      <button
        v-if="canManage"
        class="primary"
        :disabled="!interactive"
        @click="open()"
      >
        新建知识库
      </button>
    </header>
    <p v-if="message" class="error" role="alert">{{ message }}</p>
    <form
      v-if="form && canManage"
      class="panel knowledge-form"
      @submit.prevent="save"
    >
      <h2>{{ editing ? '编辑知识库' : '新建知识库' }}</h2>
      <label>知识库名称<input v-model="name" required maxlength="100" /></label>
      <label
        >说明<textarea
          v-model="description"
          maxlength="2000"
          rows="3"
        ></textarea>
      </label>
      <div class="inline">
        <button class="primary" :disabled="busy">保存知识库</button
        ><button
          type="button"
          class="text-button"
          :disabled="busy"
          @click="form = false"
        >
          取消
        </button>
      </div>
    </form>
    <form class="inline-form knowledge-filter" @submit.prevent="search">
      <label
        >搜索知识库<input
          v-model="q"
          maxlength="200"
          placeholder="按名称查找" /></label
      ><button class="secondary">搜索</button>
    </form>
    <p v-if="status === 'pending'" role="status">正在读取知识库…</p>
    <div v-else-if="error" class="error" role="alert">
      读取失败。<button class="text-button" @click="refresh()">重新读取</button>
    </div>
    <template v-else-if="data">
      <p v-if="!data.items.length" class="empty">
        {{
          filter
            ? '没有匹配的知识库，请调整搜索。'
            : '还没有知识库。管理员可以新建一个主题，开始整理资料。'
        }}
      </p>
      <ul v-else class="knowledge-list">
        <li v-for="row in data.items" :key="row.id">
          <div class="model-heading">
            <NuxtLink :to="`/o/${orgId}/knowledge/${row.id}`"
              ><h2>{{ row.name }}</h2></NuxtLink
            >
            <div v-if="canManage" class="row-actions">
              <button class="text-button" @click="open(row)">编辑</button
              ><button
                class="text-button danger"
                :disabled="busy"
                @click="remove(row)"
              >
                删除
              </button>
            </div>
          </div>
          <p class="muted">{{ row.description || '暂无说明' }}</p>
          <p class="small">
            {{ row.documentCount }} 份文档 · {{ row.readyCount }} 已就绪 ·
            {{ row.processingCount }} 待处理 / 处理中 ·
            {{ row.failedCount }} 失败
          </p>
          <span class="small muted"
            >更新时间 {{ row.updatedAt.slice(0, 10) }}</span
          >
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
  </div>
</template>
