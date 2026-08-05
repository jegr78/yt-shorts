"""A channel's proper nouns, both fed to the decoder and applied after it.

Whisper has no idea what a sim-racing league calls itself. Given a real
clip it transcribes "Rei Racing" as " very," " very" - two words it does
know, standing in for two it has never heard, with a comma attached to the
first because that is where Whisper happened to hear a pause. A `Glossary`
attacks that from both ends: `hotwords` turns its `terms` into the string
faster-whisper's decoder takes as a bias, before it errs; `apply` corrects
`replacements` still present in the decoded words, after it errs, ignoring
the punctuation Whisper attached along the way but never a sentence
boundary a real one of those two words happened to sit next to. Whichever
end catches a given term, the other is free to miss it.

Pure logic: no file access, no I/O, no imports beyond the standard library
- the same reasoning that made `captions.py` a separate, invented-word-list
testable module. The glossary's own source (a channel's profile, a file on
disk) is somebody else's concern.

Four layers feed one glossary, and a fifth sits between them: the built-in
default (now empty, see DEFAULT_LAYER), the track pack an event selects (see
tracks.py), the workspace's glossary.json, the channel's, the event's - most
specific winning per entry, with a falsy entry disabling one inherited from a
less specific layer.
"""

from __future__ import annotations

import json
import string
from dataclasses import dataclass
from pathlib import Path

# captions.py closes a caption group on any of ".!?,;:" - a broad,
# pause-driven boundary that suits reading rhythm. Matching a glossary key
# needs a narrower rule: a comma, semicolon or colon inside a mis-heard
# phrase is exactly the kind of arbitrary attachment `_normalized` already
# erases, but a period, "!" or "?" genuinely ends the sentence the glossary
# author meant to match within. This is the subset of that same constant
# that a matched run must never be found to span.
SENTENCE_END = ".!?"


@dataclass
class Glossary:
    """`terms` bias the decoder (see `hotwords`); `replacements` correct its
    output afterwards (see `apply`). A term can appear in one, the other, or
    both - they are independent lists, not two views of the same data."""
    terms: list[str]
    replacements: dict[str, str]


# The glossary a channel with no proper nouns of its own gets: no bias, no
# corrections. `apply(words, EMPTY)` is defined to return `words` unchanged.
EMPTY = Glossary(terms=[], replacements={})


def hotwords(g: Glossary) -> str | None:
    """The decoder bias string faster-whisper expects, or None when there
    is nothing to bias with - "no hotwords" and "hotwords=''" are not the
    same request to that API, so an empty glossary must produce None rather
    than an empty string."""
    if not g.terms:
        return None
    return ", ".join(g.terms)


# faster-whisper's own WhisperModel.get_prompt tokenizes `hotwords` and
# truncates once the token count reaches `max_length // 2` (224, for the
# `max_length == 448` every model in this project uses) by cutting to
# `max_length // 2 - 1` = 223 tokens - see faster_whisper/transcribe.py,
# around line 1545. This module stays stdlib-only (see its own module
# docstring) and must not import a tokenizer to check that exactly, so this
# is a CONSERVATIVE, CHARACTER-based proxy for that token count, not the
# token count itself - characters-per-token varies with content, so treat
# this as a safety margin, not an exact cutoff.
#
# Measured once, by hand, with the cached faster-whisper-small tokenizer
# (`WhisperModel("small", device="cpu", compute_type="int8").hf_tokenizer`):
# the shipped 32-term Nürburgring Nordschleife track pack alone
# (tracks.py's "nurburgring-nordschleife", formerly this module's own
# DEFAULT_TERMS before that vocabulary moved to a venue-scoped pack) joins to
# 414 characters / 161 tokens; that pack plus ERF's real 3-term channel
# glossary.json (two of which are new) joins to 441 characters / 169 tokens.
# Appending realistic multi-word team names ("Manthey EMA", "Getspeed Performance", "Dinamic
# GT", ...) on top of that crosses the 224-token truncation point at 44
# terms / 627 characters (43 terms / 610 characters still measured under
# it). HOTWORD_BUDGET_CHARS is set comfortably below that measured 627-
# character crossing point, so a warning fires before truncation actually
# happens rather than after.
HOTWORD_BUDGET_CHARS = 550


def hotwords_at_risk(g: Glossary) -> bool:
    """True when `hotwords(g)` is at or beyond HOTWORD_BUDGET_CHARS, meaning
    some of `g.terms` are at risk of silently being dropped by
    faster-whisper's own hotword-prompt truncation (see
    HOTWORD_BUDGET_CHARS's comment for the measurement behind the number).

    A pure length check, nothing more - this module stays logger-free (see
    the module docstring), so a caller that already has a logger and sees
    the glossary (`stream_transcribe.subprocess_decoder`,
    `transcribe.transcribe`) is the one that turns this into a warning."""
    text = hotwords(g)
    return text is not None and len(text) >= HOTWORD_BUDGET_CHARS


