"""Transcribes a clip's commentary into words with timestamps.

Only the finished excerpt is transcribed, not the source stream: for
subtitles the 15-60 seconds already on disk are enough. faster-whisper is
used rather than mlx-whisper because the latter pulls in torch (~2 GB).
Decoding runs with `vad_filter=False` (see the comment in `transcribe()`
for why). Measured on this machine (Apple M3, CPU, small model) against
the six real drafts under
`channels/erf/events/community-clips-back-catalogue/drafts/` (15-60
seconds each): 3.7-10.7 seconds, tracking clip duration fairly closely
(60 s clips ~9.5-10.7 s, 15 s clips ~3.7-4.2 s) though not exactly - one
25.5 s clip finished in 3.7 s. On the same six drafts, `vad_filter=True`
came out within 0.9-1.2x of that - real speech is a wash either way. The
real cost of `vad_filter=False` concentrates on near-silent audio: a
20-second synthetic silent clip took 42.3 s with `vad_filter=False` vs.
1.6 s with `vad_filter=True`, roughly 26x. Absolute timings are machine-
and run-dependent; only the shape - real speech a wash, near-silence far
slower - is expected to hold elsewhere.

The result is cached per clip, so re-rendering costs no transcription. A
clip with no speech - only engine noise, say - legitimately transcribes to
zero words; that is not an error, it is cached and returned like any other
result. `TranscriptionError` is reserved for audio extraction failing,
transcription itself failing, or a cache file that cannot be read back.

An optional `glossary` (see `yt_shorts.glossary`) biases the decoder and
corrects its output on a fresh decode - see `transcribe()`'s own docstring
for both mechanisms and, importantly, for why a cache hit is never
re-corrected against whatever glossary the caller passes on a later call.

The cache is keyed by the caller's own file name choice (see
`bin/yt-shorts`'s `cmd_render`, which passes each clip's own, permanent
`clipstore.transcript_path()` - a fixed name inside a directory that is
itself keyed to the clip's identity, see `clipid.py` and `clipstore.py`),
not by anything transcribe() itself derives from the clip. Under the
current clip store this can no longer collide the way it could under the
old layout's shared, slug-named `transcripts/` directory (see
`clipstore.py`'s own docstring), but the `source` check below is kept as
defense in depth regardless - a migrated cache, or any future caller that
is not as careful with its own naming, is still caught rather than
trusted blindly. This cache is also the one artifact meant to persist
across runs, so it cannot simply be deleted up front the way the raw
download is (see `render.build_short`'s own convention for that). Every
call therefore also carries a `source` identifying what is actually being
transcribed (the caller's clip URL, for instance): on a cache hit,
`transcribe()` only trusts the cache if its recorded `source` matches.
Anything else - a different source, or a cache written before this
parameter existed and so carrying no `source` at all - is treated as
unusable and silently re-transcribed, exactly like a fresh cache miss, and
reported once on stderr. This is deliberately not covered by
`TranscriptionError`: a source mismatch is not a failed extraction, a
failed decode, or a corrupt cache file - it is the cache correctly
refusing to hand back someone else's transcript.

Unbounded decode: `TIMEOUT_SECONDS` below bounds only the ffmpeg audio
extraction that precedes decoding (`_extract_audio`); the decode call
itself (`model.transcribe(...)`) has no timeout and can in principle run
indefinitely - this is the one place where this project's "one failed
clip must never abort a run" guarantee (see CLAUDE.md) does not hold. This
is a deliberate omission, not an oversight: it was investigated and no
available mechanism could be trusted not to misfire. A signal-based
timeout (`signal.alarm`) only works on the main thread and, more
fundamentally, can only be delivered when control returns to the Python
interpreter - a decode stuck deep inside ctranslate2's C++ has no
guaranteed point where that happens, so the timeout could simply never
fire. A thread-based timeout can abandon *waiting* for the decode, but
cannot stop it - Python threads are not killable - so the abandoned
decode keeps running in the background regardless, consuming CPU
alongside whatever runs next and leaving the underlying hang exactly as
unresolved as before, only hidden. A subprocess-based timeout (spawn the
decode in its own process, hold a hard deadline, SIGKILL on expiry) is
the only mechanism that could actually stop a hung decode, but it
requires restructuring how (and how often) the model is loaded and
invoked, which is a meaningfully sized change on its own, not a small
addition to this one - implementing it without that dedicated attention
risks trading a rare, honestly-documented hang for an occasional silently
truncated real transcription, which is worse. A misfiring timeout would
abort a good clip; the gap documented here does not.

If a render appears to hang, check whether it is stuck in transcription
(compare elapsed time against the per-clip decode timings measured above,
or check whether the stuck clip has `subtitles.enabled` on and a
transcript cache miss). Try interrupting with Ctrl-C first. If that does
not respond within a few seconds, that is itself a sign the decode is
stuck somewhere Python's own interrupt delivery cannot reach, and the
process needs to be killed outright (e.g. `kill -9 <pid>`) and the render
re-run - the render loop is not resumable mid-run either way, so nothing
is lost by killing it. As a workaround for a clip that reproducibly hangs,
disable subtitles for that one event, or remove the offending clip from
clips.json before re-rendering.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from . import glossary as _glossary
from .clipid import canonical_url
from .glossary import Glossary

TIMEOUT_SECONDS = 900  # bounds ONLY _extract_audio's ffmpeg call below - see
# the module docstring's "Unbounded decode" note: the Whisper decode itself
# has no timeout, deliberately.
SAMPLE_RATE = 16000  # what Whisper expects

# THE TWO POPULATIONS OVERLAP. THERE IS NO SEPARATING VALUE ANY MORE.
#
# This constant used to be 0.75, chosen to sit strictly between two measured
# groups: the highest no_speech_prob on any real-speech segment across the
# six original drafts was 0.6847 (speedy.mp4), and a silent clip's
# hallucinated "You" measured 0.8791, later 0.8361 on a fresh silent clip.
# A threshold between them dropped the hallucination and kept the speech.
#
# That gap closed when the glossary arrived, and nobody re-measured. Its
# terms are handed to the decoder as `hotwords`, and biasing the decoder
# also RAISES its own no_speech_prob on real speech. Measured on this
# workspace's `last-gasp-lap-steals-super-pole` clip - one 84-second file,
# one model, the ONLY difference being the glossary:
#
#   without the glossary   165 words, 0.0-84.2 s, nothing dropped
#   with the glossary       40 words, 0.5-23.4 s, 4 segments dropped
#                                     (0.7754, 0.8283, 0.8747, 0.8984)
#
# Every one of those four is demonstrably real speech - decoded separately
# they read "He's going for it... this should be enough to send the number 5
# car into the Super Pole". So real speech now reaches 0.8984, ABOVE the
# 0.8361 measured for a silence hallucination. No threshold separates them:
# any value that keeps all measured speech also keeps the hallucination.
#
# So this is now a POLICY choice, not a discriminator, and the operator made
# it explicitly: wrong captions beat missing ones, because correcting a few
# words in the studio's transcript editor is less work than typing in every
# word that was silently thrown away. 0.95 sits above every real-speech
# observation to date (0.8984) and leaves only a net for a genuinely
# degenerate segment scoring near certainty. The cost is accepted and known:
# a hallucinated "You" on a silent stretch now survives into the captions,
# where it is visible and one edit away from gone - the failure this drops
# is recoverable, the one it used to cause was not.
#
# See tests/test_transcribe.py::TestNoSpeechThreshold, which pins the new
# premise rather than the old gap.
NO_SPEECH_PROB_THRESHOLD = 0.95


class TranscriptionError(Exception):
    """Audio extraction or transcription failed."""


def _extract_audio(video: str, wav: Path, ffmpeg: str) -> None:
    command = [ffmpeg, "-v", "error", "-y", "-i", video,
               "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), str(wav)]
    result = subprocess.run(command, capture_output=True, text=True,
                            timeout=TIMEOUT_SECONDS)
    if result.returncode != 0 or not wav.exists():
        raise TranscriptionError(
            "ffmpeg failed extracting audio.\nCommand: " + " ".join(command)
            + "\nOutput: " + result.stderr.strip()
        )


def _validate_cached_words(payload: object, cache_path: Path) -> list[dict]:
    """Returns the cached word list, or raises if the cache is unusable.

    A cache file is valid JSON but can still fail its contract: a bare
    list instead of an object, no "words" key, "words" not a list, or an
    entry missing start/end/text. Any of those must be reported, not
    silently treated as zero words - only an *empty* "words" list (a
    legitimately silent clip) is a valid cached outcome.
    """
    if not isinstance(payload, dict):
        raise TranscriptionError(
            f"Cached transcript is unreadable: {cache_path}\n"
            f"Expected a JSON object, found {type(payload).__name__}.\n"
            "Delete the file to transcribe again."
        )
    if "words" not in payload:
        raise TranscriptionError(
            f"Cached transcript is unreadable: {cache_path}\n"
            "Missing \"words\" key.\n"
            "Delete the file to transcribe again."
        )
    words = payload["words"]
    if not isinstance(words, list):
        raise TranscriptionError(
            f"Cached transcript is unreadable: {cache_path}\n"
            f"\"words\" is not a list, found {type(words).__name__}.\n"
            "Delete the file to transcribe again."
        )
    for index, word in enumerate(words):
        if not isinstance(word, dict) or not {"start", "end", "text"} <= word.keys():
            raise TranscriptionError(
                f"Cached transcript is unreadable: {cache_path}\n"
                f"Entry {index} is missing start, end or text: {word!r}\n"
                "Delete the file to transcribe again."
            )
    return words


def _cached_dropped_probs(payload: dict) -> list[float]:
    """Returns the no_speech_prob values dropped when this cache was
    written, or [] if there are none to report.

    The "dropped" key postdates the cache format above: a cache written
    before it exists has no such key at all, and that means "nothing was
    dropped", not "corrupt" - unlike "words", this key is informational,
    not part of the contract, so a missing or malformed value never blocks
    a cache hit that otherwise validated fine.
    """
    dropped = payload.get("dropped")
    if not isinstance(dropped, dict):
        return []
    probs = dropped.get("no_speech_prob")
    if not isinstance(probs, list):
        return []
    return [p for p in probs if isinstance(p, (int, float))]


def _print_note(message: str) -> None:
    """The default reporter: the stderr NOTE this module has always printed.

    Kept as the DEFAULT rather than the only mechanism. A caller that has
    somewhere better to put a note passes `on_note` (see transcribe), and the
    one that matters is subtitle_pipeline: a studio render runs on a
    background thread nobody reads stderr from, so a clip that quietly lost
    three quarters of its captions used to report "done" and nothing else -
    in the job log, in the render panel, anywhere. That exact failure was
    already fixed once for the pipeline's own notes; these two reporters were
    left behind on stderr, and this is them catching up.
    """
    print(f"NOTE: {message}", file=sys.stderr)


def _report_glossary_budget(name: str, glossary: Glossary,
                            report=_print_note) -> None:
    """Prints a NOTE naming the clip when `glossary`'s decoder bias is at
    risk of faster-whisper's own hotword-prompt truncation (see
    `glossary.hotwords_at_risk` and `glossary.HOTWORD_BUDGET_CHARS`) -
    terms beyond that budget silently never reach the decoder. This module
    has no logging.Logger of its own (see the print-based NOTE convention
    `_report_dropped` already uses); this follows the same convention rather
    than introducing a second one. Counts and lengths only, never the term
    text itself - the same log-hygiene discipline the rest of this project
    keeps (see CLAUDE.md, "Secrets never reach a log")."""
    if not _glossary.hotwords_at_risk(glossary):
        return
    text = _glossary.hotwords(glossary) or ""
    report(
        f"{name}: glossary hotword bias is {len(text)} character(s) across "
        f"{len(glossary.terms)} term(s), at risk of faster-whisper's own hotword-prompt "
        "truncation - terms beyond the budget will not reach the decoder"
    )


def _report_dropped(name: str, probs: list[float], report=_print_note) -> None:
    """Prints a NOTE naming the clip, how many segments were dropped for
    scoring above NO_SPEECH_PROB_THRESHOLD, and their no_speech_prob
    values - so a caption silently going missing is visible during a run
    instead of leaving zero trace (see NO_SPEECH_PROB_THRESHOLD's comment
    on why that threshold is empirical, not a guaranteed bound). This is
    not an error: it never raises and never changes what transcribe()
    returns, on a fresh decode or a cache hit alike.
    """
    if not probs:
        return
    values = ", ".join(f"{p:.4f}" for p in probs)
    report(
        f"{name}: dropped {len(probs)} segment(s) above "
        f"no_speech_prob threshold {NO_SPEECH_PROB_THRESHOLD} ({values}) - "
        f"those seconds have no captions"
    )


def decode_wav(wav_path, *, model_name: str,
               glossary: Glossary = _glossary.EMPTY) -> tuple[list[dict], list[float]]:
    """Decodes one wav with the tool's fixed, measured settings.

    Returns (words, dropped_probs), with glossary corrections already applied to
    words. vad_filter stays False and segments above NO_SPEECH_PROB_THRESHOLD are
    dropped after decoding, not skipped during it - see the long comments below
    for why both choices are load-bearing and measured. This is the single place
    decode settings live: transcribe() calls it in-process for a clip, and
    stream_transcribe's subprocess worker calls it for a chunk, so the two decode
    identically.

    The `from faster_whisper import WhisperModel` stays a function-local import:
    the test suite patches faster_whisper.WhisperModel and relies on this late
    binding.
    """
    from faster_whisper import WhisperModel

    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    # Whisper is known to hallucinate a word (commonly "You" or "Thank
    # you.") on audio with no speech at all - reproduced here on a
    # digitally silent clip. vad_filter=True "fixes" that by running
    # Silero voice-activity detection ahead of the model and skipping
    # non-speech audio, but it does so by changing the chunk
    # boundaries the decoder sees, and that changes recognition near
    # them - measured, not assumed: on two of the six real drafts
    # under channels/erf/events/community-clips-back-catalogue/drafts/
    # it produced a *different* transcript than vad_filter=False, not
    # just a shorter one. On forcing-a-sc.mp4 it replaced "Now, of
    # course, I'm calling that as deliberate." with the fabricated
    # "Now I've got a side. I'm calling that as deliberate." - invented
    # speech, not a dropped word. That is worse than the hallucination
    # it was meant to fix: a phantom caption on a silent engine-noise
    # clip is obviously wrong, but a fabricated sentence over real
    # commentary reads as something the commentator actually said and
    # would pass a human review before upload. So vad_filter stays
    # off, and the decoder sees the unmodified audio.
    #
    # That reintroduces the silence hallucination, so it is handled
    # separately: faster-whisper reports no_speech_prob per segment
    # even when it still emits the segment (its own no_speech_threshold
    # parameter does not help here - the model's confident-but-wrong
    # logprob on the hallucinated segment overrides that skip no
    # matter what threshold is passed in, measured at every value from
    # 0.6 down to 0.2). Filtering by no_speech_prob ourselves, after
    # decoding, does work and does not touch what the decoder saw: the
    # hallucinated "You" segment measured no_speech_prob 0.8791 in the
    # original measurement (0.8361 on a fresh silent clip retested
    # later - see NO_SPEECH_PROB_THRESHOLD's own comment), while the
    # highest no_speech_prob on any real segment across all six drafts
    # measured 0.6847 (speedy.mp4). NO_SPEECH_PROB_THRESHOLD (0.75)
    # sits between those two measurements, so it drops the former and
    # keeps every real segment - all six drafts decode
    # character-identical to vad_filter=False with no filtering.
    # That gap is only ~0.15 wide using the tighter of the two silence
    # measurements, from two data points (one synthetic silence case,
    # one real clip); it is not a theoretically safe margin, only the
    # narrowest one measured so far - both figures are observations,
    # not guaranteed limits.
    segments, _ = model.transcribe(str(wav_path), word_timestamps=True,
                                   vad_filter=False,
                                   hotwords=_glossary.hotwords(glossary))
    # A segment scoring above the threshold is discarded, not merely
    # skipped: without recording *that* it happened, a drop is
    # invisible - nothing on stdout/stderr, nothing in the returned
    # words, nothing in the cache - and a missing caption is far
    # harder for a reviewer to notice than a wrong one, since nothing
    # on screen looks broken. dropped_probs is reported by the caller and
    # written into the cache alongside "words" so the fact survives a
    # cache hit too.
    words: list[dict] = []
    dropped_probs: list[float] = []
    for segment in segments:
        if segment.no_speech_prob <= NO_SPEECH_PROB_THRESHOLD:
            words.extend(
                {"start": word.start, "end": word.end, "text": word.word}
                for word in (segment.words or [])
            )
        else:
            dropped_probs.append(float(segment.no_speech_prob))
    # Corrections are applied BEFORE the cache is written, not on every
    # cache hit - see transcribe()'s docstring ("Nothing here invalidates
    # a cache...") - so the cache holds the corrected transcript and a
    # re-render costs nothing.
    words = _glossary.apply(words, glossary)
    return words, dropped_probs


def transcribe(video: str, cache: str, model_name: str = "small",
               ffmpeg: str = "ffmpeg", *, source: str,
               display_name: str | None = None,
               glossary: Glossary = _glossary.EMPTY,
               on_note=None) -> list[dict]:
    """Returns words as {"start", "end", "text"}, using the cache if present.

    ``glossary`` (see yt_shorts.glossary) attacks Whisper's ignorance of a
    channel's own proper nouns from both ends, but ONLY on a fresh decode:
    its ``terms`` are passed to the decoder as ``hotwords`` (biasing it
    before it errs), and its ``replacements`` are applied to the decoded
    words (correcting what slips through) before they are cached. A
    default-``EMPTY`` glossary changes nothing - no hotwords, no
    replacements - exactly today's behaviour.

    Nothing here invalidates a cache because the glossary changed: a cache
    hit returns exactly what was cached, uncorrected by whatever glossary
    the caller passes this time, even if it now names a term that would
    have matched. This is deliberate for this pre-alpha stage, not an
    oversight - there is no cheap way to know a glossary changed since a
    transcript was cached without hashing or versioning it, and that is
    more machinery than this stage warrants. An operator who edits a
    glossary and sees no difference on an already-transcribed clip should
    delete that clip's cached transcript.json; the next render transcribes
    it again from scratch, against the current glossary.

    ``source`` identifies what is actually being transcribed - the clip's
    URL, in `bin/yt-shorts`'s case - and is stored in the cache alongside
    the words. A cache hit is only trusted when its recorded "source"
    matches this call's ``source`` exactly; otherwise the cache is
    discarded and a fresh transcription is performed, same as if the cache
    file did not exist, with a NOTE on stderr naming what happened. This
    includes a cache with no "source" key at all - unlike the "dropped"
    key, which simply defaults to "nothing was dropped" when absent from
    an old cache, a missing "source" cannot be defaulted to a match: "no
    recorded source" is exactly the state where trusting the cache would
    be unsafe (it would silently hand back whatever clip was transcribed
    before this parameter existed, or before a name collision reused this
    same cache file for a different clip). Required, not optional, because
    every caller of this cache-backed function has - and must supply - an
    identity for what it is transcribing.

    ``display_name``, if given, is what the NOTEs below name instead of
    ``video`` - every other NOTE this tool prints names the clip's short
    name, not a raw file path, and this one should match. It defaults to
    ``video`` because `transcribe` is also called from places (tests,
    future callers) that have no display name to give it and must not be
    forced to invent one; `bin/yt-shorts`'s own caller passes the clip's
    slug here.

    The Whisper decode below has no timeout of its own - see the module
    docstring's "Unbounded decode" note for why, and what to do if a run
    appears stuck on it.
    """
    name = display_name if display_name is not None else video
    # Defaults to the stderr NOTE this module has always printed, so the CLI
    # operator's terminal is unchanged; subtitle_pipeline passes its own
    # `note`, which reaches the job log and the studio's render panel.
    report = on_note if on_note is not None else _print_note
    cache_path = Path(cache)
    if cache_path.exists():
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise TranscriptionError(
                f"Cached transcript is unreadable: {cache_path}\n{error}\n"
                "Delete the file to transcribe again."
            ) from error
        words = _validate_cached_words(payload, cache_path)
        cached_source = payload.get("source")
        # Compared canonically, not as raw strings - the same "share
        # parameter" case clipstore._existing_dir has to guard against (see
        # its own docstring): two URLs that canonicalize to the same value
        # name the same clip, and a raw-string mismatch between them (e.g.
        # a fresh ?si=... token on an otherwise-identical URL) must not be
        # treated as "a different source", which would throw away a good
        # cache and force an unnecessary re-transcription.
        if (isinstance(cached_source, str)
                and canonical_url(cached_source) == canonical_url(source)):
            _report_dropped(name, _cached_dropped_probs(payload), report)
            return words
        report(f"{name}: cached transcript at {cache_path} was recorded "
               "for a different source, re-transcribing")

    wav = cache_path.with_suffix(".wav")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    _extract_audio(video, wav, ffmpeg)

    _report_glossary_budget(name, glossary, report)
    try:
        words, dropped_probs = decode_wav(wav, model_name=model_name,
                                          glossary=glossary)
    except Exception as error:  # noqa: BLE001 - reported, never swallowed
        # Leave the wav in place: same convention as render.build_short -
        # clean up after success, keep everything after a failure, because
        # the wav is the only artifact that shows whether the audio itself
        # was the problem.
        raise TranscriptionError(
            f"{type(error).__name__}: {error}"
        ) from error

    _report_dropped(name, dropped_probs, report)
    cache_path.write_text(
        json.dumps({
            "model": model_name,
            "source": source,
            "words": words,
            "dropped": {
                "count": len(dropped_probs),
                "no_speech_prob": dropped_probs,
            },
        }, indent=2),
        encoding="utf-8",
    )
    wav.unlink(missing_ok=True)
    return words
