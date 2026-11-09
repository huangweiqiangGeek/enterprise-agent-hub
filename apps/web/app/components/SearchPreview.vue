<script setup lang="ts">
import type { components } from '../types/api'
import { apiError } from '../utils/api-error'

const props = defineProps<{
  organizationId: string
  knowledgeBaseId: string
  documents: components['schemas']['DocumentDTO'][]
  revision: number
}>()
const emit = defineEmits<{
  inspect: [item: components['schemas']['RetrievalItem']]
}>()
const { $api } = useNuxtApp()
const query = ref(''),
  selected = ref(''),
  busy = ref(false),
  message = ref('')
const result = ref<components['schemas']['RetrievalResult'] | null>(null)
const ready = computed(() =>
  props.documents.filter((d) => d.status === 'ready'),
)
let generation = 0
let controller: AbortController | undefined

/** Invalidate outstanding results when their organization, document scope or source changes. */
function reset() {
  generation++
  controller?.abort()
  busy.value = false
  result.value = null
  message.value = ''
}
watch(
  () => [props.organizationId, props.knowledgeBaseId, props.revision],
  reset,
)
watch(selected, reset)
onBeforeUnmount(reset)

async function search() {
  reset()
  const current = generation
  controller = new AbortController()
  busy.value = true
  try {
    const response = await $api.request<
      components['schemas']['RetrievalResult']
    >(`/knowledge-bases/${props.knowledgeBaseId}/search-preview`, {
      method: 'POST',
      organizationId: props.organizationId,
      body: {
        query: query.value,
        documentIds: selected.value ? [selected.value] : [],
      },
      signal: controller.signal,
      timeoutMs: 20000,
    })
    if (current === generation) result.value = response
  } catch (error) {
    if (current === generation) message.value = apiError(error).message
  } finally {
    if (current === generation) busy.value = false
  }
}
function location(locator: Record<string, unknown>) {
  const heading = Array.isArray(locator.headingPath)
    ? locator.headingPath.join(' / ')
    : ''
  return `${locator.kind === 'pdf' ? `第 ${locator.pageStart} 页 · ` : heading ? `${heading} · ` : ''}段落 ${locator.paragraphStart}`
}
</script>

<template>
  <details class="panel search-preview">
    <summary>检索试查 · 管理员</summary>
    <p class="small muted">
      核对当前知识库实际能检索到的内容。使用 Embedding 和
      Rerank，会产生模型用量，不创建会话。
    </p>
    <form class="knowledge-form" @submit.prevent="search">
      <label
        >试查问题<textarea
          v-model="query"
          required
          maxlength="4000"
          rows="3"
          placeholder="例如：发布失败后如何回滚？"
        ></textarea>
      </label>
      <label
        >试查范围<select v-model="selected" :disabled="busy">
          <option value="">当前知识库的全部就绪文档</option>
          <optgroup label="当前页的就绪文档">
            <option v-for="doc in ready" :key="doc.id" :value="doc.id">
              {{ doc.fileName }}
            </option>
          </optgroup>
        </select></label
      >
      <div class="row-actions">
        <button class="primary" :disabled="busy || !query.trim()">
          {{ busy ? '正在检索…' : '开始试查' }}
        </button>
        <button v-if="busy" type="button" class="secondary" @click="reset">
          取消等待
        </button>
      </div>
    </form>
    <p v-if="busy" role="status">正在查询就绪文档并重排片段…</p>
    <p v-if="message" class="error" role="alert">{{ message }}</p>
    <section v-if="result" aria-label="检索结果">
      <h3>试查结果</h3>
      <p class="small">查询：{{ result.query }}</p>
      <p class="notice">{{ result.coverage.note }}</p>
      <p>
        检索了 {{ result.coverage.eligibleDocumentCount }} 份文档、{{
          result.coverage.eligibleChunkCount
        }}
        个有效片段，展示 {{ result.items.length }} 个片段。
      </p>
      <p v-if="!result.items.length" class="empty">
        本次没有检索到片段，请确认文档已就绪或调整问题与范围。
      </p>
      <p v-if="result.coverage.partial" class="small muted">
        覆盖限制：{{ result.coverage.omittedChunkCount }} 个片段未展示，{{
          result.coverage.omittedDocumentCount
        }}
        份文档没有展示片段；重排摘录裁剪
        {{ result.coverage.excerptTruncatedCount }} 个，证据正文裁剪
        {{ result.coverage.contextTruncatedCount }} 个。
        {{
          result.coverage.queryTermsTruncated
            ? '关键词过多，仅使用前64个词元召回。'
            : ''
        }}
      </p>
      <article
        v-for="(item, index) in result.items"
        :key="item.chunkId"
        class="search-hit"
      >
        <h4>{{ index + 1 }}. {{ item.fileName }}</h4>
        <p class="small muted">
          {{ location(item.locator) }} · 排序分数 {{ item.score.toFixed(3) }}
        </p>
        <pre>{{ item.text }}</pre>
        <button class="text-button" @click="emit('inspect', item)">
          查看文档记录
        </button>
      </article>
      <details v-if="result.usage.length" class="small">
        <summary>本次模型用量</summary>
        <p v-for="usage in result.usage" :key="usage.attempt">
          {{ usage.attempt === 1 ? '查询向量' : '片段重排' }}：
          {{
            usage.tokenSource === 'actual'
              ? `输入 ${usage.inputTokens} token`
              : usage.tokenSource === 'reserved'
                ? `保守预留 ${usage.reservedTokens}，非实际 token`
                : '未提供输入 token'
          }}
          <span v-for="(count, unit) in usage.billedUnits" :key="unit">
            · {{ unit }}：{{ count }}</span
          >
        </p>
      </details>
    </section>
  </details>
</template>

<style scoped>
.search-preview {
  margin-block: 1.25rem;
}
summary {
  cursor: pointer;
  font-weight: 600;
}
form {
  display: grid;
  gap: 1rem;
  margin-block: 1rem;
}
.search-hit {
  border-top: 1px solid var(--border, #dde2e4);
  padding-block: 1rem;
}
.search-hit h4 {
  margin: 0;
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font: inherit;
}
</style>
