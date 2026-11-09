import { apiError } from '../utils/api-error'

export function useFormAction() {
  const busy = ref(false)
  const message = ref('')
  const fields = ref<string[]>([])
  async function run(action: () => Promise<void>) {
    if (busy.value) return
    busy.value = true
    message.value = ''
    fields.value = []
    try {
      await action()
    } catch (error) {
      const failure = apiError(error)
      if (failure.code === 'ABORTED') return
      fields.value = failure.fields
      message.value = failure.message
      await nextTick()
      const invalid = document.querySelector<HTMLElement>(
        '[aria-invalid="true"]',
      )
      ;(
        invalid || document.querySelector<HTMLElement>('[role="alert"]')
      )?.focus()
    } finally {
      busy.value = false
    }
  }
  return { busy, message, fields, run }
}
