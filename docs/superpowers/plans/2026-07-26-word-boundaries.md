# Word Boundary Normalisation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A word corrected by hand in the studio keeps its word boundary, so `Rei` typed into a field renders as `IT'S REI RACING` rather than `IT'SREIRACING`.

**Architecture:** faster-whisper marks a word start with a leading space, and `captions._to_caption` joins tokens with `""` to rely on that (so `" C"`, `".L"`, `".R."` render `C.L.R.`). A human types no leading space. One pure function in `editorial.py` restores the boundary, called at both points where words arrive from a client — the words `PATCH` and the preview `POST` — so the live preview and the burned-in render can never disagree. `captions.py` is not touched.

**Tech Stack:** Python 3 + Pillow, FastAPI (two route call sites), React + Mantine + TypeScript + Vitest, pytest + Playwright.

## Global Constraints

- `PYTHONPATH=src` is mandatory for every Python invocation. Full suite: `PYTHONPATH=src .venv/bin/pytest -q`.
- `python3 tools/lint.py` (NO `PYTHONPATH`) must print `All checks passed!` before every commit.
- Frontend gates, run from `src/yt_shorts/studio/web`: `npx tsc -b`, `npm run lint`, `npm test`. Only Task 4 runs `npm run build` and commits `src/yt_shorts/studio/static/`.
- **`src/yt_shorts/captions.py` must not appear in this branch's diff.** Its `""` join and the `C.L.R.` behaviour it protects stay exactly as they are, which is also what keeps the six pinned overlay hashes in `tests/test_event_layer_no_regression.py` unmovable. Never re-pin those hashes.
- The rule: a word whose text begins with a letter or digit (Python `str.isalnum()`, Unicode-aware) gets exactly ONE leading space. Everything else — empty, whitespace-first, punctuation-first — is returned unchanged.
- Normalising twice must equal normalising once. `"  Rei"` must be unreachable.
- Only the `text` key is touched. `start`/`end` are never read or written, and the argument is never mutated.
- Normalisation happens in the two ROUTES, never inside `editorial.save`: `save` is handed a complete `Edit`, and writing something other than what it was given would make every round-trip test lie about what it stores.
- Client state keeps the RAW word text; only the rendered input value is trimmed. Trimming into state would make every word differ from its saved form, so every clip would open already looking edited.
- Test hygiene: a test that would still pass if the behaviour under test were removed is a defect. This project has shipped that repeatedly, including an E2E guard that compared a blob URL and therefore could never fail.

## Existing tests this changes

One assertion in the suite encodes today's behaviour and will fail once Task 2
lands. It is not a flake and must not be worked around:

`tests/test_studio_e2e.py:361`, in
`TestSavingReachesEditJson.test_editing_a_word_and_saving_records_based_on` —
types `SUPER` into a word field and asserts the stored text is exactly
`"SUPER"`. After the fix the stored text is `" SUPER"`. **Task 2 updates it**,
so the suite is green at every commit.

Everything else already passes leading-space words through the routes
(`tests/test_studio_api.py:245`, `:281`) or asserts only that two previews
differ (`tests/test_studio_e2e.py:478`), so it is unaffected.

---

### Task 1: The rule

**Files:**
- Modify: `src/yt_shorts/editorial.py` (new function directly below `checksum`)
- Test: `tests/test_editorial.py`

