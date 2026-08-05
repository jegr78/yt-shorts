# Transcript Add/Remove Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An operator can add a missing transcript word and delete a spurious one in the studio, and is warned when a hand-typed timing overlaps its neighbour.

**Architecture:** Whisper drops words it cannot hear and stretches the last word it recognised across them, so there is no gap to insert into — inserting after a row splits that row's span in half. The insert, remove and overlap-check rules are pure functions in `words.ts`; `WordsEditor.tsx` gains two buttons per row, a red border on a flagged row and a summary line. Nothing server-side changes: the routes, `editorial` and the renderer already accept any number of words.

**Tech Stack:** React + Mantine + TypeScript, Vitest (jsdom), pytest + Playwright.

## Global Constraints

- Frontend gates, run from `src/yt_shorts/studio/web`: `npx tsc -b`, `npm run lint` (oxlint), `npm test` (Vitest). All three clean before every commit.
- Only Task 3 runs `npm run build` and commits `src/yt_shorts/studio/static/`. The Playwright E2E serves that COMMITTED bundle, so a frontend change is invisible to the E2E until it is rebuilt.
- **No Python file changes in Tasks 1 and 2.** This feature is client-only: `editorial` validates only that each word has `start`/`end`/`text`, `transcript.based_on` is a checksum of the DERIVED words (conflict detection, not a length constraint), and `captions.group_words` renders whatever list it is given. If any task seems to need a server change, stop and report — it means the plan is wrong.
- `src/yt_shorts/captions.py` must not appear in this branch's diff and the six SHA-256 hashes in `tests/test_event_layer_no_regression.py` must never be re-pinned. Nothing here renders.
- `python3 tools/lint.py` (NO `PYTHONPATH`) must print `All checks passed!` before any commit that touches Python or tests.
- Pure logic lives in `words.ts`, never exported from a component file — that is what keeps Vite's fast-refresh boundary component-only.
- **Contiguous rows are NOT a problem.** Decoder output has `words[i].end === words[i+1].start`; flagging that would make every clip look broken.
- **Warn, never block.** Saving stays possible with overlaps present, the same stance `quota` takes.
- The `disabled` prop freezes editing while a render runs. The new controls must respect it, or an operator could restructure a transcript while it is being burned in.
- Test hygiene: a test that would still pass if the behaviour under test were removed is a defect. This project has twice shipped a frontend guard that could not fail.
- macOS has no bare `timeout`; use `gtimeout <seconds> <cmd>` to bound anything that might hang.

## File structure

| File | Responsibility |
|---|---|
| `src/yt_shorts/studio/web/src/words.ts` | `insertWordAfter`, `removeWord`, `findWordProblems` — all pure, all total, beside the existing `wordsEqual` |
| `.../words.test.ts` | their unit tests, beside the existing `wordsEqual` cases |
| `.../components/WordsEditor.tsx` | the two buttons, the flagged-row styling, the summary line, the empty state |
| `tests/test_studio_e2e.py` | the end-to-end guard — the only thing that can catch a component regression here |

---

### Task 1: The rules, as pure functions

**Files:**
- Modify: `src/yt_shorts/studio/web/src/words.ts` (currently only `wordsEqual`, 13 lines)
- Test: `src/yt_shorts/studio/web/src/words.test.ts`

**Interfaces:**
- Produces: `insertWordAfter(words: Word[], index: number): Word[]`, `removeWord(words: Word[], index: number): Word[]`, `findWordProblems(words: Word[]): WordProblemKind[][]`, and the exported type `WordProblemKind = 'overlap' | 'inverted'`. `Word` is `{ start: number; end: number; text: string }`, imported from `./api` as `wordsEqual` already does.

- [ ] **Step 1: Write the failing tests**

Append to `src/yt_shorts/studio/web/src/words.test.ts`. The file already has a
`w(start, end, text)` helper at the top and imports `wordsEqual` from
`./words` — extend that import rather than adding a second one.

