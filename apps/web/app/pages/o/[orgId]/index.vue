<script setup lang="ts">
import type { components } from '../../../types/api'
import { apiError, roleLabel } from '../../../utils/api-error'

definePageMeta({ layout: 'workspace', key: (route) => route.path })
type S = components['schemas']
const org = String(useRoute().params.orgId)
const { $api } = useNuxtApp()
const { organization, session } = useIdentity()
const controller = new AbortController()
const mounted = ref(false)
const { data, error, status, refresh, clear } = await useAsyncData(
  `dashboard:${session.value?.user?.id}:${org}`,
  () =>
    $api.request<S['DashboardView']>('/dashboard', {
      organizationId: org,
      signal: controller.signal,
    }),
)
const currentOrg = computed(() =>
  organization.value?.id === org ? organization.value : null,
)
const dashboard = computed(() =>
  currentOrg.value && data.value?.organizationId === org ? data.value : null,
)
const cards = [
  {
    taskType: 'qa',
    name: '文档问答',
    input: '问题与就绪文档',
    output: '带引用的回答与资料不足说明',
  },
  {
    taskType: 'compare',
    name: '需求与方案对比',
    input: '两份不同的就绪文档',
    output: '逐项对比与双侧证据',
  },
  {
    taskType: 'progress',
    name: '进展与风险总结',
    input: '授权仓库、日期时区与计划资料',
    output: '进展、风险和待核实事项',
  },
] as const
const states: Record<string, string> = {
  queued: '排队中',
  running: '运行中',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
  timed_out: '超时',
}
function observed(value: string) {
  return new Date(value).toISOString().slice(0, 16).replace('T', ' ') + ' UTC'
}
async function reload() {
  if (!currentOrg.value || status.value === 'pending') return
  clear()
  await refresh()
}
function focused() {
  if (!document.hidden) void reload()
}
watch(
  () => organization.value?.id,
  (value) => {
    if (value !== org) clear()
  },
  { flush: 'sync' },
)
onMounted(() => {
  mounted.value = true
  window.addEventListener('focus', focused)
})
onBeforeUnmount(() => {
  controller.abort()
  window.removeEventListener('focus', focused)
})
</script>

<template>
  <div class="dashboard" data-testid="dashboard">
    <header class="dashboard-heading">
      <div>
        <h1>{{ currentOrg?.name || '工作台' }}</h1>
        <p class="muted">
          {{
            currentOrg
              ? roleLabel(currentOrg.effectiveRole) + '工作台'
              : '正在切换组织'
          }}。选择任务，或接着处理自己的会话。
        </p>
      </div>
      <button
        class="secondary"
        :disabled="!mounted || status === 'pending' || !currentOrg"
        @click="reload"
      >
        {{ status === 'pending' ? '正在读取…' : '刷新工作台' }}
      </button>
    </header>
    <p v-if="error && currentOrg" role="alert" class="error">
      工作台信息读取失败，准备情况和最近会话暂不可确认。{{
        apiError(error).message
      }}<button class="text-button" @click="reload">重试读取</button>
    </p>
    <p v-else-if="!dashboard" role="status" class="muted">
      正在读取当前组织的准备情况和个人会话…
    </p>
    <section aria-labelledby="task-heading">
      <h2 id="task-heading">选择任务</h2>
      <p class="small muted">
        选择入口只预选模板，不创建会话或调用模型。填写并发送后才开始运行。
      </p>
      <div class="task-grid">
        <article
          v-for="card in cards"
          :key="card.taskType"
          :data-testid="`task-${card.taskType}`"
        >
          <h3>{{ card.name }}</h3>
          <dl>
            <dt>准备</dt>
            <dd>
              {{
                dashboard?.templates.find((t) => t.taskType === card.taskType)
                  ?.inputDescription || card.input
              }}
            </dd>
            <dt>得到</dt>
            <dd>
              {{
                dashboard?.templates.find((t) => t.taskType === card.taskType)
                  ?.outputDescription || card.output
              }}
            </dd>
          </dl>
          <NuxtLink :to="`/o/${org}/chat?taskType=${card.taskType}`"
            >选择{{ card.name }}</NuxtLink
          >
        </article>
      </div>
    </section>
    <div class="dashboard-lower">
      <section
        aria-labelledby="recent-heading"
        data-testid="recent-conversations"
      >
        <div class="section-heading">
          <h2 id="recent-heading">最近会话</h2>
          <NuxtLink :to="`/o/${org}/chat`">查看全部会话</NuxtLink>
        </div>
        <p class="small muted">仅显示你在当前组织的最近会话，最多5个。</p>
        <template v-if="dashboard">
          <ul v-if="dashboard.recentConversations.length" class="recent-list">
            <li
              v-for="conversation in dashboard.recentConversations"
              :key="conversation.id"
            >
              <NuxtLink :to="`/o/${org}/chat/${conversation.id}`">{{
                conversation.title
              }}</NuxtLink>
              <span class="badge">{{
                conversation.lastRunStatus
                  ? states[conversation.lastRunStatus]
                  : '尚未运行'
              }}</span>
              <p class="small muted">
                {{ conversation.agentName }}<br /><time
                  :datetime="conversation.updatedAt"
                  >{{ observed(conversation.updatedAt) }}</time
                >
              </p>
            </li>
          </ul>
          <div v-else class="first-use">
            <p>你在此组织还没有会话。</p>
            <p>
              首次使用先准备模型与资料，再配置并启用对应的
              Agent，最后选择上方任务。
            </p>
            <p class="small muted">
              {{
                dashboard.effectiveRole === 'member'
                  ? '请联系管理员补齐准备情况列出的条件。'
                  : '展开对应任务的准备情况，可直接进入缺失条件对应页面。'
              }}
            </p>
          </div>
        </template>
        <p v-else class="muted">
          {{ error ? '暂时无法读取会话，请重试。' : '正在读取个人会话…' }}
        </p>
      </section>
      <section
        aria-labelledby="readiness-heading"
        data-testid="dashboard-readiness"
      >
        <h2 id="readiness-heading">准备情况</h2>
        <p class="small muted">
          仅核对当前已保存配置，不保证外部服务可用；运行前会再次检查。
        </p>
        <template v-if="dashboard">
          <details
            v-for="item in dashboard.templates"
            :key="item.taskType"
            :data-testid="`readiness-${item.taskType}`"
            class="readiness-item"
          >
            <summary>
              {{ item.name
              }}<span class="small" :class="{ ready: item.ready }">{{
                item.ready ? '可以填写任务' : `${item.issues.length} 项待准备`
              }}</span>
            </summary>
            <p v-if="item.ready">
              {{ item.readyAgentCount }} 个已启用 Agent 满足当前准备条件。
            </p>
            <ul v-else>
              <li v-for="issue in item.issues" :key="issue.code">
                <p>{{ issue.message }}</p>
                <NuxtLink
                  v-if="issue.actionPath?.startsWith(`/o/${org}/`)"
                  :to="issue.actionPath"
                  >{{ issue.actionLabel }}</NuxtLink
                ><span v-else class="small muted">联系管理员</span>
              </li>
            </ul>
          </details>
          <p class="small muted">
            核对时间：{{ observed(dashboard.observedAt) }}
          </p>
        </template>
        <p v-else class="muted">
          {{
            error ? '准备情况未知，不能视为已准备完成。' : '正在核对准备条件…'
          }}
        </p>
      </section>
    </div>
  </div>
