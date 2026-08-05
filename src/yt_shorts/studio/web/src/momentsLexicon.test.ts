import { describe, expect, it } from 'vitest'
import type { MomentsLexicon } from './api'
import {
  addOwnRow,
  disableRow,
  MAX_WEIGHT,
  normaliseMarker,
  overrideRow,
  parseWeight,
  pendingRemovals,
  removeOwnRow,
  rowsToMarkers,
  setOwnWeight,
  sourceLabel,
  toRows,
} from './momentsLexicon'
import type { MarkerRow } from './momentsLexicon'

describe('MAX_WEIGHT', () => {
  it('matches the backend lexicon.MAX_WEIGHT', () => {
    expect(MAX_WEIGHT).toBe(10)
  })
})

describe('toRows', () => {
  it('marks own vs inherited from the own map, disabled from a zero weight, and sorts own-first then by descending weight then marker', () => {
    const lex: MomentsLexicon = {
      scope: 'event',
      own: { crash: 3, pole: 0 },
      effective: {
        crash: { weight: 3, source: 'event' },
        pole: { weight: 0, source: 'event' },
        'safety car': { weight: 2.5, source: 'channel' },
        overtake: { weight: 2, source: 'default' },
        wow: { weight: 1, source: 'default' },
      },
      problems: [],
    }
    const rows = toRows(lex)
    expect(rows).toEqual<MarkerRow[]>([
      { marker: 'crash', weight: 3, source: 'event', own: true, disabled: false },
      { marker: 'pole', weight: 0, source: 'event', own: true, disabled: true },
      { marker: 'safety car', weight: 2.5, source: 'channel', own: false, disabled: false },
      { marker: 'overtake', weight: 2, source: 'default', own: false, disabled: false },
      { marker: 'wow', weight: 1, source: 'default', own: false, disabled: false },
    ])
  })

  it('sorts ties by marker name ascending', () => {
    const lex: MomentsLexicon = {
      scope: 'workspace',
      own: {},
      effective: {
        zeta: { weight: 1, source: 'default' },
        alpha: { weight: 1, source: 'default' },
      },
      problems: [],
    }
    expect(toRows(lex).map((r) => r.marker)).toEqual(['alpha', 'zeta'])
  })

  it('an inherited zero-weight entry (disabled by a more specific layer than the one shown) is still disabled: true', () => {
    const lex: MomentsLexicon = {
      scope: 'event',
      own: {},
      effective: {
        pole: { weight: 0, source: 'channel' },
      },
      problems: [],
    }
    expect(toRows(lex)).toEqual<MarkerRow[]>([
      { marker: 'pole', weight: 0, source: 'channel', own: false, disabled: true },
    ])
  })

  it('returns an empty array for an empty lexicon', () => {
    const lex: MomentsLexicon = { scope: 'workspace', own: {}, effective: {}, problems: [] }
    expect(toRows(lex)).toEqual([])
  })
})

