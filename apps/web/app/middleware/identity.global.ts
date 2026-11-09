import type { components } from '../types/api'
import { apiError } from '../utils/api-error'

export default defineNuxtRouteMiddleware(async (to, from) => {
  if (to.path === '/status') return
  const { $api } = useNuxtApp()
  const { session, organization, refresh, clearOrganization } = useIdentity()
  try {
    if (!session.value) await refresh()
    const publicPage = to.path === '/login' || to.path === '/register'
    if (!session.value?.user) {
      if (!publicPage) return navigateTo('/login')
      return
    }
    if (publicPage) return navigateTo('/onboarding')
    const orgId =
      typeof to.params.orgId === 'string' ? to.params.orgId : undefined
    if (orgId) {
      if (orgId !== from.params.orgId) clearOrganization()
      organization.value = await $api.request<
        components['schemas']['OrganizationDTO']
      >('/organizations/current', { organizationId: orgId })
    } else if (organization.value) clearOrganization()
  } catch (error) {
    const failure = apiError(error)
    clearOrganization()
    if (failure.status === 404) {
      await refresh()
      return navigateTo('/onboarding?notice=access')
    }
    if (failure.status === 401) return navigateTo('/login')
    throw createError({
      statusCode: failure.status || 503,
      message: failure.message,
    })
  }
})
