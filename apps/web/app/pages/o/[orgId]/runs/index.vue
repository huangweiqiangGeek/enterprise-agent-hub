<script setup lang="ts">
import type { components } from '../../../../types/api'
import { apiError } from '../../../../utils/api-error'
definePageMeta({ layout: 'workspace', key: (route) => route.path })
type S = components['schemas']
const org = String(useRoute().params.orgId)
const { $api } = useNuxtApp()
const { organization } = useIdentity()
const canAudit = computed(() =>
  ['owner', 'admin'].includes(organization.value?.effectiveRole || ''),
)
const scope = ref('mine'),
  state = ref(''),
  agent = ref(''),
  start = ref(''),
  end = ref(''),
  timezone = ref('UTC')
const applied = ref('timezone=UTC'),
  cursor = ref<string | null>(null)
const states: Record<string, string> = {
  queued: '排队中',
  running: '执行中',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
  timed_out: '超时',
}
const { data, error, status, refresh, clear } = await useAsyncData(
  `runs:${org}`,
  async () => {
    const [page, summary, agents] = await Promise.all([
      $api.request<S['Page_RunSummary_']>(
        `/runs?${applied.value}${cursor.value ? `&cursor=${encodeURIComponent(cursor.value)}` : ''}`,
        { organizationId: org },
      ),
      $api.request<S['UsageSummary']>(`/usage/summary?${applied.value}`, {
        organizationId: org,
      }),
      $api.request<S['Page_RunAgentOption_']>(
        `/runs/agents?scope=${new URLSearchParams(applied.value).get('scope') || 'mine'}`,
        {
          organizationId: org,
        },
      ),
    ])
    return { page, summary, agents }
  },
  { watch: [applied, cursor] },
)
const agentOptions = computed(() => {
  const options = new Map(
    (data.value?.agents.items || []).map((a) => [a.id, a.name]),
  )
  for (const run of data.value?.page.items || [])
    options.set(run.agentId, run.agentName)
  return [...options].map(([id, name]) => ({ id, name }))
})
function search() {
  cursor.value = null
  applied.value = new URLSearchParams({
    scope: scope.value,
    timezone: timezone.value,
    ...(state.value ? { status: state.value } : {}),
    ...(agent.value ? { agentId: agent.value } : {}),
    ...(start.value ? { startDate: start.value } : {}),
    ...(end.value ? { endDate: end.value } : {}),
  }).toString()
}
function date(value: string) {
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: data.value?.summary.timezone || 'UTC',
    dateStyle: 'short',
    timeStyle: 'medium',
  }).format(new Date(value))
}
function duration(value: number | null | undefined) {
  return value == null ? '暂无' : `${(value / 1000).toFixed(2)} 秒`
}
watch(
  () => organization.value?.accessRevision,
  () => {
    clear()
    scope.value = 'mine'
    cursor.value = null
    search()
    refresh()
  },
)
</script>
<template>
  <div>
    <h1>执行记录与用量</h1>
    <p class="muted">
      按运行创建时间筛选；成功率仅计算终态，不代表答案有用率。查看记录不会重新执行任务。
    </p>
    <form class="inline-form knowledge-filter" @submit.prevent="search">
      <label v-if="canAudit"
        >查看范围<select v-model="scope">
          <option value="mine">我的运行</option>
          <option value="organization">组织审计（只读）</option>
        </select></label
      >
      <label
        >运行状态<select v-model="state">
          <option value="">全部状态</option>
          <option v-for="(label, value) in states" :key="value" :value="value">
            {{ label }}
          </option>
        </select></label
      >
      <label
        >筛选 Agent<select v-model="agent">
          <option value="">全部 Agent</option>
          <option v-for="item in agentOptions" :key="item.id" :value="item.id">
            {{ item.name }}
          </option>
        </select></label
      >
      <label>开始日期<input v-model="start" type="date" /></label
      ><label>结束日期<input v-model="end" type="date" /></label>
      <label
        >时区<input
          v-model="timezone"
          list="timezones"
          required
          maxlength="100"
      /></label>
      <datalist id="timezones">
        <option>UTC</option>
        <option>Asia/Shanghai</option>
        <option>America/New_York</option>
      </datalist>
      <button class="primary">应用筛选</button
      ><button type="button" @click="refresh()">刷新记录</button>
    </form>
    <p v-if="status === 'pending'" role="status">正在读取记录…</p>
    <p v-if="error" class="error" role="alert">
      {{ apiError(error).message }} 请检查日期和时区后重试。
    </p>
    <template v-else-if="data">
      <div class="panel" aria-label="运行统计">
        <h2>
          运行 {{ data.summary.runCount }} 次 · 成功率
          {{
            data.summary.successRate === null
              ? '暂无'
              : `${(data.summary.successRate * 100).toFixed(1)}%`
          }}
        </h2>
        <p>
          <span v-for="(count, value) in data.summary.statusCounts" :key="value"
            >{{ states[value] }} {{ count }}&ensp;</span
          >
        </p>
        <p>
          平均排队 {{ duration(data.summary.queueDuration.avg) }} · 平均执行
          {{ duration(data.summary.executionDuration.avg) }}
        </p>
        <p>
          执行 P50 {{ duration(data.summary.executionDuration.p50) }} · P95
          {{ duration(data.summary.executionDuration.p95) }}（已结束执行
          {{ data.summary.executionDuration.count }} 次）
        </p>
        <p class="small muted">
          时区 {{ data.summary.timezone }} · 查询于
          {{ date(data.summary.observedAt) }}<br />UTC 区间 [{{
            data.summary.rangeUtc.start || '不限'
          }}, {{ data.summary.rangeUtc.endExclusive || '不限' }})
        </p>
        <p class="small">
          列表与统计使用相同筛选；运行变化时两次读取可能相差一个结算，请刷新核对。排队/执行未结束的时长不参与对应均值。
        </p>
      </div>
      <UsageBreakdown :values="data.summary.byKind" />
      <p v-if="!data.page.items.length" class="panel">
        没有符合条件的运行。可调整筛选，或到对话工作台开始任务。
      </p>
      <div v-else class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>创建时间（{{ data.summary.timezone }}）</th>
              <th>Agent / 任务</th>
              <th>发起者</th>
              <th>状态</th>
              <th>排队 / 执行</th>
              <th>聊天用量</th>
              <th>记录</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="run in data.page.items" :key="run.id">
              <td>{{ date(run.createdAt) }}</td>
              <td>
                {{ run.agentName }} · v{{ run.versionNumber }}<br />{{
                  { qa: '文档问答', compare: '需求对比', progress: '进展总结' }[
                    run.taskType
                  ] || run.taskType
                }}
              </td>
              <td>{{ run.initiatorName }}</td>
              <td>{{ states[run.status] }}</td>
              <td>
                {{ duration(run.queueDurationMs) }} /
                {{ duration(run.executionDurationMs) }}
              </td>
              <td>
                {{
                  run.chat.actualUsageCount
                    ? `${run.chat.inputTokens + run.chat.outputTokens} 实测 Token`
                    : '暂无实测'
                }}<br />估算 {{ run.chat.estimatedTokens }} · 不可得
                {{ run.chat.unavailableUsageCount }} 次 · 结果未知
                {{ run.chat.unknownOutcomeCount }} 次
              </td>
              <td>
                <NuxtLink :to="`/o/${org}/runs/${run.id}`">查看详情</NuxtLink>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="actions">
        <button v-if="cursor" @click="cursor = null">返回第一页</button
        ><button
          v-if="data.page.nextCursor"
          @click="cursor = data.page.nextCursor"
        >
          下一页
        </button>
      </div>
    </template>
  </div>
</template>
<style scoped>
.table-scroll {
  overflow-x: auto;
  margin-top: 20px;
}
th,
td {
  text-align: left;
  padding: 12px;
  border-bottom: 1px solid #dbe2ea;
}
</style>
