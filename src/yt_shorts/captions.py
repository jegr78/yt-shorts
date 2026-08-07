"""Groups transcribed words into short caption lines.

Pure logic: no files, no network, no drawing. This is where subtitle
quality is decided, so it is kept testable with invented word lists.

Whisper emits one segment per sentence, which is far too long to read as a
subtitle (one measured clip produced a single 14.6 second segment of 33
words). It also emits no measurable pauses between words, so groups cannot
be split on silence. Grouping therefore runs on three rules: a word count,
an elapsed time, and sentence punctuation closing a group early - followed
by two cleanup passes. The first merges single-word "orphan" captions into
a neighbour, so a lone word never has to hold the screen by itself. The
second moves a trailing function word ("a", "the", "to", ...) forward into
the next caption, because with no punctuation and no pauses to split on, a
caption ending on a word that grammatically points forward to what follows
reads as if it had been cut off mid-thought.

Internally, a caption-in-progress is a list of the original word dicts it
is made of, not text. All three passes - the initial split, orphan
merging, and function-word forwarding - move whole word dicts between
these lists; a `Caption` is built only once, in a final pass, from each
finished list's first word, last word, and joined text. This is
deliberate: a word dict's own `text` may itself contain whitespace (Whisper
can emit " Y Z W" as a single entry), so "number of word dicts" and
"number of whitespace-separated tokens" are not the same count. Nothing
here ever re-derives one from the other.
"""

from __future__ import annotations

from dataclasses import dataclass

# A word ending in one of these closes the group after it, so a caption
# does not straddle a sentence boundary.
CLOSING_PUNCTUATION = ".!?,;:"

# Deliberately small: articles, common prepositions, conjunctions, forms of
# "to be", and pronouns. This is not meant to be an exhaustive list of
# English function words - only common enough to catch the case the
# forwarding rule exists for (a caption ending on a word that points
# forward to the next), without being so broad that it starts overriding
# max_words in ordinary captions.
FUNCTION_WORDS = frozenset({
    "a", "an", "the",
    "to", "of", "in", "on", "at", "for", "from", "with",
    "and", "or", "but", "as",
    "is", "was", "are", "were", "be", "been",
    "that", "this", "it", "its",
    "his", "her", "their",
    "he", "she", "they", "we", "you", "i", "my", "your",
})


@dataclass
class Caption:
    start: float
    end: float
    text: str


# The single source of truth for the documented grouping defaults (see the
# wiki's Subtitles page, which makes a measured quality claim about
# these exact numbers). bin/yt-shorts falls back to these same names rather
# than repeating the literals, so there is exactly one place to change them.
# Tighter values were tried and read worse: at 3 words / 1.6 s the grouping
# cuts phrases into single words that then stand alone for a long time -
# measured on the real speedy transcript, "by" for 2.08 s and "originally"
# for 2.00 s, four captions over their own max_seconds. At 4 / 3.0 the same
# transcript keeps phrases intact ("for the last", "two months or so,") and
# no caption exceeds it.
DEFAULT_MAX_WORDS = 4
DEFAULT_MAX_SECONDS = 3.0


def group_words(words: list[dict], max_words: int = DEFAULT_MAX_WORDS,
                max_seconds: float = DEFAULT_MAX_SECONDS) -> list[Caption]:
    """Groups words into captions of at most max_words and max_seconds."""
    if max_words < 1:
        raise ValueError(f"max_words must be at least 1, got {max_words}")
    if max_seconds <= 0:
        raise ValueError(f"max_seconds must be positive, got {max_seconds}")

    groups: list[list[dict]] = []
    current: list[dict] = []

    def flush() -> None:
        if not current:
            return
        groups.append(list(current))
        current.clear()

    for word in words:
        text = word["text"].strip()
        if not text:
            continue
        # Would this word push the group past the time limit? Decide before
        # appending, so a group never exceeds the limit - unless it is a
        # single word that is longer than the limit all by itself.
        if current and word["end"] - current[0]["start"] > max_seconds:
            flush()
        current.append(word)
        if len(current) >= max_words or text[-1] in CLOSING_PUNCTUATION:
            flush()

    flush()
    groups = _merge_orphans(groups, max_words, max_seconds)
    groups = _forward_function_words(groups, max_words, max_seconds)

    return [_to_caption(group) for group in groups]