</template>

<style scoped>
.dashboard {
  max-width: 1100px;
  margin-inline: auto;
}
.dashboard h1 {
  font-size: 26px;
  margin: 0;
}
.dashboard h2 {
  font-size: 20px;
  margin: 0;
}
.dashboard h3 {
  font-size: 18px;
  margin: 0 0 16px;
}
.dashboard-heading,
.section-heading {
  display: flex;
  justify-content: space-between;
  align-items: start;
  gap: 16px;
}
.dashboard-heading {
  margin-bottom: 28px;
}
.dashboard-heading button {
  flex: none;
}
/* Three peers span 4+4+4 columns, followed by 7+5; both rows share the full canvas. */
.task-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
}
.task-grid article {
  background: #fff;
  border: 1px solid #e2e9e4;
  border-radius: 8px;
  padding: 20px;
  display: flex;
  flex-direction: column;
}
.task-grid dl {
  margin: 0 0 20px;
  flex: 1;
}
.task-grid dt {
  font-size: 13px;
  color: #52665f;
}
.task-grid dd {
  margin: 4px 0 12px;
}
.task-grid a {
  align-self: start;
  min-height: 32px;
}
.dashboard-lower {
  display: grid;
  grid-template-columns: minmax(0, 7fr) minmax(0, 5fr);
  gap: 32px;
  margin-top: 32px;
}
.recent-list {
  list-style: none;
  padding: 0;
  margin: 0;
}
.recent-list li {
  padding-block: 16px;
  border-bottom: 1px solid var(--line);
  overflow-wrap: anywhere;
}
.recent-list a {
  font-weight: 600;
}
.recent-list .badge {
  margin-left: 10px;
}
.recent-list p {
  margin: 8px 0 0;
}
.readiness-item {
  border-bottom: 1px solid var(--line);
  padding-block: 16px;
}
.readiness-item summary {
  cursor: pointer;
}
.readiness-item summary span {
  display: block;
  margin: 6px 0 0 20px;
}
.readiness-item ul {
  padding-left: 20px;
}
.readiness-item li {
  margin-bottom: 16px;
}
.readiness-item li p {
  margin-bottom: 4px;
}
.ready {
  color: var(--accent);
}
.first-use {
  padding: 16px 20px;
  background: #fff;
  border: 1px solid #e2e9e4;
  border-radius: 8px;
}
@media (max-width: 1000px) {
  .task-grid {
    grid-template-columns: 1fr;
  }
  .task-grid article {
    display: block;
  }
  .task-grid dl {
    margin-bottom: 10px;
  }
  .dashboard-lower {
    grid-template-columns: 1fr;
  }
}
@media (max-width: 600px) {
  .dashboard-heading {
    flex-direction: column;
  }
  .section-heading {
    align-items: center;
  }
  .dashboard h1 {
    font-size: 24px;
  }
  .task-grid a {
    display: inline-flex;
    align-items: center;
    min-height: 44px;
  }
}
</style>