describe('parseWeight', () => {
  it('accepts a plain integer', () => {
    expect(parseWeight('2')).toBe(2)
  })
  it('accepts a decimal', () => {
    expect(parseWeight('2.5')).toBe(2.5)
  })
  it('accepts zero', () => {
    expect(parseWeight('0')).toBe(0)
  })
  it('accepts the max weight', () => {
    expect(parseWeight('10')).toBe(10)
  })
  it('accepts a comma decimal (German keyboard)', () => {
    expect(parseWeight('2,5')).toBe(2.5)
  })
  it('trims surrounding whitespace', () => {
    expect(parseWeight('  3  ')).toBe(3)
  })
  it('rejects an empty string', () => {
    expect(parseWeight('')).toBeNull()
  })
  it('rejects whitespace only', () => {
    expect(parseWeight('   ')).toBeNull()
  })
  it('rejects non-numeric text', () => {
    expect(parseWeight('abc')).toBeNull()
  })
  it('rejects a negative number', () => {
    expect(parseWeight('-1')).toBeNull()
  })
  it('rejects a number above MAX_WEIGHT', () => {
    expect(parseWeight('11')).toBeNull()
  })
  it('rejects Infinity', () => {
    expect(parseWeight('Infinity')).toBeNull()
  })
  it('rejects NaN', () => {
    expect(parseWeight('NaN')).toBeNull()
  })
  it('rejects a malformed multi-dot number like "2.5.5"', () => {
    // Number("2.5.5") is NaN, so this falls out of the same finite check as
    // "abc" - documented here because the brief calls it out explicitly.
    expect(parseWeight('2.5.5')).toBeNull()
  })

  // Edge cases pinned explicitly (see parseWeight's own docstring) rather
  // than left as incidental behaviour of the regex + Number() combination.
  it('rejects a leading "+" sign', () => {
    // The regex requires an optional leading "-" only; "+" is not in the
    // character class at all, so this is rejected before Number() ever runs.
    expect(parseWeight('+2.5')).toBeNull()
  })
  it('rejects exponential notation', () => {
    // "1e1" has letters the regex does not allow, even though Number("1e1")
    // would happily parse it as 10.
    expect(parseWeight('1e1')).toBeNull()
  })
  it('rejects a trailing-dot number like "5."', () => {
    // The regex's fractional group `\.?\d+` requires at least one digit
    // after a dot, so a bare trailing dot with nothing following it never
    // matches - unlike Number("5.") which is 5.
    expect(parseWeight('5.')).toBeNull()
  })
  it('rejects a triple comma-separated value like "2,5,5"', () => {
    // Only the FIRST comma is normalised to a dot ("2.5,5"), and the second
    // comma is not a valid character in the regex, so this is rejected the
    // same way "2.5.5" is.
    expect(parseWeight('2,5,5')).toBeNull()
  })
  it('accepts "-0" as zero (an explicit disable, not a rejection)', () => {
    // Number("-0") is the IEEE 754 negative zero, which is finite and
    // `0 <= -0 <= MAX_WEIGHT` holds (-0 === 0), so this is accepted. Compared
    // with `===` rather than `toBe` (which uses Object.is and would
    // distinguish -0 from 0) because the point being pinned is exactly that
    // distinction NOT mattering here: JSON.stringify(-0) itself serialises
    // as "0", so a disable written from "-0" round-trips as an ordinary
    // weight-0 entry, not a special value.
    const value = parseWeight('-0')
    expect(value === 0).toBe(true)
    expect(JSON.stringify({ weight: value })).toBe('{"weight":0}')
  })
})

describe('rowsToMarkers', () => {
  it('keeps only own rows, dropping inherited ones', () => {
    const rows: MarkerRow[] = [
      { marker: 'crash', weight: 3, source: 'event', own: true, disabled: false },
      { marker: 'safety car', weight: 2.5, source: 'channel', own: false, disabled: false },
    ]
    expect(rowsToMarkers(rows)).toEqual({ crash: 3 })
  })

  it('keeps an own row with weight 0 (an explicit disable in this layer)', () => {
    const rows: MarkerRow[] = [
      { marker: 'pole', weight: 0, source: 'event', own: true, disabled: true },
    ]
    expect(rowsToMarkers(rows)).toEqual({ pole: 0 })
  })

  it('returns an empty object when there are no own rows', () => {
    const rows: MarkerRow[] = [
      { marker: 'crash', weight: 3, source: 'default', own: false, disabled: false },
    ]
    expect(rowsToMarkers(rows)).toEqual({})
  })

  it('keeps a marker literally named "__proto__" as a real own property, not the prototype', () => {
    // A plain `markers[row.marker] = row.weight` assignment into a `{}`
    // object literal special-cases this exact key: it reassigns the
    // object's prototype instead of creating an own property, so
    // JSON.stringify silently drops it and Save would lose the marker with
    // no error at all. Object.fromEntries has no such special case.
    const rows: MarkerRow[] = [
      { marker: '__proto__', weight: 4, source: 'event', own: true, disabled: false },
      { marker: 'crash', weight: 3, source: 'event', own: true, disabled: false },
    ]
    const result = rowsToMarkers(rows)
    expect(Object.prototype.hasOwnProperty.call(result, '__proto__')).toBe(true)
    expect(result.__proto__).toBe(4)
    expect(result.crash).toBe(3)
    // JSON.stringify walks OWN enumerable properties, so a `__proto__` that
    // is a real own property (unlike the object-literal special case) is
    // serialised like any other key - this is the actual bug's symptom: the
    // old `markers[row.marker] = row.weight` version produced '{"crash":3}'
    // here, silently short by one marker.
    expect(JSON.stringify(result)).toBe('{"__proto__":4,"crash":3}')
    expect(Object.getPrototypeOf(result)).toBe(Object.prototype)
  })
})

