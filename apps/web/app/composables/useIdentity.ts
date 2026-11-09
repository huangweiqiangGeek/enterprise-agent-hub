import type { components } from '../types/api'

export function useIdentity() {
  const { $api } = useNuxtApp()
  const session = useState<components['schemas']['SessionDTO'] | null>(
    'identity',
    () => null,
  )
  const organization = useState<
    components['schemas']['OrganizationDTO'] | null
  >('organization', () => null)

  async function refresh() {
    const current =
      await $api.request<components['schemas']['SessionDTO']>('/auth/session')
    session.value = current
    return current
  }
  function clearOrganization() {
    $api.cancelAll()
    organization.value = null
    clearNuxtData()
  }
  async function logout() {
    await $api.empty('/auth/logout', { method: 'POST' })
    clearOrganization()
    session.value = null
    await navigateTo('/login')
  }
  return { session, organization, refresh, clearOrganization, logout }
}
