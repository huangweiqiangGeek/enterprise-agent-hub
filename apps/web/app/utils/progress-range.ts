/** Calendar days in an explicit timezone, not 24-hour durations across DST. */
export function todayInZone(timezone: string, now = new Date()): string {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: timezone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(now)
  const get = (kind: string) => parts.find((p) => p.type === kind)?.value || ''
  return `${get('year')}-${get('month')}-${get('day')}`
}
export function defaultProgressDates(timezone: string, now = new Date()) {
  const endDate = todayInZone(timezone, now)
  const start = new Date(`${endDate}T12:00:00Z`)
  start.setUTCDate(start.getUTCDate() - 6)
  return { startDate: start.toISOString().slice(0, 10), endDate }
}
export function validProgressDates(
  start: string,
  end: string,
  timezone: string,
  now = new Date(),
): boolean {
  try {
    const validDate = (value: string) =>
      /^\d{4}-\d{2}-\d{2}$/.test(value) &&
      new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) === value
    return (
      Boolean(timezone.trim()) &&
      validDate(start) &&
      validDate(end) &&
      start <= end &&
      end <= todayInZone(timezone, now)
    )
  } catch {
    return false
  }
}
