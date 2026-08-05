import { describe, expect, it } from 'vitest'
import {
  CATEGORIES,
  VISIBILITIES,
  isFutureLocal,
  metadataFieldsValid,
  metadataValid,
  needsConfirm,
  parseTags,
  tagsEqual,
  tagsToInput,
  toRfc3339,
} from './uploadMeta'

describe('VISIBILITIES', () => {
  it('is the three YouTube privacy statuses, private first', () => {
    expect(VISIBILITIES).toEqual(['private', 'unlisted', 'public'])
  })
})

describe('CATEGORIES', () => {
  it('is the curated id/label list', () => {
    expect(CATEGORIES).toEqual([
      { id: '20', label: 'Gaming' },
      { id: '17', label: 'Sports' },
      { id: '2', label: 'Autos & Vehicles' },
      { id: '22', label: 'People & Blogs' },
      { id: '24', label: 'Entertainment' },
    ])
  })
})

describe('parseTags', () => {
  it('splits, trims, drops empties, dedups', () => {
    expect(parseTags('gt7, erf ,, gt7\nsim')).toEqual(['gt7', 'erf', 'sim'])
    expect(parseTags('')).toEqual([])
  })
  it('splits on newlines and commas together', () => {
    expect(parseTags('a\nb, c\n\nd')).toEqual(['a', 'b', 'c', 'd'])
  })
})

describe('tagsToInput', () => {
  it('joins with comma-space', () => {
    expect(tagsToInput(['a', 'b'])).toBe('a, b')
  })
  it('round-trips through parseTags', () => {
    const tags = ['gt7', 'erf', 'sim']
    expect(parseTags(tagsToInput(tags))).toEqual(tags)
  })
  it('is empty for an empty list', () => {
    expect(tagsToInput([])).toBe('')
  })
})

describe('toRfc3339', () => {
  it('converts a datetime-local value to a UTC RFC3339 string ending in Z', () => {
    const result = toRfc3339('2099-01-01T10:00')
    expect(result.endsWith('Z')).toBe(true)
    // Round-trips: parsing the result back gives the same instant as parsing
    // the original as a local datetime (tz-robust - no fixed-hour assertion).
    expect(new Date(result).getTime()).toBe(new Date('2099-01-01T10:00').getTime())
  })
  it('is a valid ISO string', () => {
    const result = toRfc3339('2030-06-15T23:45')
    expect(new Date(result).toISOString()).toBe(result)
  })
})

describe('needsConfirm', () => {
  it('is false only for plain private', () => {
    expect(needsConfirm('private', false)).toBe(false)
    expect(needsConfirm('private', true)).toBe(true) // scheduled
    expect(needsConfirm('public', false)).toBe(true)
    expect(needsConfirm('unlisted', false)).toBe(true)
  })
})

describe('metadataValid', () => {
  it('enforces the YouTube caps', () => {
    expect(metadataValid({ title: 'ok', description: 'x', tags: ['a'] })).toBe(true)
    expect(metadataValid({ title: 'x'.repeat(101), description: '', tags: [] })).toBe(false)
    expect(
      metadataValid({ title: 'ok', description: '', tags: ['x'.repeat(300), 'y'.repeat(300)] }),
    ).toBe(false)
  })
  it('enforces the description cap', () => {
    expect(metadataValid({ title: 'ok', description: 'x'.repeat(5000), tags: [] })).toBe(true)
    expect(metadataValid({ title: 'ok', description: 'x'.repeat(5001), tags: [] })).toBe(false)
  })
  it('accepts a title at exactly the cap', () => {
    expect(metadataValid({ title: 'x'.repeat(100), description: '', tags: [] })).toBe(true)
  })
})

describe('metadataFieldsValid', () => {
  it('is true for a valid description/tags pair', () => {
    expect(metadataFieldsValid({ description: 'x', tags: ['a', 'b'] })).toBe(true)
  })
  it('is false for an over-cap description, regardless of title', () => {
    expect(metadataFieldsValid({ description: 'x'.repeat(5001), tags: [] })).toBe(false)
  })
  it('is false for over-cap combined tags', () => {
    expect(
      metadataFieldsValid({ description: '', tags: ['x'.repeat(300), 'y'.repeat(300)] }),
    ).toBe(false)
  })
  it('never looks at the title - an over-cap title alone does not fail it', () => {
    // metadataFieldsValid takes no title param at all; this documents WHY:
    // the Save PATCH never sends the title (see UploadPanel's
    // handleSaveMetadata), so a clip with a >100-char hook must still be
    // able to save a valid description/tags fix.
    expect(metadataFieldsValid({ description: 'ok', tags: ['ok'] })).toBe(true)
  })
})

describe('tagsEqual', () => {
  it('is true for the same tags in a different order', () => {
    expect(tagsEqual(['a', 'b', 'c'], ['c', 'a', 'b'])).toBe(true)
  })
  it('is true for two empty lists', () => {
    expect(tagsEqual([], [])).toBe(true)
  })
  it('is false when the tag sets actually differ', () => {
    expect(tagsEqual(['a', 'b'], ['a', 'c'])).toBe(false)
  })
  it('is false when only the count differs', () => {
    expect(tagsEqual(['a'], ['a', 'a', 'b'])).toBe(false)
  })
})

describe('isFutureLocal', () => {
  const now = new Date('2026-07-23T12:00:00')
  it('is true for a datetime-local value after now', () => {
    expect(isFutureLocal('2026-07-23T13:00', now)).toBe(true)
  })
  it('is false for a datetime-local value before now', () => {
    expect(isFutureLocal('2026-07-23T11:00', now)).toBe(false)
  })
  it('is false for now itself (not strictly future)', () => {
    expect(isFutureLocal('2026-07-23T12:00', now)).toBe(false)
  })
  it('is false for an empty or unparseable value', () => {
    expect(isFutureLocal('', now)).toBe(false)
    expect(isFutureLocal('not-a-date', now)).toBe(false)
  })
  it('defaults `now` to the real current time', () => {
    expect(isFutureLocal('2099-01-01T00:00')).toBe(true)
    expect(isFutureLocal('2000-01-01T00:00')).toBe(false)
  })
})