**Interfaces:**
- Produces: `editorial.normalise_word_boundaries(words: list[dict]) -> list[dict]` — a new list of new dicts, `text` adjusted per the rule, every other key copied through untouched.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_editorial.py`:

```python
class TestNormaliseWordBoundaries:
    """faster-whisper marks a word START with a leading space, and
    captions._to_caption joins the tokens with "" to rely on that - which is
    what makes " C" + ".L" + ".R." render as C.L.R. rather than C .L .R.
    A human typing into the studio's text field types "Rei", not " Rei", so a
    hand-corrected word used to glue itself to its predecessor and render as
    IT'SREIRACING.
    """

    def test_a_hand_typed_word_gains_one_leading_space(self):
        out = editorial.normalise_word_boundaries([{"start": 0.0, "end": 1.0, "text": "Rei"}])
        assert out[0]["text"] == " Rei"

    def test_a_word_that_already_has_one_gains_none(self):
        """The property that makes the whole thing idempotent."""
        out = editorial.normalise_word_boundaries([{"start": 0.0, "end": 1.0, "text": " Rei"}])
        assert out[0]["text"] == " Rei"

    def test_normalising_twice_equals_normalising_once(self):
        words = [{"start": 0.0, "end": 1.0, "text": "Rei"},
                 {"start": 1.0, "end": 2.0, "text": " Racing"},
                 {"start": 2.0, "end": 3.0, "text": ".L"},
                 {"start": 3.0, "end": 4.0, "text": ""}]
        once = editorial.normalise_word_boundaries(words)
        assert editorial.normalise_word_boundaries(once) == once

    def test_a_double_space_is_unreachable(self):
        out = editorial.normalise_word_boundaries([{"start": 0.0, "end": 1.0, "text": "  Rei"}])
        assert out[0]["text"] == "  Rei"  # left alone, never made worse

    @pytest.mark.parametrize("text", [".L", ".R.", ".5", ".57", "-qualifying", "-up."])
    def test_the_measured_continuation_shapes_are_untouched(self, text):
        """Built from a census of this workspace's real transcripts: of 11518
        decoder tokens, 91 carry no leading space, and every one of them starts
        with '.' (71) or '-' (20). Those are continuations - "C.L.R.", "1.5",
        "pre-qualifying" - and splitting them is the regression this pins.
        """
        out = editorial.normalise_word_boundaries([{"start": 0.0, "end": 1.0, "text": text}])
        assert out[0]["text"] == text

    @pytest.mark.parametrize("text", ["Ähnlich", "Überholmanöver", "Öl"])
    def test_a_german_proper_noun_gains_its_boundary(self, text):
        """isalnum() is Unicode-aware, and it has to be: the corrections in
        this project are full of German names."""
        out = editorial.normalise_word_boundaries([{"start": 0.0, "end": 1.0, "text": text}])
        assert out[0]["text"] == " " + text

    def test_a_digit_led_word_gains_its_boundary(self):
        out = editorial.normalise_word_boundaries([{"start": 0.0, "end": 1.0, "text": "7"}])
        assert out[0]["text"] == " 7"

    @pytest.mark.parametrize("text", ["", " ", "   "])
    def test_empty_and_whitespace_only_are_left_alone(self, text):
        """An empty correction is the operator clearing a word;
        captions.group_words already skips it."""
        out = editorial.normalise_word_boundaries([{"start": 0.0, "end": 1.0, "text": text}])
        assert out[0]["text"] == text

    def test_timings_travel_through_untouched(self):
        out = editorial.normalise_word_boundaries([{"start": 1.25, "end": 2.5, "text": "Rei"}])
        assert (out[0]["start"], out[0]["end"]) == (1.25, 2.5)

    def test_the_input_is_not_mutated(self):
        words = [{"start": 0.0, "end": 1.0, "text": "Rei"}]
        editorial.normalise_word_boundaries(words)
        assert words[0]["text"] == "Rei"

    def test_an_unknown_key_survives(self):
        """The dicts reaching this come from a Pydantic model today, but the
        function must not quietly drop a field it does not know about."""
        out = editorial.normalise_word_boundaries(
            [{"start": 0.0, "end": 1.0, "text": "Rei", "probability": 0.9}])
        assert out[0]["probability"] == 0.9

    def test_an_empty_list_is_an_empty_list(self):
        assert editorial.normalise_word_boundaries([]) == []

    def test_the_reported_bug_renders_correctly_end_to_end(self):
        """The operator's own token sequence, straight out of their edit.json.
        Before the fix this rendered "and it'sReiRacing"."""
        from yt_shorts.captions import group_words
        words = [{"start": 1.18, "end": 2.78, "text": " and"},
                 {"start": 2.78, "end": 3.16, "text": " it's"},
                 {"start": 3.16, "end": 3.66, "text": "Rei"},
                 {"start": 3.70, "end": 4.02, "text": "Racing"}]
        assert [c.text for c in group_words(words)] == ["and it'sReiRacing"]
        fixed = editorial.normalise_word_boundaries(words)
        assert [c.text for c in group_words(fixed)] == ["and it's Rei Racing"]