```ts
describe('insertWordAfter', () => {
  it('splits the target row in half and leaves the rest alone', () => {
    // Whisper does not leave a GAP where words are missing - it stretches the
    // last word it recognised across them. Measured on the operator's clip:
    // "that's" spanning 4.92 -> 12.36, 7.5 seconds on one word. So an insert
    // takes its time from the row it follows, not from empty space.
    const words = [w(1.36, 4.92, ' and'), w(4.92, 12.36, " that's"), w(12.36, 12.6, ' all')]
    expect(insertWordAfter(words, 1)).toEqual([
      w(1.36, 4.92, ' and'),
      w(4.92, 8.64, " that's"),
      w(8.64, 12.36, ''),
      w(12.36, 12.6, ' all'),
    ])
  })

  it('rounds the midpoint to two decimals', () => {
    // The inputs render with decimalScale={2}. An unrounded 8.639999999999999
    // would DISPLAY as 8.64 while being stored as something else, so a value
    // the operator never touched would read back differently from what they
    // see.
    const [, inserted] = insertWordAfter([w(0, 0.7, 'x')], 0)
    expect(inserted.start).toBe(0.35)
    expect(insertWordAfter([w(0, 1 / 3, 'x')], 0)[1].start).toBe(0.17)
  })

  it('gives the new row empty text, which renders nothing', () => {
    // captions.group_words skips empty words and normalise_word_boundaries
    // leaves them alone, so adding a row before deciding what goes in it
    // changes no output.
    expect(insertWordAfter([w(0, 2, 'x')], 0)[1].text).toBe('')
  })

  it('works after the last row', () => {
    expect(insertWordAfter([w(0, 1, 'a'), w(1, 3, 'b')], 1)).toEqual([
      w(0, 1, 'a'), w(1, 2, 'b'), w(2, 3, ''),
    ])
  })

  it('seeds a first word into an empty transcript', () => {
    // Decision 8: an empty transcript must not be a dead end with no row to
    // insert after. The index is ignored here, which is what lets the
    // component call this one function for both cases.
    expect(insertWordAfter([], 0)).toEqual([w(0, 1, '')])
    expect(insertWordAfter([], -1)).toEqual([w(0, 1, '')])
  })

  it('changes nothing for an out-of-range index', () => {
    const words = [w(0, 1, 'a')]
    expect(insertWordAfter(words, 5)).toEqual(words)
    expect(insertWordAfter(words, -1)).toEqual(words)
  })

  it('does not mutate its argument', () => {
    const words = [w(0, 2, 'a')]
    insertWordAfter(words, 0)
    expect(words).toEqual([w(0, 2, 'a')])
  })
})

describe('removeWord', () => {
  it('removes the row at the index', () => {
    expect(removeWord([w(0, 1, 'a'), w(1, 2, 'b'), w(2, 3, 'c')], 1)).toEqual([
      w(0, 1, 'a'), w(2, 3, 'c'),
    ])
  })

  it('can empty the list', () => {
    // A legitimate state meaning "this clip has no captions", not an error.
    expect(removeWord([w(0, 1, 'a')], 0)).toEqual([])
  })

  it('changes nothing for an out-of-range index', () => {
    const words = [w(0, 1, 'a')]
    expect(removeWord(words, 3)).toEqual(words)
    expect(removeWord(words, -1)).toEqual(words)
  })

  it('does not mutate its argument', () => {
    const words = [w(0, 1, 'a'), w(1, 2, 'b')]
    removeWord(words, 0)
    expect(words).toHaveLength(2)
  })
})

describe('findWordProblems', () => {
  it('treats contiguous decoder output as clean', () => {
    // THE case that matters most: faster-whisper emits words[i].end ===
    // words[i+1].start. Flagging that would put a red border on every row of
    // every clip and make the warning worthless.
    expect(findWordProblems([w(0, 1.5, 'a'), w(1.5, 2, 'b'), w(2, 3, 'c')])).toEqual([[], [], []])
  })

  it('flags an overlap on the later row', () => {
    // The later row is the one whose start is wrong, and the one the operator
    // just typed into.
    expect(findWordProblems([w(0, 2, 'a'), w(1.5, 3, 'b')])).toEqual([[], ['overlap']])
  })

  it('flags a row whose end precedes its start', () => {
    expect(findWordProblems([w(5, 4, 'a')])).toEqual([['inverted']])
  })

  it('can report both problems on one row', () => {
    expect(findWordProblems([w(0, 3, 'a'), w(2, 1, 'b')])).toEqual([[], ['overlap', 'inverted']])
  })

  it('treats a zero-length row as clean', () => {
    // Reachable by splitting a row shorter than 0.01s (the rounding case in
    // insertWordAfter). Harmless: it is neither an overlap nor an inversion,
    // and it renders nothing.
    expect(findWordProblems([w(1, 1, 'a')])).toEqual([[]])
  })

  it('is clean for an empty list and a single word', () => {
    expect(findWordProblems([])).toEqual([])
    expect(findWordProblems([w(0, 1, 'a')])).toEqual([[]])
  })
})

describe('wordsEqual after a structural change', () => {
  it('sees an added row', () => {
    // The Save button is gated on `dirty`, which is computed with wordsEqual.
    // If length were not compared, adding a row would leave Save disabled and
    // the whole feature would be unreachable.
    const words = [w(0, 2, 'a')]
    expect(wordsEqual(words, insertWordAfter(words, 0))).toBe(false)
  })

  it('sees a removed row', () => {
    const words = [w(0, 1, 'a'), w(1, 2, 'b')]
    expect(wordsEqual(words, removeWord(words, 1))).toBe(false)
  })
})
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd src/yt_shorts/studio/web && npm test -- words
```
Expected: FAIL — `insertWordAfter`, `removeWord` and `findWordProblems` are not exported from `./words`.

