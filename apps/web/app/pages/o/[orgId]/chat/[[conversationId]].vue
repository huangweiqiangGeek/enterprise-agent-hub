<script setup lang="ts">
definePageMeta({ layout: 'workspace' })
const route = useRoute()
</script>
<template>
  <ChatWorkspace
    :key="`${route.params.orgId}:${route.params.conversationId || ''}:${route.params.conversationId ? '' : route.query.taskType || ''}`"
    :org="String(route.params.orgId)"
    :conversation-id="String(route.params.conversationId || '')"
  />
</template>
