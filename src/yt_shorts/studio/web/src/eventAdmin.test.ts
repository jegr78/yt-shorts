import { describe, expect, it } from 'vitest'
import { deleteConfirmed, isValidEventName, MAX_EVENT_NAME_LENGTH } from './eventAdmin'

describe('isValidEventName', () => {
  // The same accept/reject table the backend's test_event_admin.py pins for
  // validate_name - the two rules must not drift apart.
  it.each(['race-1', 'Round_2', 'a', 'a.b-c', '2026-07', 'A'.repeat(MAX_EVENT_NAME_LENGTH)])(
    'accepts a safe slug: %s',
    (good) => {
      expect(isValidEventName(good)).toBe(true)
    },
  )

  it.each(['', '.hidden', '..', 'a/b', '/abs', 'a b', 'A'.repeat(MAX_EVENT_NAME_LENGTH + 1), '-x'])(
    'rejects an unsafe name: %s',
    (bad) => {
      expect(isValidEventName(bad)).toBe(false)
    },
  )
})

describe('deleteConfirmed', () => {
  it('is true for an exact match', () => {
    expect(deleteConfirmed('round-1', 'round-1')).toBe(true)
  })

  it('trims trailing/leading whitespace before comparing', () => {
    expect(deleteConfirmed('  round-1  ', 'round-1')).toBe(true)
  })

  it('is false for a mismatch', () => {
    expect(deleteConfirmed('round-2', 'round-1')).toBe(false)
  })

  it('is false for an empty entry', () => {
    expect(deleteConfirmed('', 'round-1')).toBe(false)
  })
})
