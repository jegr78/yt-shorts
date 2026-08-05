# Stage B — Glossary Implementation Plan

**Goal:** Stop Whisper inventing words it has never heard — a sim-racing league's proper nouns — by biasing the decoder with a per-channel term list, and correcting whatever still slips through.

**Architecture:** A `glossary.json` per channel (optionally overridden per event) carries `terms` and `replacements`. `terms` are passed to faster-whisper as `hotwords` so the decoder knows them before it errs. `replacements` are applied to the decoded word list by a new pure-logic module.

**Scope note:** this is a pre-alpha project. Nothing on disk is precious, so there is deliberately **no** cache invalidation, no migration path, no byte-identity criterion (the transcript is *meant* to change), and no conflict handling for a glossary change. Delete a transcript to re-derive it.

## Global Constraints

- `PYTHONPATH=src` is mandatory. Full suite: `PYTHONPATH=src .venv/bin/pytest -q` — 402 tests pass at the start of this plan.
- No new dependencies. `hotwords` is already a parameter of `faster_whisper`'s `transcribe` (verified: `Optional[str]`).
- English only: code, comments, docstrings, tests, commit messages. Imperative commit messages.
- ffmpeg has no `libfreetype`/`libass` and must not be reinstalled. `/Users/jegr/racecast/` is read-only.
- A missing or empty `glossary.json` must change nothing: no hotwords, no replacements, current behaviour exactly.
- `hotwords` applies **only when a transcript is first derived.** An existing cached transcript is never re-derived because the glossary changed.

---

## Task 1: The replacement logic

**Files:** create `src/yt_shorts/glossary.py`, `tests/test_glossary.py`

**Produces:**
- `glossary.Glossary` — dataclass with `terms: list[str]`, `replacements: dict[str, str]`
- `glossary.EMPTY` — a `Glossary` with both empty
- `glossary.hotwords(g) -> str | None` — the terms joined for faster-whisper, `None` when there are none
- `glossary.apply(words: list[dict], g: Glossary) -> list[dict]` — replacements applied to a word list

**The one piece of real logic.** A replacement spans several words. `"very very"` is two word dicts with their own timestamps; `"Rei Racing"` is also two, which lines up. But `"very very" -> "Rei"` is two-to-one, and the surviving entry must span the whole matched range: the start of the first matched word, the end of the last. Getting this wrong leaves subtitles hanging on the wrong beats or a word shown twice.

Rules:
- Matching is on the words' text, whitespace-normalised and case-insensitively, against consecutive runs of words.
- The replacement's own words inherit the matched range, divided evenly when the counts differ. Even division is deliberate: word-level timings inside a corrected phrase are a guess either way, and an even split keeps them monotonic.
- Whisper marks a word boundary with a leading space; the replacement's first word keeps whatever the matched run's first word had, so joining stays correct (see `captions._to_caption`).
- Longer replacement keys win over shorter overlapping ones, so `"very very"` beats `"very"`.
- A word list untouched by any replacement comes back unchanged.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from yt_shorts.glossary import EMPTY, Glossary, apply, hotwords


def w(start, end, text):
    return {"start": start, "end": end, "text": text}


class TestHotwords:
    def test_no_terms_means_no_hotwords(self):
        assert hotwords(EMPTY) is None

    def test_terms_are_joined_for_the_decoder(self):
        assert hotwords(Glossary(terms=["Rei Racing", "Nordschleife"],
                                 replacements={})) == "Rei Racing, Nordschleife"