- [ ] **Step 3: Implement**

Append to `src/yt_shorts/studio/web/src/words.ts`:

```ts
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
  const middle = round2(target.start + (target.end - target.start) / 2)
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
```

- [ ] **Step 4: Run every frontend gate**

```bash
cd src/yt_shorts/studio/web
npm test -- words
npx tsc -b
npm run lint
npm test
```
Expected: the new cases pass, tsc exit 0, oxlint clean, the whole Vitest suite green.

- [ ] **Step 5: Mutation-check the two rules most likely to be "simplified"**

Both of these have bitten this project's frontend before, so prove the tests bite:

1. In `findWordProblems`, change `word.start < words[index - 1].end` to `<=`. Run `npm test -- words`. Expected: `treats contiguous decoder output as clean` FAILS. Restore.
2. In `insertWordAfter`, drop `round2` (use the raw midpoint). Run `npm test -- words`. Expected: `rounds the midpoint to two decimals` FAILS. Restore.

Report the real failure output for each. Restore by editing the line back, not with `git checkout`.

- [ ] **Step 6: Commit**

```bash
git add src/yt_shorts/studio/web/src/words.ts src/yt_shorts/studio/web/src/words.test.ts
git commit -m "feat(studio-web): insert, remove and overlap-check rules for transcript words"
```

---

### Task 2: The editor offers them

**Files:**
- Modify: `src/yt_shorts/studio/web/src/components/WordsEditor.tsx` (whole component, currently ~115 lines)

**Interfaces:**
- Consumes: `insertWordAfter`, `removeWord`, `findWordProblems` from `../words` (Task 1). No new props — add and remove are expressed through the existing `onChange: (words: Word[]) => void`.

- [ ] **Step 1: Wire the three functions into the component**

In `WordsEditor.tsx`, extend the import from `@mantine/core` with `Alert` and
`Stack`, add `import { findWordProblems, insertWordAfter, removeWord } from '../words'`,
and compute the problems once per render, above the returned JSX:

```tsx
  const problems = findWordProblems(words)
  const flagged = problems.filter((row) => row.length > 0).length
```

- [ ] **Step 2: Add the two buttons to the actions column**