def _normalized(text: str) -> str:
    """Whitespace-, case- and punctuation-insensitive form of a single word
    dict's text, for matching against a glossary key's own tokens.

    A comma, hyphen, apostrophe and the like attach to a word arbitrarily -
    Whisper's choice, not the glossary author's - so every punctuation
    character is stripped, not just a leading or trailing one: an internal
    apostrophe ("it's") is exactly as arbitrary as a trailing comma
    ("very,"). This also strips `SENTENCE_END` characters, which is safe
    only because whether a run may cross one is decided separately, by
    `_ends_sentence`, against the raw text before this normalised form is
    ever compared.
    """
    core = text.strip().lower()
    return "".join(ch for ch in core if ch not in string.punctuation)


def _ends_sentence(text: str) -> bool:
    """True when `text` (a single word dict's raw text) ends a sentence -
    its last non-whitespace character is one of `SENTENCE_END`. A glossary
    key of several words must not match a run in which an earlier word ends
    a sentence: "very very" was meant to catch two words in the same
    breath, not "very." followed by the start of a new one."""
    stripped = text.rstrip()
    return bool(stripped) and stripped[-1] in SENTENCE_END


def _replacement_keys(g: Glossary) -> list[tuple[list[str], str]]:
    """`g.replacements`, each key split into its normalized tokens, longest
    (most tokens) first. Sorting this way and taking the first match at a
    given position is what makes a longer key win over a shorter one it
    overlaps with - "very very" is tried, and matches, before "very" ever
    gets a chance to claim just one of the two words."""
    keyed = [
        ([_normalized(tok) for tok in key.split()], replacement)
        for key, replacement in g.replacements.items()
    ]
    keyed.sort(key=lambda pair: len(pair[0]), reverse=True)
    return keyed


def _replace_run(matched: list[dict], replacement: str) -> list[dict]:
    """Builds the word dicts that replace `matched`, a consecutive run of
    the original word dicts a glossary key matched.

    The surviving entries span the whole matched range - the start of its
    first word, the end of its last - divided evenly across whatever number
    of words the replacement has. Word-level timing inside a corrected
    phrase is a guess either way (the words being replaced are not the
    words that were actually said), and an even split is the defensible,
    monotonic choice: no cleverer heuristic is warranted.

    Whisper marks the start of a word with a leading space, never a
    continuation token; `captions._to_caption` joins word texts with
    "".join(...) precisely because of that convention. The replacement's
    first word keeps whichever leading whitespace the matched run's first
    word had, so a run that started a new word still starts a new word.
    Every later replacement word is new by construction, so it always gets
    exactly one leading space.
    """
    tokens = replacement.split()
    if not tokens:
        return []

    start = matched[0]["start"]
    end = matched[-1]["end"]
    span = end - start
    step = span / len(tokens)

    first_text = matched[0]["text"]
    leading = first_text[:len(first_text) - len(first_text.lstrip())]

    out = []
    for i, token in enumerate(tokens):
        word_start = start + step * i
        # The last word's end is the matched range's own end, not another
        # step multiple, so float division never leaves it short of `end`.
        word_end = end if i == len(tokens) - 1 else start + step * (i + 1)
        text = (leading + token) if i == 0 else (" " + token)
        out.append({"start": word_start, "end": word_end, "text": text})
    return out


def apply(words: list[dict], g: Glossary) -> list[dict]:
    """Applies `g.replacements` to a chronologically-ordered word list.

    Matching runs on the words' text, whitespace-, case- and
    punctuation-normalised (see `_normalized`), against consecutive runs of
    words - a glossary key of several words must line up with that many
    word dicts in a row. A run is never allowed to cross a sentence
    boundary though (see `_ends_sentence`): "very. Very" does not match
    "very very", even though "very, very" - a comma, not a full stop - does.
    Words no replacement's key covers come back exactly as given, so a
    caller that passes `EMPTY` gets its list back unchanged. The result
    stays chronological and non-decreasing, the ordering
    `captions.group_words` assumes and never re-sorts.
    """
    keys = _replacement_keys(g)
    n = len(words)
    result: list[dict] = []
    i = 0
    while i < n:
        match = None
        for key_tokens, replacement in keys:
            klen = len(key_tokens)
            if klen == 0 or i + klen > n:
                continue
            candidate = words[i:i + klen]
            if not all(_normalized(candidate[j]["text"]) == key_tokens[j]
                       for j in range(klen)):
                continue
            # Every word but the last must not end a sentence - a key that
            # matched up to that point matched the wrong phrase, split
            # across a boundary its author never meant to cross. The last
            # word may end one freely: nothing follows it in this run for
            # a boundary to cut off.
            if any(_ends_sentence(candidate[j]["text"])
                   for j in range(klen - 1)):
                continue
            match = (klen, replacement, candidate)
            break
        if match is None:
            result.append(words[i])
            i += 1
            continue
        klen, replacement, candidate = match
        result.extend(_replace_run(candidate, replacement))
        i += klen
    return result