```

`tests/test_editorial.py` already imports `pytest` and the `editorial` module —
confirm both at the top of the file before running, and report if either import
name differs from what these tests use.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_editorial.py::TestNormaliseWordBoundaries -q
```
Expected: FAIL — `AttributeError: module 'yt_shorts.editorial' has no attribute 'normalise_word_boundaries'`.

- [ ] **Step 3: Implement**

In `src/yt_shorts/editorial.py`, directly below `checksum` — `editorial` is
already the pure module that owns what a correction IS, and both routes can
reach it:

```python
def normalise_word_boundaries(words: list[dict]) -> list[dict]:
    """Restores the word boundary a text field cannot express.

    faster-whisper marks the START of a word with a leading space, and
    captions._to_caption joins the tokens with "" precisely so it can rely on
    that: a continuation token carries no leading space, so " C" + ".L" +
    ".R." must render as C.L.R. rather than C .L .R. (see that function's own
    docstring - the real example is this project's "Speedy" clip).

    A human types "Rei", not " Rei". Without this, a hand-corrected word glues
    itself to its predecessor and the caption reads IT'SREIRACING - measured on
    the operator's own clip.

    The rule: a text beginning with a letter or digit gets exactly one leading
    space. Everything else is returned unchanged - empty (the operator clearing
    a word), already-spaced (which is what makes this idempotent), and
    punctuation-led, which is the deliberate way to write a continuation.

    "Letter or digit" is str.isalnum(), Unicode-aware on purpose: the
    corrections here are full of German proper nouns, and 'Überholmanöver'
    needs its boundary exactly like 'Rei' does.

    Measured basis for that discriminator: across the 11518 decoder tokens in
    this workspace's transcripts, 91 carry no leading space, and every single
    one starts with '.' (71) or '-' (20). Not one starts with a letter or a
    digit, at any index. The counts date a growing corpus and are worth
    re-measuring rather than trusting; the PROPERTY is what this rule rests on.

    Called from the two routes where words arrive from a client (the words
    PATCH and the preview POST), never from save(): save is handed a complete
    Edit, and rewriting its content would make every round-trip test lie about
    what it stores.
    """
    normalised = []
    for word in words:
        copy = dict(word)
        text = copy.get("text") or ""
        # text[:1] rather than text[0]: an empty string slices to "", whose
        # isalnum() is False, so the empty case needs no branch of its own.
        if text[:1].isalnum():
            copy["text"] = " " + text
        normalised.append(copy)
    return normalised
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_editorial.py tests/test_captions.py -q
python3 tools/lint.py
```
Expected: all pass; `All checks passed!`.

- [ ] **Step 5: Confirm the render path is untouched**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_event_layer_no_regression.py -q
git diff --stat src/yt_shorts/captions.py
```
Expected: PASS, and an EMPTY diff for `captions.py`. If that file changed, the change is wrong regardless of what the tests say.

- [ ] **Step 6: Commit**

```bash
git add src/yt_shorts/editorial.py tests/test_editorial.py
git commit -m "feat(editorial): restore the word boundary a text field cannot express"
```

---

### Task 2: Both routes use it

**Files:**
- Modify: `src/yt_shorts/studio/api.py` (the `"words" in fields_set` branch of the clip `PATCH`, and `post_preview`)
- Test: `tests/test_studio_api.py`
- Modify: `tests/test_studio_e2e.py:361` (the existing assertion named in "Existing tests this changes")

**Interfaces:**
- Consumes: `editorial.normalise_word_boundaries(words) -> list[dict]` from Task 1. `api.py` already imports the `editorial` module; verify and report the exact import form it uses.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_studio_api.py`. The names used below already exist in that
file: module-level `EVENT_PREFIX`, `clip_entry`, `_solid_video`, the imported
`clipstore` and `editorial` modules, and the `event_dir` / `client` fixtures.

