<script setup lang="ts">
import type { components } from '../types/api'
import { apiError } from '../utils/api-error'

type Policy = components['schemas']['UploadPolicy']
type Acceptance = components['schemas']['AcceptanceDTO']
type Item = {
  key: string
  file: File
  progress: number
  state:
    'waiting' | 'sending' | 'checking' | 'accepted' | 'rejected' | 'unknown'
  note: string
  controller?: AbortController
  retryable: boolean
}
const props = defineProps<{
  organizationId: string
  knowledgeBaseId: string
  policy: Policy
}>()
const emit = defineEmits<{ accepted: [] }>()
const { $api } = useNuxtApp()
const items = ref<Item[]>([])
const interactive = ref(false)
let alive = true
const active = computed(() =>
  items.value.some((item) =>
    ['waiting', 'sending', 'checking'].includes(item.state),
  ),
)
const uncertain = computed(() =>
  items.value.some((item) => item.state === 'unknown'),
)
onMounted(() => {
  interactive.value = true
})
onBeforeRouteLeave(() => {
  if (
    (active.value || uncertain.value) &&
    !window.confirm(
      '仍有上传或待核对结果。离开将中断传输，已接受的文件仍会保留，继续吗？',
    )
  )
    return false
})
onUnmounted(() => {
  alive = false
  for (const item of items.value) item.controller?.abort()
})

/** Preserve the submission identity across retries and unknown transport outcomes. */
async function reconcile(item: Item) {
  item.state = 'checking'
  item.note = '正在核对上传结果…'
  try {
    const result = await $api.request<Acceptance>(
      `/idempotency-requests/${item.key}?operation=document.upload&resourceId=${props.knowledgeBaseId}`,
      { organizationId: props.organizationId },
    )
    if (!alive) return
    if (result.status === 'accepted') {
      item.state = 'accepted'
      item.note = result.document?.deletedAt
        ? '文件已接受，后已撤回。'
        : '已接受 · 待处理；尚不可检索。'
      item.retryable = false
      emit('accepted')
    } else {
      item.state = 'unknown'
      item.note =
        '暂未查到接受记录；原请求仍可能完成。再次核对或使用原标识重试。'
      item.retryable = true
    }
  } catch (error) {
    if (!alive) return
    item.state = 'unknown'
    item.note = apiError(error).message
  }
}

async function transmit(item: Item) {
  if (!alive) return
  item.state = 'sending'
  item.note = '正在传输…'
  item.progress = 0
  item.controller = new AbortController()
  try {
    await $api.upload(
      `/knowledge-bases/${props.knowledgeBaseId}/documents`,
      item.file,
      {
        organizationId: props.organizationId,
        key: item.key,
        signal: item.controller.signal,
        progress(value) {
          if (alive) {
            item.progress = value
            if (value === 100) item.note = '传输完成，等待服务端接受…'
          }
        },
      },
    )
    if (alive) await reconcile(item)
  } catch (error) {
    if (!alive) return
    const failure = apiError(error)
    if (!failure.status || failure.status >= 500 || failure.status === 408)
      await reconcile(item)
    else {
      item.state = 'rejected'
      item.note = failure.message
      item.retryable = failure.status === 429
    }
  }
}

async function add(files: FileList | null) {
  if (!files || active.value || !props.policy.embeddingReady) return
  for (const file of Array.from(files)) {
    const item: Item = {
      key: crypto.randomUUID(),
      file,
      progress: 0,
      state: 'waiting',
      note: '等待传输',
      retryable: false,
    }
    const extension = file.name.split('.').pop()?.toLowerCase() || ''
    const allowed: Record<string, string[]> = {
      pdf: ['application/pdf'],
      txt: ['text/plain'],
      md: ['text/markdown', 'text/plain', 'text/x-markdown'],
    }
    if (
      !(props.policy.extensions || ['pdf', 'txt', 'md']).includes(extension) ||
      (file.type &&
        file.type !== 'application/octet-stream' &&
        !allowed[extension]?.includes(file.type))
    ) {
      item.state = 'rejected'
      item.note = '仅支持 PDF、TXT 和 MD，文件类型必须匹配。'
    } else if (!file.size) {
      item.state = 'rejected'
      item.note = '不能上传空文件。'
    } else if (file.size > props.policy.maxBytes) {
      item.state = 'rejected'
      item.note = '超过当前组织的单文件大小限制。'
    }
    items.value.push(item)
  }
  // Sequential transfer bounds browser/API pressure, with independent outcomes for every file.
  for (const item of items.value)
    if (item.state === 'waiting' && alive) await transmit(item)
}
function choose(event: Event) {
  if (event.target instanceof HTMLInputElement) {
    void add(event.target.files)
    event.target.value = ''
  }
}
</script>

<template>
  <section
    class="panel upload-panel"
    aria-labelledby="upload-title"
    @dragover.prevent
    @drop.prevent="add($event.dataTransfer?.files || null)"
  >
    <h2 id="upload-title">添加资料</h2>
    <p class="muted small">
      PDF、UTF-8 TXT、MD · 单文件上限
      {{ (policy.maxBytes / 1048576).toFixed(2) }} MiB。可以拖入多个文件。
    </p>
    <p v-if="!policy.embeddingReady" class="notice">
      先在<NuxtLink :to="`/o/${organizationId}/settings/models`"
        >模型配置</NuxtLink
      >中测试并保存 Embedding，再添加资料。
    </p>
    <label
      >选择文件<input
        type="file"
        multiple
        accept=".pdf,.txt,.md"
        :disabled="!interactive || active || !policy.embeddingReady"
        @change="choose"
    /></label>
    <p class="small muted">
      上传完成表示已接受，后台将自动解析。请在下方列表查看处理结果；已就绪文档可预览文本与切片。
    </p>
    <ul class="upload-list" aria-live="polite">
      <li v-for="item in items" :key="item.key">
        <div class="model-heading">
          <strong>{{ item.file.name }}</strong
          ><span class="small muted">{{ item.file.size }} 字节</span>
        </div>
        <progress
          v-if="item.state === 'sending'"
          :value="item.progress"
          max="100"
          :aria-label="`${item.file.name} 上传进度`"
        ></progress>
        <p :class="['small', item.state === 'rejected' ? 'danger' : 'muted']">
          {{ item.note }}
        </p>
        <button
          v-if="item.state === 'sending'"
          class="text-button"
          @click="item.controller?.abort()"
        >
          取消传输
        </button>
        <button
          v-if="item.state === 'unknown'"
          class="text-button"
          :disabled="active"
          @click="reconcile(item)"
        >
          再次核对
        </button>
        <button
          v-if="item.retryable && ['unknown', 'rejected'].includes(item.state)"
          class="text-button"
          :disabled="active"
          @click="transmit(item)"
        >
          用原标识重试
        </button>
      </li>
    </ul>
  </section>
</template>
