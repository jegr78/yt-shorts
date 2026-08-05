import { describe, expect, it } from 'vitest'
import type { GlossaryLayers } from './api'
import {
  addOwnReplacementRow,
  addOwnTermRow,
  disableRow,
  enableRow,
  incompleteReplacements,
  normaliseKey,
  normaliseTerm,
  overrideRow,
  pendingRemovals,
  removeOwnRow,
  rowsToOwn,
  setReplacementText,
  toReplacementRows,
  toTermRows,
} from './glossaryLayers'

function layers(overrides: Partial<GlossaryLayers> = {}): GlossaryLayers {
  return {
    scope: 'channel',
    own: { terms: {}, replacements: {} },
    own_keys: { terms: [], replacements: [] },
    effective: { terms: {}, replacements: {} },
    problems: [],
    track: null,
    ...overrides,
  }
}

describe('normalisation', () => {
  it('lower-cases and trims a term', () => {
    expect(normaliseTerm('  Karussell ')).toBe('karussell')
  })

  it('strips punctuation per token in a key, matching the backend', () => {
    expect(normaliseKey('Kleine, Carousel')).toBe('kleine carousel')
  })

  it('collapses runs of whitespace in a key', () => {
    expect(normaliseKey('kleine   carousel')).toBe('kleine carousel')
  })

  // Every expectation below is the REAL output of the backend's
  // glossary.normalise_key for the same input, captured by running it. They
  // exist because the first version of normaliseKey stripped all non-letter,
  // non-digit characters, which silently disagreed with Python's ASCII-only
  // string.punctuation on three of these - and every ASCII case the suite
  // happened to cover agreed, so the divergence passed unnoticed. A
  // mismatched key makes the editor treat an own entry as inherited, and the
  // next Save then deletes it from disk without an error.
  it.each([
    // Non-ASCII punctuation: Python KEEPS these, so this must too.
    ['O’Brien', 'o’brien'],
    ['„Karussell“', '„karussell“'],
    ['foo—bar', 'foo—bar'],
    // ASCII punctuation: Python strips these.
    ['Ex-Mühle', 'exmühle'],
    ['Stefan-Bellof-S', 'stefanbellofs'],
    ['a_b', 'ab'],
    ['super+pole', 'superpole'],
    // Letters and digits, including non-ASCII letters, always survive.
    ['Döttinger Höhe', 'döttinger höhe'],
    ['café 3', 'café 3'],
  ])('agrees with the backend on %j', (input, expected) => {
    expect(normaliseKey(input)).toBe(expected)
  })
})

describe('toTermRows', () => {
  it('marks a row own when the scope has its own entry', () => {
    const rows = toTermRows(
      layers({
        own: { terms: { Karussell: true }, replacements: {} },
        own_keys: { terms: ['karussell'], replacements: [] },
        effective: {
          terms: { karussell: { term: 'Karussell', enabled: true, source: 'channel' } },
          replacements: {},
        },
      }),
    )
    expect(rows).toEqual([
      { key: 'karussell', term: 'Karussell', enabled: true, source: 'channel', own: true },
    ])
  })

  it('marks an inherited row not own', () => {
    const rows = toTermRows(
      layers({
        effective: {
          terms: { karussell: { term: 'Karussell', enabled: true, source: 'default' } },
          replacements: {},
        },
      }),
    )
    expect(rows[0].own).toBe(false)
  })

  it('sorts own first, then disabled last, then alphabetically', () => {
    const rows = toTermRows(
      layers({
        own: { terms: { Bravo: true }, replacements: {} },
        own_keys: { terms: ['bravo'], replacements: [] },
        effective: {
          terms: {
            alpha: { term: 'Alpha', enabled: true, source: 'default' },
            bravo: { term: 'Bravo', enabled: true, source: 'channel' },
            charlie: { term: 'Charlie', enabled: false, source: 'default' },
          },
          replacements: {},
        },
      }),
    )
    expect(rows.map((r) => r.key)).toEqual(['bravo', 'alpha', 'charlie'])
  })

  it('keeps a disabled row visible with the layer that disabled it', () => {
    const rows = toTermRows(
      layers({
        effective: {
          terms: { karussell: { term: 'Karussell', enabled: false, source: 'workspace' } },
          replacements: {},
        },
      }),
    )
    expect(rows[0]).toMatchObject({ enabled: false, source: 'workspace', own: false })
  })
})

