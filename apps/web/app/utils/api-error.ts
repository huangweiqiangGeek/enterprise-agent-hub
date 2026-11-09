export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string,
    readonly fields: string[] = [],
  ) {
    super(message)
  }
}

export function apiError(error: unknown): ApiError {
  if (error instanceof ApiError) return error
  if (typeof error === 'object' && error !== null) {
    const status =
      'statusCode' in error && typeof error.statusCode === 'number'
        ? error.statusCode
        : 0
    const data = 'data' in error ? error.data : null
    if (typeof data === 'object' && data !== null && 'error' in data) {
      const detail = data.error
      if (
        typeof detail === 'object' &&
        detail !== null &&
        'message' in detail &&
        typeof detail.message === 'string'
      ) {
        const code =
          'code' in detail && typeof detail.code === 'string'
            ? detail.code
            : 'REQUEST_FAILED'
        const fields: string[] = []
        if (
          'details' in detail &&
          typeof detail.details === 'object' &&
          detail.details !== null &&
          'fields' in detail.details &&
          Array.isArray(detail.details.fields)
        ) {
          for (const field of detail.details.fields) {
            if (
              typeof field === 'object' &&
              field !== null &&
              'field' in field &&
              typeof field.field === 'string'
            )
              fields.push(field.field)
          }
        }
        return new ApiError(detail.message, status, code, fields)
      }
    }
  }
  return new ApiError(
    '连接未完成，请检查网络后重试。提交内容已保留。',
    0,
    'NETWORK_ERROR',
  )
}

export const roleLabel = (role: string) =>
  ({ owner: '所有者', admin: '管理员', member: '成员' })[role] || role
