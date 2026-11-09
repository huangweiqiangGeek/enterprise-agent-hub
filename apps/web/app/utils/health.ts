import type { components } from '../types/api'

export function healthLabel(
  requestStatus: 'idle' | 'pending' | 'success' | 'error',
  healthStatus?: components['schemas']['HealthData']['status'],
): string {
  if (requestStatus === 'pending' || requestStatus === 'idle') {
    return '正在检查服务…'
  }
  if (requestStatus === 'success' && healthStatus === 'ready') {
    return '服务与数据库已就绪'
  }
  return '服务尚未就绪'
}
