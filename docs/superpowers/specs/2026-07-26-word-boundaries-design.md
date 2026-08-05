# Word boundaries survive a hand-typed correction

## Why

An operator corrected two words in the studio's transcript editor — `very` →
`Rei` and `very` → `Racing` — and the rendered caption read `AND IT'SREIRACING`.
The words are right; the spaces are gone.

The cause is a convention leaking out of the decoder and into a text field.
faster-whisper marks the start of a word with a LEADING SPACE, and
`captions._to_caption` joins the tokens with `""` precisely so it can rely on
that:

```python
text="".join(word["text"] for word in group).strip(),
```

That is deliberate and documented. A continuation token — part of the same
word — carries no leading space, so stripping every token and rejoining with
`" "` would turn `" C"`, `".L"`, `".R."` into `C .L .R.` instead of `C.L.R.`
The measured example in this project's own data is the "Speedy" clip.

A human typing into a text field types `Rei`, not `" Rei"`. So the corrected
word loses its boundary and glues to whatever precedes it. Measured in the
operator's own `edit.json`:

```
' and'      <- decoder, leading space
" it's"     <- decoder, leading space
'Rei'       <- typed, none
'Racing'    <- typed, none
```

Two clips in the workspace were affected. One rendered visibly wrong; in the
other the spaceless token happened to land at the start of a caption line,
where the outer edges are stripped anyway, so the defect was latent rather
than visible. Both were corrected by hand before this work; the fix below is
what stops it recurring.

## The discriminator, measured

Across **5937** real decoder tokens in this workspace, 47 carry no leading
space at index > 0 — and **not one of them begins with a letter or a digit**:

| first character | count | examples |
|---|---|---|
| `.` | 37 | `' C'` + `'.L'`, `'.L'` + `'.R.'`, `' 1'` + `'.5'`, `' 7'` + `'.57'` |
| `-` | 10 | `' pre'` + `'-qualifying'`, `' build'` + `'-up.'` |

So "begins with a letter or digit" separates a new word from a continuation on
every token this project has ever decoded. The rule below rests on that
measurement, not on an assumption about tokenisers in general.

## Decisions

1. **Normalise on the way IN, not on the way out.** A word whose text begins
   with a letter or digit and carries no leading whitespace gets exactly one
   leading space. Stored word text then carries its own boundary, so every
   reader — the renderer, the preview, a future exporter — sees the same thing
   without having to remember the convention.
2. **`captions.py` is not touched.** Its join and its `C.L.R.` reasoning stay
   exactly as they are, which also means the six pinned overlay hashes cannot
   move.
3. **The rule applies at EVERY point where words enter from a client**, not
   only at save. The studio's preview route receives the operator's UNSAVED
   words; normalising only on save would show `IT'SREIRACING` in the preview
   while the rendered short said `IT'S REI RACING`. That inconsistency is the
   thing the operator asked to be rid of.
4. **The input field shows trimmed text.** Without this the field would hold
   `" Rei"` — a leading space that is invisible, unexplainable and in the way
   of the cursor. The editor displays `Rei`; the server owns the boundary. The
   operator never meets the decoder's convention.
5. **Idempotent, and that is a test not a hope.** Normalising twice must equal
   normalising once. `"  Rei"` must be unreachable by any number of
   save/reload/save cycles.
6. **Deliberately gluing two letter-led words is no longer possible** across
   two rows. The escape hatch is to put both into ONE word's text — a word
   dict's own text may contain whitespace, which `captions.py` already
   documents. Accepted because the measured need is zero (0 of 5937 tokens)
   and predictability is worth more here than the special case.

## Where the rule lives

A pure function in `editorial.py`, beside the other word helpers:

```python
def normalise_word_boundaries(words: list[dict]) -> list[dict]:
```

It returns a new list; it does not mutate its argument. `editorial.py` is
already the module that owns what a correction IS, it is pure, and both
entry points can reach it.

**Called from both client entry points in `studio/api.py`:**

- `PATCH …/clips/{name}` — the `"words" in fields_set` branch, before the
  payload becomes `transcript["words"]`.
