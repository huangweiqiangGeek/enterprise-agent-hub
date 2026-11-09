<script setup lang="ts">
const props = defineProps<{ mode: 'login' | 'register' }>()
const { $api } = useNuxtApp()
const { refresh, clearOrganization } = useIdentity()
const { busy, message, fields, run } = useFormAction()
const email = ref('')
const password = ref('')
const displayName = ref('')
const showPassword = ref(false)
const registering = computed(() => props.mode === 'register')
async function submit() {
  await run(async () => {
    await $api.request(`/auth/${props.mode}`, {
      method: 'POST',
      body: registering.value
        ? {
            email: email.value,
            password: password.value,
            displayName: displayName.value,
          }
        : { email: email.value, password: password.value },
    })
    password.value = ''
    clearOrganization()
    await refresh()
    await navigateTo('/onboarding')
  })
}
</script>
<template>
  <main class="auth-layout">
    <aside class="auth-story">
      <NuxtLink class="brand" to="/">Enterprise Agent Hub</NuxtLink>
      <div>
        <h1>让团队的知识，<br />成为共同的起点。</h1>
        <p>产品文档、技术方案与项目进展，<br />在同一个团队空间中协作。</p>
      </div>
      <p class="quiet">研发团队的知识与任务助手</p>
    </aside>
    <section class="auth-form" :aria-labelledby="`${mode}-title`">
      <h2 :id="`${mode}-title`">
        {{ registering ? '创建你的账号' : '登录工作空间' }}
      </h2>
      <p class="muted">
        {{
          registering
            ? '注册后创建组织，或由管理员添加到已有团队。'
            : '继续与你的团队协作。'
        }}
      </p>
      <p
        v-if="$route.query.notice === 'password'"
        class="success"
        role="status"
      >
        密码已修改，请使用新密码登录。
      </p>
      <form @submit.prevent="submit">
        <label v-if="registering"
          >显示名<input
            v-model="displayName"
            name="displayName"
            autocomplete="name"
            required
            maxlength="100"
            :aria-invalid="fields.includes('displayName')" /></label
        ><label
          >邮箱<input
            v-model="email"
            name="email"
            type="email"
            autocomplete="email"
            required
            maxlength="254"
            :aria-invalid="fields.includes('email')" /></label
        ><label
          >密码<span class="password-control"
            ><input
              v-model="password"
              name="password"
              :type="showPassword ? 'text' : 'password'"
              :autocomplete="registering ? 'new-password' : 'current-password'"
              required
              minlength="8"
              maxlength="128"
              :aria-invalid="fields.includes('password')"
            /><button
              type="button"
              class="text-button"
              :aria-pressed="showPassword"
              @click="showPassword = !showPassword"
            >
              {{ showPassword ? '隐藏' : '显示' }}
            </button></span
          ></label
        >
        <p v-if="registering" class="muted small">
          密码为 8～128 个字符。邮箱用作登录标识，暂不进行邮件验证。
        </p>
        <p v-if="message" class="error" role="alert" tabindex="-1">
          {{ message }}
        </p>
        <button class="primary full" :disabled="busy">
          {{ busy ? '正在处理…' : registering ? '注册并继续' : '登录' }}
        </button>
      </form>
      <p class="auth-switch">
        {{ registering ? '已有账号？' : '还没有账号？' }}
        <NuxtLink :to="registering ? '/login' : '/register'">{{
          registering ? '去登录' : '创建账号'
        }}</NuxtLink>
      </p>
    </section>
  </main>
</template>