describe('toReplacementRows', () => {
  it('carries the from/to pair and ownership', () => {
    const rows = toReplacementRows(
      layers({
        own: { terms: {}, replacements: { kessichen: 'Kesselchen' } },
        own_keys: { terms: [], replacements: ['kessichen'] },
        effective: {
          terms: {},
          replacements: {
            kessichen: { key: 'kessichen', value: 'Kesselchen', source: 'channel' },
          },
        },
      }),
    )
    expect(rows).toEqual([
      {
        key: 'kessichen',
        from: 'kessichen',
        to: 'Kesselchen',
        enabled: true,
        source: 'channel',
        own: true,
      },
    ])
  })

  it('marks a smart-quoted own entry own, not inherited', () => {
    // Ownership comes from the SERVER's own_keys now, not from re-normalising
    // `own`'s raw text here - so this stays correct no matter how subtle the
    // raw spelling's normalisation is. The server reports source 'channel' -
    // this scope's own layer - so anything but own: true here means the next
    // Save drops the entry (rowsToOwn sends own rows only; update overwrites
    // the whole layer).
    const rows = toReplacementRows(
      layers({
        own: { terms: {}, replacements: { 'O’Brien': 'OBrien' } },
        own_keys: { terms: [], replacements: ['o’brien'] },
        effective: {
          terms: {},
          replacements: {
            'o’brien': { key: 'O’Brien', value: 'OBrien', source: 'channel' },
          },
        },
      }),
    )
    expect(rows[0]).toMatchObject({ key: 'o’brien', own: true, enabled: true })
  })

  it('marks a mid-token U+FEFF own key as own even though the client would '
    + 'normalise it differently from the server', () => {
    // A paste stitched from two UTF-8-with-BOM fragments can carry a BOM
    // (U+FEFF) mid-token. JS's `\s` (what normaliseKey's own `.split(/\s+/)`
    // uses) treats U+FEFF as whitespace and would split "kessi﻿chen"
    // into two tokens; Python's `str.split()` (what the backend's
    // glossary.normalise_key uses) does NOT, and keeps it as one token with
    // the BOM still embedded. Re-normalising the raw key client-side would
    // therefore compute a DIFFERENT identity than the server did - which is
    // exactly the bug this fix closes structurally, not by hand-mirroring
    // one more character. Proven two ways: the naive client-side
    // normalisation of the raw key does NOT match what the server reports as
    // its own key, and yet the row is still correctly marked own because
    // ownership is now read from the server's own_keys, not recomputed here.
    const rawKey = 'Kessi﻿chen'
    const serverKey = 'kessi﻿chen'   // glossary.normalise_key's real output: one token, BOM kept
    expect(normaliseKey(rawKey)).not.toBe(serverKey)   // the divergence this fix works around

    const rows = toReplacementRows(
      layers({
        own: { terms: {}, replacements: { [rawKey]: 'Kesselchen' } },
        own_keys: { terms: [], replacements: [serverKey] },
        effective: {
          terms: {},
          replacements: {
            [serverKey]: { key: rawKey, value: 'Kesselchen', source: 'channel' },
          },
        },
      }),
    )
    expect(rows[0]).toMatchObject({ key: serverKey, own: true, enabled: true })
  })

  it('renders a disabled replacement with an empty target', () => {
    const rows = toReplacementRows(
      layers({
        effective: {
          terms: {},
          replacements: { carousel: { key: 'carousel', value: null, source: 'event' } },
        },
      }),
    )
    expect(rows[0]).toMatchObject({ to: '', enabled: false })
  })
})

describe('row actions', () => {
  const inherited = [
    { key: 'karussell', term: 'Karussell', enabled: true, source: 'default' as const, own: false },
  ]

  it('override copies an inherited row into the own layer at its current value', () => {
    expect(overrideRow(inherited, 'karussell')[0]).toMatchObject({ own: true, enabled: true })
  })

  it('disable creates an own entry pinned to disabled', () => {
    expect(disableRow(inherited, 'karussell')[0]).toMatchObject({ own: true, enabled: false })
  })

  it('override is a no-op for an unknown key', () => {
    expect(overrideRow(inherited, 'nope')).toEqual(inherited)
  })

  it('remove drops the row entirely', () => {
    const own = [{ ...inherited[0], own: true }]
    expect(removeOwnRow(own, 'karussell')).toEqual([])
  })

  it('setReplacementText edits an own row without re-sorting', () => {
    const rows = [
      { key: 'b', from: 'b', to: 'B', enabled: true, source: 'channel' as const, own: true },
      { key: 'a', from: 'a', to: 'A', enabled: true, source: 'channel' as const, own: true },
    ]
    const next = setReplacementText(rows, 'a', 'AA')
    expect(next.map((r) => r.key)).toEqual(['b', 'a'])
    expect(next[1].to).toBe('AA')
  })

  it('setReplacementText ignores an inherited row', () => {
    expect(setReplacementText(inherited.map((r) => ({
      key: r.key, from: r.key, to: 'x', enabled: true, source: r.source, own: false,
    })), 'karussell', 'y')[0].to).toBe('x')
  })
})

