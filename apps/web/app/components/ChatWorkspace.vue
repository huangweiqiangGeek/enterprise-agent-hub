<script setup lang="ts">
import type { components } from '../types/api'
import { apiError } from '../utils/api-error'
import {
  defaultProgressDates,
  validProgressDates,
} from '../utils/progress-range'
import { object, terminal } from '../utils/run-stream'
import { parseTaskType } from '../utils/task-type'
type S = components['schemas']
type Pending = {
  conversationKey: string
  runKey: string
  conversationId: string
  agentId: string
  question: string
  knowledgeBaseIds: string[]
  documentIds: string[]
  taskType?: 'qa' | 'compare' | 'progress'
  repoId?: string
  startDate?: string
  endDate?: string
  timezone?: string
  requirementDocumentId?: string
  solutionDocumentId?: string
  retryOfRunId?: string
}
const props = defineProps<{ org: string; conversationId: string }>()
const { $api } = useNuxtApp()
const { session, organization } = useIdentity()
const opts = { organizationId: props.org }
const identityKey = `${session.value?.user?.id}:${props.org}`
const {
  data: initial,
  error: initialError,
  refresh,
} = await useAsyncData(
  `chat:${identityKey}:${props.conversationId}`,
  async () => {
    const [conversations, agents, conversation, messages] = await Promise.all([
      $api.request<S['Page_ConversationSummary_']>('/conversations', opts),
      $api.request<S['Page_AgentSummary_']>(
        '/agents?enabled=true&limit=100',
        opts,
      ),
      props.conversationId
        ? $api.request<S['ConversationSummary']>(
            `/conversations/${props.conversationId}`,
            opts,
          )
        : null,
      props.conversationId
        ? $api.request<S['Page_MessageView_']>(
            `/conversations/${props.conversationId}/messages`,
            opts,
          )
        : null,
    ])
    return { conversations, agents, conversation, messages }
  },
)
const conversations = ref(initial.value?.conversations.items || [])
const cursor = ref(initial.value?.conversations.nextCursor || null)
const messages = ref(initial.value?.messages?.items || [])
const messageCursor = ref(initial.value?.messages?.nextCursor || null)
const selectedRun = ref(
  initial.value?.conversation?.activeRunId ||
    initial.value?.conversation?.lastRunId ||
    '',
)
const activeRun = ref(initial.value?.conversation?.activeRunId || '')
const route = useRoute()
const requestedType = parseTaskType(route.query.taskType)
const agentId = ref(
  initial.value?.conversation?.agentId ||
    (typeof route.query.agent === 'string' ? route.query.agent : ''),
)
const taskType = computed(
  () =>
    initial.value?.conversation?.taskType ||
    initial.value?.agents.items.find((a) => a.id === agentId.value)?.taskType ||
    requestedType ||
    'qa',
)
const requirementDocumentId = ref(''),
  solutionDocumentId = ref('')
const repoId = ref(''),
  startDate = ref(''),
  endDate = ref(''),
  timezone = ref('UTC')
const dateError = computed(() =>
  validProgressDates(startDate.value, endDate.value, timezone.value)
    ? ''
    : '请选择有效时区和日期，开始日期不能晚于结束日期，且不能选择未来日期。',
)
const validScope = computed(() =>
  taskType.value === 'compare'
    ? Boolean(
        requirementDocumentId.value &&
        solutionDocumentId.value &&
        requirementDocumentId.value !== solutionDocumentId.value,
      )
    : Boolean(kbIds.value.length) &&
      (taskType.value !== 'progress' ||
        Boolean(repoId.value && !dateError.value)),
)
const taskOptions = ref<S['TaskOptions'] | null>(null)
const kbIds = ref<string[]>([]),
  documentIds = ref<string[]>([])
const question = ref(''),
  search = ref(''),
  title = ref(initial.value?.conversation?.title || '')
const busy = ref(false),
  loadingOptions = ref(false),
  message = ref(''),
  pending = ref<Pending | null>(null)