```python
class TestWordBoundariesThroughTheRoutes:
    """The studio is the only way a human types word text, so these two call
    sites are the whole fix. They are tested separately because they fail
    separately: normalising only on save would show IT'SREIRACING in the live
    preview while the rendered short said IT'S REI RACING.
    """

    def test_patch_stores_the_boundary(self, event_dir, client):
        directory = clipstore.write_clip(event_dir, clip_entry())
        response = client.patch(
            f"{EVENT_PREFIX}/clips/{directory.name}",
            json={"words": [{"start": 0.0, "end": 1.0, "text": "Rei"},
                            {"start": 1.0, "end": 2.0, "text": "Racing"}]})
        assert response.status_code == 200
        assert [w["text"] for w in response.json()["words"]] == [" Rei", " Racing"]
        saved = editorial.load(directory)
        assert [w["text"] for w in saved.transcript["words"]] == [" Rei", " Racing"]

    def test_patch_leaves_a_continuation_alone(self, event_dir, client):
        """The other half of the rule, through the route: a punctuation-led
        token is a continuation and must stay glued to what precedes it."""
        directory = clipstore.write_clip(event_dir, clip_entry())
        response = client.patch(
            f"{EVENT_PREFIX}/clips/{directory.name}",
            json={"words": [{"start": 0.0, "end": 1.0, "text": "C"},
                            {"start": 1.0, "end": 2.0, "text": ".L"}]})
        assert response.status_code == 200
        assert [w["text"] for w in response.json()["words"]] == [" C", ".L"]

    def test_the_preview_renders_the_same_picture_either_way(self, event_dir, client):
        """The assertion that makes the preview and the render agree: a
        spaceless word and the same word carrying its boundary must produce
        IDENTICAL bytes, because the route normalises before drawing.
        Byte-comparing two preview responses is already trusted in this file
        (TestPreviewPost.test_title_omitted_falls_back_to_the_saved_effective_title
        does it), and asserting only a 200 here would pass against a preview
        that ignored the fix entirely.
        """
        directory = clipstore.write_clip(event_dir, clip_entry(hook="Speedy!"))
        _solid_video(clipstore.raw_path(directory))

        def preview(text):
            response = client.post(
                f"{EVENT_PREFIX}/clips/{directory.name}/preview",
                json={"at": 0.5,
                      "words": [{"start": 0.0, "end": 3.0, "text": " it's"},
                                {"start": 0.0, "end": 3.0, "text": text}]})
            assert response.status_code == 200
            return response.content

        assert preview("Rei") == preview(" Rei")
        # Guards the equality above from passing for the wrong reason: if the
        # route ignored `words` altogether, every preview would be identical
        # and that assertion would be worthless.
        assert preview("Rei") != preview("Racing")
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py::TestWordBoundariesThroughTheRoutes -q
```
Expected: FAIL — the PATCH stores `"Rei"` unchanged, and the two previews differ.

- [ ] **Step 3: Normalise in the words PATCH**

In the clip `PATCH` handler's `if "words" in fields_set:` branch, replace:

```python
            words_payload = [w.model_dump() for w in body.words] if body.words is not None else []
```

with:

```python
            # Normalised on the way IN: the operator types "Rei", and
            # captions._to_caption relies on faster-whisper's leading space to
            # know where a word starts (see editorial.normalise_word_boundaries).
            words_payload = editorial.normalise_word_boundaries(
                [w.model_dump() for w in body.words] if body.words is not None else [])
```

