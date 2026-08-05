/**
 * Pure helpers for the glossary editor (GlossaryEditor.tsx wires these up).
 * No React import - unit-tested directly with Vitest, the same pattern
 * momentsLexicon.ts/words.ts/format.ts follow.
 *
 * See api.ts's GlossaryLayers for the wire shape, and glossary_admin.read's
 * own docstring (src/yt_shorts/glossary_admin.py) for why `effective`
 * deliberately KEEPS disabled entries rather than dropping them the way the
 * transcription merge does: this editor must show a disabled entry struck
 * through, with the layer that disabled it, not make it disappear.
 *
 * Terms and replacements are two lists with the same ownership rules, so the
 * row actions here are generic over anything carrying `{ key, own }` - one
 * implementation, not two that can drift apart.
 */

import type { GlossaryLayers, LayerSource } from './api'

/** One row of the Terms list: the decoder-bias spelling, its enabled state,
 * whether it belongs to THIS scope's own layer (editable in place, vs.
 * inherited and read-only until Override/Disable creates an own entry), and
 * the layer the effective value came from. */
export type TermRow = {
  key: string
  term: string
  enabled: boolean
  source: LayerSource
  own: boolean
}

/** One row of the Corrections list: what the decoder heard (`from`), what it
 * should say (`to`, empty when the entry is disabled), plus the same
 * ownership/source fields TermRow carries. */
export type ReplacementRow = {
  key: string
  from: string
  to: string
  enabled: boolean
  source: LayerSource
  own: boolean
}

type Row = { key: string; own: boolean; enabled: boolean }

/** Mirrors the backend's glossary.normalise_term (`term.strip().lower()`) -
 * used to compare a freshly-typed term against existing rows (see
 * addOwnTermRow) and to build a fresh row's key, so a row added here already
 * carries exactly the key a save writes. Not used to decide ownership of an
 * existing row - see toTermRows and normaliseKey's own docstring for why
 * that now comes from the server's `own_keys` instead. */
export function normaliseTerm(input: string): string {
  return input.trim().toLowerCase()
}

/** Exactly the characters Python's `string.punctuation` contains, which is
 * the set the backend's `glossary._normalized` strips - ASCII only, and that
 * limitation is the whole reason this constant is spelled out rather than
 * approximated. An earlier version of this module used
 * `/[^\p{L}\p{N}]/gu` ("keep letters and digits") on the theory that it
 * could not drift out of sync the way a hand-copied list could. It was
 * wrong, and measurably so: `\p{L}\p{N}` also strips every NON-ASCII
 * punctuation character, which Python keeps. `"O’Brien"` normalised to
 * `obrien` here and `o’brien` there - and since a smart apostrophe is what
 * macOS, iOS and Word produce by default, that is an ordinary paste, not an
 * exotic input. See normaliseKey below for what the mismatch cost. */
const PYTHON_PUNCTUATION = new Set('!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~')

/** Mirrors the backend's glossary.normalise_key: each whitespace-separated
 * token lower-cased with punctuation stripped, rejoined by single spaces.
 *
 * Ownership no longer depends on this agreeing with the backend character
 * for character - it used to, and that contract broke: a mid-token U+FEFF
 * (what a paste stitched from two UTF-8-with-BOM fragments carries) is
 * whitespace to Python's `str.split()` but not to JS's `\s`, and U+0085 is
 * the same problem in the other direction - so a hand-mirrored rule here
 * could and did disagree with the server's on real input. `toReplacementRows`
 * now determines ownership from `own_keys`, the server's own already-
 * normalised keys (see api.ts's GlossaryLayers), not from re-normalising
 * `own`'s raw keys with this function - see that function for what actually
 * fixed the class of bug this docstring used to warn about.
 *
 * This function still matters for two things `own_keys` cannot do:
 * `addOwnReplacementRow` normalises a FRESHLY TYPED key to compare it against
 * existing rows before deciding whether to add or promote one, and the
 * result is what a fresh row's `key` is built from - both purely local
 * decisions the server has not seen yet. Iterating with `[...token]` rather
 * than indexing keeps a surrogate pair intact. */
export function normaliseKey(input: string): string {
  return input
    .split(/\s+/)
    .map((token) =>
      [...token.toLowerCase()].filter((ch) => !PYTHON_PUNCTUATION.has(ch)).join(''),
    )
    .filter((token) => token !== '')
    .join(' ')
}

/** Own first (what this scope can edit belongs at the top), then enabled
 * before disabled, then alphabetically by key - stable and predictable.
 * Shared by the toRows builders and every row action that can move a row
 * between the own and inherited groups, so a row always lands where a fresh
 * load would put it. */