Replace the actions `Table.Td` (the one holding the `▶` `ActionIcon`) with:

```tsx
              <Table.Td>
                <Group gap={0} justify="center" wrap="nowrap">
                  <ActionIcon
                    variant="subtle"
                    color="steel"
                    size="sm"
                    title={`Jump preview to ${word.start.toFixed(1)}s`}
                    onClick={() => onJumpTo(word.start)}
                  >
                    ▶
                  </ActionIcon>
                  <ActionIcon
                    variant="subtle"
                    color="steel"
                    size="sm"
                    title="Insert a word after this one (splits its time in half)"
                    disabled={disabled}
                    onClick={() => onChange(insertWordAfter(words, index))}
                  >
                    +
                  </ActionIcon>
                  <ActionIcon
                    variant="subtle"
                    color="steel"
                    size="sm"
                    title="Remove this word"
                    disabled={disabled}
                    onClick={() => onChange(removeWord(words, index))}
                  >
                    ✕
                  </ActionIcon>
                </Group>
              </Table.Td>
```

The jump control stays enabled while `disabled` is set — it only moves the
preview and writes nothing, exactly as the component's own docstring says.
The two new controls are editing actions and must be disabled with the rest,
or an operator could restructure a transcript while it is being burned in.

- [ ] **Step 3: Widen the actions column**

The header cell for that column is `<Table.Th w={40} />`. Three controls no
longer fit; change it to:

```tsx
            <Table.Th w={110} />
```

- [ ] **Step 4: Mark a flagged row**

On the row's `start` and `end` `NumberInput`s, add an `error` prop. Pass a
BOOLEAN, not a string: Mantine renders a string as a message line beneath the
input, which would make rows change height as the operator types.

On the `start` input:

```tsx
                  error={problems[index].includes('overlap')}
```

On the `end` input:

```tsx
                  error={problems[index].includes('inverted')}
```

- [ ] **Step 5: Add the summary line and the empty state**

Wrap the existing `ScrollArea.Autosize` in a `Stack gap="xs"`, with the
summary above it, and render the empty state instead of the table when there
are no words:

```tsx
  if (words.length === 0) {
    return (
      <Stack gap="xs" align="flex-start">
        <Text size="sm" c="dimmed">
          No transcript words for this clip.
        </Text>
        <Button
          size="xs"
          variant="light"
          color="steel"
          disabled={disabled}
          onClick={() => onChange(insertWordAfter(words, 0))}
        >
          Add word
        </Button>
      </Stack>
    )
  }
```

and, in the normal return:

```tsx
  return (
    <Stack gap="xs">
      {flagged > 0 && (
        <Alert color="orange" variant="light" p="xs">
          <Text size="xs">
            {flagged === 1 ? '1 row has' : `${flagged} rows have`} a timing that
            overlaps the previous word or ends before it starts. The short will
            still render and you can still save - check the highlighted rows.
          </Text>
        </Alert>
      )}
      <ScrollArea.Autosize ...unchanged>
        ...unchanged table...
      </ScrollArea.Autosize>
    </Stack>
  )
```

Add `Button` to the `@mantine/core` import. Keep the existing
`ScrollArea.Autosize` props exactly as they are — this project treats
scrolling as a mandatory acceptance criterion, and the transcript table is
one of the regions that must keep scrolling at a short viewport.

- [ ] **Step 6: Run every frontend gate**

```bash
cd src/yt_shorts/studio/web
npx tsc -b
npm run lint
npm test
```
Expected: tsc exit 0, oxlint clean, all Vitest pass.

Do NOT run `npm run build` — Task 3 owns the committed bundle.

- [ ] **Step 7: Commit**

```bash
git add src/yt_shorts/studio/web/src/components/WordsEditor.tsx
git commit -m "feat(studio-web): add and remove transcript rows, and warn on overlapping timings"
```

---

### Task 3: E2E, docs, bundle, full verification

**Files:**
- Modify: `CLAUDE.md`
- Test: `tests/test_studio_e2e.py` (new class)
- Modify: `src/yt_shorts/studio/static/**` (rebuilt, committed)