describe('sourceLabel', () => {
  it('labels every source', () => {
    expect(sourceLabel('default')).toBe('built-in')
    expect(sourceLabel('workspace')).toBe('workspace')
    expect(sourceLabel('channel')).toBe('channel')
    expect(sourceLabel('event')).toBe('event')
  })
})

describe('normaliseMarker', () => {
  it('trims and lowercases, mirroring the backend', () => {
    expect(normaliseMarker('  Safety Car  ')).toBe('safety car')
  })
  it('reduces whitespace-only input to an empty string', () => {
    expect(normaliseMarker('   ')).toBe('')
  })
})

describe('overrideRow', () => {
  it('flips an inherited row to own, keeping its weight, and re-sorts it to the top', () => {
    const rows: MarkerRow[] = [
      { marker: 'crash', weight: 3, source: 'event', own: true, disabled: false },
      { marker: 'safety car', weight: 2.5, source: 'channel', own: false, disabled: false },
    ]
    expect(overrideRow(rows, 'safety car')).toEqual<MarkerRow[]>([
      { marker: 'crash', weight: 3, source: 'event', own: true, disabled: false },
      { marker: 'safety car', weight: 2.5, source: 'channel', own: true, disabled: false },
    ])
  })

  it('is a no-op when the marker is not found', () => {
    const rows: MarkerRow[] = [{ marker: 'crash', weight: 3, source: 'event', own: true, disabled: false }]
    expect(overrideRow(rows, 'missing')).toEqual(rows)
  })
})

describe('disableRow', () => {
  it('writes an own entry at weight 0 for an inherited row', () => {
    const rows: MarkerRow[] = [{ marker: 'pole', weight: 2, source: 'default', own: false, disabled: false }]
    expect(disableRow(rows, 'pole')).toEqual<MarkerRow[]>([
      { marker: 'pole', weight: 0, source: 'default', own: true, disabled: true },
    ])
  })

  it('disables an already-own row in place', () => {
    const rows: MarkerRow[] = [{ marker: 'pole', weight: 4, source: 'event', own: true, disabled: false }]
    expect(disableRow(rows, 'pole')).toEqual<MarkerRow[]>([
      { marker: 'pole', weight: 0, source: 'event', own: true, disabled: true },
    ])
  })
})

describe('removeOwnRow', () => {
  it('drops the row entirely rather than reverting it to inherited', () => {
    const rows: MarkerRow[] = [
      { marker: 'crash', weight: 3, source: 'event', own: true, disabled: false },
      { marker: 'wow', weight: 1, source: 'default', own: false, disabled: false },
    ]
    expect(removeOwnRow(rows, 'crash')).toEqual<MarkerRow[]>([
      { marker: 'wow', weight: 1, source: 'default', own: false, disabled: false },
    ])
  })
})

describe('pendingRemovals', () => {
  it('lists an own row present in savedRows but removed from rows', () => {
    const savedRows: MarkerRow[] = [
      { marker: 'crash', weight: 3, source: 'event', own: true, disabled: false },
      { marker: 'wow', weight: 1, source: 'default', own: false, disabled: false },
    ]
    const rows = removeOwnRow(savedRows, 'crash')
    expect(pendingRemovals(rows, savedRows)).toEqual(['crash'])
  })

  it('returns an empty array when nothing was removed', () => {
    const savedRows: MarkerRow[] = [
      { marker: 'crash', weight: 3, source: 'event', own: true, disabled: false },
    ]
    expect(pendingRemovals(savedRows, savedRows)).toEqual([])
  })

  it('ignores a row that was already inherited (never own) in savedRows', () => {
    const savedRows: MarkerRow[] = [
      { marker: 'wow', weight: 1, source: 'default', own: false, disabled: false },
    ]
    // A row that was never own cannot be removed via removeOwnRow (the UI
    // only offers Remove for own rows), but the helper should not count it
    // even if it vanished from `rows` for some other reason.
    expect(pendingRemovals([], savedRows)).toEqual([])
  })

  it('does not count a row that is still present but weight-edited', () => {
    const savedRows: MarkerRow[] = [
      { marker: 'crash', weight: 3, source: 'event', own: true, disabled: false },
    ]
    const rows = setOwnWeight(savedRows, 'crash', 9)
    expect(pendingRemovals(rows, savedRows)).toEqual([])
  })

  it('lists multiple removed own markers', () => {
    const savedRows: MarkerRow[] = [
      { marker: 'crash', weight: 3, source: 'event', own: true, disabled: false },
      { marker: 'pole', weight: 4, source: 'event', own: true, disabled: false },
      { marker: 'wow', weight: 1, source: 'default', own: false, disabled: false },
    ]
    const rows = removeOwnRow(removeOwnRow(savedRows, 'crash'), 'pole')
    expect(pendingRemovals(rows, savedRows)).toEqual(['crash', 'pole'])
  })
})