- `POST …/clips/{name}/preview` — before the client's unsaved words reach
  `preview.build`.

Deliberately NOT called inside `editorial.save`. `save` is handed a complete
`Edit` and writing something other than what it was given would make every
round-trip test lie about what it stores. The two routes are the boundary
where untrusted, human-typed text arrives; that is where it gets normalised.

The CLI's own render path needs no change: it reads words that were either
decoded (already correct) or saved through the route (normalised on the way
in).

## The rule, exactly

For each word dict, in order:

- text is empty or whitespace only → unchanged. An empty correction is the
  operator clearing a word, and `captions.group_words` already skips it.
- first character is whitespace → unchanged. Already carries its boundary;
  this is what makes the function idempotent.
- first character is a letter or a digit → one `" "` prepended.
- anything else (punctuation, symbol) → unchanged. This is the `.L`,
  `-qualifying`, `.5` case, and the one deliberate way to write a
  continuation.

"Letter or digit" means Python's `str.isalnum()`, i.e. Unicode-aware. This
matters here rather than being pedantry: this project's corrections are full of
German proper nouns, and `Ähnlich`, `Öl`, `Überholmanöver` must gain their
boundary exactly like `Rei` does.

Only the `text` key is touched. `start` and `end` are never read or written by
this function.

## The client half

`WordsEditor.tsx` renders each word's `text` straight into a `TextInput` and
reports it back unchanged. Two small changes:

- Display `word.text.trim()` as the field's value rather than the raw text, so
  no invisible leading space ever appears in front of the cursor.
- Keep the RAW text in state. Only the rendered value is trimmed; `onChange`
  writes whatever the operator typed. The server owns the boundary, so the
  client neither adds nor preserves one.

That last point is what keeps dirty-detection honest, and it is easy to get
backwards. `words.wordsEqual` compares staged against saved word text, so if
loading a clip TRIMMED the words into state, every word would differ from its
saved form and every clip would open already looking edited. Because state
keeps the raw value and only the display is trimmed, an untouched word stays
byte-identical to what the server sent and `wordsEqual` needs no change at
all. A touched word becomes exactly what was typed, which the server then
normalises.

## Testing

- **Idempotence**, as a property: normalising the output again changes
  nothing, for every case in the table below.
- **The 47 measured continuation shapes stay untouched**: `.L`, `.R.`, `.5`,
  `.57`, `-qualifying`, `-up.` — a regression test built from the real
  first-character census above, so a future "simplification" that strips and
  rejoins fails immediately.
- **A hand-typed word gains exactly one space**, and one that already has
  whitespace gains none — `"  Rei"` is unreachable.
- **The empty and whitespace-only cases** are left alone.
- **The reported bug, end to end**: the operator's own token sequence
  (`' and'`, `" it's"`, `'Rei'`, `'Racing'`) renders `"and it's Rei Racing"`
  through `captions.group_words`, and rendered `"and it'sReiRacing"` before.
- **Both routes**: a PATCH with a spaceless word stores it with the boundary;
  a preview POST with the same word renders a caption with the space. The
  second is the one that makes the preview and the render agree, so it must
  fail if the preview call site is removed.
- **The six pinned overlay hashes** stay untouched, and `captions.py` stays
  out of the diff.
- **Client**: the field shows trimmed text while state keeps the raw value, so
  loading a clip and touching nothing leaves it NOT dirty and sends back
  exactly what was received. This is the one client behaviour that would break
  quietly and visibly at once — get it wrong and every clip opens looking
  edited — so it is worth a Vitest case on `wordsEqual` against real
  leading-space words as well as the E2E.

## Out of scope

- Migrating corrections already on disk. The two affected clips were fixed by
  hand before this work; a fresh checkout has no such data, and a migration
  pass would be code that runs once and is then wrong to keep.
- Changing `captions.py`'s join, or the `C.L.R.` behaviour it protects.
- Letting the operator glue two letter-led words across two rows (decision 6).
- Word-level add/remove in the editor. It updates words in place today, and
  this design neither needs nor prevents that changing later.