describe('enableRow', () => {
  const inherited = [
    { key: 'karussell', term: 'Karussell', enabled: true, source: 'default' as const, own: false },
  ]

  it('re-enables a disabled own row', () => {
    const own = [
      { key: 'karussell', term: 'Karussell', enabled: false, source: 'channel' as const, own: true },
    ]
    expect(enableRow(own, 'karussell')[0]).toMatchObject({ own: true, enabled: true })
  })

  it('creates an own entry, enabled, from a disabled inherited row - the Override dead end this exists to escape', () => {
    const disabledInherited = [
      { key: 'karussell', term: 'Karussell', enabled: false, source: 'workspace' as const, own: false },
    ]
    expect(enableRow(disabledInherited, 'karussell')[0]).toMatchObject({ own: true, enabled: true })
  })

  it('is a no-op for an unknown key', () => {
    expect(enableRow(inherited, 'nope')).toEqual(inherited)
  })

  it('sorts the newly-enabled row to where a fresh load would place it', () => {
    const rows = [
      { key: 'bravo', term: 'Bravo', enabled: true, source: 'channel' as const, own: true },
      { key: 'alpha', term: 'Alpha', enabled: false, source: 'workspace' as const, own: false },
    ]
    // 'alpha' becomes own+enabled, same group as 'bravo' - alphabetical within
    // that group puts it first, exactly as toTermRows would sort a fresh load.
    expect(enableRow(rows, 'alpha').map((r) => r.key)).toEqual(['alpha', 'bravo'])
  })
})

describe('incompleteReplacements', () => {
  it('reports an own enabled row with blank target text', () => {
    const rows = [
      { key: 'a', from: 'a', to: '', enabled: true, source: 'channel' as const, own: true },
    ]
    expect(incompleteReplacements(rows)).toEqual(['a'])
  })

  it('reports an own enabled row with whitespace-only target text', () => {
    const rows = [
      { key: 'a', from: 'a', to: '   ', enabled: true, source: 'channel' as const, own: true },
    ]
    expect(incompleteReplacements(rows)).toEqual(['a'])
  })

  it('does not report an own DISABLED row with blank text - that is a legitimate disable', () => {
    const rows = [
      { key: 'a', from: 'a', to: '', enabled: false, source: 'channel' as const, own: true },
    ]
    expect(incompleteReplacements(rows)).toEqual([])
  })

  it('never reports an inherited row', () => {
    const rows = [
      { key: 'a', from: 'a', to: '', enabled: true, source: 'default' as const, own: false },
    ]
    expect(incompleteReplacements(rows)).toEqual([])
  })
})

describe('addOwnTermRow', () => {
  it('adds a new own row', () => {
    const rows = addOwnTermRow([], ' Neu ')
    expect(rows).not.toBeNull()
    expect(rows![0]).toMatchObject({ key: 'neu', term: 'Neu', own: true, enabled: true })
  })

  it('promotes an already-inherited term instead of adding a second row', () => {
    const inheritedRows = [
      { key: 'karussell', term: 'Karussell', enabled: false, source: 'default' as const, own: false },
    ]
    const rows = addOwnTermRow(inheritedRows, 'KARUSSELL')
    expect(rows).toHaveLength(1)
    expect(rows![0]).toMatchObject({ own: true, enabled: true })
  })

  it('carries the TYPED spelling through when promoting an inherited row, not the '
    + 'inherited spelling', () => {
    // Typing "HOHE ACHT" while the default carries "Hohe Acht" must promote
    // at the typed spelling - a term row renders no editable field, so
    // silently keeping the inherited spelling here would strand it with no
    // way to change it afterwards short of Remove-then-Add.
    const inheritedRows = [
      { key: 'hohe acht', term: 'Hohe Acht', enabled: true, source: 'default' as const, own: false },
    ]
    const rows = addOwnTermRow(inheritedRows, 'HOHE ACHT')
    expect(rows).toHaveLength(1)
    expect(rows![0]).toMatchObject({ key: 'hohe acht', term: 'HOHE ACHT', own: true, enabled: true })
  })

  it('rejects a blank term', () => {
    expect(addOwnTermRow([], '   ')).toBeNull()
  })

  it('rejects a duplicate of an existing own row', () => {
    const own = [
      { key: 'neu', term: 'Neu', enabled: true, source: 'channel' as const, own: true },
    ]
    expect(addOwnTermRow(own, 'neu')).toBeNull()
  })
})