const interactive = ref(false)
const retryOfRunId = ref<string | undefined>()
async function prepareRetry(value: S['RunSnapshot']) {
  if (activeRun.value || busy.value || pending.value) return
  const n = epoch
  await chooseAgent()
  if (!current(n) || !taskOptions.value) return
  question.value = value.input.question || ''
  if ('requirementDocumentId' in value.input) {
    const comparison = value.input
    const docs = taskOptions.value.documents
    requirementDocumentId.value = docs.some(
      (d) => d.id === comparison.requirementDocumentId,
    )
      ? comparison.requirementDocumentId
      : ''
    solutionDocumentId.value = docs.some(
      (d) => d.id === comparison.solutionDocumentId,
    )
      ? comparison.solutionDocumentId
      : ''
    retryOfRunId.value = value.id
    message.value = '请核对两份文档和当前版本后创建新重试。'
    return
  }
  const input = value.input
  kbIds.value = input.knowledgeBaseIds.filter((id) =>
    taskOptions.value?.knowledgeBases.some((k) => k.id === id),
  )
  if ('repoId' in input) {
    repoId.value = taskOptions.value.repositories?.some(
      (r) => r.id === input.repoId,
    )
      ? input.repoId
      : ''
    startDate.value = input.startDate
    endDate.value = input.endDate
    timezone.value = input.timezone
    retryOfRunId.value = value.id
    if (kbIds.value.length !== input.knowledgeBaseIds.length) kbIds.value = []
    message.value =
      '已载入原仓库、日期与时区。请核对当前授权和计划资料后创建新重试。'
    return
  }
  await nextTick()
  documentIds.value = (input.documentIds || []).filter((id) =>
    visibleDocuments.value.some((d) => d.id === id),
  )
  retryOfRunId.value = value.id
  message.value =
    '已载入原问题与仍可用范围。请核对资料后创建新重试，原运行保持不变。'
  if (
    kbIds.value.length !== input.knowledgeBaseIds.length ||
    documentIds.value.length !== (input.documentIds || []).length
  ) {
    kbIds.value = []
    documentIds.value = []
    message.value = '原范围已发生变化，请重新选择资料；新重试仍关联原运行。'
  }
}
const storageKey = `eah:submission:${identityKey}:${props.conversationId || 'new'}`
let epoch = 0,
  scopeEpoch = 0,
  listEpoch = 0