class TestApply:
    def test_an_empty_glossary_changes_nothing(self):
        words = [w(0.0, 0.5, " very"), w(0.5, 1.0, " very")]
        assert apply(words, EMPTY) == words

    def test_a_two_word_phrase_becomes_two_words(self):
        words = [w(0.0, 0.5, " very"), w(0.5, 1.0, " very")]
        result = apply(words, Glossary(terms=[],
                                       replacements={"very very": "Rei Racing"}))
        assert [x["text"] for x in result] == [" Rei", " Racing"]
        assert result[0]["start"] == 0.0
        assert result[-1]["end"] == 1.0

    def test_two_words_collapsing_to_one_span_the_whole_range(self):
        words = [w(0.0, 0.5, " very"), w(0.5, 1.0, " very")]
        result = apply(words, Glossary(terms=[], replacements={"very very": "Rei"}))
        assert [x["text"] for x in result] == [" Rei"]
        assert result[0]["start"] == 0.0
        assert result[0]["end"] == 1.0

    def test_one_word_expanding_to_two_splits_the_range(self):
        words = [w(0.0, 1.0, " Fullsend")]
        result = apply(words, Glossary(terms=[],
                                       replacements={"Fullsend": "Team Fullsend"}))
        assert [x["text"] for x in result] == [" Team", " Fullsend"]
        assert result[0]["start"] == 0.0
        assert result[-1]["end"] == 1.0
        assert result[0]["end"] == result[1]["start"]

    def test_matching_ignores_case_and_punctuation_spacing(self):
        words = [w(0.0, 0.5, " Very"), w(0.5, 1.0, " VERY")]
        result = apply(words, Glossary(terms=[], replacements={"very very": "Rei"}))
        assert result[0]["text"] == " Rei"

    def test_surrounding_words_are_untouched(self):
        words = [w(0.0, 0.4, " and"), w(0.4, 0.9, " very"),
                 w(0.9, 1.4, " very"), w(1.4, 1.8, " wins")]
        result = apply(words, Glossary(terms=[], replacements={"very very": "Rei"}))
        assert [x["text"] for x in result] == [" and", " Rei", " wins"]
        assert result[0] == words[0]
        assert result[-1] == words[-1]

    def test_a_longer_key_wins_over_a_shorter_overlapping_one(self):
        words = [w(0.0, 0.5, " very"), w(0.5, 1.0, " very")]
        result = apply(words, Glossary(
            terms=[], replacements={"very": "X", "very very": "Rei"}))
        assert [x["text"] for x in result] == [" Rei"]

    def test_a_leading_space_is_preserved_from_the_matched_run(self):
        # Whisper marks word starts with a leading space; a replacement that
        # dropped it would run into the previous word when joined.
        words = [w(0.0, 0.4, " and"), w(0.4, 1.0, " Fullsend")]
        result = apply(words, Glossary(terms=[], replacements={"Fullsend": "Team"}))
        assert result[1]["text"].startswith(" ")

    def test_timestamps_stay_monotonic(self):
        words = [w(0.0, 1.2, " Fullsend")]
        result = apply(words, Glossary(
            terms=[], replacements={"Fullsend": "Team Fullsend Racing"}))
        starts = [x["start"] for x in result]
        ends = [x["end"] for x in result]
        assert starts == sorted(starts)
        assert all(s <= e for s, e in zip(starts, ends))
```

- [ ] **Step 2:** Run it; expect `ModuleNotFoundError`.
- [ ] **Step 3:** Implement `src/yt_shorts/glossary.py` to satisfy exactly these tests. Keep it pure — no file access, no I/O.
- [ ] **Step 4:** Run the file's tests, then the full suite.
- [ ] **Step 5:** Commit: `Replace glossary terms in a decoded word list`

---

## Task 2: Loading the glossary

**Files:** modify `src/yt_shorts/profile.py`, `tests/test_profile.py`

**Consumes:** `glossary.Glossary`, `glossary.EMPTY`
**Produces:** `profile.load(...).config["glossary"]` — always a `Glossary`, `EMPTY` when there is no file

- The channel's `glossary.json` is read from `channels/<channel>/glossary.json`; an event's own `events/<event>/glossary.json` **replaces it wholesale** when present. No deep merge: merging two term lists or two replacement maps has no obvious right answer, and guessing would be worse than the explicit override.
- A malformed glossary is reported through the existing `ProfileError` collection, naming the file and what is wrong: not an object, `terms` not a list of strings, `replacements` not an object of string-to-string.
- No file at either level is not an error — it yields `EMPTY`.

Tests: channel-only, event overriding channel, neither present, and each malformed shape reported as `ProfileError` alongside other defects rather than short-circuiting.

Commit: `Load a channel or event glossary into the profile`

---

## Task 3: Wiring, and a real glossary for ERF

**Files:** modify `src/yt_shorts/transcribe.py`, `bin/yt-shorts`, `channels/erf/glossary.json` (create), `README.md`

- `transcribe(...)` gains a keyword-only `glossary: Glossary = EMPTY`. It passes `glossary.hotwords(...)` to `model.transcribe(...)` as `hotwords`, and applies `glossary.apply(...)` to the decoded words **before** they are cached — so the cache holds the corrected transcript and a re-render costs nothing.
- The cache is unchanged in shape. **Nothing invalidates it when the glossary changes:** an existing transcript is never re-derived for that reason. Say so in the docstring and the README — an operator who edits the glossary and sees no change on an already-transcribed clip must be able to find out why, and that deleting the transcript is the answer.
- `cmd_render`'s subtitle provider passes `config["glossary"]` through.
- Write `channels/erf/glossary.json` with the terms actually known from this project's material: `Rei Racing`, `Team Fullsend`, `Nordschleife`, plus the replacement `"very very" -> "Rei Racing"` that this stage exists to fix.
- README: a short section — what the file is, the two mechanisms, that terms bias the decoder only on a fresh transcription, and that deleting a transcript re-derives it.

Verification: transcribe `rei-got-sliced` (its raw clip is not on disk, so use the workspace transcript as the input to `glossary.apply` directly) and confirm `very very` becomes `Rei Racing`. Report the before and after text.

Commit: `Bias transcription with a channel glossary and correct what slips through`