- [ ] **Step 4: Normalise in the preview POST**

In `post_preview`, replace:

```python
        words = [word.model_dump() for word in body.words]
```

with:

```python
        # Same normalisation as the words PATCH, and for the reason this route
        # exists: it draws what the operator is holding unsaved. Without it the
        # preview would show IT'SREIRACING while the saved render showed
        # IT'S REI RACING.
        words = editorial.normalise_word_boundaries(
            [word.model_dump() for word in body.words])
```

- [ ] **Step 5: Update the one existing assertion this changes**

In `tests/test_studio_e2e.py`,
`TestSavingReachesEditJson.test_editing_a_word_and_saving_records_based_on`
types `SUPER` into a word field. Replace:

```python
        assert saved.transcript["words"] == [{"start": 0.1, "end": 0.5, "text": "SUPER"}]
```

with:

```python
        # " SUPER", not "SUPER": the route restores the word boundary a text
        # field cannot express (editorial.normalise_word_boundaries), which is
        # what stops a hand-typed correction gluing itself to the word before
        # it. TestWordBoundariesThroughTheRoutes in tests/test_studio_api.py
        # owns that rule; this line records that the E2E save path goes
        # through it.
        assert saved.transcript["words"] == [{"start": 0.1, "end": 0.5, "text": " SUPER"}]
```

Change nothing else in that test. If any OTHER existing test fails in Step 6,
do not adjust it on your own judgement — report it with the failure output.

- [ ] **Step 6: Run the affected suites**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py tests/test_editorial.py -q
PYTHONPATH=src .venv/bin/pytest tests/test_studio_e2e.py -q -k "SavingReachesEditJson or LivePreview"
python3 tools/lint.py
```
Expected: all pass; `All checks passed!`.

- [ ] **Step 7: Mutation-check both call sites**

Remove the normalisation from the PATCH only, run
`TestWordBoundariesThroughTheRoutes`, and confirm the two PATCH tests fail
while the preview test still passes. Restore it, then do the same for the
preview call site — the PATCH tests must stay green while the preview test
fails. Report the real pass/fail counts for both directions. This is what
proves the two call sites are independently guarded rather than one test
covering both by accident.

Restore by editing the line back, NOT with `git checkout src/yt_shorts/studio/api.py` — that would also discard the other call site's fix. Confirm both
edits are present before Step 8.

- [ ] **Step 8: Commit**

```bash
git add src/yt_shorts/studio/api.py tests/test_studio_api.py tests/test_studio_e2e.py
git commit -m "fix(studio): normalise word boundaries at both client entry points"
```

---

### Task 3: The field stops showing the convention

**Files:**
- Modify: `src/yt_shorts/studio/web/src/components/WordsEditor.tsx:91`
- Test: `src/yt_shorts/studio/web/src/words.test.ts`

**Interfaces:**
- Consumes: nothing from earlier tasks. `wordsEqual(a, b)` in `words.ts` stays exactly as it is — this task must NOT change it.

- [ ] **Step 1: Trim what the field displays**

In `WordsEditor.tsx`, the text `TextInput` currently reads `value={word.text}`
(line 91). Change that one line to:

```tsx
                  value={word.text.trim()}
