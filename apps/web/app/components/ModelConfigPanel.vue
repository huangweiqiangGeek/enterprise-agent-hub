<script setup lang="ts">
import type { components } from '../types/api'

type Config = components['schemas']['ModelConfig']
type Option = components['schemas']['ModelOption']
type Result = components['schemas']['TestResult']
const props = defineProps<{
  options: Option[]
  kind: Option['kind']
  config?: Config
  organizationId: string
  refreshConfig: () => Promise<void>
}>()
const { $api } = useNuxtApp()
const { session } = useIdentity()
const { busy, message, code, run } = useFormAction()
const interactive = ref(false)
onMounted(() => {
  interactive.value = true
})
const titles = {
  chat: '聊天模型',
  embedding: 'Embedding 模型',
  rerank: 'Rerank 服务',
}
const baseUrl = ref('')
const modelName = ref('')
const dimension = ref<number | null>(null)
// A write-only credential stays in this component and never enters SSR/shared state.
const credential = ref('')
const result = ref<Result | null>(null)
const notice = ref('')
const initial = ref('')
const revision = ref(props.config?.revision ?? 0)
const stale = computed(() => (props.config?.revision ?? 0) !== revision.value)
const protocol = ref<Config['providerProtocol']>('openai-chat')
const option = computed(() =>
  props.options.find((item) => item.providerProtocol === protocol.value),
)
const fingerprint = () =>
  JSON.stringify([
    protocol.value,
    baseUrl.value,
    modelName.value,
    dimension.value,
  ])
