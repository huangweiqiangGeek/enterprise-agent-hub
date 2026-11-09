<script setup lang="ts">
import type { components } from '../types/api'
import { apiError } from '../utils/api-error'
import { object, terminal } from '../utils/run-stream'
const props = defineProps<{ org: string; runId: string; editable?: boolean }>()
const emit = defineEmits<{
  settled: []
  retry: [value: components['schemas']['RunSnapshot']]
}>()
const { $api } = useNuxtApp()
const { organization } = useIdentity()
const { snapshot, draft, connection, error, recover } = useRun(
  props.org,
  toRef(props, 'runId'),
)
watch(
  () => snapshot.value?.status,
  (value) => {
    if (value && terminal(value)) emit('settled')
  },
)
const source = ref<components['schemas']['SourceDetail'] | null>(null)
const steps = ref<components['schemas']['StepSummary'][]>([])
const stepCursor = ref<string | null>(null)
const message = ref('')
const dialog = ref<HTMLDialogElement>()
let generation = 0
const stopping = ref(false)
const cancelPending = ref(false)
async function cancel() {
  if (stopping.value || !props.editable) return
  const epoch = generation
  stopping.value = true
  message.value = ''
  try {
    const result = await $api.request<components['schemas']['RunSnapshot']>(
      `/runs/${props.runId}/cancel`,
      { organizationId: props.org, method: 'POST' },
    )
    if (epoch !== generation) return
    snapshot.value = result
    cancelPending.value = result.cancelRequested && !terminal(result.status)
    recover()
  } catch (cause) {
    if (epoch === generation) {
      message.value = `停止请求尚未确认：${apiError(cause).message} 可重试停止，仍作用于原运行。`
      recover()
    }
  } finally {
    if (epoch === generation) stopping.value = false
  }
}
const status: Record<string, string> = {
  queued: '排队中',
  running: '执行中',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
  timed_out: '超时',
}
const failureReason: Record<string, string> = {
  RUN_CANCELLED: '运行已停止，已发生的调用仍保留计量。可核对资料后准备新重试。',
  RUN_DEADLINE_EXCEEDED:
    '已达到执行时限，未继续调用。可缩小问题范围后准备新重试。',
  RUN_INTERRUPTED:
    '执行被中断，无法确认的输出没有继续生成或拼接。原记录与部分文本已保留。',
  MODEL_STREAM_INTERRUPTED: '模型流中断，部分文本未经最终校验。可准备新重试。',
  RUN_QUEUE_TIMEOUT: '排队超过时限，尚未开始执行。可稍后准备新重试。',
  BUDGET_EXCEEDED: '本次预算已耗尽，未继续调用。可缩小范围后准备新重试。',
}
function invalidate() {
  generation++
  source.value = null
  steps.value = []
  stepCursor.value = null
  message.value = ''
  stopping.value = false
  cancelPending.value = false
  dialog.value?.close()
}
watch(
  () => [
    props.runId,
    organization.value?.id,
    organization.value?.accessRevision,
  ],
  invalidate,
)
onUnmounted(invalidate)
const citations = computed(() =>
  (snapshot.value?.citations || []).flatMap((c) =>
    typeof c.sourceId === 'string' &&
    typeof c.label === 'number' &&
    typeof c.claimPath === 'string' &&
    typeof c.title === 'string'
      ? [
          {
            sourceId: c.sourceId,
            label: c.label,
            claimPath: c.claimPath,
            title: c.title,
          },
        ]
      : [],
  ),
)
async function openSource(id: string) {
  const epoch = ++generation
  source.value = null
  message.value = ''
  dialog.value?.showModal()
  try {
    const result = await $api.request<components['schemas']['SourceDetail']>(
      `/runs/${props.runId}/sources/${id}`,
      { organizationId: props.org },
    )
    if (epoch === generation) source.value = result
  } catch (cause) {
    if (epoch === generation) message.value = apiError(cause).message
  }
}
function closeSource() {
  generation++
  dialog.value?.close()
  source.value = null
}
async function loadSteps(more = false) {
  const epoch = generation
  try {
    const page = await $api.request<components['schemas']['Page_StepSummary_']>(
      `/runs/${props.runId}/steps?cursor=${more ? stepCursor.value || '0' : '0'}`,
      { organizationId: props.org },
    )
    if (epoch !== generation) return
    steps.value = more ? [...steps.value, ...page.items] : page.items
    stepCursor.value = page.nextCursor || null
  } catch (cause) {
    if (epoch === generation) message.value = apiError(cause).message
  }
}
async function exportResult(copy = false) {
  const epoch = generation
  try {
    const text = await $api.read(
      `/runs/${props.runId}/export?format=markdown`,
      { organizationId: props.org },
      (r) => r.text(),
    )
    if (epoch !== generation) return
    if (copy) {
      await navigator.clipboard.writeText(text)
      message.value = '已复制含来源与范围的 Markdown。'
      return
    }
    const url = URL.createObjectURL(
      new Blob([text], { type: 'text/markdown;charset=utf-8' }),
    )
    const link = document.createElement('a')
    link.href = url
    link.download = `run-${props.runId}.md`
    link.click()
    URL.revokeObjectURL(url)
  } catch (cause) {
    if (epoch === generation) message.value = apiError(cause).message
  }
}
async function copyPartial() {
  try {
    await navigator.clipboard.writeText(
      `[未完成 · ${status[snapshot.value?.status || 'running']}]\n${draft.value || snapshot.value?.partialMessage || ''}`,
    )
    message.value = '已复制并附未完成标记。'
  } catch {
    message.value = '浏览器未允许复制，请手动选择文本。'
  }
}
function locator(value: unknown) {
  if (!object(value)) return ''
  if (value.pageStart)
    return `第 ${value.pageStart}–${value.pageEnd || value.pageStart} 页`
  const heading = Array.isArray(value.headingPath)
    ? value.headingPath.filter((x) => typeof x === 'string').join(' / ')
    : ''
  return `${heading} · 段落 ${value.paragraphStart ?? '?'}–${value.paragraphEnd ?? '?'}`
}
</script>
<template>
  <section class="panel run-result" aria-label="运行结果">
    <div class="inline">
      <h2>运行结果</h2>
      <span class="badge">{{
        snapshot &&
        !terminal(snapshot.status) &&
        (snapshot.cancelRequested || cancelPending)
          ? '正在停止'
          : status[snapshot?.status || ''] || '读取中'
      }}</span>
    </div>
    <p class="small muted" role="status">{{ connection }}</p>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <template v-if="snapshot">
      <p class="small muted">
        运行 {{ snapshot.id }} · Agent 第
        {{ snapshot.configVersion.versionNumber }} 版
      </p>
      <p>{{ snapshot.inputSummary }}</p>
      <p v-if="snapshot.retryOfRunId" class="small muted">
        重试自运行 {{ snapshot.retryOfRunId }}，本次使用重新校验后的配置与预算。
      </p>
      <div v-if="editable" class="actions">
        <button
          v-if="!terminal(snapshot.status)"
          :disabled="stopping || snapshot.cancelRequested || cancelPending"
          @click="cancel"
        >
          {{
            snapshot.cancelRequested || cancelPending
              ? '正在停止…'
              : stopping
                ? '正在请求停止…'
                : '停止运行'
          }}
        </button>
        <button
          v-else-if="snapshot.status !== 'succeeded'"
          @click="emit('retry', snapshot)"
        >
          准备重试
        </button>
      </div>
      <p v-if="snapshot.errorCode" class="error">
        {{
          failureReason[snapshot.errorCode] ||
          '请检查资料、权限与模型配置，当前运行已保留。'
        }}
        <span class="small">（{{ snapshot.errorCode }}）</span>
      </p>
      <template
        v-if="
          snapshot.status === 'succeeded' &&
          snapshot.result &&
          snapshot.resultVisibility !== 'withheld'
        "
      >
        <p
          v-if="snapshot.resultVisibility === 'partially_unavailable'"
          class="notice"
        >
          部分来源已删除，历史答案保留，原文不可用。
        </p>
        <div data-testid="final-answer">
          <p
            v-if="
              !snapshot.result.claims.length &&
              !snapshot.result.conflicts.length
            "
            class="answer-text"
          >
            {{ snapshot.result.answer }}
          </p>
          <div
            v-for="(claim, index) in snapshot.result.claims"
            :key="`claim-${index}`"
            class="answer-text"
          >
            <span class="badge">{{
              {
                fact: '事实',
                inference: '推断',
                suggestion: '建议',
                unknown: '待补充',
              }[claim.kind]
            }}</span>
            {{ claim.text }}
            <button
              v-for="c in citations.filter(
                (c) => c.claimPath === `claims.${index}`,
              )"
              :key="c.label"
              class="text-button"
              :aria-label="`来源 ${c.label}：${c.title}`"
              @click="openSource(c.sourceId)"
            >
              [{{ c.label }}]
            </button>
          </div>
          <div
            v-for="(claim, index) in snapshot.result.conflicts"
            :key="`conflict-${index}`"
            class="notice"
          >
            资料冲突：{{ claim.text }}
            <button
              v-for="c in citations.filter(
                (c) => c.claimPath === `conflicts.${index}`,
              )"
              :key="c.label"
              class="text-button"
              @click="openSource(c.sourceId)"
            >
              来源 [{{ c.label }}]
            </button>
          </div>
          <p v-for="text in snapshot.result.missingInformation" :key="text">
            待补充：{{ text }}
          </p>
        </div>
        <details>
          <summary>覆盖范围与限制</summary>
          <pre>{{ JSON.stringify(snapshot.coverage, null, 2) }}</pre>
          <p>仅依据实际检索证据，不代表全部资料。</p>
        </details>
        <div class="actions">
          <button @click="exportResult(true)">复制 Markdown</button
          ><button @click="exportResult()">下载 Markdown</button>
        </div>
      </template>
      <template v-else-if="snapshot.resultVisibility !== 'withheld'">
        <p class="muted">生成中或未完成的内容尚未经最终校验。</p>
        <p data-testid="draft" class="answer-text">
          {{ draft || snapshot.partialMessage }}
        </p>
        <button v-if="draft || snapshot.partialMessage" @click="copyPartial">
          复制部分文本
        </button>
      </template>
      <div class="actions">
        <button @click="loadSteps(false)">查看简明步骤</button
        ><button class="text-button" @click="recover">重新同步结果</button>
      </div>
      <details v-for="step in steps" :key="step.id">
        <summary>
          {{ step.name }} · 尝试 {{ step.attempt }} · {{ step.status }}
        </summary>
        <p>{{ step.durationMs ?? '—' }} ms · {{ step.errorCode || '' }}</p>
        <pre>{{
          JSON.stringify(
            {
              输入: step.publicInputSummary,
              输出: step.publicOutputSummary,
              用量: step.usage,
            },
            null,
            2,
          )
        }}</pre>
      </details>
      <button v-if="stepCursor" @click="loadSteps(true)">更多步骤</button>
    </template>
    <p v-if="message && !dialog?.open" role="status">{{ message }}</p>
    <dialog ref="dialog" aria-label="来源详情" @cancel="closeSource">
      <button autofocus @click="closeSource">关闭来源</button>
      <p v-if="message" role="alert">{{ message }}</p>
      <template v-if="source"
        ><h2>{{ source.title }}</h2>
        <p>{{ locator(source.locator) }}</p>
        <p class="small">
          文档：{{ source.documentId }}<br />处理版本：{{ source.attemptId
          }}<br />片段：{{ source.chunkId }}<br />观察时间：{{
            source.observedAt
          }}<br />内容摘要：{{ source.contentHash }}
        </p>
        <p v-if="source.availability !== 'available'">
          来源已删除或不可用，无法读取原文。
        </p>
        <pre v-else>{{ source.excerpt }}</pre>
      </template>
      <p v-else-if="!message">正在读取来源…</p>
    </dialog>
  </section>
</template>
<style scoped>
.answer-text {
  white-space: pre-wrap;
  margin: 1rem 0;
  line-height: 1.8;
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
dialog {
  max-width: min(720px, 90vw);
  max-height: 85vh;
  border: 1px solid #c8d0da;
  border-radius: 12px;
  padding: 24px;
  overflow-wrap: anywhere;
}
dialog::backdrop {
  background: #15233480;
}
.run-result {
  overflow-wrap: anywhere;
}
</style>