# A term, a replacement key and a replacement text are all JSON string
# values: not paths, and never used as one, so this cap is hygiene rather
# than a security boundary - the same reasoning lexicon.MAX_MARKER_LENGTH
# carries. Nothing legitimate needs a 200-character corner name, and a
# control character in one is a stray newline from a bad paste.
MAX_ENTRY_LENGTH = 200


def normalise_term(term: str) -> str:
    """A term's identity across layers: stripped and lower-cased.

    Only IDENTITY is normalised. The raw spelling is kept beside it in a
    GlossaryLayer because that spelling is what biases the decoder (see
    hotwords) and what the studio shows the operator."""
    return term.strip().lower()


def normalise_key(key: str) -> str:
    """A replacement key's identity across layers: each whitespace-separated
    token run through the same `_normalized` form `apply` matches with,
    rejoined by single spaces.

    Using `apply`'s own normalisation rather than a second, similar rule is
    the point: two layers writing "Kessichen" and "kessichen," must collide
    here exactly when they would have matched the same words there."""
    return " ".join(_normalized(token) for token in key.split() if _normalized(token))


@dataclass
class GlossaryLayer:
    """ONE glossary.json's worth of entries, keyed for merging.

    `terms` maps a term's normalised key to `(spelling, enabled)`; a layer
    that DISABLES an inherited term still records the spelling, so the studio
    can render the disabled row by name rather than by key. `replacements`
    maps a key's normalised form to `(raw key, replacement)`, with a
    replacement of None meaning "disabled at this layer".

    This is the layer format, not the runtime one: `profile.merge_glossaries`
    folds several of these into the `Glossary` that `hotwords` and `apply`
    consume, dropping every disabled entry on the way.

    `parse_layer` and `load` are the ONLY intended constructors, and unlike
    its sibling `lexicon.Lexicon` this class does not re-validate in
    `__post_init__`: `Lexicon.markers` IS a file shape, so normalising on
    construction is meaningful there, while these two dicts are the
    already-parsed internal form that only `parse_layer` knows how to
    produce. Hand-building one therefore bypasses EVERY check in this module
    - the length cap, the control-character guard, the duplicate-key
    detection and the bool-vs-truthy distinction - and hands a downstream
    merge tuples it will unpack blindly. Build layers through `parse_layer`.

    `track` names a venue in tracks.PACKS whose vocabulary applies to this
    layer's event. It is a SELECTION, not content: the pack is meant to be
    referenced and never copied in, so correcting a name in the registry
    corrects every event at that venue.

    What is validated HERE is the value's shape only - a non-empty string
    within the same length and control-character limits every other text
    value in a layer file gets. Two rules deliberately are NOT: that the id
    names a real pack, and that only an EVENT may set one. Both need context
    this function does not have (the registry, and which layer it is
    reading), so they belong to profile._load_glossary, which enforces both -
    an unknown id is reported as a defect naming the valid ones, and a
    `track` found at workspace or channel scope is reported and ignored
    rather than silently applied. Do not read this field as guarded here;
    the guard lives one layer up."""
    terms: dict[str, tuple[str, bool]]
    replacements: dict[str, tuple[str, str | None]]
    track: str | None = None


# A layer contributing nothing. Never mutated - both fields are replaced
# wholesale by every function that produces a layer, never appended to.
EMPTY_LAYER = GlossaryLayer(terms={}, replacements={})


def _check_text(value: str, what: str) -> None:
    if len(value) > MAX_ENTRY_LENGTH:
        raise ValueError(
            f"{what} is {len(value)} characters, over the {MAX_ENTRY_LENGTH}-character "
            f"cap: {value[:40]!r}…")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value):
        raise ValueError(f"{what} {value!r} must not contain control characters")


def _parse_terms(value: object) -> dict[str, tuple[str, bool]]:
    if isinstance(value, dict):
        items = list(value.items())
    elif isinstance(value, (list, tuple)):
        items = [(term, True) for term in value]
    else:
        raise ValueError(
            f"'terms' must be a list or an object, found {type(value).__name__}")

    result: dict[str, tuple[str, bool]] = {}
    for term, flag in items:
        if not isinstance(term, str) or not term.strip():
            raise ValueError(f"each term must be a non-empty string, found {term!r}")
        spelling = term.strip()
        _check_text(spelling, "term")
        key = normalise_term(spelling)
        if key in result:
            raise ValueError(f"duplicate term {key!r} (terms are case-insensitive)")
        if flag is None or flag is False:
            enabled = False
        elif flag is True:
            enabled = True
        else:
            raise ValueError(
                f"term {key!r} must map to true or false, found {type(flag).__name__}")
        result[key] = (spelling, enabled)
    return result


