export default defineEventHandler((event) => {
  const config = useRuntimeConfig(event)
  const url = getRequestURL(event)
  // Stream file bodies to the API, which applies the organization-specific byte limit.
  return proxyRequest(event, `${config.apiBase}${url.pathname}${url.search}`, {
    streamRequest: true,
  })
})