- [ ] **Step 1: Add the E2E guard**

Append a new class to `tests/test_studio_e2e.py`, immediately after
`class TestStaleShortRefresh` and before the class that follows it (report
where you placed it). The names used here — `clip_entry`, `CLIP_URL`,
`editor_url`, `clipstore`, `editorial`, `json`, and the `event_dir` /
`live_server` / `page` fixtures — are the ones neighbouring tests use; the
wait-for-save block is copied from
`TestSavingReachesEditJson.test_editing_a_word_and_saving_records_based_on`,
which is this file's established way of waiting out a save round-trip.

```python
class TestTranscriptAddRemove:
    """Whisper drops words it cannot hear - on sung audio, whole phrases.
    The editor could only ever update a row in place, so there was no way to
    type a missing line back in. This is the only automated guard on the
    buttons: the pure rules are unit-tested in words.test.ts, but nothing
    except an E2E proves they are wired to anything.
    """

    def _seed(self, event_dir):
        directory = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        derived = [{"start": 0.0, "end": 1.0, "text": " here"},
                   {"start": 1.0, "end": 9.0, "text": " stretched"}]
        clipstore.transcript_path(directory).write_text(
            json.dumps({"model": "small", "source": CLIP_URL, "words": derived}))
        return directory

    def _save(self, page):
        page.get_by_role("button", name="Save changes").click()
        page.wait_for_timeout(50)
        page.wait_for_function(
            "Array.from(document.querySelectorAll('button')).some("
            "b => b.textContent.includes('Save changes') && b.disabled)"
        )

    def test_inserting_a_word_splits_the_row_and_reaches_edit_json(
            self, event_dir, live_server, page):
        """The operator's case: a word Whisper stretched across the words it
        missed, split so the missing one can be typed in."""
        directory = self._seed(event_dir)

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Speedy!").click()

        row = page.get_by_role("row").filter(has_text="stretched")
        row.get_by_title("Insert a word after this one (splits its time in half)").click()

        # The new row is the third body row (header is row 0). `.last` is
        # scoped to that single row, where the text field is the last input -
        # not a bare page-wide .last, which is how this file was caught before.
        page.get_by_role("row").nth(3).get_by_role("textbox").last.fill("missing")
        self._save(page)

        saved = editorial.load(directory)
        assert saved.transcript["words"] == [
            {"start": 0.0, "end": 1.0, "text": " here"},
            {"start": 1.0, "end": 5.0, "text": " stretched"},
            {"start": 5.0, "end": 9.0, "text": " missing"},
        ], saved.transcript["words"]

    def test_removing_a_word_reaches_edit_json(self, event_dir, live_server, page):
        directory = self._seed(event_dir)

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Speedy!").click()

        page.get_by_role("row").filter(has_text="stretched").get_by_title(
            "Remove this word").click()
        self._save(page)

        saved = editorial.load(directory)
        assert [word["text"] for word in saved.transcript["words"]] == [" here"]

    def test_an_overlapping_timing_warns_but_still_saves(
            self, event_dir, live_server, page):
        """Decision 5 as a test rather than a comment: the warning appears AND
        the save still goes through. Refusing to save would trap an operator
        in a state the tool itself let them reach."""
        directory = self._seed(event_dir)

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Speedy!").click()

        # Drag the second row's start back into the first row's span.
        row = page.get_by_role("row").filter(has_text="stretched")
        row.get_by_role("textbox").first.fill("0.5")

        warning = page.get_by_text("overlaps the previous word")
        warning.wait_for(timeout=5000)

        self._save(page)
        saved = editorial.load(directory)
        assert saved.transcript["words"][1]["start"] == 0.5

    def test_inserting_does_not_strand_focus_in_another_words_field(
            self, event_dir, live_server, page):
        """Rows are keyed by array index. That was harmless while the list
        could only be edited in place; an insert now shifts every later row's
        key, so React reuses the DOM nodes by POSITION and a focused input can
        end up showing a different word than the one the operator was editing.

        A review demonstrated exactly that with React Testing Library - but
        RTL's synthetic click does not move focus the way a real click on a
        button does, so the demonstration may be an artifact. This settles it
        in a real browser, which is the only place the answer counts.

        Passing means focus went to the button (the normal browser behaviour)
        or stayed on the same word. Failing means the operator's next
        keystrokes would land in a different word's box with nothing but the
        changed content to warn them.
        """
        directory = self._seed(event_dir)
        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Speedy!").click()

        rows = page.get_by_role("row")
        # Put the caret in the LATER row, then insert above it.
        rows.filter(has_text="stretched").get_by_role("textbox").last.click()
        rows.filter(has_text="here").get_by_title(
            "Insert a word after this one (splits its time in half)").click()

        focused = page.evaluate(
            "() => { const el = document.activeElement;"
            " return el && el.tagName === 'INPUT' ? el.value : null }"
        )
        assert focused in (None, "stretched"), (
            f"focus was left in a text field showing {focused!r} - the "
            "operator's next keystrokes would edit the wrong word"
        )
```