def _parse_replacements(value: object) -> dict[str, tuple[str, str | None]]:
    if not isinstance(value, dict):
        raise ValueError(
            f"'replacements' must be an object, found {type(value).__name__}")

    result: dict[str, tuple[str, str | None]] = {}
    for raw, replacement in value.items():
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(
                f"each replacement key must be a non-empty string, found {raw!r}")
        raw_key = raw.strip()
        _check_text(raw_key, "replacement key")
        key = normalise_key(raw_key)
        if not key:
            raise ValueError(f"replacement key {raw_key!r} is only punctuation")
        if key in result:
            raise ValueError(
                f"duplicate replacement key {key!r} (keys are matched case- and "
                f"punctuation-insensitively)")
        if replacement is None or replacement is False:
            result[key] = (raw_key, None)
            continue
        if not isinstance(replacement, str):
            raise ValueError(
                f"replacement for {key!r} must be a string, or null to disable it, "
                f"found {type(replacement).__name__}")
        text = replacement.strip()
        if not text:
            # Refused rather than accepted as "delete these words": an empty
            # replacement makes _replace_run drop the matched run entirely,
            # which is indistinguishable from a typo and would silently eat
            # transcript text. Disabling is what null is for.
            raise ValueError(
                f"replacement for {key!r} must not be empty - use null to disable it")
        _check_text(text, "replacement text")
        result[key] = (raw_key, text)
    return result


def _parse_track(value: object) -> str | None:
    """An optional venue id. `null` and an absent key mean the same thing -
    "no track" - because that is what the studio's selector sends when the
    operator clears it, and a save that meant something different from an
    unset file would be a trap."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"'track' must be a non-empty string naming a venue, found {value!r}")
    track = value.strip()
    _check_text(track, "track")
    return track


def parse_layer(data: object) -> GlossaryLayer:
    """Validates one glossary.json's already-parsed JSON as a layer.

    Raises ValueError on the first defect. Lives here rather than in
    profile.py so glossary_admin validates a payload EXACTLY the way
    profile.load validates a file - the "what this accepts, profile.load
    accepts" invariant every admin module in this project keeps."""
    if not isinstance(data, dict):
        raise ValueError(f"glossary must be an object, found {type(data).__name__}")
    return GlossaryLayer(terms=_parse_terms(data.get("terms", [])),
                         replacements=_parse_replacements(data.get("replacements", {})),
                         track=_parse_track(data.get("track")))


def load(path) -> GlossaryLayer:
    """Reads one layer from disk. An absent file is EMPTY_LAYER, not an error -
    every layer being absent still leaves the built-in default."""
    path = Path(path)
    if not path.exists():
        return EMPTY_LAYER
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"is not valid JSON\n{error}") from error
    # No path is appended to a parse_layer defect here (and none was caught
    # only to re-raise it unchanged) - every caller (profile._load_glossary,
    # glossary_admin._load_layer_or_empty) already prefixes the path itself;
    # doing it here too doubled it, in a ProfileError line and in the
    # studio's problems Alert. lexicon.load mirrors this exactly, for the
    # same reason - keep both in sync.
    return parse_layer(data)


# The layer that applies before any other, for every channel and every clip.
# It is EMPTY, and that is the design rather than an omission: there is no
# proper noun that is correct on every circuit, so an always-on layer has
# nothing it can safely hold. What used to live here - the Nürburgring
# Nordschleife's section names and the ten mis-hearings measured on a real
# transcript - is now tracks.PACKS["nurburgring-nordschleife"], selected by
# the events that actually race there. `carousel -> Karussell` is the reason:
# as an always-on rule it rewrote the Carousel at Road America, Sears Point
# and Watkins Glen too.
#
# Do NOT repopulate this with "generally useful" racing vocabulary without
# measuring it first. Every term here costs hotword-prompt budget on every
# window of every clip and every chunk, for every channel (see
# HOTWORD_BUDGET_CHARS), and that cost is what the track packs exist to spend
# deliberately.
#
# This is EMPTY_LAYER itself, not a copy - so the "never mutated" rule on
# EMPTY_LAYER binds here too, and doubly: mutating either name in place would
# corrupt both, and with it every merge in the process.
DEFAULT_LAYER = EMPTY_LAYER