function sortRows<T extends Row>(rows: T[]): T[] {
  return [...rows].sort((a, b) => {
    if (a.own !== b.own) return a.own ? -1 : 1
    if (a.enabled !== b.enabled) return a.enabled ? -1 : 1
    return a.key.localeCompare(b.key)
  })
}

/** Every effective term as a row. Ownership comes from `layers.own_keys.terms`
 * - the server's own already-normalised keys for this scope's own layer -
 * rather than from re-normalising `layers.own.terms`' raw keys here (see
 * normaliseKey's docstring for why that used to be required, and what broke
 * because of it). */
export function toTermRows(layers: GlossaryLayers): TermRow[] {
  const ownKeys = new Set(layers.own_keys.terms)
  return sortRows(
    Object.entries(layers.effective.terms).map(([key, entry]) => ({
      key,
      term: entry.term,
      enabled: entry.enabled,
      source: entry.source,
      own: ownKeys.has(key),
    })),
  )
}

/** Every effective replacement as a row. A disabled entry has no target text
 * to show (the merge keeps only the winning value, which is null), so `to`
 * is empty - the row still names what it matches, which is the half that
 * identifies it. Ownership comes from `layers.own_keys.replacements` - see
 * toTermRows above for why that, and not a client-side re-normalisation of
 * `layers.own.replacements`' raw keys, is what decides it now. */
export function toReplacementRows(layers: GlossaryLayers): ReplacementRow[] {
  const ownKeys = new Set(layers.own_keys.replacements)
  return sortRows(
    Object.entries(layers.effective.replacements).map(([key, entry]) => ({
      key,
      from: entry.key,
      to: entry.value ?? '',
      enabled: entry.value !== null,
      source: entry.source,
      own: ownKeys.has(key),
    })),
  )
}

/** Copies an inherited row into this scope's own layer at its current value -
 * the "Override" row action. A no-op if `key` is not found. */
export function overrideRow<T extends Row>(rows: T[], key: string): T[] {
  return sortRows(rows.map((row) => (row.key === key ? { ...row, own: true } : row)))
}

/** Writes an own entry marked disabled for `key` - the "Disable" row action.
 * Works on an inherited row exactly like overrideRow, just pinned to
 * disabled instead of carrying the inherited state forward. */
export function disableRow<T extends Row>(rows: T[], key: string): T[] {
  return sortRows(
    rows.map((row) => (row.key === key ? { ...row, own: true, enabled: false } : row)),
  )
}

/** Writes an own entry marked enabled for `key` - the "Enable" row action.
 * Exists because overrideRow copies the row's CURRENT enabled state: a row
 * that some less-specific layer disabled is still disabled after Override,
 * and neither a term row (no editable field at all) nor a disabled
 * correction's target (its TextInput only renders for an own row that is
 * ALSO enabled) then gives the operator anything to click - the only
 * remaining action is Remove, which reverts to the same disabled inherited
 * value they were trying to change. This is the escape hatch: works on an
 * inherited row exactly like disableRow, just pinned to enabled instead of
 * disabled. A no-op if `key` is not found. */
export function enableRow<T extends Row>(rows: T[], key: string): T[] {
  return sortRows(
    rows.map((row) => (row.key === key ? { ...row, own: true, enabled: true } : row)),
  )
}

/** Drops `key`'s row entirely - the "Remove" action on an own row. It does
 * NOT flip the row back to inherited and leave it displayed: what would then
 * show (an inherited value, the built-in default, or nothing) cannot be known
 * reliably once the row may have been edited in this session, and a stale
 * value under an invented "inherited" label would be wrong more often than
 * helpful. The true picture arrives with the next Save's response; until
 * then the component surfaces the pending state via pendingRemovals. */
export function removeOwnRow<T extends Row>(rows: T[], key: string): T[] {
  return rows.filter((row) => row.key !== key)
}

/** Edits an own replacement's target text in place WITHOUT re-sorting -
 * re-sorting on every keystroke would shuffle the row out from under the
 * operator's cursor. A no-op for an inherited row. */
export function setReplacementText(
  rows: ReplacementRow[],
  key: string,
  to: string,
): ReplacementRow[] {
  return rows.map((row) => (row.own && row.key === key ? { ...row, to } : row))
}