```

`value` is trimmed; `onChange` still writes exactly what was typed, so state
keeps the raw text and an untouched word stays byte-identical to what the
server sent. The server owns the boundary from here on (Task 2), so the client
neither adds nor preserves one.

- [ ] **Step 2: Pin the state rule that makes this safe**

Append to `src/yt_shorts/studio/web/src/words.test.ts`, using the `w` helper
already defined at the top of that file:

```ts
describe('wordsEqual with decoder words', () => {
  it('treats a freshly loaded word list as unchanged', () => {
    // faster-whisper's own words carry a leading space. Loading a clip and
    // touching nothing must not look edited - which is why WordsEditor trims
    // only what it DISPLAYS and keeps the raw text in state. Trimming into
    // state would make every word differ from its saved form, so every clip
    // would open showing "Unsaved changes".
    const loaded = [w(0, 1, ' drives'), w(1, 2, " it's")]
    expect(wordsEqual(loaded, loaded.map((word) => ({ ...word })))).toBe(true)
  })

  it('sees a difference when only the leading space differs', () => {
    // The corollary, and the reason the case above is not vacuous: wordsEqual
    // compares raw text, so a state-level trim WOULD be visible to it.
    expect(wordsEqual([w(0, 1, ' Rei')], [w(0, 1, 'Rei')])).toBe(false)
  })
})
```

These two cases pass on first run — `wordsEqual` already compares raw text and
this task does not change it. They exist to FAIL LATER if someone "simplifies"
the display trim into a state trim, which is the one way this task can be got
wrong. Say so in your report rather than treating the immediate pass as a
missing test.

- [ ] **Step 3: Run every frontend gate**

```bash
cd src/yt_shorts/studio/web
npx tsc -b
npm run lint
npm test
```
Expected: tsc exit 0, oxlint clean, all Vitest pass. Do NOT run `npm run build` — Task 4 owns the committed bundle.

- [ ] **Step 4: Commit**

```bash
git add src/yt_shorts/studio/web/src
git commit -m "fix(studio-web): the word field no longer shows an invisible leading space"
```

---

### Task 4: Docs, E2E, bundle, full verification

**Files:**
- Modify: `CLAUDE.md`
- Test: `tests/test_studio_e2e.py` (new test in `TestSavingReachesEditJson`)
- Modify: `src/yt_shorts/studio/static/**` (rebuilt, committed)

- [ ] **Step 1: Add the E2E guard**

Append to `TestSavingReachesEditJson` in `tests/test_studio_e2e.py`, directly
after `test_editing_a_word_and_saving_records_based_on`. Every name used here —
`clip_entry`, `CLIP_URL`, `editor_url`, `clipstore`, `editorial`, `json`, and
the `event_dir` / `live_server` / `page` fixtures — is one that neighbouring
test already uses; the wait-for-save block is copied from it deliberately,
since it is this file's established way of waiting out a save round-trip.

```python
    def test_a_hand_typed_word_keeps_its_boundary(self, event_dir, live_server, page):
        """The operator's actual bug, end to end: typing a correction lost the
        space and the caption rendered IT'SREIRACING. Covers all THREE halves
        of the fix in one pass - the field must DISPLAY the decoder's word
        without its leading space, loading it must not make the clip look
        edited, and the save must STORE the correction with the boundary.

        This is the ONLY automated guard on the first and second of those. A
        review proved it: reverting WordsEditor's display trim, and trimming
        into state instead (in its own onChange, or at the load site), each
        leaves tsc, oxlint and all 293 Vitest cases green - this project
        covers component behaviour by E2E, not by component tests.
        """
        directory = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        derived = [{"start": 0.1, "end": 0.5, "text": " very"}]
        clipstore.transcript_path(directory).write_text(
            json.dumps({"model": "small", "source": CLIP_URL, "words": derived}))

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Speedy!").click()

        word_box = page.get_by_role("cell", name="very").locator("input")
        # The display half: the decoder's own " very" is shown WITHOUT its
        # leading space, so no invisible character sits in front of the cursor.
        assert word_box.input_value() == "very"

        # The state half, and the reason the trim is display-ONLY: state keeps
        # the raw " very", so a clip nobody has touched is not dirty. Trimming
        # into state instead would make every word differ from its saved form
        # and every clip would open showing this badge.
        assert page.get_by_text("Unsaved changes").count() == 0

        word_box.fill("Rei")
        page.get_by_role("button", name="Save changes").click()
        page.wait_for_timeout(50)
        page.wait_for_function(
            "Array.from(document.querySelectorAll('button')).some("
            "b => b.textContent.includes('Save changes') && b.disabled)"
        )

        saved = editorial.load(directory)
        assert saved.transcript is not None
        # The server half: stored WITH the boundary, which is what the renderer
        # burns in. A stored "Rei" here is the bug.
        assert saved.transcript["words"] == [{"start": 0.1, "end": 0.5, "text": " Rei"}]
```

If a locator does not resolve, fix the SELECTOR and report exactly what you
changed and why — never weaken an assertion. This file has been caught twice by
locators that resolved to the wrong thing: a bare `.last` on a control two
editors both render, and an assertion against a blob object URL that differs on
every refire and therefore could never fail.

- [ ] **Step 2: Build the bundle, then run the E2E**

The Playwright E2E serves the COMMITTED bundle, so Task 3's frontend change is
invisible to it until this build runs. Build BEFORE pytest or the new test
exercises a stale page and its display-half assertion fails for the wrong
reason.

```bash
cd src/yt_shorts/studio/web
npm run lint
npx tsc -b
npm test
npm run build
cd -
PYTHONPATH=src .venv/bin/pytest tests/test_studio_e2e.py -q -k "SavingReachesEditJson"
```
Expected: every frontend gate clean, build exit 0, and all three tests in that class pass.

- [ ] **Step 3: Update `CLAUDE.md`**

In the Architecture section, immediately after the paragraph beginning
**"Subtitles are an optional layer, attached at one point."**, insert:

```markdown
**A word's text carries its own boundary, and the studio's text field cannot.**
faster-whisper marks the START of a word with a leading space, and
`captions._to_caption` joins the tokens with `""` to rely on exactly that -
which is what makes `" C"`, `".L"`, `".R."` render as `C.L.R.` instead of
`C .L .R.`. A human types `Rei`, not `" Rei"`, so a hand-corrected word used to
glue itself to its predecessor and render as `IT'SREIRACING`.
`editorial.normalise_word_boundaries` restores the boundary: a text beginning
with a letter or digit (`str.isalnum()`, Unicode-aware for the German names
this project is full of) gets exactly one leading space; empty, already-spaced
and punctuation-led text is returned untouched, which is both what keeps the
function idempotent and how a continuation is deliberately written. The
discriminator is measured, not assumed - across the 11518 decoder tokens in
this workspace's transcripts, 91 carry no leading space and every one of them
starts with `.` (71) or `-` (20). Not one starts with a letter or a digit, at
any index. Those counts are a snapshot of a corpus that keeps growing, so
re-measure rather than trusting the numbers; what has to hold is the property,
and a letter-led continuation token appearing one day would break the rule
rather than merely dating it.

It is called from the TWO routes where words arrive from a client - the words
`PATCH` and the preview `POST` - and deliberately NOT from `editorial.save`.
Both, because normalising only on save would show `IT'SREIRACING` in the live
preview while the rendered short said `IT'S REI RACING`; not `save`, because
`save` is handed a complete `Edit` and rewriting its content would make every
round-trip test lie about what it stores. The studio's own field DISPLAYS
`word.text.trim()` while keeping the RAW text in state - trimming into state
instead would make every word differ from its saved form and every clip would
open showing "Unsaved changes". `captions.py` is untouched by all of this,
which is also why the six pinned overlay hashes cannot move.

**The display trim has one sharp edge, and the obvious reading of it is
wrong.** Because the rendered value is trimmed while state holds the raw text,
a space typed at either END of a word survives only as the operator's LAST
keystroke: it is stored, but it is invisible in the field, and the very next
keypress reads back the trimmed DOM value and silently destroys it. The
tempting conclusion - that this is harmless because the server recomputes the
boundary anyway - is false, and measured false in a real browser. The server
recomputes it only for ALNUM-led text. For a correction that genuinely starts a
new word but begins with punctuation (`(pit)`, `"Rei"`, `#12`), a hand-typed
leading space is the ONLY boundary mechanism there is, and this is exactly the
input that makes it invisible and fragile. The supported way to write that case
is the one a word dict already allows: put both words into a SINGLE row's text,
which `captions.py` documents and which no trim can damage. Do not "simplify"
this on the premise that a typed boundary space has no job left to do - it has
one, in the punctuation-led case, and removing the escape hatch would leave a
silently glued caption with no way to fix it.

Note where the guard for all of this lives: reverting the trim, or moving it
into state, leaves `tsc`, `oxlint` and every Vitest case green, because this
project covers component behaviour by E2E (`tests/test_studio_e2e.py`'s
`test_a_hand_typed_word_keeps_its_boundary`), not by component tests.
```

- [ ] **Step 4: Run every gate**

```bash
PYTHONPATH=src .venv/bin/pytest -q
python3 tools/lint.py
git diff --stat src/yt_shorts/captions.py
```
Expected: the whole suite passes, `All checks passed!`, and an EMPTY diff for `captions.py`.

- [ ] **Step 5: Name the pinned hashes separately**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_event_layer_no_regression.py -q
```
Expected: PASS. Report this on its own line even though Step 4 covers it — this is the assertion the whole "do not touch `captions.py`" constraint exists to protect.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md tests/test_studio_e2e.py src/yt_shorts/studio/static
git commit -m "docs+build(editorial): document word-boundary normalisation; e2e; rebuild static"
```

- [ ] **Step 7: Operator check (not the implementer's job)**

In the studio: open a clip, retype a word without a leading space, watch the
live preview show the space immediately, save, then re-render the short so the
burned-in caption changes.

---

## Self-Review

**Spec coverage.** Decision 1 (normalise on the way in) and the exact rule →
Task 1. Decision 2 (`captions.py` untouched) → a Global Constraint plus an
explicit empty-diff check in Tasks 1 and 4. Decision 3 (every client entry
point, not only save) → Task 2, including the mutation check that proves each
call site is independently guarded. Decision 4 (the field shows trimmed text) →
Task 3, plus the E2E's `input_value() == "very"` assertion. Decision 5
(idempotence as a test, not a hope) → Task 1. Decision 6 (cross-row gluing is
accepted as no longer possible) needs no code and appears in no task,
correctly. The spec's testing list maps onto Task 1's unit tests, Task 2's
route tests, Task 3's Vitest cases and Task 4's E2E. The spec's "out of scope"
items (migrating on-disk corrections, changing `captions.py`, cross-row gluing,
word add/remove) appear in no task.

**Placeholder scan.** No TBD/TODO, and every code step carries its final code.
Three places ask the implementer to confirm a name against the file rather than
trust this plan — the `pytest`/`editorial` imports in `tests/test_editorial.py`
(Task 1), the `editorial` import form in `api.py` (Task 2), and any E2E locator
that fails to resolve (Task 4); each names what to report. Every other
identifier here was read out of the real files: `EVENT_PREFIX`, `clip_entry`,
`_solid_video`, `clipstore.write_clip`, `clipstore.raw_path`,
`clipstore.transcript_path`, the `event_dir`/`client`/`live_server`/`page`
fixtures, `editor_url`, `CLIP_URL`, `WordsEditor.tsx:91`, and the `w` helper in
`words.test.ts`.

**Type consistency.** `normalise_word_boundaries(words: list[dict]) -> list[dict]`
is defined in Task 1 and called with the same shape at both Task 2 call sites —
a list of `model_dump()` dicts carrying `start`/`end`/`text`. `wordsEqual(a, b)`
is named identically in Task 3's test and its existing module, and Task 3
states it must not change.

**Breaking-change audit.** I grepped the suite for assertions on stored word
text rather than assuming none existed. Exactly one encodes today's behaviour
(`tests/test_studio_e2e.py:361`) and Task 2 updates it in the same commit that
breaks it, so the suite stays green at every commit; the "Existing tests this
changes" section records why the others are unaffected.
