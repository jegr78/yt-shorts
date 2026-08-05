# Adding and removing transcript words in the studio

## Why

Whisper drops words it cannot hear. On a clip whose audio is singing rather
than speech, whole phrases are missing from the transcript, and the studio
offers no way to put them back: `WordsEditor` only ever calls
`updateWord(index, …)`, so an operator can correct a word's text and its
timings but cannot add a row or delete one. The single button per row is the
jump-to-timestamp control.

The measured shape of the problem, from the operator's own clip: `and` spans
1.36 → 4.92 and `that's` spans 4.92 → 12.36. Whisper did not leave a gap where
the missing words are — it STRETCHED the last word it recognised across them.
That is what makes 7.5 seconds land on a single word.

**This is a UI gap and nothing else.** The server accepts any number of words:
`editorial` validates that each has `start`, `end` and `text` and imposes no
relationship to the derived transcript, and `transcript.based_on` is a checksum
of the DERIVED words used for conflict detection, not a length constraint.
`captions.group_words` renders whatever list it is given. So no route, no
validation and no rendering code changes here.

## Decisions

1. **Inserting after a row splits that row's span in half.** `that's`
   4.92 → 12.36 becomes `that's` 4.92 → 8.64 plus a new empty row 8.64 → 12.36.

   The obvious alternative — put the new word in the gap after the row — does
   not work, because decoder timings are contiguous: `words[i].end` IS
   `words[i+1].start`, so there is usually no gap at all. Splitting the row is
   one rule with no special cases, needs no lookahead to the next row, and can
   never produce an overlap or reorder anything. It works identically for the
   last row and for a row that happens to have a gap after it.

2. **The split is a SEED, not a constraint.** `start` and `end` are already
   editable number inputs (step 0.1, two decimals) and stay that way; an
   inserted row is an ordinary row. The midpoint exists so a new row starts
   somewhere sensible rather than at zero.

3. **The midpoint is rounded to two decimals**, matching the inputs'
   `decimalScale={2}`. Without it a computed `8.639999999999999` would display
   as `8.64` while being stored as something else, so a value the operator
   never touched would read back differently from what they see.

   **The rounded midpoint must then be CLAMPED into the row's own span.** This
   paragraph originally claimed the only degenerate case was a row shorter than
   0.01s rounding onto its own end, giving a harmless zero-length word. Both
   halves of that were wrong, and a review proved it: rounding can push the
   midpoint BELOW the row's start — a row from 1.0111 to 1.0161 has a true
   midpoint of 1.0136, which rounds to 1.01 — so the FIRST half ends before it
   begins and (5) flags it `inverted`. The tool would put a red border on a row
   it had just created itself, which is precisely the worthless-warning failure
   (5) exists to avoid. It rounds UP past the end symmetrically.

   This is reachable on real data, not synthetic: `transcribe.py` writes
   faster-whisper's floats through unrounded, and `decimalScale={2}` affects
   only DISPLAY, so an untouched row carries whatever the model emitted — and
   short rows are exactly the ones an operator splits. Clamping to
   `[start, end]` is what makes "an insert can never create a flagged row"
   true, and that pairing deserves its own test rather than being assumed.

   A genuinely zero-length result is still possible and still harmless: it is
   neither an overlap nor an inversion under (5), and the operator retypes the
   numbers anyway.

4. **A new row's text is empty, and that renders nothing.** `captions.group_words`
   skips empty words and `editorial.normalise_word_boundaries` leaves empty text
   alone, so a row added but not yet typed into changes no output. Adding a row
   is therefore safe to do before deciding what goes in it.

5. **Overlaps are WARNED about, never blocked.** A row is flagged when its
   `start` is earlier than the previous row's `end` (`overlap`), or when its own
   `end` is before its own `start` (`inverted`). Contiguous rows — `start`
   exactly equal to the previous `end` — are the decoder's normal shape and must
   never be flagged.

   Saving stays possible with problems present. The data model permits such a
   list, and refusing to save would trap an operator mid-edit with no way out of
   a state the tool itself let them reach. This is the same stance `quota`
   already takes: warn, never block.

   **The warning must state the consequence, because it is severe.** This
   paragraph originally said "the renderer accepts it", which is false and was
   caught in review. `captions.group_words` groups an overlapping list happily,
   but `subtitle_track._validate` REFUSES a caption list that is unsorted or
   overlapping, and `subtitle_pipeline` catches that `ValueError` in its blanket
   degrade-to-"no subtitles" handler — so the clip renders with NO captions at
   all, and a studio-started render reports nothing, because the provider's
   `NOTE:` goes to stderr and never reaches the job log. The two flags differ in
   severity too: an `inverted` row is usually absorbed into its neighbour's
   caption and validates fine; it is the `overlap` that is fatal. Warning
   without blocking is still right — but the warning has to say what is at
   stake, not reassure.