function reset() {
  protocol.value =
    props.config?.providerProtocol ??
    props.options[0]?.providerProtocol ??
    'openai-chat'
  baseUrl.value = props.config?.baseUrl ?? option.value?.baseUrls[0] ?? ''
  modelName.value =
    props.config?.modelName ?? option.value?.supportedChatModels?.[0] ?? ''
  dimension.value = props.config?.dimension ?? null
  credential.value = ''
  initial.value = fingerprint()
  revision.value = props.config?.revision ?? 0
}
function changeProtocol() {
  baseUrl.value = option.value?.baseUrls[0] ?? ''
  modelName.value = option.value?.supportedChatModels?.[0] ?? ''
  dimension.value = null
  credential.value = ''
  result.value = null
  notice.value = ''
}
reset()
const dirty = computed(
  () => initial.value !== fingerprint() || credential.value.length > 0,
)
watch(
  () => props.config,
  () => {
    if (!dirty.value) reset()
  },
)
async function reload() {
  if (
    dirty.value &&
    !window.confirm('重新读取会丢弃未保存的模型配置，继续吗？')
  )
    return
  await run(async () => {
    reset()
    result.value = null
    notice.value = ''
    await props.refreshConfig()
    reset()
  })
}
onBeforeRouteLeave(() => {
  if (
    session.value?.user &&
    dirty.value &&
    !window.confirm('离开后将丢弃未保存的模型配置，继续吗？')
  )
    return false
})
onBeforeUnmount(() => {
  credential.value = ''
})
async function submit(save: boolean) {
  notice.value = ''
  result.value = null
  await run(async () => {
    const body = {
      providerProtocol: protocol.value,
      baseUrl: baseUrl.value,
      modelName: modelName.value,
      expectedRevision: revision.value,
      ...(credential.value ? { credential: credential.value } : {}),
      ...(props.kind === 'embedding' && dimension.value
        ? { embeddingDimension: dimension.value }
        : {}),
    }
    try {
      if (save) {
        await $api.request<Config>(`/settings/models/${props.kind}`, {
          method: 'PUT',
          body,
          organizationId: props.organizationId,
          timeoutMs: 40000,
        })
        initial.value = fingerprint()
        credential.value = ''
        await props.refreshConfig()
        notice.value = '配置已测试通过并生效。'
      } else {
        result.value = await $api.request<Result>(
          `/settings/models/${props.kind}/test`,
          {
            method: 'POST',
            body,
            organizationId: props.organizationId,
            timeoutMs: 40000,
          },
        )
      }
    } finally {
      credential.value = ''
    }
  })
}
</script>
<template>
  <section v-if="option" class="panel model-panel" :aria-label="titles[kind]">
    <div class="model-heading">
      <div>
        <p class="small muted">{{ option.providerProtocol }}</p>
        <h2>{{ titles[kind] }}</h2>
      </div>
      <span class="badge">{{
        config ? (config.enabled ? '已生效' : '已停用') : '未配置'
      }}</span>
    </div>
    <p v-if="config" class="small muted">
      当前模型：{{ config.modelName }} · 生效版本 {{ config.revision }} ·
      上次测试：{{
        new Date(config.lastTestedAt)
          .toISOString()
          .replace('T', ' ')
          .slice(0, 19)
      }}
      UTC
    </p>
    <p v-if="config?.embeddingLocked" class="notice">
      已有文档或处理任务，模型与维度已锁定。更换模型需要维护重建；仍可更新凭据。
    </p>
    <p v-if="kind === 'chat'" class="small muted">
      变更只作用于新运行。测试检查流式工具调用、结构化输出和用量能力。
    </p>
    <p v-else-if="kind === 'embedding'" class="small muted">
      用于文档向量。留空维度将以实际返回为准；已有文档后不能更换索引语义。
    </p>
    <p v-else class="small muted">
      用于重排检索结果。测试只发送一个短查询和两条示例文本。
    </p>
    <p v-if="stale || code === 'VERSION_CONFLICT'" class="notice">
      配置已更新，当前输入已保留。重新读取生效配置后再核对修改。
    </p>
    <p v-if="code === 'EMBEDDING_LOCKED'" class="notice">
      资料状态可能已变化。请重新读取锁定状态，不要重复测试不同的索引模型。
    </p>
    <form @submit.prevent="submit(true)">
      <fieldset :disabled="busy || !interactive">
        <label
          >服务协议<select
            v-model="protocol"
            :disabled="config?.embeddingLocked"
            @change="changeProtocol"
          >
            <option
              v-for="item in options"
              :key="item.providerProtocol"
              :value="item.providerProtocol"
            >
              {{ item.providerProtocol }}
            </option>
          </select></label
        >
        <label
          >服务地址<select
            v-model="baseUrl"
            :disabled="config?.embeddingLocked"
            required
          >
            <option v-for="url in option.baseUrls" :key="url" :value="url">
              {{ url }}
            </option>
          </select></label
        >
        <label
          >模型名称
          <select v-if="kind === 'chat'" v-model="modelName" required>
            <option
              v-for="name in option.supportedChatModels"
              :key="name"
              :value="name"
            >
              {{ name }}
            </option>
          </select>
          <input
            v-else
            v-model="modelName"
            :disabled="config?.embeddingLocked"
            required
            maxlength="100"
            placeholder="填写提供方的模型标识"
          />
        </label>
        <label v-if="kind === 'embedding'"
          >向量维度（可选）<input
            v-model.number="dimension"
            :disabled="config?.embeddingLocked"
            type="number"
            min="1"
            max="65536"
            step="1"
            placeholder="留空后由测试确认"
        /></label>
        <label
          >API Key<input
            v-model="credential"
            type="password"
            autocomplete="new-password"
            :required="!config?.hasCredential || baseUrl !== config.baseUrl"
            maxlength="4096"
            :placeholder="
              config?.hasCredential
                ? '已保存；不更换时留空'
                : '填写本服务专用凭据'
            "
        /></label>
      </fieldset>
      <p class="small muted">
        测试与保存都会发起最小调用，可能产生用量；聊天模型最多两次调用。保存会重新测试，失败保留当前配置。凭据不会读回，提交后清空输入框。
      </p>
      <p v-if="message" role="alert" tabindex="-1" class="error">
        {{ message }}
      </p>
      <p v-if="notice" role="status" class="success">{{ notice }}</p>
      <div v-if="result" :class="result.ok ? 'success' : 'error'" role="status">
        <p>
          {{ result.message
          }}<span v-if="result.errorCode">（{{ result.errorCode }}）</span>
        </p>
        <p v-if="result.ok">测试通过尚未保存，点击“测试并保存”使其生效。</p>
        <p class="small">
          耗时 {{ result.latencyMs }} ms · 调用 {{ result.usage.length }} 次
        </p>
        <p v-for="usage in result.usage" :key="usage.attempt" class="small">
          第 {{ usage.attempt }} 次：{{
            usage.tokenSource === 'actual'
              ? `实际输入 ${usage.inputTokens ?? 0} / 输出 ${usage.outputTokens ?? 0} Token`
              : usage.tokenSource === 'reserved'
                ? `按预留估算 ${usage.reservedTokens} Token`
                : 'Token 用量不可得'
          }}
          <span v-for="(amount, unit) in usage.billedUnits" :key="unit">
            · {{ unit }} {{ amount }}</span
          >
        </p>
      </div>
      <div class="inline">
        <button
          type="button"
          class="secondary"
          :disabled="busy || !interactive || stale || !modelName || !baseUrl"
          @click="submit(false)"
        >
          仅测试</button
        ><button
          type="submit"
          class="primary"
          :disabled="busy || !interactive || stale || !option.baseUrls.length"
        >
          {{ busy ? '正在测试…' : '测试并保存' }}
        </button>
        <button
          type="button"
          class="secondary"
          :disabled="busy || !interactive"
          @click="reload"
        >
          重新读取生效配置
        </button>
      </div>
    </form>
  </section>
</template>
