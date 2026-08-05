import { describe, expect, it } from 'vitest'
import { isValidSlug, MAX_SLUG_LENGTH } from './slug'

describe('isValidSlug', () => {
  // The same accept/reject table the backend's test_pathnames.py pins for
  // validate_segment - the two rules must not drift apart.
  it.each(['race-1', 'Round_2', 'a', 'a.b-c_d', 'erf', '2026-07', 'A'.repeat(MAX_SLUG_LENGTH)])(
    'accepts a safe segment: %s',
    (good) => {
      expect(isValidSlug(good)).toBe(true)
    },
  )

  it.each([
    '',
    '.hidden',
    '..',
    'a/b',
    '/abs',
    'a b',
    'A'.repeat(MAX_SLUG_LENGTH + 1),
    '-x',
    'x\n',
  ])('rejects an unsafe segment: %s', (bad) => {
    expect(isValidSlug(bad)).toBe(false)
  })
})