describe('setOwnWeight', () => {
  it('updates an own row weight without re-sorting the list', () => {
    const rows: MarkerRow[] = [
      { marker: 'crash', weight: 1, source: 'event', own: true, disabled: false },
      { marker: 'wow', weight: 5, source: 'default', own: false, disabled: false },
    ]
    // Would sort AFTER 'wow' by weight if re-sorted (own-first still wins,
    // so position would not actually move here) - the real point is the
    // array order/identity of the untouched row is preserved.
    expect(setOwnWeight(rows, 'crash', 9)).toEqual<MarkerRow[]>([
      { marker: 'crash', weight: 9, source: 'event', own: true, disabled: false },
      { marker: 'wow', weight: 5, source: 'default', own: false, disabled: false },
    ])
  })

  it('recomputes disabled from the new weight', () => {
    const rows: MarkerRow[] = [{ marker: 'crash', weight: 3, source: 'event', own: true, disabled: false }]
    expect(setOwnWeight(rows, 'crash', 0)).toEqual<MarkerRow[]>([
      { marker: 'crash', weight: 0, source: 'event', own: true, disabled: true },
    ])
  })

  it('does not touch an inherited row of the same marker', () => {
    const rows: MarkerRow[] = [{ marker: 'crash', weight: 3, source: 'event', own: false, disabled: false }]
    expect(setOwnWeight(rows, 'crash', 9)).toEqual(rows)
  })
})

describe('addOwnRow', () => {
  it('adds a brand-new own row, normalising the marker', () => {
    const rows: MarkerRow[] = []
    expect(addOwnRow(rows, '  Yellow Flag  ', 2)).toEqual<MarkerRow[]>([
      { marker: 'yellow flag', weight: 2, source: 'default', own: true, disabled: false },
    ])
  })

  it('rejects a blank marker', () => {
    expect(addOwnRow([], '   ', 2)).toBeNull()
  })

  it('rejects a duplicate of an existing own marker, case- and whitespace-insensitively', () => {
    const rows: MarkerRow[] = [{ marker: 'crash', weight: 3, source: 'event', own: true, disabled: false }]
    expect(addOwnRow(rows, ' Crash ', 5)).toBeNull()
  })

  it('promotes a matching INHERITED row to own instead of adding a second row for it', () => {
    const rows: MarkerRow[] = [{ marker: 'crash', weight: 3, source: 'default', own: false, disabled: false }]
    expect(addOwnRow(rows, 'crash', 7)).toEqual<MarkerRow[]>([
      { marker: 'crash', weight: 7, source: 'default', own: true, disabled: false },
    ])
  })

  it('sorts the newly added row into place (own-first, then descending weight)', () => {
    const rows: MarkerRow[] = [{ marker: 'crash', weight: 3, source: 'event', own: true, disabled: false }]
    expect(addOwnRow(rows, 'wow', 8)).toEqual<MarkerRow[]>([
      { marker: 'wow', weight: 8, source: 'default', own: true, disabled: false },
      { marker: 'crash', weight: 3, source: 'event', own: true, disabled: false },
    ])
  })
})

describe('sourceLabel', () => {
  it('labels the track layer', () => {
    expect(sourceLabel('track')).toBe('track')
  })

  it('still labels the four original layers', () => {
    expect(sourceLabel('default')).toBe('built-in')
    expect(sourceLabel('workspace')).toBe('workspace')
    expect(sourceLabel('channel')).toBe('channel')
    expect(sourceLabel('event')).toBe('event')
  })
})
