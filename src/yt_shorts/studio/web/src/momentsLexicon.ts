/**
 * Pure helpers for the moments-lexicon editor (Task 7 wires these into the
 * component). No React import - unit-tested directly with Vitest, same
 * pattern as words.ts/format.ts/uploadMeta.ts.
 *
 * See api.ts's MomentsLexicon/MarkerSource for the wire shape this operates
 * on, and lexicon_admin.read's own docstring (src/yt_shorts/lexicon_admin.py)
 * for why `effective` deliberately KEEPS weight-0 entries rather than
 * dropping them the way the scoring merge does: this editor needs to show a
 * disabled marker struck through, with the layer that disabled it, not make
 * it disappear.
 */

import type { LayerSource, MarkerSource, MomentsLexicon } from './api'

/** Must match the backend's lexicon.MAX_WEIGHT (src/yt_shorts/lexicon.py). */
export const MAX_WEIGHT = 10

/** One row of the editor's table: one marker, its effective weight/source,
 * whether it is OWNED by the current scope's own layer (editable in place,
 * vs. inherited and read-only until the operator adds an override), and
 * whether it is disabled (weight === 0) regardless of which layer disabled
 * it. */
export type MarkerRow = {
  marker: string
  weight: number
  source: MarkerSource['source']
  own: boolean
  disabled: boolean
}

/** Own-first, then by descending weight, then by marker name ascending - so
 * ties are stable and alphabetical. Shared by toRows (the initial load) and
 * every row-mutating helper below that can move a row between the "own" and
 * "inherited" groups (override/disable/addOwnRow), so a row always lands in
 * the same place toRows would put it, regardless of which of these built it. */
function sortRows(rows: MarkerRow[]): MarkerRow[] {
  return [...rows].sort((a, b) => {
    if (a.own !== b.own) return a.own ? -1 : 1
    if (a.weight !== b.weight) return b.weight - a.weight
    return a.marker.localeCompare(b.marker)
  })
}

/** Every effective marker as a row, sorted own-first (what the operator can
 * edit at this scope belongs at the top), then by descending weight, then by
 * marker name ascending - so ties are stable and alphabetical. */
export function toRows(lex: MomentsLexicon): MarkerRow[] {
  const rows: MarkerRow[] = Object.entries(lex.effective).map(([marker, { weight, source }]) => ({
    marker,
    weight,
    source,
    own: Object.prototype.hasOwnProperty.call(lex.own, marker),
    disabled: weight === 0,
  }))
  return sortRows(rows)
}

/** Parses an operator-typed weight. Accepts a plain integer or decimal
 * ("2", "2.5") and a comma decimal ("2,5" - what a German keyboard produces,
 * per this operator's locale): the comma is normalised to a dot before
 * parsing, so it goes through the exact same numeric check as a dot decimal.
 * Returns null for anything that is not a FINITE number in [0, MAX_WEIGHT]:
 * empty/whitespace-only input, non-numeric text, a negative number, a number
 * above MAX_WEIGHT, and the non-finite literals "Infinity"/"NaN" that
 * `Number()` would otherwise accept.
 *
 * A malformed value with more than one decimal point, e.g. "2.5.5", is
 * rejected: swapping only the FIRST comma still leaves the second dot in
 * place, so `Number("2.5.5")` is NaN and this returns null - it is not
 * silently truncated to "2.5". */
export function parseWeight(input: string): number | null {
  const trimmed = input.trim()
  if (trimmed === '') return null
  const normalised = trimmed.replace(',', '.')
  if (!/^-?\d*\.?\d+$/.test(normalised)) return null
  const value = Number(normalised)
  if (!Number.isFinite(value)) return null
  if (value < 0 || value > MAX_WEIGHT) return null
  return value
}

/** The PUT payload for the current scope: ONLY the own rows, keyed by
 * marker. This is the single most important correctness property here - a
 * PUT overwrites exactly one layer (see lexicon_admin.update), so an
 * inherited row leaking into this map would be silently PROMOTED into the
 * current layer and stop tracking its real source. An own row with weight 0
 * is kept (it is an explicit disable written at this layer, not an absence).
 *
 * Built via Object.entries + Object.fromEntries rather than assigning into a
 * `{}` object literal one key at a time: an operator is free to type
 * `__proto__` as a marker (the backend has no reason to refuse it - it is
 * just a string), and `markers[row.marker] = row.weight` for that key does
 * NOT create an own property - it reassigns the object's PROTOTYPE instead,
 * so the marker silently vanishes from `JSON.stringify(markers)` and Save
 * would drop it without any error. Object.fromEntries has no such special
 * case - every entry, `__proto__` included, becomes a real own property. */
export function rowsToMarkers(rows: MarkerRow[]): Record<string, number> {
  return Object.fromEntries(
    rows.filter((row) => row.own).map((row) => [row.marker, row.weight] as const),
  )
}

/** Human label for a layer source. Shared by the moments editor (whose own
 * MarkerSource['source'] never carries 'track' - the lexicon's excitement
 * markers have no venue pack, unlike the glossary's track-scoped corner
 * names) and the glossary editor's LayerSource, which does - see the
 * 'track' case below. */
