import { describe, expect, it } from 'vitest'
import {
  defaultProgressDates,
  todayInZone,
  validProgressDates,
} from '../../app/utils/progress-range'
describe('progress calendar range', () => {
  it('uses local day and seven inclusive calendar days across DST', () => {
    const now = new Date('2026-03-09T01:00:00Z')
    expect(todayInZone('Asia/Shanghai', now)).toBe('2026-03-09')
    expect(defaultProgressDates('America/New_York', now)).toEqual({
      startDate: '2026-03-02',
      endDate: '2026-03-08',
    })
  })
  it('rejects blank, impossible, reversed, future dates and unknown zones', () => {
    const now = new Date('2026-03-09T01:00:00Z')
    for (const [start, end, zone] of [
      ['', '', 'UTC'],
      ['2026-02-30', '2026-03-01', 'UTC'],
      ['2026-03-09', '2026-03-08', 'UTC'],
      ['2026-03-09', '2026-03-10', 'UTC'],
      ['2026-03-01', '2026-03-02', 'Invalid/Zone'],
    ]) {
      expect(validProgressDates(start || '', end || '', zone || '', now)).toBe(
        false,
      )
    }
    expect(
      validProgressDates('2026-03-08', '2026-03-08', 'America/New_York', now),
    ).toBe(true)
  })
})
