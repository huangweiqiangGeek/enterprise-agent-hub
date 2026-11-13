<script setup lang="ts">
import type { components } from '../types/api'
defineProps<{ values: Record<string, components['schemas']['UsageTotals']> }>()
</script>
<template>
  <div class="usage-grid">
    <section v-for="(value, kind) in values" :key="kind" class="panel">
      <h3>
        {{
          { chat: '聊天', embedding: '向量检索', rerank: '重排' }[kind] || kind
        }}
      </h3>
      <p>调用 {{ value.attemptCount }} 次 · 失败 {{ value.failedCount }} 次</p>
      <p v-if="kind === 'chat'">
        实测输入 / 输出：{{
          value.actualUsageCount
            ? `${value.inputTokens} / ${value.outputTokens} Token`
            : '暂无实测'
        }}
      </p>
      <p v-else>
        实测输入：{{
          value.actualUsageCount
            ? `${value.inputTokens} Token`
            : '暂无实测 Token'
        }}
      </p>
      <p>
        保守估算：{{
          value.estimatedUsageCount
            ? `${value.estimatedTokens} Token（${value.estimatedUsageCount} 次）`
            : '无估算记录'
        }}
      </p>
      <p>
        Token 不可得 {{ value.unavailableUsageCount }} 次 · 结果未知
        {{ value.unknownOutcomeCount }} 次 · 待结算 {{ value.pendingCount }} 次
      </p>
      <p v-for="(amount, unit) in value.billedUnits" :key="unit">
        提供方计量 {{ unit }}：{{ amount }}（独立单位）
      </p>
    </section>
  </div>
</template>
<style scoped>
.usage-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 230px), 1fr));
  gap: 12px;
}
</style>
