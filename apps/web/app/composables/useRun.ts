import type { components } from '../types/api'
import { apiError } from '../utils/api-error'
import { RunStream, SSEParser, terminal } from '../utils/run-stream'

type Snapshot = components['schemas']['RunSnapshot']
export function useRun(org: string, runId: Ref<string>) {
  const { $api } = useNuxtApp()
  const { organization } = useIdentity()
  const snapshot = ref<Snapshot | null>(null)
  const draft = ref('')
  const connection = ref('')
  const error = ref('')
  let generation = 0
  let controller: AbortController | undefined
  let retry: ReturnType<typeof setTimeout> | undefined
  let retries = 0

  function stop(clear = true) {
    generation++
    controller?.abort()
    clearTimeout(retry)
    if (clear) {
      snapshot.value = null
      draft.value = ''
      error.value = ''
    }
  }
  async function recover() {
    stop(false)
    if (!runId.value || organization.value?.id !== org) return
    const id = runId.value,
      epoch = generation
    controller = new AbortController()
    const signal = controller.signal
    const valid = () =>
      epoch === generation &&
      !signal.aborted &&
      runId.value === id &&
      organization.value?.id === org
    connection.value = '正在同步运行'
    try {
      const current = await $api.request<Snapshot>(`/runs/${id}`, {
        organizationId: org,
        signal,
      })
      if (!valid()) return
      snapshot.value = current
      draft.value = current.partialMessage || ''
      error.value = ''
      if (terminal(current.status)) {
        draft.value = ''
        connection.value = '已同步最终结果'
        retries = 0
        return
      }
      const state = new RunStream(id, current.lastSeq, draft.value)
      let ended = false
      connection.value = '实时连接'
      await $api.read(
        `/runs/${id}/events`,
        {
          organizationId: org,
          signal,
          headers: { 'Last-Event-ID': `${id}:${state.seq}` },
        },
        async (response) => {
          if (
            !response.body ||
            !response.headers.get('content-type')?.includes('text/event-stream')
          )
            throw new Error('SSE_INVALID')
          const reader = response.body.getReader()
          const parser = new SSEParser((frame) => {
            if (!valid()) return
            const action = state.apply(frame)
            if (action === 'unknown') {
              connection.value = '事件版本不匹配，正在核对快照'
              console.warn('Unsupported run event version')
            }
            draft.value = state.draft
            if (action === 'terminal') ended = true
          })
          try {
            while (valid() && !ended) {
              const part = await reader.read()
              if (part.done) break
              parser.feed(part.value)
            }
          } finally {
            await reader.cancel()
            reader.releaseLock()
          }
        },
      )
      if (valid()) {
        retries = 0
        retry = setTimeout(recover, ended ? 0 : 1000)
      }
    } catch (cause) {
      if (!valid()) return
      const failure = apiError(cause)
      if ([401, 403, 404].includes(failure.status)) {
        stop()
        error.value = '权限或登录状态已变化，请重新进入组织。'
        connection.value = '访问已失效'
        return
      }
      connection.value = '连接中断，正在恢复原运行'
      retries++
      retry = setTimeout(recover, Math.min(10000, 1000 * retries))
    }
  }
  watch(runId, () => {
    stop()
    retries = 0
    if (import.meta.client) void recover()
  })
  watch(
    () => [organization.value?.id, organization.value?.accessRevision],
    () => {
      stop()
      if (import.meta.client) void recover()
    },
  )
  onMounted(() => {
    void recover()
    window.addEventListener('online', recover)
    window.addEventListener('focus', recover)
  })
  onUnmounted(() => {
    stop()
    window.removeEventListener('online', recover)
    window.removeEventListener('focus', recover)
  })
  return { snapshot, draft, connection, error, recover }
}
