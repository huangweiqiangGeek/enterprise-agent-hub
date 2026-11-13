<script setup lang="ts">
import type { components } from '../types/api'
import { apiError } from '../utils/api-error'
import { object, terminal } from '../utils/run-stream'
const props = defineProps<{
  org: string
  runId: string
  editable?: boolean
  detailed?: boolean
}>()
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
const {
  data: report,
  error: reportError,
  refresh: refreshReport,
  clear: clearReport,
} = await useAsyncData(`run-report:${props.org}:${props.runId}`, () =>
  props.detailed
    ? $api.request<components['schemas']['RunReport']>(
        `/runs/${props.runId}/report`,
        { organizationId: props.org },
      )
    : Promise.resolve(null),
)
function duration(value: number | null) {
  return value === null ? '尚未结束' : `${(value / 1000).toFixed(2)} 秒`
}
function syncDetails() {
  recover()
  refreshReport()
}
watch(
  () => snapshot.value?.status,
  (value) => {
    if (value && terminal(value)) {
      emit('settled')
      if (props.detailed) refreshReport()
    }
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
  PERMISSION_REVOKED: '访问权限已变化，请核对团队资格、Agent 和资料范围。',
  GITHUB_PERMISSION_REVOKED:
    'GitHub 授权已变化，混合结果与来源暂不可查看。请核对仓库授权。',
  GITHUB_UNAVAILABLE: 'GitHub 连接或授权仓库不可用，请管理员检查后重试。',
  GITHUB_AUTH_FAILED: 'GitHub 认证失败，不能将失败查询视为空结果。',
  REQUIRED_SOURCE_UNAVAILABLE: '必需资料已失效，请重新选择就绪资料。',
  MODEL_AUTH_FAILED: '模型连接认证失败，请联系管理员检查模型配置。',
  MODEL_UNAVAILABLE: '模型服务暂时不可用，请联系管理员检查连接后再重试。',
}
function invalidate() {
  clearReport()
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
const progressResult = computed(() => {
  const result = snapshot.value?.result
  return result && 'queryScope' in result ? result : null
})
const progressGroups = computed(() =>
  progressResult.value
    ? [
        { label: '进展', items: progressResult.value.progress },
        { label: '阻塞', items: progressResult.value.blockers },
        { label: '风险', items: progressResult.value.risks },
        { label: '待核实', items: progressResult.value.toVerify },
        { label: '建议', items: progressResult.value.suggestions },
      ]
    : [],
)
const sourceUrl = computed(() => {
  const raw = source.value?.htmlUrl
  if (!raw || source.value?.kind !== 'github') return null
  try {
    const url = new URL(raw)
    return url.protocol === 'https:' &&
      url.hostname === 'github.com' &&
      !url.username &&
      !url.password &&
      !url.port &&
      !url.search &&
      !url.hash &&
      /^\/[^/]+\/[^/]+\/(issues|pull)\/\d+$/.test(url.pathname)
      ? url.href
      : null
  } catch {
    return null
  }
})
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
      <NuxtLink v-if="!detailed" :to="`/o/${org}/runs/${runId}`"
        >执行详情与用量</NuxtLink
      >
      <template v-if="detailed">
        <p v-if="reportError" class="error">用量详情读取失败，请重新同步。</p>
        <template v-if="report">
          <h3>{{ report.agentName }} · 历史版本 {{ report.versionNumber }}</h3>
          <p>
            发起者：{{ report.initiatorName }} · 排队
            {{ duration(report.queueDurationMs) }} · 执行
            {{ duration(report.executionDurationMs) }}
          </p>
          <p>
            时间（UTC）：创建 {{ snapshot.createdAt }}<br />开始
            {{ snapshot.startedAt || '尚未开始' }} · 结束
            {{ snapshot.finishedAt || '尚未结束' }}
          </p>
          <details>
            <summary>历史配置与资料范围</summary>
            <pre>{{
              JSON.stringify(
                {
                  配置: snapshot.configVersion,
                  资料: snapshot.input,
                  预算: snapshot.budgetSnapshot,
                },
                null,
                2,
              )
            }}</pre>
          </details>
          <UsageBreakdown :values="report.byKind" />
          <p v-if="report.isOwner">
            <NuxtLink :to="`/o/${org}/chat/${snapshot.conversationId}`"
              >返回本人对话修改输入或准备重试</NuxtLink
            >
          </p>
          <p v-else class="notice">
            组织审计仅供查看，不能代发或重试他人的任务。
          </p>
          <p v-for="id in report.retryIds" :key="id">
            <NuxtLink :to="`/o/${org}/runs/${id}`"
              >查看关联重试 {{ id }}</NuxtLink
            >
          </p>
        </template>
      </template>
      <p v-if="snapshot.retryOfRunId" class="small muted">
        重试自<NuxtLink :to="`/o/${org}/runs/${snapshot.retryOfRunId}`"
          >运行 {{ snapshot.retryOfRunId }}</NuxtLink
        >，本次使用重新校验后的配置与预算。
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
          <template v-if="progressResult">
            <h3>项目进展与风险</h3>
            <p class="notice">
              仅依据当前可见的更新样本与计划证据。PR 合并不等于部署或验收；Issue
              关闭不等于业务目标完成。
            </p>
            <p class="small">
              仓库：{{ progressResult.queryScope.repository }} · 时区：{{
                progressResult.queryScope.timezone
              }}
            </p>
            <p class="small">
              UTC 范围：{{ progressResult.queryScope.start }}（含）至
              {{ progressResult.queryScope.endExclusive }}（不含）
            </p>
            <details>
              <summary>查询范围</summary>
              <pre>{{
                JSON.stringify(progressResult.queryScope, null, 2)
              }}</pre>
            </details>
            <section v-for="group in progressGroups" :key="group.label">
              <h4>{{ group.label }}</h4>
              <p v-if="!group.items.length" class="muted">
                本次证据未形成该类结论，不代表不存在。
              </p>
              <p
                v-for="(claim, index) in group.items"
                :key="index"
                class="answer-text"
              >
                <span class="badge">{{
                  {
                    fact: '事实',
                    inference: '推断',
                    suggestion: '建议',
                    unknown: '待核实',
                  }[claim.kind]
                }}</span>
                {{ claim.text }}
                <button
                  v-for="id in claim.sourceIds"
                  :key="id"
                  class="text-button"
                  @click="openSource(id)"
                >
                  查看依据
                </button>
              </p>
            </section>
          </template>
          <template v-else>
            <div v-if="'items' in snapshot.result" class="table-scroll">
              <h3>逐项对比</h3>
              <p>{{ snapshot.result.summary }}</p>
              <table>
                <thead>
                  <tr>
                    <th>需求与来源</th>
                    <th>方案证据与来源</th>
                    <th>状态</th>
                    <th>影响（待核对）</th>
                    <th>建议</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="(item, index) in snapshot.result.items"
                    :key="index"
                  >
                    <td>
                      {{ item.requirement
                      }}<button
                        v-for="id in item.requirementSourceIds"
                        :key="id"
                        class="text-button"
                        @click="openSource(id)"
                      >
                        查看需求来源
                      </button>
                    </td>
                    <td>
                      {{ item.solutionEvidence
                      }}<button
                        v-for="id in item.solutionSourceIds"
                        :key="id"
                        class="text-button"
                        @click="openSource(id)"
                      >
                        查看方案来源
                      </button>
                    </td>
                    <td>
                      {{
                        {
                          covered: '已覆盖（本次证据）',
                          partial: '部分覆盖',
                          not_found: '本次未检索到',
                          conflict: '证据冲突',
                        }[item.status]
                      }}
                    </td>
                    <td>{{ item.impact }}</td>
                    <td>{{ item.suggestion }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
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
          </template>
          <p
            v-for="text in progressResult
              ? []
              : snapshot.result.missingInformation"
            :key="text"
          >
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
      <p v-if="snapshot.resultVisibility === 'withheld'" class="notice">
        资料授权已撤销，整段混合结果、草稿与来源暂不可查看。
      </p>
      <div class="actions">
        <button @click="loadSteps(false)">查看简明步骤</button
        ><button class="text-button" @click="syncDetails">重新同步结果</button>
      </div>
      <details v-for="step in steps" :key="step.id">
        <summary>
          {{ step.name }} · 尝试 {{ step.attempt }} · {{ step.status }}
        </summary>
        <p>{{ step.durationMs ?? '—' }} ms · {{ step.errorCode || '' }}</p>
        <p>UTC {{ step.startedAt }} → {{ step.finishedAt || '尚未结束' }}</p>
        <button v-for="id in step.sourceIds" :key="id" @click="openSource(id)">
          查看步骤来源
        </button>
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
        <template v-if="source.kind === 'github'">
          <p>
            GitHub {{ source.itemType === 'pr' ? 'PR' : 'Issue' }} #{{
              source.number
            }}
            · 授权仓库 {{ source.repoId }}
          </p>
          <a
            v-if="sourceUrl"
            :href="sourceUrl"
            target="_blank"
            rel="noopener noreferrer"
            >打开 GitHub 原条目</a
          >
          <p class="small">
            观察时间：{{ source.observedAt }}<br />内容摘要：{{
              source.contentHash
            }}
          </p>
          <details>
            <summary>来源元数据</summary>
            <pre>{{ JSON.stringify(source.locator, null, 2) }}</pre>
          </details>
        </template>
        <template v-else>
          <p>{{ locator(source.locator) }}</p>
          <p class="small">
            文档：{{ source.documentId }}<br />处理版本：{{ source.attemptId
            }}<br />片段：{{ source.chunkId }}<br />观察时间：{{
              source.observedAt
            }}<br />内容摘要：{{ source.contentHash }}
          </p>
        </template>
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