/** Adds a brand-new own term, or - if the typed term already matches an
 * INHERITED row - promotes and enables that row rather than creating a second
 * row for the same key. Returns null (reject, nothing changed) for a blank
 * term or one that already matches an existing OWN row: that is a real
 * duplicate, and silently overwriting it would surprise the operator.
 *
 * The promote branch carries the TYPED spelling through, not the inherited
 * row's - typing "HOHE ACHT" while the default carries "Hohe Acht" must
 * promote at the spelling the operator typed. A term row renders no editable
 * field, so getting this wrong silently strands the inherited spelling at
 * this scope with no way to change it afterwards short of Remove-then-Add. */
export function addOwnTermRow(rows: TermRow[], termInput: string): TermRow[] | null {
  const term = termInput.trim()
  const key = normaliseTerm(term)
  if (key === '') return null
  if (rows.some((row) => row.own && row.key === key)) return null
  const index = rows.findIndex((row) => row.key === key)
  if (index !== -1) {
    return sortRows(
      rows.map((row, i) => (i === index ? { ...row, term, own: true, enabled: true } : row)),
    )
  }
  return sortRows([...rows, { key, term, enabled: true, source: 'default', own: true }])
}

/** Adds a brand-new own correction, or promotes an inherited one at the typed
 * target text. Returns null for a blank `from`/`to`, a `from` that normalises
 * to nothing (punctuation only - the backend refuses it too), or a duplicate
 * of an existing own row. */
export function addOwnReplacementRow(
  rows: ReplacementRow[],
  fromInput: string,
  toInput: string,
): ReplacementRow[] | null {
  const from = fromInput.trim()
  const to = toInput.trim()
  const key = normaliseKey(from)
  if (key === '' || to === '') return null
  if (rows.some((row) => row.own && row.key === key)) return null
  const index = rows.findIndex((row) => row.key === key)
  if (index !== -1) {
    return sortRows(
      rows.map((row, i) => (i === index ? { ...row, own: true, enabled: true, to } : row)),
    )
  }
  return sortRows([...rows, { key, from, to, enabled: true, source: 'default', own: true }])
}

/** The PUT payload for the current scope: ONLY the own rows. This is the most
 * important correctness property here - a PUT overwrites exactly one layer
 * (see glossary_admin.update), so an inherited row leaking into this payload
 * would be silently PROMOTED into the current layer and stop tracking its
 * real source. A disabled own row is kept as an explicit `false`/`null`: it
 * is a deliberate disable written at this layer, not an absence.
 *
 * Built via Object.fromEntries rather than assigning into a `{}` literal: an
 * operator is free to type `__proto__` as a term (the backend has no reason
 * to refuse a plain string), and `payload[key] = value` for that key does NOT
 * create an own property - it reassigns the object's PROTOTYPE, so the entry
 * silently vanishes from JSON.stringify and Save would drop it with no error. */
export function rowsToOwn(
  termRows: TermRow[],
  replacementRows: ReplacementRow[],
): { terms: Record<string, boolean>; replacements: Record<string, string | null> } {
  return {
    terms: Object.fromEntries(
      termRows.filter((row) => row.own).map((row) => [row.term, row.enabled] as const),
    ),
    replacements: Object.fromEntries(
      replacementRows
        .filter((row) => row.own)
        .map((row) => [row.from, row.enabled ? row.to : null] as const),
    ),
  }
}

/** Keys of own, ENABLED correction rows whose target text is blank (including
 * whitespace-only) - the sequence that arises when Enable (see enableRow
 * above) turns a disabled row own-and-enabled while its `to` is still the
 * empty string a disabled row always renders. This cannot simply be sent to
 * Save: rowsToOwn would put it through as `""`, and the backend's
 * glossary._parse_replacements refuses an empty replacement outright ("use
 * null to disable it"), because an empty replacement makes glossary.apply
 * DELETE every matched word rather than replace it - a disable is expressed
 * as an explicit `null`, never `""`. This lets the component refuse Save and
 * name the row before that 400 ever reaches the server. */
export function incompleteReplacements(rows: ReplacementRow[]): string[] {
  return rows
    .filter((row) => row.own && row.enabled && row.to.trim() === '')
    .map((row) => row.key)
}

/** The keys dropped from `rows` since `savedRows` (the last load/Save) by
 * removeOwnRow - rows that were own as of the last save and are no longer
 * present at all. Pure so the "Remove is pending until Save" caption never
 * has to inline this comparison. Compares PRESENCE only: a row that is still
 * there, even if edited, is not a pending removal. */
export function pendingRemovals<T extends Row>(rows: T[], savedRows: T[]): string[] {
  const current = new Set(rows.map((row) => row.key))
  return savedRows.filter((row) => row.own && !current.has(row.key)).map((row) => row.key)
}