6. **No auto-sorting.** Typing a start that reorders rows leaves them where they
   are and raises the warning. Silently re-sorting rows under the operator's
   cursor while they type would be worse than the problem it solves.

7. **Deleting takes no confirmation.** The change is staged, Save is explicit,
   and the unsaved-changes badge already reports it. Deleting the last remaining
   row leaves an empty list, which is a legitimate state meaning "this clip has
   no captions" — not an error to guard against.

8. **A clip with no words gets a single "Add word" seeded 0 → 1.0**, so an empty
   transcript is not a dead end with nothing to insert after.

## Where the logic lives

Three pure functions in `src/yt_shorts/studio/web/src/words.ts`, beside
`wordsEqual` — not in the component. That is this project's existing rule:
pure logic lives in its own modules so Vite's fast-refresh boundary stays
component-only, and so it is unit-testable without rendering anything.

```ts
export type WordProblemKind = 'overlap' | 'inverted'

export function insertWordAfter(words: Word[], index: number): Word[]
export function removeWord(words: Word[], index: number): Word[]
export function findWordProblems(words: Word[]): WordProblemKind[][]
```

`findWordProblems` returns an array parallel to `words` — `result[i]` holds row
i's problems and is empty for a clean row. Index-aligned rather than a map or a
filtered list, because the caller's question is always "does THIS row have a
problem", asked once per rendered row.

All three are total: an out-of-range index returns the list unchanged rather
than throwing, and `insertWordAfter` on an empty list ignores the index and
returns the single seeded word from decision 8, so the component needs no
separate code path for the empty case.

## What the component gets

`WordsEditor.tsx` gains, with no new props — add and remove are expressed
through the `onChange` it already has:

- two more `ActionIcon`s per row beside the existing `▶`: insert-after and
  delete. Plain text glyphs with `title` attributes, exactly like `▶` today —
  this frontend has no icon library and does not need one for this.
- the actions column widened from `w={40}` to fit three controls.
- `error` on a flagged row's `start`/`end` inputs — as a BOOLEAN, so Mantine
  draws the red border without adding a message line that would make rows jump
  in height as the operator types.
- one summary line above the table naming how many rows are affected, so the
  problem is visible without hunting for a red border in a scrolled list.
- the empty-state button from decision 8.

Everything else about the component is unchanged, including that `disabled`
freezes editing during a render — the new controls must respect it, or an
operator could restructure a transcript while it is being burned in.

## Testing

- **`insertWordAfter`**: splits the target row and leaves every other row
  untouched; the new row is empty-texted and takes the second half; the
  midpoint is rounded to two decimals; inserting after the LAST row works;
  inserting into an empty list yields the 0 → 1.0 seed; an out-of-range index
  changes nothing; the input array is not mutated.
- **`removeWord`**: removes the right row; removing the only row yields `[]`;
  out-of-range changes nothing; the input is not mutated.
- **`findWordProblems`**: contiguous decoder output is clean — the case that
  matters most, since flagging it would make every clip look broken; an overlap
  is flagged on the LATER row; an inverted row is flagged; a row can carry both;
  an empty list and a single word are clean.
- **`wordsEqual` already compares length**, so add and remove mark the editor
  dirty with no change to it. Pinned by a test rather than assumed, because the
  Save button is gated on `dirty`.
- **E2E**: insert a row, type into it, save, and assert `edit.json` holds one
  more word with the split timings; delete a row, save, assert one fewer. Then
  type a start that runs into the previous row and assert the warning appears —
  and that Save is still possible, which is decision 5 as a test rather than a
  comment.
- The six pinned overlay hashes and `captions.py` are untouched; nothing here
  renders.

## Out of scope

- Any server, `editorial` or renderer change. The data model already supports
  this, which is what makes the work small.
- Auto-sorting, or refusing to save a list with problems (decisions 5 and 6).
- Splitting a word by its text, or re-running Whisper on a sub-range. Both are
  plausible future features and neither is needed to type in a missing line.
- Undo. The edit is staged and reloading the clip without saving discards it,
  which is the existing escape hatch for every other edit in this editor.
