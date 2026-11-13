<script setup lang="ts">
definePageMeta({ layout: 'workspace', key: (route) => route.path })
const route = useRoute()
const org = String(route.params.orgId)
const runId = String(route.params.runId)
</script>
<template>
  <div>
    <h1>运行只读详情</h1>
    <p class="muted">此页用于查看和审计，不提供代发、停止或修改操作。</p>
    <NuxtLink :to="`/o/${org}/runs`">返回执行记录</NuxtLink>
    <RunView :key="`${org}:${runId}`" :org="org" :run-id="runId" detailed />
  </div>
</template>