If this test FAILS, do not weaken it and do not paper over it: report it, and
say what `focused` actually was. It means the index-key defect is real in a
browser and the fix belongs in `WordsEditor.tsx` — most likely by moving focus
to the newly inserted row's text field, which is where the operator wants it
anyway.

The `nth(3)` and `.first` locators above are the plan's best guess at the
rendered structure (a header row plus three body rows; start being the first
textbox in a row). If either resolves to the wrong element, FIX THE SELECTOR
and report exactly what you changed and why — never weaken an assertion. This
file has been caught twice by a locator that resolved to the wrong thing,
including a bare `.last` on a control two editors both render.

- [ ] **Step 2: Build the bundle, then run the new E2E**

The E2E serves the COMMITTED bundle, so Tasks 1 and 2 are invisible to it
until this build runs. Build BEFORE pytest.

```bash
cd src/yt_shorts/studio/web
npm run lint
npx tsc -b
npm test
npm run build
cd -
PYTHONPATH=src .venv/bin/pytest tests/test_studio_e2e.py -q -k "TranscriptAddRemove"
```
Expected: every frontend gate clean, build exit 0, all three tests pass.

- [ ] **Step 3: Mutation-check the guard**

The E2E is the only thing that proves the buttons are wired up, so prove it
fails when they are not. Each mutation needs a rebuild before the E2E sees it.

1. In `WordsEditor.tsx`, change the insert button's handler to `onClick={() => undefined}`, rebuild, run the class. Expected: the insert test FAILS.
2. Restore, rebuild. Change the remove button's handler the same way, rebuild, run. Expected: the remove test FAILS.
3. Restore, rebuild. Remove the `Alert` block, rebuild, run. Expected: the overlap test FAILS on the warning, and NOT on the save assertion.
4. Restore, rebuild, confirm all three pass and `git status` shows only the intended changes.

Report the real outcome of each. Restore by editing the code back, never with `git checkout` on a file that also holds changes you want to keep.

- [ ] **Step 4: Update `CLAUDE.md`**

In the Architecture section, immediately after the paragraph beginning
**"A word's text carries its own boundary, and the studio's text field
cannot."**, insert:

```markdown
**The transcript editor can add and remove rows, and the timings it writes are
advisory.** Whisper drops words it cannot hear - on sung audio, whole phrases -
and it does NOT leave a gap where they belong: it stretches the last word it
recognised across them, which is how one word ends up spanning 7.5 seconds.
So `words.ts`'s `insertWordAfter` splits the target row's span in half rather
than filling the gap after it, because decoder timings are contiguous
(`words[i].end` IS `words[i+1].start`) and there is usually no gap at all. One
rule, no lookahead, works identically on the last row, and it cannot produce an
overlap. The split is only a SEED - both numbers stay editable, as they always
were.

`findWordProblems` flags a row whose `start` precedes the previous row's `end`,
or whose `end` precedes its own `start`. **Contiguous rows are clean** - that is
the decoder's normal shape, and flagging it would put a red border on every row
of every clip. It WARNS and never blocks: the data model permits such a list,
`captions.group_words` renders it, and refusing to save would trap an operator
in a state the tool itself let them reach. Same stance as `quota`. There is
deliberately no auto-sorting either - re-ordering rows under the operator's
cursor while they type is worse than the problem it solves.

All three rules are pure functions in `words.ts` beside `wordsEqual`, not in the
component, so they are unit-tested without rendering and Vite's fast-refresh
boundary stays component-only. Nothing server-side was needed for any of this:
`editorial` validates only that each word has `start`/`end`/`text`,
`transcript.based_on` is a checksum of the DERIVED words (conflict detection,
not a length constraint), and an empty word list is a legitimate state meaning
"this clip has no captions".
```

- [ ] **Step 5: Run every gate**

```bash
PYTHONPATH=src .venv/bin/pytest -q
python3 tools/lint.py
git diff --stat src/yt_shorts/captions.py
```
Expected: the whole suite passes, `All checks passed!`, and an EMPTY diff for `captions.py`.

- [ ] **Step 6: Name the pinned hashes separately**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_event_layer_no_regression.py -q
```
Expected: PASS. Report it on its own line — nothing here renders, so a move means something is badly wrong.

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md tests/test_studio_e2e.py src/yt_shorts/studio/static
git commit -m "docs+build(studio): document transcript add/remove; e2e; rebuild static"
```

- [ ] **Step 8: Operator check (not the implementer's job)**

Open the sung clip in the studio, insert rows into the stretched word, type the
missing lyrics, adjust the timings against the player, save, and re-render to
see them burned in.

---

## Self-Review

**Spec coverage.** Decision 1 (split, not gap-fill) → Task 1's `insertWordAfter`
plus its first test. Decision 2 (seed, not constraint) → the inputs are
untouched, and the E2E's overlap test retypes a start. Decision 3 (round to two
decimals) → Task 1 Step 3 and its own test and mutation. Decision 4 (empty text
renders nothing) → Task 1's test. Decision 5 (warn, never block) → Task 2's
`Alert`, the E2E's third test, and Task 3's mutation 3. Decision 6 (no
auto-sorting) → no task sorts anything, and CLAUDE.md records why. Decision 7
(no confirmation, empty list legal) → `removeWord`'s "can empty the list" test.
Decision 8 (empty-state seed) → `insertWordAfter([])` and Task 2 Step 5. The
spec's testing list maps onto Task 1's unit tests and Task 3's E2E; its "out of
scope" items (server changes, auto-sort, save-blocking, word splitting by text,
undo) appear in no task.

**Placeholder scan.** No TBD/TODO; every code step carries its final code. Three
places ask the implementer to confirm something against the file rather than
trust me: where exactly to place the new E2E class, and the two E2E locators
(`nth(3)`, `.first`) that are this plan's best guess at the rendered row
structure. Each says what to report and forbids weakening an assertion instead.

**Type consistency.** `insertWordAfter(words: Word[], index: number): Word[]`,
`removeWord(words: Word[], index: number): Word[]` and
`findWordProblems(words: Word[]): WordProblemKind[][]` are defined in Task 1 and
called with exactly those arities in Task 2 Steps 1, 2 and 5. `problems[index]`
is `WordProblemKind[]`, which is why Step 4 uses `.includes(...)`. `Word` is the
existing `{ start, end, text }` from `./api`, and `w()` in the test file builds
one.

**Risk I want the reviewer to weigh.** Task 2 changes a component with no
component-level tests, so between Task 2's commit and Task 3's E2E there is a
window where nothing automated covers the wiring. That ordering is deliberate —
the E2E cannot see the change until the bundle is rebuilt, and rebuilding is
Task 3's job so the committed bundle moves exactly once — but it means Task 2's
review must read the JSX carefully rather than lean on a green suite.
