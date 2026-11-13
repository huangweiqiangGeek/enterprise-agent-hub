export type TaskType = 'qa' | 'compare' | 'progress'
export function parseTaskType(value: unknown): TaskType | null {
  return value === 'qa' || value === 'compare' || value === 'progress'
    ? value
    : null
}
