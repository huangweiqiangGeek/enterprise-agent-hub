import { describe, expect, it } from 'vitest'
import { healthLabel } from '../../app/utils/health'

describe('health state', () => {
  it('does not interpret liveness as database readiness', () => {
    expect(healthLabel('success', 'ok')).toBe('服务尚未就绪')
    expect(healthLabel('success', 'ready')).toBe('服务与数据库已就绪')
  })

  it('does not display cached success during a failed refresh', () => {
    expect(healthLabel('error', 'ready')).toBe('服务尚未就绪')
    expect(healthLabel('pending', 'ready')).toBe('正在检查服务…')
  })
})
