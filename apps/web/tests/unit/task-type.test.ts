import { describe, expect, it } from 'vitest'
import { parseTaskType } from '../../app/utils/task-type'
describe('task template preselection', () => {
  it('accepts only fixed task types', () => {
    for (const task of ['qa', 'compare', 'progress'])
      expect(parseTaskType(task)).toBe(task)
  })
  it('ignores arrays, unknown selectors and redirects', () => {
    for (const value of [
      undefined,
      null,
      ['qa'],
      'admin',
      'https://example.com',
      'QA',
    ])
      expect(parseTaskType(value)).toBeNull()
  })
})
