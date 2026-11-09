import type { components } from '../types/api'
import { ApiError, apiError } from '../utils/api-error'

type Session = components['schemas']['SessionDTO']
type Options = {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  body?: Record<string, unknown>
  organizationId?: string
  key?: string
  timeoutMs?: number
  signal?: AbortSignal
}

export default defineNuxtPlugin(() => {
  const forwardedHeaders = useRequestHeaders(['cookie'])
  const responseCookies = useResponseHeader('set-cookie')
  const cacheControl = useResponseHeader('cache-control')
  if (import.meta.server) cacheControl.value = 'no-store'
  const session = useState<Session | null>('identity', () => null)
  // Each plugin instance belongs to one Nuxt app/SSR request, never another user.
  const controllers = new Set<AbortController>()
  let generation = 0

  async function transport<T>(
    path: string,
    options: Options = {},
  ): Promise<T | undefined> {
    const controller = new AbortController()
    const started = generation
    controllers.add(controller)
    const abort = () => controller.abort()
    options.signal?.addEventListener('abort', abort)
    if (options.signal?.aborted) controller.abort()
    const headers: Record<string, string> = { ...forwardedHeaders }
    if (options.organizationId)
      headers['X-Organization-Id'] = options.organizationId
    if (options.key) headers['Idempotency-Key'] = options.key
    if (options.method && options.method !== 'GET')
      headers['X-CSRF-Token'] = session.value?.csrfToken || ''
    try {
      const response = await $fetch.raw<{ data: T; requestId: string }>(
        `/api/v1${path}`,
        {
          method: options.method || 'GET',
          body: options.body,
          headers,
          signal: controller.signal,
          retry: 0,
          timeout: options.timeoutMs ?? 15000,
        },
      )
      if (started !== generation)
        throw new ApiError('请求已取消。', 0, 'ABORTED')
      if (import.meta.server) {
        const existing = responseCookies.value
        responseCookies.value = [
          ...(Array.isArray(existing)
            ? existing
            : typeof existing === 'string'
              ? [existing]
              : []),
          ...response.headers.getSetCookie(),
        ]
      }
      if (response.status === 204) return undefined
      if (!response._data)
        throw new ApiError(
          '服务未返回有效结果，请重试。',
          502,
          'INVALID_RESPONSE',
        )
      return response._data.data
    } catch (error) {
      if (controller.signal.aborted || started !== generation)
        throw new ApiError('请求已取消。', 0, 'ABORTED')
      const converted = apiError(error)
      if (converted.status === 401) session.value = null
      throw converted
    } finally {
      options.signal?.removeEventListener('abort', abort)
      controllers.delete(controller)
    }
  }

  async function request<T>(path: string, options: Options = {}): Promise<T> {
    const data = await transport<T>(path, options)
    if (data === undefined)
      throw new ApiError(
        '服务未返回有效结果，请重试。',
        502,
        'INVALID_RESPONSE',
      )
    return data
  }

  async function empty(path: string, options: Options): Promise<void> {
    await transport<unknown>(path, options)
  }

  /** Send one file with progress; callers reconcile acceptance by the same idempotency key. */
  async function upload(
    path: string,
    file: File,
    options: {
      organizationId: string
      key: string
      signal: AbortSignal
      progress: (value: number) => void
    },
  ): Promise<void> {
    const controller = new AbortController()
    const started = generation
    controllers.add(controller)
    const xhr = new XMLHttpRequest()
    const abort = () => xhr.abort()
    controller.signal.addEventListener('abort', abort)
    options.signal.addEventListener('abort', abort)
    try {
      await new Promise<void>((resolve, reject) => {
        xhr.open('POST', `/api/v1${path}`)
        xhr.withCredentials = true
        xhr.timeout = 210000
        xhr.setRequestHeader('X-Organization-Id', options.organizationId)
        xhr.setRequestHeader('X-CSRF-Token', session.value?.csrfToken || '')
        xhr.setRequestHeader('Idempotency-Key', options.key)
        xhr.upload.onprogress = (event) => {
          if (started === generation && event.lengthComputable)
            options.progress(
              Math.min(100, Math.round((event.loaded * 100) / event.total)),
            )
        }
        xhr.onload = () => {
          if (started !== generation)
            return reject(new ApiError('请求已取消。', 0, 'ABORTED'))
          if (xhr.status === 202) return resolve()
          let data: unknown
          try {
            data = JSON.parse(xhr.responseText)
          } catch {
            data = null
          }
          reject(apiError({ statusCode: xhr.status, data }))
        }
        xhr.onerror = xhr.ontimeout = () =>
          reject(new ApiError('上传接受结果待核对。', 0, 'NETWORK_ERROR'))
        xhr.onabort = () =>
          reject(new ApiError('传输已中断，正在核对接受结果。', 0, 'ABORTED'))
        const body = new FormData()
        body.append('file', file)
        if (options.signal.aborted)
          return reject(new ApiError('传输已取消。', 0, 'ABORTED'))
        xhr.send(body)
      })
    } finally {
      controllers.delete(controller)
      controller.signal.removeEventListener('abort', abort)
      options.signal.removeEventListener('abort', abort)
    }
  }

  /** Invalidate pending results as well as aborting transport on organization/session changes. */
  function cancelAll() {
    generation += 1
    for (const controller of controllers) controller.abort()
    controllers.clear()
  }

  return { provide: { api: { request, empty, upload, cancelAll } } }
})