let timer: ReturnType<typeof setInterval> | undefined
let polling = false
const visibleDocuments = computed(
  () =>
    taskOptions.value?.documents.filter((d) =>
      kbIds.value.includes(d.knowledgeBaseId),
    ) || [],
)
const history = computed(() => [...messages.value].reverse())
const usable = computed(
  () =>
    initial.value?.agents.items.filter(
      (a) =>
        ['qa', 'compare', 'progress'].includes(a.taskType) &&
        a.enabled &&
        !a.readiness.length &&
        (props.conversationId ||
          pending.value ||
          !requestedType ||
          a.taskType === requestedType),
    ) || [],
)
function current(n: number) {
  return n === epoch && organization.value?.id === props.org
}
async function chooseAgent() {
  const n = ++scopeEpoch,
    id = agentId.value
  taskOptions.value = null
  kbIds.value = []
  documentIds.value = []
  requirementDocumentId.value = ''
  solutionDocumentId.value = ''
  repoId.value = ''
  if (!id) return
  loadingOptions.value = true
  try {
    const value = await $api.request<S['TaskOptions']>(
      `/agents/${id}/task-options`,
      opts,
    )
    if (n !== scopeEpoch || organization.value?.id !== props.org) return
    taskOptions.value = value
    if (pending.value?.taskType === 'progress') {
      repoId.value = pending.value.repoId || ''
      startDate.value = pending.value.startDate || ''
      endDate.value = pending.value.endDate || ''
      timezone.value = pending.value.timezone || ''
      kbIds.value = [...pending.value.knowledgeBaseIds]
    }
    // Follow-ups require explicit range selection; never broaden an earlier selection.
    if (!props.conversationId && !pending.value)
      kbIds.value = value.knowledgeBases.map((k) => k.id)
  } catch (cause) {
    if (n === scopeEpoch) message.value = apiError(cause).message
  } finally {
    if (n === scopeEpoch) loadingOptions.value = false
  }
}
watch(agentId, chooseAgent)
watch(kbIds, () => {
  documentIds.value = documentIds.value.filter((id) =>
    visibleDocuments.value.some((d) => d.id === id),
  )
})
watch(
  () => [organization.value?.id, organization.value?.accessRevision],
  () => {
    epoch++
    scopeEpoch++
    listEpoch++
    taskOptions.value = null
    messages.value = []
    conversations.value = []
    selectedRun.value = ''
    pending.value = null
    if (organization.value?.id === props.org)
      void refresh().then(() => {
        if (organization.value?.id === props.org) location.reload()
      })
  },
)
async function list(more = false) {
  const n = ++listEpoch
  try {
    const query = new URLSearchParams({ q: search.value })
    if (more && cursor.value) query.set('cursor', cursor.value)
    const value = await $api.request<S['Page_ConversationSummary_']>(
      `/conversations?${query}`,
      opts,
    )
    if (n !== listEpoch) return
    conversations.value = more
      ? [...conversations.value, ...value.items]
      : value.items
    cursor.value = value.nextCursor || null
  } catch (cause) {
    if (n === listEpoch) message.value = apiError(cause).message
  }
}
async function older() {
  const n = epoch
  try {
    const value = await $api.request<S['Page_MessageView_']>(
      `/conversations/${props.conversationId}/messages?cursor=${encodeURIComponent(messageCursor.value || '')}`,
      opts,
    )
    if (!current(n)) return
    messages.value = [...messages.value, ...value.items]
    messageCursor.value = value.nextCursor || null
  } catch (cause) {
    if (current(n)) message.value = apiError(cause).message
  }
}
async function rename() {
  const n = epoch
  if (!initial.value?.conversation || busy.value) return
  busy.value = true
  try {
    const value = await $api.request<S['ConversationSummary']>(
      `/conversations/${props.conversationId}`,
      {
        ...opts,
        method: 'PATCH',
        body: {
          title: title.value,
          expectedRevision: initial.value.conversation.revision,
        },
      },
    )
    if (!current(n)) return
    initial.value.conversation = value
    await list()
  } catch (cause) {
    if (current(n)) {
      message.value =
        apiError(cause).code === 'VERSION_CONFLICT'
          ? '标题已被更新，请重新加载后修改。当前输入已保留。'
          : apiError(cause).message
    }
  } finally {
    if (current(n)) busy.value = false
  }
}
function savePending(value: Pending) {
  // Persist before the first POST. Failure to persist prevents an ambiguous submission.
  localStorage.setItem(storageKey, JSON.stringify(value))
  pending.value = value
}
async function submit() {
  if (
    busy.value ||
    activeRun.value ||
    pending.value ||
    !question.value.trim() ||
    !validScope.value
  )
    return
  try {
    savePending({
      conversationKey: crypto.randomUUID(),
      runKey: crypto.randomUUID(),
      conversationId: props.conversationId,
      agentId: agentId.value,
      question: question.value.trim(),
      knowledgeBaseIds: [...kbIds.value],
      documentIds: [...documentIds.value],
      taskType:
        taskType.value === 'progress'
          ? 'progress'
          : taskType.value === 'compare'
            ? 'compare'
            : 'qa',
      repoId: repoId.value,
      startDate: startDate.value,
      endDate: endDate.value,
      timezone: timezone.value,
      requirementDocumentId: requirementDocumentId.value,
      solutionDocumentId: solutionDocumentId.value,
      retryOfRunId: retryOfRunId.value,
    })
    await reconcile()
  } catch {
    message.value = '无法保存本次提交，请允许浏览器本地存储后重试。'
  }
}
async function reconcile() {
  if (busy.value || !pending.value) return
  const n = epoch,
    request = pending.value
  busy.value = true
  message.value = ''
  try {
    if (!request.conversationId) {
      const acceptance = await $api.request<S['AcceptanceDTO']>(
        `/idempotency-requests/${request.conversationKey}?operation=conversation.create`,
        opts,
      )
      if (!current(n)) return
      const conversation =
        acceptance.conversation ||
        (await $api.request<S['ConversationView']>('/conversations', {
          ...opts,
          method: 'POST',
          key: request.conversationKey,
          body: {
            agentId: request.agentId,
            taskType: request.taskType || 'qa',
            title: request.question.slice(0, 100),
          },
        }))
      if (!current(n)) return
      request.conversationId = conversation.id
      savePending(request)
    }
    const acceptance = await $api.request<S['AcceptanceDTO']>(
      `/idempotency-requests/${request.runKey}?operation=run.create&resourceId=${request.conversationId}`,
      opts,
    )
    if (!current(n)) return
    const accepted =
      acceptance.run ||
      (await $api.request<S['RunAccepted']>(
        `/conversations/${request.conversationId}/runs`,
        {
          ...opts,
          method: 'POST',
          key: request.runKey,
          body: {
            agentId: request.agentId,
            input:
              request.taskType === 'compare'
                ? {
                    requirementDocumentId: request.requirementDocumentId,
                    solutionDocumentId: request.solutionDocumentId,
                    question: request.question,
                    focus: request.question,
                  }
                : request.taskType === 'progress'
                  ? {
                      repoId: request.repoId,
                      startDate: request.startDate,
                      endDate: request.endDate,
                      timezone: request.timezone,
                      knowledgeBaseIds: request.knowledgeBaseIds,
                      question: request.question,
                      focus: request.question,
                    }
                  : {
                      question: request.question,
                      knowledgeBaseIds: request.knowledgeBaseIds,
                      documentIds: request.documentIds,
                    },
            retryOfRunId: request.retryOfRunId,
          },
        },
      ))
    if (!current(n)) return
    localStorage.removeItem(storageKey)
    pending.value = null
    question.value = ''
    retryOfRunId.value = undefined
    selectedRun.value = accepted.runId
    activeRun.value = terminal(accepted.status) ? '' : accepted.runId
    if (props.conversationId !== request.conversationId)
      await navigateTo(`/o/${props.org}/chat/${request.conversationId}`)
    else {
      await updateConversation()
      await list()
    }
  } catch (cause) {
    if (!current(n)) return
    const failure = apiError(cause)
    if (
      failure.status >= 400 &&
      failure.status < 500 &&
      ![408, 429].includes(failure.status)
    ) {
      localStorage.removeItem(storageKey)
      pending.value = null
      message.value =
        failure.code === 'CONVERSATION_BUSY'
          ? '此会话已有运行，请查看当前结果。'
          : failure.message
      await updateConversation()
    } else
      message.value = '接受结果待核对。请点击“核对原提交”，不会创建重复运行。'
  } finally {
    if (current(n)) busy.value = false
  }
}
async function updateConversation() {
  if (!props.conversationId || polling || document.hidden) return
  const n = epoch
  polling = true
  try {
    const value = await $api.request<S['ConversationSummary']>(
      `/conversations/${props.conversationId}`,
      opts,
    )
    if (!current(n)) return
    const wasActive = activeRun.value
    activeRun.value = value.activeRunId || ''
    if (activeRun.value) selectedRun.value = activeRun.value
    if (wasActive && !activeRun.value) {
      const value = await $api.request<S['Page_MessageView_']>(
        `/conversations/${props.conversationId}/messages`,
        opts,
      )
      if (!current(n)) return
      messages.value = value.items
      messageCursor.value = value.nextCursor || null
      await list()
    }
  } catch (cause) {
    if (current(n) && [401, 403, 404].includes(apiError(cause).status)) {
      epoch++
      messages.value = []
      selectedRun.value = ''
      taskOptions.value = null
      message.value = '会话已不可访问，请重新进入组织。'
    }
  } finally {
    polling = false
  }
}
onMounted(() => {
  interactive.value = true
  timezone.value = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  const dates = defaultProgressDates(timezone.value)
  startDate.value = dates.startDate
  endDate.value = dates.endDate
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(storageKey) || 'null')
    if (
      object(raw) &&
      typeof raw.conversationKey === 'string' &&
      typeof raw.runKey === 'string' &&
      typeof raw.conversationId === 'string' &&
      typeof raw.agentId === 'string' &&
      typeof raw.question === 'string' &&
      Array.isArray(raw.knowledgeBaseIds) &&
      raw.knowledgeBaseIds.every((x) => typeof x === 'string') &&
      Array.isArray(raw.documentIds) &&
      raw.documentIds.every((x) => typeof x === 'string') &&
      (raw.taskType !== 'progress' ||
        (typeof raw.repoId === 'string' &&
          typeof raw.startDate === 'string' &&
          typeof raw.endDate === 'string' &&
          typeof raw.timezone === 'string'))
    ) {
      pending.value = {
        conversationKey: raw.conversationKey,
        runKey: raw.runKey,
        conversationId: raw.conversationId,
        agentId: raw.agentId,
        question: raw.question,
        knowledgeBaseIds: raw.knowledgeBaseIds,
        documentIds: raw.documentIds,
        taskType:
          raw.taskType === 'progress'
            ? 'progress'
            : raw.taskType === 'compare'
              ? 'compare'
              : 'qa',
        repoId: typeof raw.repoId === 'string' ? raw.repoId : undefined,
        startDate:
          typeof raw.startDate === 'string' ? raw.startDate : undefined,
        endDate: typeof raw.endDate === 'string' ? raw.endDate : undefined,
        timezone: typeof raw.timezone === 'string' ? raw.timezone : undefined,
        requirementDocumentId:
          typeof raw.requirementDocumentId === 'string'
            ? raw.requirementDocumentId
            : undefined,
        solutionDocumentId:
          typeof raw.solutionDocumentId === 'string'
            ? raw.solutionDocumentId
            : undefined,
        retryOfRunId:
          typeof raw.retryOfRunId === 'string' ? raw.retryOfRunId : undefined,
      }
      question.value = raw.question
      agentId.value = raw.agentId
      message.value = '发现待核对提交，请核对原提交。'
    }
  } catch {
    message.value = '本地提交记录无法读取，请查看会话列表核对。'
  }
  void chooseAgent()
  timer = setInterval(updateConversation, 2000)
})
onUnmounted(() => {
  epoch++
  scopeEpoch++
  listEpoch++
  clearInterval(timer)
})
</script>
<template>
  <div>
    <h1>对话工作台</h1>
    <p class="muted">
      基于授权文档与仓库开展问答、对比和进展总结，保留每轮证据与运行记录。
    </p>
    <p v-if="initialError" class="error" role="alert">
      {{ apiError(initialError).message }}
    </p>
    <p v-if="message" role="status" class="notice">{{ message }}</p>
    <div class="chat-grid">
      <aside class="panel">
        <NuxtLink :to="`/o/${org}/chat`">新建会话</NuxtLink>
        <form @submit.prevent="list(false)">
          <label>搜索会话<input v-model="search" maxlength="200" /></label
          ><button>搜索</button>
        </form>
        <p v-if="!conversations.length">暂无会话。</p>
        <ul>
          <li v-for="item in conversations" :key="item.id">
            <NuxtLink :to="`/o/${org}/chat/${item.id}`">{{
              item.title
            }}</NuxtLink>
            <p class="small muted">
              {{ item.agentName }} ·
              {{ item.activeRunId ? '进行中' : '已保存' }}
            </p>
          </li>
        </ul>
        <button v-if="cursor" @click="list(true)">更多会话</button>
      </aside>
      <div>
        <form
          v-if="conversationId && initial?.conversation"
          class="inline"
          @submit.prevent="rename"
        >
          <label
            >会话标题<input v-model="title" maxlength="100" required /></label
          ><button :disabled="busy">重命名</button>
        </form>
        <details v-if="messages.length" class="panel">
          <summary>会话历史（{{ messages.length }} 条已加载）</summary>
          <button v-if="messageCursor" @click="older">更早消息</button>
          <article v-for="item in history" :key="item.id">
            <strong>{{
              item.role === 'user'
                ? '你'
                : item.isPartial
                  ? '助手 · 未完成'
                  : '助手'
            }}</strong>
            <p class="history-text">
              {{
                item.resultVisibility === 'withheld'
                  ? '资料授权已撤销，整段历史结果暂不可查看。'
                  : item.content
              }}
            </p>
            <button
              v-if="item.role === 'assistant'"
              @click="selectedRun = item.runId"
            >
              查看此轮来源与结果
            </button>
          </article>
        </details>
        <RunView
          v-if="selectedRun"
          :key="selectedRun"
          :org="org"
          :run-id="selectedRun"
          editable
          @settled="updateConversation"
          @retry="prepareRetry"
        />
        <form class="panel" @submit.prevent="submit">
          <h2>
            {{
              conversationId
                ? '继续追问'
                : taskType === 'compare'
                  ? '开始需求与方案对比'
                  : taskType === 'progress'
                    ? '开始项目进展与风险总结'
                    : '开始文档问答'
            }}
          </h2>
          <p v-if="retryOfRunId" class="notice">
            将创建关联
            {{ retryOfRunId }} 的新运行，使用当前已保存配置，重新计算预算。
            <button
              type="button"
              :disabled="busy || Boolean(pending)"
              @click="retryOfRunId = undefined"
            >
              改为普通追问
            </button>
          </p>
          <label
            >任务类型<select :value="taskType" disabled>
              <option value="qa">文档问答</option>
              <option value="compare">需求与方案对比</option>
              <option value="progress">项目进展与风险总结</option>
            </select></label
          >
          <p class="small muted">
            选择对应类型的 Agent 开始任务。更换 Agent 或任务类型请新建会话。
          </p>
          <label
            >Agent<select
              v-model="agentId"
              aria-label="Agent"
              :disabled="
                !interactive ||
                Boolean(conversationId) ||
                busy ||
                Boolean(pending)
              "
              required
            >
              <option value="">选择可用 Agent</option>
              <option v-for="agent in usable" :key="agent.id" :value="agent.id">
                {{ agent.name }}
              </option>
              <option
                v-if="conversationId && !usable.some((a) => a.id === agentId)"
                :value="agentId"
              >
                {{ initial?.conversation?.agentName }}（请检查可用状态）
              </option>
            </select></label
          >
          <p v-if="!usable.length">
            暂无可运行的 Agent，请管理员配置模型、资料并启用 Agent。
          </p>
          <p v-if="loadingOptions">正在核对可用资料…</p>
          <template v-if="taskOptions"
            ><p class="small">
              将使用 Agent 第
              {{ taskOptions.versionNumber }}
              版；提交时以最新保存配置和当前权限为准。
            </p>
            <fieldset
              v-if="taskType === 'qa' || taskType === 'progress'"
              :disabled="busy || Boolean(pending)"
            >
              <legend>知识范围（追问请明确选择）</legend>
              <label
                v-for="kb in taskOptions.knowledgeBases"
                :key="kb.id"
                class="check-label"
                ><input v-model="kbIds" type="checkbox" :value="kb.id" />{{
                  kb.name
                }}</label
              >
            </fieldset>
            <fieldset
              v-if="taskType === 'progress'"
              :disabled="busy || Boolean(pending)"
            >
              <legend>单仓库与观察日期</legend>
              <label
                >授权仓库<select v-model="repoId" required>
                  <option value="">请选择一个仓库</option>
                  <option
                    v-for="repo in taskOptions.repositories"
                    :key="repo.id"
                    :value="repo.id"
                  >
                    {{ repo.owner }}/{{ repo.repo }}
                  </option>
                </select></label
              >
              <p v-if="!taskOptions.repositories?.length" class="notice">
                暂无可用授权仓库，请管理员检查 GitHub 连接与仓库共享。
              </p>
              <label
                >开始日期<input v-model="startDate" type="date" required
              /></label>
              <label
                >结束日期<input v-model="endDate" type="date" required
              /></label>
              <label
                >时区（IANA）<input
                  v-model="timezone"
                  placeholder="Asia/Shanghai"
                  required
                  list="progress-timezones"
              /></label>
              <datalist id="progress-timezones">
                <option value="Asia/Shanghai"></option>
                <option value="UTC"></option>
                <option value="America/New_York"></option>
                <option value="Europe/London"></option>
              </datalist>
              <p v-if="dateError" role="status" class="error">
                {{ dateError }}
              </p>
              <p class="small muted">
                包含开始日和结束日，按所选时区换算
                UTC。仅总结该日期范围内更新且当前可见的 Issue/PR
                样本，不代表完整历史。PR 合并不等于部署或验收。
              </p>
            </fieldset>
            <details v-if="taskType === 'qa'">
              <summary>限定文档（未选时使用所选库中所有就绪文档）</summary>
              <label
                v-for="doc in visibleDocuments"
                :key="doc.id"
                class="check-label"
                ><input
                  v-model="documentIds"
                  type="checkbox"
                  :value="doc.id"
                  :disabled="busy || Boolean(pending)"
                />{{ doc.title }} · 处理版本 {{ doc.attemptId }}</label
              >
            </details>
            <fieldset
              v-if="taskType === 'compare'"
              :disabled="busy || Boolean(pending)"
            >
              <legend>选择两份不同的就绪文档</legend>
              <label
                >需求文档<select v-model="requirementDocumentId" required>
                  <option value="">请选择需求文档</option>
                  <option
                    v-for="doc in taskOptions.documents"
                    :key="doc.id"
                    :value="doc.id"
                  >
                    {{ doc.title }} · {{ doc.attemptId }}
                  </option>
                </select></label
              >
              <label
                >方案文档<select v-model="solutionDocumentId" required>
                  <option value="">请选择方案文档</option>
                  <option
                    v-for="doc in taskOptions.documents"
                    :key="doc.id"
                    :value="doc.id"
                    :disabled="doc.id === requirementDocumentId"
                  >
                    {{ doc.title }} · {{ doc.attemptId }}
                  </option>
                </select></label
              >
              <p>
                双侧分别检索，仅对本次取回的片段作比较；未检索到不代表全文没有，不能据此认定完全满足。
              </p>
            </fieldset>
            <p class="small muted">
              最多 {{ taskOptions.limits.max_run_steps }} 步 /
              {{ taskOptions.limits.max_tool_attempts }} 次工具尝试 /
              {{ taskOptions.limits.run_deadline_seconds }} 秒 / 聊天
              {{ taskOptions.limits.max_run_tokens }}
              Token。检索与重排用量单独记录。
            </p>
          </template>
          <label
            >问题<textarea
              v-model="question"
              maxlength="4000"
              required
              rows="4"
              :disabled="busy || Boolean(pending)"
            ></textarea>
          </label>
          <p v-if="activeRun" role="status">
            当前会话正在执行，可编辑草稿或切换会话。刷新与断网会恢复原运行。
          </p>
          <button
            v-if="pending"
            type="button"
            :disabled="busy"
            @click="reconcile"
          >
            {{ busy ? '正在核对…' : '核对原提交' }}
          </button>
          <button
            v-else
            :disabled="
              busy ||
              Boolean(activeRun) ||
              !taskOptions ||
              !validScope ||
              !question.trim()
            "
          >
            {{ busy ? '正在接受…' : retryOfRunId ? '创建新重试' : '发送问题' }}
          </button>
          <p class="small muted">
            关闭页面不会取消后台任务。停止以服务端终态为准；失败、取消或超时后可准备新重试。
          </p>
        </form>
      </div>
    </div>
  </div>
</template>
<style scoped>
.chat-grid {
  display: grid;
  grid-template-columns: 240px minmax(0, 1fr);
  gap: 20px;
  align-items: start;
}
.chat-grid > aside {
  position: sticky;
  top: 20px;
}
.chat-grid ul {
  padding-left: 20px;
}
.chat-grid li {
  margin: 16px 0;
  overflow-wrap: anywhere;
}
.check-label {
  display: flex;
  align-items: center;
  gap: 8px;
  overflow-wrap: anywhere;
}
.check-label input {
  width: auto;
}
.history-text {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.chat-grid form > label {
  display: grid;
  gap: 8px;
}
textarea {
  box-sizing: border-box;
  width: 100%;
  min-height: 110px;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 6px;
  font: inherit;
  resize: vertical;
}
fieldset {
  border: 1px solid #d9e0e7;
  border-radius: 8px;
  margin: 12px 0;
}
@media (max-width: 900px) {
  .chat-grid {
    grid-template-columns: 1fr;
  }
  .chat-grid > aside {
    position: static;
  }
}
</style>
