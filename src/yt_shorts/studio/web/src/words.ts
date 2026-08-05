import type { Word } from './api'

/** Shared by ClipEditor (to compute its "dirty"/unsaved-changes state) and
 * PreviewPane (to decide whether the staged transcript still matches the
 * saved state, i.e. whether the preview should GET the saved state or
 * POST the client's own unsaved words - see PreviewPane's own docstring).
 * Kept in its own module rather than exported from a component file so
 * Vite's fast-refresh boundary stays component-only. */
export function wordsEqual(a: Word[], b: Word[]): boolean {
  if (a.length !== b.length) return false
  return a.every((word, i) => word.start === b[i].start && word.end === b[i].end && word.text === b[i].text)
}

/** A hand-typed timing that cannot be right: `overlap` means this row starts
 * before the previous row ended, `inverted` means it ends before it starts. */
export type WordProblemKind = 'overlap' | 'inverted'

/** Two decimals, matching the editor's NumberInput decimalScale - so a seeded
 * value reads back as exactly what the operator was shown. */
function round2(value: number): number {
  return Math.round(value * 100) / 100
}

/**
 * Inserts an empty word after `index` by splitting that row's span in half.
 *
 * It splits rather than filling the gap after the row because there usually
 * IS no gap: faster-whisper's timings are contiguous, `words[i].end` being
 * exactly `words[i+1].start`. Where words are missing it does not leave a
 * hole - it STRETCHES the last word it recognised across them, which is how
 * one word ends up spanning 7.5 seconds. Splitting is also one rule with no
 * special cases: it needs no lookahead, works identically on the last row,
 * and can never produce an overlap or reorder anything.
 *
 * The new row's text is empty, which renders nothing (captions.group_words
 * skips empty words), so adding a row before deciding what goes in it is
 * safe. The split is only a SEED - both timings stay editable.
 *
 * Total by design: an empty list yields a single seeded word regardless of
 * the index (so the caller needs no separate empty-state path), and an
 * out-of-range index returns the list unchanged rather than throwing.
 */
export function insertWordAfter(words: Word[], index: number): Word[] {
  if (words.length === 0) return [{ start: 0, end: 1, text: '' }]
  if (index < 0 || index >= words.length) return [...words]
  const target = words[index]
  // CLAMPED into the row's own span, because rounding can push the midpoint
  // outside it: a row from 1.0111 to 1.0161 has a true midpoint of 1.0136,
  // which rounds to 1.01 - BELOW its own start. The first half would then end
  // before it began, and findWordProblems would put a red border on a row this
  // function had just created: exactly the false positive that makes a warning
  // worthless. Decoder timings are not 2-decimal-clean - transcribe.py writes
  // faster-whisper's floats through unrounded, and the editor's decimalScale
  // only affects DISPLAY - so an untouched row carries whatever the model
  // emitted, and short rows are precisely the ones an operator splits. The
  // clamp is what makes "an insert can never create a flagged row" true.
  const middle = Math.min(
    target.end,
    Math.max(target.start, round2(target.start + (target.end - target.start) / 2)),
  )
  return [
    ...words.slice(0, index),
    { ...target, end: middle },
    { start: middle, end: target.end, text: '' },
    ...words.slice(index + 1),
  ]
}

/** Removes one row. Emptying the list is allowed: it means "this clip has no
 * captions", not an error. An out-of-range index changes nothing. */
export function removeWord(words: Word[], index: number): Word[] {
  if (index < 0 || index >= words.length) return [...words]
  return [...words.slice(0, index), ...words.slice(index + 1)]
}

/**
 * Timing problems per row, index-aligned with `words` - `result[i]` is row
 * i's problems and is empty for a clean row. Index-aligned rather than a map
 * or a filtered list because the caller's question is always "does THIS row
 * have a problem", asked once per rendered row.
 *
 * Contiguous rows are CLEAN. Decoder output has `words[i].end` exactly equal
 * to `words[i+1].start`, so flagging that would put a red border on every row
 * of every clip and make the warning worthless.
 */
export function findWordProblems(words: Word[]): WordProblemKind[][] {
  return words.map((word, index) => {
    const problems: WordProblemKind[] = []
    if (index > 0 && word.start < words[index - 1].end) problems.push('overlap')
    if (word.end < word.start) problems.push('inverted')
    return problems
  })
}