export function sourceLabel(source: MarkerSource['source'] | LayerSource): string {
  switch (source) {
    case 'default':
      return 'built-in'
    case 'workspace':
      return 'workspace'
    case 'channel':
      return 'channel'
    case 'event':
      return 'event'
    case 'track':
      // The venue pack an event selected (see tracks.py). Labelled by layer
      // rather than by venue name: the row already sits in that event's
      // editor, so which track is not in question - only where the row came
      // from.
      return 'track'
  }
}

/** Mirrors the backend's own normalisation (lexicon.normalise:
 * `marker.strip().lower()`) - used before every own-marker comparison and
 * before storing a freshly-typed marker, so a row added here already has
 * exactly the key a save would end up writing, and "is this a duplicate of
 * an existing own marker" compares like with like regardless of the
 * operator's capitalisation or stray whitespace. */
export function normaliseMarker(input: string): string {
  return input.trim().toLowerCase()
}

/** Copies an inherited row into this scope's own layer at its CURRENT
 * weight - the "Override" row action. A no-op if `marker` is not found (the
 * row may have been removed by a concurrent edit in the same session). */
export function overrideRow(rows: MarkerRow[], marker: string): MarkerRow[] {
  return sortRows(rows.map((row) => (row.marker === marker ? { ...row, own: true } : row)))
}

/** Writes an own entry at weight 0 for `marker` - the "Disable" row action.
 * Works on an inherited row (creates the own override) exactly like
 * `overrideRow`, just pinned to zero instead of carrying the inherited
 * weight forward. */
export function disableRow(rows: MarkerRow[], marker: string): MarkerRow[] {
  return sortRows(
    rows.map((row) => (row.marker === marker ? { ...row, own: true, weight: 0, disabled: true } : row)),
  )
}

/** Drops `marker`'s row entirely - the "Remove" row action on an own row.
 * This does NOT flip the row back to `own: false` and leave it displayed:
 * what would then show (an inherited value, the built-in default, or
 * nothing at all) cannot be known RELIABLY once the row's weight may have
 * been edited in this session (`setOwnWeight` has no flag distinguishing "a
 * freshly-overridden row, still carrying its inherited weight" from "the
 * same row, since edited" - see `setOwnWeight`), so showing a possibly-stale
 * weight under an invented "inherited" label would be actively wrong more
 * often than it would help. The true picture only exists in the response of
 * the next Save (`toRows` rebuilds the whole list from it) - until then, the
 * removed marker simply is not in the table, same as if it had never been
 * typed. The component surfaces that this is PENDING, not permanent, via
 * `pendingRemovals` below rather than by reviving the row here. */
export function removeOwnRow(rows: MarkerRow[], marker: string): MarkerRow[] {
  return rows.filter((row) => row.marker !== marker)
}

/** The markers dropped from `rows` since `savedRows` (the last load/Save) by
 * `removeOwnRow` - i.e. rows that were own as of the last save and are no
 * longer present at all. Pure so the "Remove is pending until Save" caption
 * in MomentsEditor never has to inline this comparison (see the component's
 * module docstring: all pure logic lives here). Only compares PRESENCE, not
 * weight/own - a row that is still present (even if its weight was since
 * edited) is not a pending removal, only a row that vanished from the table
 * is. */
export function pendingRemovals(rows: MarkerRow[], savedRows: MarkerRow[]): string[] {
  const current = new Set(rows.map((row) => row.marker))
  return savedRows.filter((row) => row.own && !current.has(row.marker)).map((row) => row.marker)
}

/** Updates an own row's weight in place (the per-row NumberInput) without
 * re-sorting - re-sorting on every keystroke/step would shuffle the row the
 * operator is actively editing out from under their cursor. The list
 * catches up to the new sort order on the next load/save/row-action. A
 * no-op for a row that is not own (the input is disabled for those, so this
 * should never be called with one, but it is still not a silent
 * corruption if it is). */
export function setOwnWeight(rows: MarkerRow[], marker: string, weight: number): MarkerRow[] {
  return rows.map((row) =>
    row.own && row.marker === marker ? { ...row, weight, disabled: weight === 0 } : row,
  )
}

/** Adds a brand-new own row from the "New marker" input, or - if the typed
 * marker (after normalising) matches an ALREADY-INHERITED row - promotes
 * that row to own at the typed weight instead of creating a second row for
 * the same marker (which `rowsToMarkers` would tolerate, since it only ever
 * reads own rows, but would leave two visible rows for one marker until the
 * next Save untangles it). Returns null - reject, no rows changed - for a
 * blank marker or one that already matches an EXISTING OWN row: that is a
 * real duplicate, and silently overwriting it would surprise whichever
 * weight the operator typed second. */
export function addOwnRow(rows: MarkerRow[], markerInput: string, weight: number): MarkerRow[] | null {
  const marker = normaliseMarker(markerInput)
  if (marker === '') return null
  if (rows.some((row) => row.own && row.marker === marker)) return null
  const existingIndex = rows.findIndex((row) => row.marker === marker)
  if (existingIndex !== -1) {
    return sortRows(
      rows.map((row, index) =>
        index === existingIndex ? { ...row, own: true, weight, disabled: weight === 0 } : row,
      ),
    )
  }
  return sortRows([...rows, { marker, weight, source: 'default', own: true, disabled: weight === 0 }])
}
