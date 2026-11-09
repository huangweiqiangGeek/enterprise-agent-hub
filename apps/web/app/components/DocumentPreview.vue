<script setup lang="ts">
import type { components } from '../types/api'
import { apiError } from '../utils/api-error'

const props = defineProps<{
  organizationId: string
  document: components['schemas']['DocumentDTO']
  canManage: boolean
}>()
const { $api } = useNuxtApp()
const text = ref<components['schemas']['TextPage'] | null>(null)
const chunks = ref<components['schemas']['ChunkPreview'] | null>(null)
const busy = ref(false),
  message = ref('')
let controller: AbortController | undefined
let generation = 0
watch(
  () => [props.document.id, props.document.currentAttemptId],
  () => {
    generation++
    controller?.abort()
    text.value = null
    chunks.value = null
    message.value = ''
    busy.value = false
  },
)
onBeforeUnmount(() => {
  generation++
  controller?.abort()
})
async function load(kind: 'text' | 'chunks', cursor?: string | null) {
  const current = ++generation
  controller?.abort()
  controller = new AbortController()
  busy.value = true
  message.value = ''
  const path = kind === 'text' ? 'content?view=text&' : 'chunks?'
  try {
    const value = await $api.request<
      components['schemas']['TextPage'] | components['schemas']['ChunkPreview']
    >(
      `/documents/${props.document.id}/${path}attemptId=${props.document.currentAttemptId}${cursor ? `&cursor=${cursor}` : ''}`,
      { organizationId: props.organizationId, signal: controller.signal },
    )
    if (current !== generation) return
    if ('text' in value) text.value = value
    else chunks.value = value
  } catch (error) {
    if (current === generation) message.value = apiError(error).message
  } finally {
    if (current === generation) busy.value = false
  }
}
function location(locator: Record<string, unknown>) {
  if (locator.kind === 'pdf')
    return `第 ${locator.pageStart} 页 · 段落 ${locator.paragraphStart}`
  const heading = Array.isArray(locator.headingPath)
    ? locator.headingPath.join(' / ')
    : ''
  return `${heading ? `${heading} · ` : ''}段落 ${locator.paragraphStart}`
}
</script>

<template>
  <div>
    <p class="small muted">已生成 {{ document.chunkCount }} 个片段。</p>
    <div class="row-actions">
      <button class="secondary" :disabled="busy" @click="load('text')">
        查看提取文本
      </button>
      <button
        v-if="canManage"
        class="secondary"
        :disabled="busy"
        @click="load('chunks')"
      >
        查看切片
      </button>
    </div>
    <p v-if="busy" role="status">正在读取预览…</p>
    <p v-if="message" class="error" role="alert">{{ message }}</p>
    <section v-if="text" aria-label="提取文本">
      <h3>提取文本</h3>
      <pre class="document-preview">{{ text.text }}</pre>
      <button
        v-if="text.nextCursor"
        class="text-button"
        :disabled="busy"
        @click="load('text', text.nextCursor)"
      >
        下一段文本
      </button>
    </section>
    <section v-if="chunks && canManage" aria-label="文档切片">
      <h3>文档切片</h3>
      <article v-for="chunk in chunks.items" :key="chunk.chunkId">
        <p class="small muted">
          片段 {{ chunk.ordinal + 1 }} · {{ location(chunk.locator) }}
        </p>
        <pre class="document-preview">{{ chunk.text }}</pre>
      </article>
      <button
        v-if="chunks.nextCursor"
        class="text-button"
        :disabled="busy"
        @click="load('chunks', chunks.nextCursor)"
      >
        下一页切片
      </button>
    </section>
  </div>
</template>

<style scoped>
.document-preview {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font: inherit;
  max-height: 30rem;
  overflow: auto;
  padding: 1rem;
  background: var(--surface-muted, #f4f6f7);
  border-radius: 0.5rem;
}
</style>