describe('addOwnReplacementRow', () => {
  it('adds a from/to pair', () => {
    const rows = addOwnReplacementRow([], ' Kessichen ', ' Kesselchen ')
    expect(rows![0]).toMatchObject({
      key: 'kessichen', from: 'Kessichen', to: 'Kesselchen', own: true, enabled: true,
    })
  })

  it('rejects a blank from or to', () => {
    expect(addOwnReplacementRow([], '', 'x')).toBeNull()
    expect(addOwnReplacementRow([], 'x', '  ')).toBeNull()
  })

  it('rejects a key that is only punctuation', () => {
    expect(addOwnReplacementRow([], '...', 'x')).toBeNull()
  })

  it('rejects a duplicate of an existing own key', () => {
    const own = [
      { key: 'kessichen', from: 'kessichen', to: 'Kesselchen', enabled: true,
        source: 'channel' as const, own: true },
    ]
    expect(addOwnReplacementRow(own, 'Kessichen,', 'Other')).toBeNull()
  })
})

describe('rowsToOwn', () => {
  it('sends ONLY own rows - an inherited row must never be promoted silently', () => {
    const termRows = [
      { key: 'a', term: 'A', enabled: true, source: 'channel' as const, own: true },
      { key: 'b', term: 'B', enabled: true, source: 'default' as const, own: false },
    ]
    const repRows = [
      { key: 'c', from: 'c', to: 'C', enabled: true, source: 'channel' as const, own: true },
      { key: 'd', from: 'd', to: 'D', enabled: true, source: 'default' as const, own: false },
    ]
    expect(rowsToOwn(termRows, repRows)).toEqual({
      terms: { A: true },
      replacements: { c: 'C' },
    })
  })

  it('keeps a disabled own row as an explicit false/null', () => {
    const termRows = [
      { key: 'a', term: 'A', enabled: false, source: 'channel' as const, own: true },
    ]
    const repRows = [
      { key: 'c', from: 'c', to: '', enabled: false, source: 'channel' as const, own: true },
    ]
    expect(rowsToOwn(termRows, repRows)).toEqual({
      terms: { A: false },
      replacements: { c: null },
    })
  })

  it('survives a __proto__ entry', () => {
    // Assigning into a `{}` literal at this key reassigns the prototype
    // instead of creating an own property, so the entry would silently vanish
    // from JSON.stringify - the exact bug momentsLexicon.rowsToMarkers
    // documents. Object.fromEntries has no such special case.
    const termRows = [
      { key: '__proto__', term: '__proto__', enabled: true, source: 'channel' as const, own: true },
    ]
    const payload = rowsToOwn(termRows, [])
    expect(Object.prototype.hasOwnProperty.call(payload.terms, '__proto__')).toBe(true)
    expect(JSON.parse(JSON.stringify(payload)).terms.__proto__).toBe(true)
  })
})

describe('pendingRemovals', () => {
  it('names own rows that vanished since the last save', () => {
    const saved = [
      { key: 'a', term: 'A', enabled: true, source: 'channel' as const, own: true },
      { key: 'b', term: 'B', enabled: true, source: 'default' as const, own: false },
    ]
    expect(pendingRemovals([], saved)).toEqual(['a'])
  })

  it('ignores an edited-but-present row', () => {
    const saved = [
      { key: 'a', term: 'A', enabled: true, source: 'channel' as const, own: true },
    ]
    const now = [{ ...saved[0], enabled: false }]
    expect(pendingRemovals(now, saved)).toEqual([])
  })
})

describe('track rows', () => {
  it('marks a pack row inherited, never own', () => {
    const rows = toTermRows(
      layers({
        own: { terms: {}, replacements: {} },
        effective: {
          terms: { lesmo: { term: 'Lesmo', enabled: true, source: 'track' } },
          replacements: {},
        },
      }),
    )
    expect(rows[0]).toMatchObject({ source: 'track', own: false })
  })

  it('lets an own row override a pack row', () => {
    const rows = toTermRows(
      layers({
        own: { terms: { Lesmo: false }, replacements: {} },
        // own_keys is the server's own already-normalised key for the own
        // entry above (see toTermRows/normaliseKey's docstring) - required
        // here the same way every other "marks/lets an own row..." case in
        // this file sets it, or ownership falls back to false and this test
        // fails regardless of the track layer this task adds.
        own_keys: { terms: ['lesmo'], replacements: [] },
        effective: {
          terms: { lesmo: { term: 'Lesmo', enabled: false, source: 'event' } },
          replacements: {},
        },
      }),
    )
    expect(rows[0]).toMatchObject({ own: true, enabled: false })
  })
})
