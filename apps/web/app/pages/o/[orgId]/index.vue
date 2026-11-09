<script setup lang="ts">
import { roleLabel } from '../../../utils/api-error'
definePageMeta({ layout: 'workspace' })
const { organization } = useIdentity()
</script>
<template>
  <div v-if="organization">
    <div class="page-intro">
      <h1>{{ organization.name }}</h1>
      <p class="muted">
        组织空间已建立，你的角色是{{ roleLabel(organization.effectiveRole) }}。
      </p>
    </div>
    <section class="welcome-panel">
      <h2>从团队协作开始</h2>
      <p>在团队成员页查看组织成员。所有者和管理员可以添加已注册账号。</p>
      <NuxtLink
        class="primary button-link"
        :to="`/o/${organization.id}/settings/team`"
        >查看团队成员</NuxtLink
      >
    </section>
    <section class="section-note">
      <h2>接下来可用的能力</h2>
      <p class="muted">
        管理员可配置模型、上传并检索知识库资料，以及管理 Agent 配置版本。 Agent
        任务执行尚未开放。
      </p>
      <NuxtLink :to="`/o/${organization.id}/settings/models`"
        >查看模型配置</NuxtLink
      >
    </section>
  </div>
</template>
