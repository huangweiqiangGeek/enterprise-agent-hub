import { apiError } from '../utils/api-error'

export function useFormAction() {
  const busy = ref(false)
  const message = ref('')
  const code = ref('')
  const fields = ref<string[]>([])
  async function run(action: () => Promise<void>) {
    if (busy.value) return
    busy.value = true
    message.value = ''
    code.value = ''
    fields.value = []
    try {
      await action()
    } catch (error) {
      const failure = apiError(error)
      if (failure.code === 'ABORTED') return
      code.value = failure.code
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
  return { busy, message, code, fields, run }
}