def _to_caption(group: list[dict]) -> Caption:
    """Builds the Caption for a finished group of word dicts. Called once
    per group, only after nothing more will move between groups.

    Whisper marks the start of a new word with a leading space; a
    continuation token (part of the same word - e.g. the transcript for
    "Speedy" spells out as " C", ".L", ".R." with no space before ".L" or
    ".R.") carries none. Stripping every token and rejoining with " "
    destroys that distinction and turns a continuation into a word of its
    own - measured on the real speedy transcript, it renders on screen as
    "friend C .L .R." instead of "friend C.L.R.". Joining the raw token
    texts and stripping only the finished caption's own outer edges
    preserves Whisper's own spacing exactly."""
    return Caption(
        start=group[0]["start"],
        end=group[-1]["end"],
        text="".join(word["text"] for word in group).strip(),
    )


def _ends_a_sentence(group: list[dict]) -> bool:
    """True if this group was deliberately closed by punctuation, i.e. it
    must not be silently rejoined to whatever follows it."""
    text = group[-1]["text"].strip()
    return bool(text) and text[-1] in CLOSING_PUNCTUATION


def _merge_orphans(groups: list[list[dict]], max_words: int,
                    max_seconds: float) -> list[list[dict]]:
    """Folds single-word-dict groups into a neighbour so a lone word never
    has to carry a caption by itself when it can be avoided.

    A word is merged into whichever neighbour yields the shorter resulting
    time span; ties prefer the preceding neighbour. A merge may push a
    group one word dict past max_words, but never past max_seconds, and
    never crosses a sentence boundary that the punctuation rule
    deliberately created. A word with no eligible neighbour - surrounded by
    silence, or alone in the input - stays a group of its own.

    "Single word" means a group holding exactly one word dict, regardless
    of how many whitespace-separated tokens that dict's own text contains.
    """
    result: list[list[dict]] = []
    i = 0
    n = len(groups)
    while i < n:
        orphan = groups[i]
        if len(orphan) != 1:
            result.append(orphan)
            i += 1
            continue

        prev = result[-1] if result else None
        nxt = groups[i + 1] if i + 1 < n else None

        orphan_start = orphan[0]["start"]
        orphan_end = orphan[-1]["end"]

        prev_ok = (
            prev is not None
            and not _ends_a_sentence(prev)
            and len(prev) <= max_words
            and (orphan_end - prev[0]["start"]) <= max_seconds
        )
        next_ok = (
            nxt is not None
            and not _ends_a_sentence(orphan)
            and len(nxt) <= max_words
            and (nxt[-1]["end"] - orphan_start) <= max_seconds
        )

        if prev_ok and next_ok:
            merge_into_prev = (orphan_end - prev[0]["start"]) <= (nxt[-1]["end"] - orphan_start)
        elif prev_ok:
            merge_into_prev = True
        elif next_ok:
            merge_into_prev = False
        else:
            result.append(orphan)
            i += 1
            continue

        if merge_into_prev:
            result[-1] = prev + orphan
            i += 1
        else:
            result.append(orphan + nxt)
            i += 2

    return result


# A run of trailing function words longer than this stops being moved, so a
# caption cannot be hollowed out by a string of them (see
# _forward_function_words).
MAX_FORWARDED_WORDS = 2


def _is_function_word(word_text: str) -> bool:
    """True if word_text - ignoring case, surrounding whitespace and
    trailing punctuation - is one of FUNCTION_WORDS."""
    bare = word_text.strip().rstrip(CLOSING_PUNCTUATION).lower()
    return bare in FUNCTION_WORDS


def _forward_function_words(groups: list[list[dict]], max_words: int,
                             max_seconds: float) -> list[list[dict]]:
    """Moves up to MAX_FORWARDED_WORDS trailing function-word dicts from
    each group into the group that follows it, so a caption does not end
    on a word ("a", "the", "to", ...) that grammatically points forward to
    whatever comes next.

    This is a preference, not an absolute: it gives way rather than cause a
    worse problem. A move is refused when it would leave the source group
    with fewer than two word dicts (recreating the orphans _merge_orphans
    just removed), when it would push the destination group past
    max_words + 1 word dicts or max_seconds, when there is no destination
    group to move into, or when the source group was deliberately closed by
    sentence punctuation (moving a word across that boundary would undo the
    punctuation rule).

    Each element of groups is the list of original word dicts making up
    that caption, in order; groups are mutated and read in lockstep as
    words move between adjacent entries.
    """
    for i in range(len(groups) - 1):
        src = groups[i]
        if _ends_a_sentence(src):
            continue

        run = 0
        for word in reversed(src):
            if run >= MAX_FORWARDED_WORDS:
                break
            if _is_function_word(word["text"]):
                run += 1
            else:
                break
        if run == 0:
            continue
        if len(src) - run < 2:
            continue

        dst = groups[i + 1]
        moved = src[-run:]

        if len(dst) + run > max_words + 1:
            continue
        new_dst_start = moved[0]["start"]
        if dst[-1]["end"] - new_dst_start > max_seconds:
            continue

        del src[-run:]
        dst[0:0] = moved

    return groups
