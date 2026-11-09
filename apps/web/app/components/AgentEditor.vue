<script setup lang="ts">
import type { components } from '../types/api'
import { apiError } from '../utils/api-error'

type Config = components['schemas']['AgentConfig']
type Form = Required<components['schemas']['AgentWrite']>
const props = defineProps<{ organizationId: string; initial?: Config }>()
const emit = defineEmits<{ saved: [config: Config] }>()
const { $api } = useNuxtApp()
const { organization } = useIdentity()
const canManage = computed(() =>
  ['owner', 'admin'].includes(organization.value?.effectiveRole || ''),
)
const blank = (): Form => ({
  name: '',
  description: '',
  taskType: 'qa',
  prompt: '',
  knowledgeBaseIds: [],
  toolKeys: [],
  chatConnectionId: null,
  enabled: false,
})
const form = ref<Form>(blank())
const saved = ref<Config | null>(null)
const snapshot = ref('')
const template = ref('')
const cloneName = ref('')
const cloning = ref(false)
const conflict = ref(false)
const message = ref('')
const busy = ref(false)
const interactive = ref(false)
const controller = new AbortController()
let createKey = '',
  submitted = ''
let cloneKey = '',
  cloneSubmitted = ''
const dirty = computed(() => JSON.stringify(form.value) !== snapshot.value)
function accept(value?: Config) {
  saved.value = value || null
  form.value = value
    ? {
        name: value.name,
        description: value.description,
        taskType: value.taskType,
        prompt: value.prompt,
        knowledgeBaseIds: [...value.knowledgeBaseIds],
        toolKeys: [...value.toolKeys],
        chatConnectionId: value.chatConnectionId,
        enabled: value.enabled,
      }
    : blank()
  snapshot.value = JSON.stringify(form.value)
  conflict.value = false
}
accept(props.initial)
watch(
  () => props.initial,
  (value) => {
    if (value?.revision === saved.value?.revision) return
    if (dirty.value) {
      conflict.value = true
      return
    }
    accept(value)
  },
)
watch(canManage, (value) => {
  if (!value) accept()
})
const {
  data: choices,
  error: choicesError,
  refresh: refreshChoices,
} = await useAsyncData(`agent-options:${props.organizationId}`, async () => {
  const [catalog, knowledge, models] = await Promise.all([
    $api.request<components['schemas']['AgentOptions']>('/agents/options', {
      organizationId: props.organizationId,
    }),
    $api.request<components['schemas']['Page_KnowledgeDTO_']>(
      '/knowledge-bases?limit=100',
      { organizationId: props.organizationId },
    ),
    $api.request<components['schemas']['ModelSettingsView']>(
      '/settings/models',
      { organizationId: props.organizationId },
    ),
  ])
  return { catalog, knowledge, models }
})
async function moreKnowledge() {
  if (!choices.value?.knowledge.nextCursor) return
  const next = await $api.request<components['schemas']['Page_KnowledgeDTO_']>(
    `/knowledge-bases?limit=100&cursor=${encodeURIComponent(choices.value.knowledge.nextCursor)}`,
    { organizationId: props.organizationId, signal: controller.signal },
  )
  choices.value.knowledge.items.push(...next.items)
  choices.value.knowledge.nextCursor = next.nextCursor
}
function applyTemplate() {
  const value = choices.value?.catalog.templates.find(
    (t) => t.taskType === template.value,
  )
  if (!value) return
  form.value.taskType = value.taskType
  form.value.name = value.name
  form.value.description = value.description
  form.value.prompt = value.prompt
  form.value.toolKeys = [...value.toolKeys]
}
async function action(work: () => Promise<void>) {
  if (busy.value) return
  busy.value = true
  message.value = ''
  try {
    await work()
  } catch (error) {
    const failure = apiError(error)
    if (failure.code !== 'ABORTED') message.value = failure.message
    if (failure.code === 'VERSION_CONFLICT') conflict.value = true
  } finally {
    busy.value = false
  }
}
async function save() {
  await action(async () => {
    const serialized = JSON.stringify(form.value)
    if (serialized !== submitted) {
      createKey = crypto.randomUUID()
      submitted = serialized
    }
    const value = await $api.request<Config>(
      saved.value ? `/agents/${saved.value.id}` : '/agents',
      {
        method: saved.value ? 'PUT' : 'POST',
        organizationId: props.organizationId,
        key: createKey,
        body: {
          ...form.value,
          ...(saved.value ? { expectedRevision: saved.value.revision } : {}),
        },
        signal: controller.signal,
      },
    )
    accept(value)
    emit('saved', value)
    message.value = `已保存第 ${value.versionNumber} 版配置。`
  })
}
async function reload() {
  if (
    !saved.value ||
    (dirty.value && !window.confirm('放弃未保存修改并重新读取配置？'))
  )
    return
  await action(async () => {
    const value = await $api.request<Config>(`/agents/${saved.value?.id}`, {
      organizationId: props.organizationId,
      signal: controller.signal,
    })
    accept(value)
    emit('saved', value)
  })
}
async function changeStatus() {
  if (!saved.value || dirty.value) return
  if (
    saved.value.enabled &&
    !window.confirm(
      '停用后不能发起新任务；在途任务将在下个授权检查点停止，历史记录保留。当前任务执行功能尚未开放。确认停用？',
    )
  )
    return
  await action(async () => {
    const current = saved.value
    if (!current) return
    await $api.request<components['schemas']['AgentSummary']>(
      `/agents/${current.id}/status`,
      {
        method: 'PATCH',
        organizationId: props.organizationId,
        body: { enabled: !current.enabled, expectedRevision: current.revision },
        signal: controller.signal,
      },
    )
    const value = await $api.request<Config>(`/agents/${current.id}`, {
      organizationId: props.organizationId,
      signal: controller.signal,
    })
    accept(value)
    emit('saved', value)
  })
}
async function clone() {
  if (!saved.value || dirty.value) return
  await action(async () => {
    if (cloneName.value !== cloneSubmitted) {
      cloneKey = crypto.randomUUID()
      cloneSubmitted = cloneName.value
    }
    const value = await $api.request<Config>(
      `/agents/${saved.value?.id}/clone`,
      {
        method: 'POST',
        organizationId: props.organizationId,
        key: cloneKey,
        body: { newName: cloneName.value },
        signal: controller.signal,
      },
    )
    cloning.value = false
    await navigateTo(`/o/${props.organizationId}/agents/${value.id}?copied=1`)
  })
}
/** Leave guards preserve unsaved edits; permission revocation always clears protected state. */
function leave() {
  return (
    !canManage.value ||
    !dirty.value ||
    window.confirm('有未保存修改。放弃修改并离开？')
  )
}
onBeforeRouteLeave(leave)
function beforeUnload(event: BeforeUnloadEvent) {
  if (dirty.value && canManage.value) {
    event.preventDefault()
    event.returnValue = ''
  }
}
onMounted(() => {
  interactive.value = true
  window.addEventListener('beforeunload', beforeUnload)
})
onBeforeUnmount(() => {
  controller.abort()
  window.removeEventListener('beforeunload', beforeUnload)
})
</script>

<template>
  <div v-if="canManage">
    <p v-if="message" role="alert" class="notice">{{ message }}</p>
    <p v-if="conflict" role="alert" class="error">
      配置已被更新。当前输入已保留；重载后再应用修改。<button
        class="text-button"
        :disabled="busy"
        @click="reload"
      >
        重新读取配置
      </button>
    </p>
    <p v-if="choicesError" class="error" role="alert">
      配置选项读取失败。<button class="text-button" @click="refreshChoices()">
        重试读取选项
      </button>
    </p>
    <p v-if="$route.query.copied" class="notice">
      副本已保存为停用配置；仅保留仍可用的资源引用，请重新核对知识范围、工具和模型。
    </p>
    <form class="panel knowledge-form agent-form" @submit.prevent="save">
      <fieldset class="editor-fields" :disabled="busy || !interactive">
        <h2>基本信息</h2>
        <label v-if="!saved"
          >预置模板<select v-model="template" @change="applyTemplate">
            <option value="">空白配置</option>
            <option
              v-for="item in choices?.catalog.templates"
              :key="item.taskType"
              :value="item.taskType"
            >
              {{ item.name }}
            </option>
          </select></label
        >
        <label
          >Agent名称<input v-model="form.name" required maxlength="100"
        /></label>
        <label
          >说明<textarea
            v-model="form.description"
            maxlength="2000"
            rows="2"
          ></textarea>
        </label>
        <label
          >任务类型<select v-model="form.taskType" :disabled="!!saved">
            <option value="qa">文档问答</option>
            <option value="compare">需求与方案对比</option>
            <option value="progress">进展与风险总结</option>
          </select></label
        >
        <p class="small muted">创建后任务类型固定。保存配置不会执行任务。</p>
        <h2 id="prompt">提示词</h2>
        <label
          >任务提示词<textarea
            v-model="form.prompt"
            maxlength="20000"
            rows="7"
          ></textarea>
        </label>
        <p class="small muted">提示词不能改变资料授权、只读工具和执行上限。</p>
        <h2 id="knowledgeBaseIds">知识范围</h2>
        <fieldset>
          <legend>绑定知识库</legend>
          <label
            v-for="kb in choices?.knowledge.items"
            :key="kb.id"
            class="choice"
            ><input
              v-model="form.knowledgeBaseIds"
              type="checkbox"
              :value="kb.id"
            />{{ kb.name }} · {{ kb.readyCount }} 份已就绪</label
          >
          <p v-if="choices && !choices.knowledge.items.length" class="muted">
            暂无知识库，可先保存停用配置。
          </p>
          <button
            v-if="choices?.knowledge.nextCursor"
            type="button"
            class="text-button"
            :disabled="busy"
            @click="action(moreKnowledge)"
          >
            加载更多知识库
          </button>
        </fieldset>
        <NuxtLink :to="`/o/${organizationId}/knowledge`">管理知识库</NuxtLink>
        <h2 id="toolKeys">允许工具</h2>
        <fieldset>
          <legend>只读工具</legend>
          <label
            v-for="tool in choices?.catalog.tools"
            :key="tool.key"
            class="choice"
            ><input
              v-model="form.toolKeys"
              type="checkbox"
              :value="tool.key"
            /><span
              >{{ tool.name }}{{ !tool.available ? '（尚未接入）' : ''
              }}<small>{{ tool.note }}</small></span
            ></label
          >
        </fieldset>
        <p class="small muted">
          所有任务必需知识库检索；进展总结还必需查询Issue和Pull
          Request。当前GitHub工具尚不可用。
        </p>
        <h2 id="chatConnectionId">聊天模型</h2>
        <label
          >模型连接<select v-model="form.chatConnectionId">
            <option :value="null">暂不选择</option>
            <option
              v-for="model in choices?.models.items.filter(
                (m) => m.kind === 'chat',
              )"
              :key="model.id"
              :value="model.id"
            >
              {{ model.name }}
            </option>
          </select></label
        >
        <NuxtLink :to="`/o/${organizationId}/settings/models`"
          >管理模型配置</NuxtLink
        >
        <p v-if="dirty" role="status" class="notice">
          有未保存修改；当前生效配置尚未改变。
        </p>
        <button class="primary" :disabled="busy || !form.name.trim()">
          {{ busy ? '正在处理…' : saved ? '保存新版本' : '保存停用配置' }}
        </button>
      </fieldset>
    </form>
    <section
      v-if="saved && initial"
      class="panel agent-actions"
      aria-label="已保存配置"
    >
      <h2>
        已保存的第 {{ saved.versionNumber }} 版 ·
        {{ saved.enabled ? '已启用' : '已停用' }}
      </h2>
      <p>以下条件基于已保存配置。资料或模型状态变化后，请重新读取核对。</p>
      <ul v-if="saved.readiness.length">
        <li v-for="issue in saved.readiness" :key="issue.code">
          <a :href="`#${issue.field}`">{{ issue.message }}</a>
        </li>
      </ul>
      <p v-else class="notice">配置条件已满足，可以启用。</p>
      <div class="row-actions">
        <button
          class="secondary"
          :disabled="busy || dirty || !interactive"
          @click="changeStatus"
        >
          {{ saved.enabled ? '停用Agent' : '启用Agent' }}</button
        ><button
          class="secondary"
          :disabled="busy || dirty || !interactive"
          @click="cloning = !cloning"
        >
          复制Agent</button
        ><button
          class="text-button"
          :disabled="busy || !interactive"
          @click="reload"
        >
          刷新已保存配置
        </button>
      </div>
      <form v-if="cloning" class="knowledge-form" @submit.prevent="clone">
        <label
          >副本名称<input
            v-model="cloneName"
            :disabled="busy"
            required
            maxlength="100"
        /></label>
        <p class="small muted">
          复制已保存版本，副本始终停用；不可用引用会剔除，不复制凭据。
        </p>
        <button class="primary" :disabled="busy || !cloneName.trim()">
          确认复制
        </button>
      </form>
      <button class="secondary" disabled>试运行（尚未开放）</button>
      <p class="small muted">
        试运行将使用已保存的第
        {{ saved.versionNumber }} 版并计入用量。会话和运行功能开放后可使用。
      </p>
    </section>
  </div>
</template>

<style scoped>
.editor-fields {
  display: grid;
  gap: 1rem;
  border: 0;
  padding: 0;
  margin: 0;
}
.agent-form h2 {
  margin: 1rem 0 0;
}
fieldset {
  border: 1px solid var(--border);
  border-radius: 0.5rem;
  padding: 1rem;
  min-width: 0;
}
.choice {
  display: flex;
  align-items: flex-start;
  gap: 0.65rem;
  margin-block: 0.75rem;
}
.choice input {
  width: auto;
  margin-top: 0.3rem;
  flex: 0 0 auto;
}
.choice small {
  display: block;
  color: var(--muted);
  margin-top: 0.25rem;
}
.agent-actions {
  margin-top: 1.5rem;
}
.agent-actions > button {
  margin-top: 1.25rem;
}
</style>
