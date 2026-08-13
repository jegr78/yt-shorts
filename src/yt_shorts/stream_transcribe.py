"""Transcribe a whole stream in resumable chunks (see the stage D2a design).

An 8-hour stream is ~1 hour of Whisper decode; done as one call it cannot be
resumed or safely timed out. This splits the stream's audio into fixed windows,
decodes each in a killable subprocess, caches each chunk's words, and assembles
the present chunks into one timed transcript. The downloaded audio is kept
because decoding needs it - loudness-based ranking was tried and removed (see
CLAUDE.md). downloader and decoder are injected so all of this tests
without the network or a real model.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import atomicwrite
from . import glossary as _glossary
from .cancel import CancelToken, Stopped, cancel_kwargs, run_cancellable
from .glossary import Glossary
from .logsetup import shorten_urls
from .pathnames import validate_segment

_logger = logging.getLogger("ytshorts.transcribe")


def chunk_windows(total_seconds: float, chunk_seconds: int) -> list[tuple[int, float, float]]:
    """(index, start, length) windows covering total_seconds; last one short."""
    windows: list[tuple[int, float, float]] = []
    index = 0
    start = 0.0
    while start < total_seconds:
        length = min(float(chunk_seconds), total_seconds - start)
        windows.append((index, start, length))
        index += 1
        start += chunk_seconds
    return windows


def offset_words(words: list[dict], start: float) -> list[dict]:
    """Shift each word's start/end by `start`; text unchanged."""
    return [{"start": w["start"] + start, "end": w["end"] + start, "text": w["text"]}
            for w in words]


class StreamTranscribeError(Exception):
    """Understandable message about a failed stream transcription."""


@dataclass
class DownloadedAudio:
    path: Path
    duration_seconds: float


@dataclass
class StreamTranscript:
    video_id: str
    audio_path: Path
    duration_seconds: float
    words: list[dict]
    missing_chunks: list[int] = field(default_factory=list)


def _stream_dir(workspace_dir: Path, video_id: str) -> Path:
    # video_id becomes a path segment (streams/<id>/...) and is embedded in the
    # download URL; validate it before either. Real YouTube ids are [A-Za-z0-9_-],
    # so a '/' or '..' id is malformed input, not a legitimate stream.
    try:
        validate_segment(video_id, what="video id")
    except ValueError as error:
        raise StreamTranscribeError(str(error)) from error
    return Path(workspace_dir) / "streams" / video_id


def _read_cached_chunk(path: Path, video_id: str, start: float, length: float) -> list[dict] | None:
    """Returns the cached words if the file is for exactly this window, else None."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if (payload.get("stream") == video_id
            and payload.get("start") == start
            and payload.get("length") == length
            and isinstance(payload.get("words"), list)):
        return payload["words"]
    return None


def run_with_timeout(cmd: list[str], timeout: float, *,
                     env: dict[str, str] | None = None,
                     cancel: CancelToken | None = None) -> str:
    """Runs cmd, returns stdout; kills the process on timeout.

    subprocess.run(timeout=...) raises TimeoutExpired and kills the immediate
    child, which is what a chunk decode needs: the point of running the decode in
    a separate process is that this kill actually stops it, where a signal or a
    thread could not (see transcribe.py's "Unbounded decode" note).

    `env`, when given, replaces the environment the child inherits (None means
    "inherit the parent's unchanged", subprocess.run's own default) - this is how
    the decode worker below gets a PYTHONPATH that can find its own package. The
    timeout semantics are unaffected either way.

    `cancel` is the operator's HARD stop, and it works for exactly the same
    reason the timeout does: this function owns the child, so terminating it
    really stops the decode. Without a token nothing changes at all - the
    call is still a plain `subprocess.run` (see `cancel.run_cancellable`).
    A `Stopped` raised here travels out untouched: it is not a decode
    failure, and wrapping it in `StreamTranscribeError` would make the
    per-chunk handler below record a chunk nobody finished looking at as one
    that was attempted and failed.
    """
    try:
        result = run_cancellable(cmd, timeout=timeout, env=env, cancel=cancel)
    except subprocess.TimeoutExpired as error:
        raise StreamTranscribeError(
            f"decode exceeded {timeout}s and was killed") from error
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-1:] or ["worker failed"]
        raise StreamTranscribeError(tail[0])
    return result.stdout


# A generous multiple of the measured ~74s decode for a 600s chunk: it must only
# ever fire on a genuine hang, never on a slow-but-healthy chunk (same reasoning
# transcribe.py gives for having no clip timeout at all).
CHUNK_TIMEOUT_SECONDS = 600


def _worker_env() -> dict[str, str]:
    """Environment for the `-m yt_shorts._decode_worker` subprocess.

    This project has no pyproject.toml / installed package - every invocation
    needs `src` on sys.path, which the parent gets either from the PYTHONPATH
    env var (CLI/pytest) or, for bin/yt-shorts, an in-process sys.path.insert.
    Neither reaches a subprocess: sys.path edits are process-local and
    PYTHONPATH is only inherited if it was actually set. Without this, `python -m
    yt_shorts._decode_worker` cannot find the yt_shorts package at all - the bug
    this function fixes. The src dir is derived from this module's own location
    (never hard-coded) and PREPENDED to any PYTHONPATH the parent already has,
    rather than clobbering it; starting from a full copy of os.environ keeps
    everything else the child needs (PATH, HOME, the venv, HuggingFace cache
    vars for the model download).
    """
    src_dir = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join([src_dir, existing]) if existing else src_dir
    return env


def subprocess_decoder(audio_path, start, length, *, glossary: Glossary = _glossary.EMPTY,
                       ffmpeg="ffmpeg", model_name="small",
                       runner=run_with_timeout,
                       cancel: CancelToken | None = None) -> list[dict]:
    """Extracts [start, start+length] to a wav and decodes it in a killable worker.

    `glossary`'s terms are handed to the worker as the hotword bias for this
    chunk, via a JSON file whose path becomes the worker's argv[3]. That
    parameter has existed in _decode_worker.main since D2a and was never
    passed - which is why every stream chunk in this project decoded with no
    bias at all. The file lives in the same TemporaryDirectory as the wav, so
    it is cleaned up with it and never lands in the workspace.

    Only `terms` are ever written here - never `replacements`. `decode_wav`
    (called by the worker's `main`, same as it is called in-process for a
    clip) applies replacements UNCONDITIONALLY before returning, so a
    replacement sent here would correct the chunk's text inside the worker
    and get it cached in that already-corrected form. That breaks two things
    `transcribe_stream` promises: a correction whose key spans a chunk
    boundary ("kleine carousel", one word decoded per chunk) can never match,
    because chunk 1 would already read "Karussell" by the time it is
    assembled next to chunk 0's "kleine"; and the "a glossary change takes
    effect on the next assembly with no re-decode" property (see
    transcribe_stream's docstring) would be false, since the cached text
    would already carry the OLD correction baked in with no way back to what
    was actually decoded. It would also risk a second, unintended pass if a
    replacement's own output text happened to collide with another key. The
    split is deliberate: terms bias the worker, replacements are applied
    ONCE, at assembly, in transcribe_stream itself. If a future change adds
    `replacements` back to this payload, that is this regression again, not
    an improvement.

    An EMPTY glossary, and a glossary with terms=[] (replacements-only),
    both append nothing, keeping the argv byte-identical to what it was
    before this parameter existed: "no hotwords" and "hotwords=''" are not
    the same request (see glossary.hotwords), so a glossary with nothing to
    bias with must not reach the worker as one that does.

    `cancel` is forwarded to `runner` ONLY when there is one, for the same
    reason the glossary path above appends nothing when there is nothing to
    append: a runner written before this parameter existed (every fake in
    the suite, and any future one) keeps being called with exactly the
    keyword arguments it already accepts. A caller that actually wants a
    hard stop is the only one that changes the call shape.
    """
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "chunk.wav"
        extract = [ffmpeg, "-v", "error", "-y", "-ss", str(start), "-t", str(length),
                   "-i", str(audio_path), "-ac", "1", "-ar", "16000", str(wav)]
        subprocess.run(extract, capture_output=True, text=True, timeout=900, check=True)
        args = [sys.executable, "-m", "yt_shorts._decode_worker", str(wav), model_name]
        if glossary.terms:
            glossary_path = Path(tmp) / "glossary.json"
            glossary_path.write_text(
                json.dumps({"terms": list(glossary.terms)}),
                encoding="utf-8")
            args.append(str(glossary_path))
        if _glossary.hotwords_at_risk(glossary):
            # Counts and lengths only, never term text - see the log-hygiene
            # discipline logsetup.shorten_urls documents elsewhere in this
            # project (CLAUDE.md, "Secrets never reach a log"). This chunk's
            # decoder bias is at risk of faster-whisper's own hotword-prompt
            # truncation (see glossary.HOTWORD_BUDGET_CHARS): terms beyond
            # that budget silently never reach the decoder for this chunk.
            _logger.warning(
                "chunk at %.0fs (%.0fs): glossary hotword bias is %d character(s) across "
                "%d term(s), at risk of faster-whisper's own hotword-prompt truncation - "
                "terms beyond the budget will not reach the decoder",
                start, length, len(_glossary.hotwords(glossary) or ""), len(glossary.terms))
        _logger.info("decoding chunk at %.0fs (%.0fs) with model %s, %d hotword(s)",
                     start, length, model_name, len(glossary.terms))
        out = runner(args, timeout=CHUNK_TIMEOUT_SECONDS, env=_worker_env(),
                    **cancel_kwargs(cancel))
    return json.loads(out)


# A container's reported duration is not bit-exact with the source's true
# duration, so "matches" allows either 1% or 2 seconds of slack, whichever is
# larger (2s dominates for short streams, 1% for long ones).
DURATION_TOLERANCE_FRACTION = 0.01
DURATION_TOLERANCE_MIN_SECONDS = 2.0


def _duration_matches(local_seconds: float, true_seconds: float) -> bool:
    tolerance = max(true_seconds * DURATION_TOLERANCE_FRACTION, DURATION_TOLERANCE_MIN_SECONDS)
    return abs(local_seconds - true_seconds) <= tolerance


def _default_runner(args: list[str], *, timeout: float) -> str:
    """Runs a subprocess, raising RuntimeError with its own last stderr line on failure.

    Shared by every yt-dlp and ffprobe call `ytdlp_downloader` makes, so a fake
    substituted in tests exercises the same failure shape production hits.
    """
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-1:] or ["command failed"]
        raise RuntimeError(tail[0])
    return result.stdout


def ytdlp_downloader(video_id, dest_dir, *, ytdlp="yt-dlp", ffprobe="ffprobe",
                     runner=_default_runner) -> DownloadedAudio:
    """Downloads a stream's audio once (bestaudio, resumable) and its duration.

    `--continue` against an ALREADY-COMPLETE file asks yt-dlp for a byte range
    past the end, which YouTube answers with HTTP 416 - so every run after the
    first successful download would otherwise fail outright, permanently
    blocking the resumable chunk cache this exists to serve. To avoid that: ask
    yt-dlp for the stream's TRUE duration (metadata only, no download), and if
    an audio.* file already exists in dest_dir, probe its local duration with
    ffprobe. Only when the two agree within tolerance is the existing file
    reused outright, skipping the download entirely. Anything else - no local
    file, a short/partial one, or a probe that fails - falls through to the
    normal `--continue` download, which still resumes a genuinely partial file
    exactly as before.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={video_id}"

    try:
        meta_out = runner([ytdlp, "--skip-download", "--print-json", url], timeout=120)
        true_duration = float(json.loads(meta_out.splitlines()[-1]).get("duration", 0.0))
    except Exception as error:
        raise RuntimeError(f"could not fetch metadata for {video_id}: {error}") from error

    existing = sorted(p for p in dest_dir.glob("audio.*") if p.suffix != ".json")
    if existing and true_duration > 0:
        try:
            probe_out = runner(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(existing[0])],
                timeout=30)
            local_duration = float(probe_out.strip())
        except Exception:
            # ffprobe missing, the file is corrupt, or its output isn't a
            # parseable float - an unverifiable local file is never trusted;
            # fall through to a normal (resuming) download instead.
            local_duration = None
        if local_duration is not None and _duration_matches(local_duration, true_duration):
            return DownloadedAudio(path=existing[0], duration_seconds=true_duration)

    template = str(dest_dir / "audio.%(ext)s")
    cmd = [ytdlp, "-f", "bestaudio", "--continue", "--no-part",
           "--print-json", "--no-simulate", "-o", template, url]
    out = runner(cmd, timeout=3600)
    info = json.loads(out.splitlines()[-1])
    audio_files = sorted(p for p in dest_dir.glob("audio.*") if p.suffix != ".json")
    if not audio_files:
        raise RuntimeError("yt-dlp produced no audio file")
    return DownloadedAudio(path=audio_files[0],
                           duration_seconds=float(info.get("duration", 0.0)))


def transcribe_stream(video_id, workspace_dir, *, glossary: Glossary = _glossary.EMPTY,
                      downloader=ytdlp_downloader,
                      decoder=subprocess_decoder,
                      chunk_seconds: int = 600,
                      cancel: CancelToken | None = None,
                      progress=None) -> StreamTranscript:
    """Downloads a stream's audio once and transcribes it in resumable chunks.

    A present, matching chunk cache is reused; only missing chunks are decoded.
    A chunk whose decoder raises (an error, or the production decoder's timeout on
    a hang) leaves a gap and is recorded in missing_chunks - the run does not
    abort, and a later run fills it. The audio is kept for D2b.

    ``glossary`` (see yt_shorts.glossary) is applied at BOTH ends: its terms
    bias each chunk's decode, and its replacements correct the ASSEMBLED word
    list just before StreamTranscript and transcript.json are built. The
    per-chunk cache files keep the RAW decode output - this is true only
    BECAUSE `subprocess_decoder` sends the worker terms and never
    replacements (see that function's own docstring); if it ever sent
    replacements too, the cache below would stop being raw and everything
    that follows from that would silently stop holding. Deliberately:

    - a glossary change then takes effect on the next assembly with NO
      re-decode, which for an hours-long stream is the difference between
      seconds and half an hour;
    - a correction may span a chunk boundary, because two adjacent chunks'
      words are adjacent in the assembled list - applying per chunk before
      caching would lose exactly those matches.

    The consequence, and it is real: chunks decoded BEFORE a glossary change
    keep the old decoder bias. The correction half still fixes their text,
    but a term the decoder could have heard correctly with a hotword stays a
    guess. Recovering the bias means deleting streams/<video_id>/chunks/ and
    running again. The chunk cache key stays (video_id, start, length) -
    fingerprinting the glossary into it would invalidate hours of decode on
    every edit in the studio's glossary editor, the wrong trade for a
    best-effort bias.

    ``cancel`` (see yt_shorts.cancel) makes this stoppable. BETWEEN CHUNKS is
    the one place stopping costs nothing: every chunk decoded so far is
    already cached, so a re-run picks up at the first missing one. The stop
    RAISES `Stopped` rather than returning what it has - a short transcript
    that looks complete is how a stream ends up detected against half its own
    audio, and nothing downstream could tell the difference. Nothing is
    written when it raises: no transcript.json, and no chunk that was not
    fully decoded. A HARD stop additionally reaches the decode worker (see
    `subprocess_decoder`), so a chunk in flight ends in seconds rather than
    minutes; that arrives here as a `Stopped` from the decoder, which is
    re-raised rather than recorded as a failed chunk.

    ``progress``, when given, is called `progress(done, total)` once per
    chunk with the number of chunks ACCOUNTED FOR so far and how many there
    are in total - the same signature and the same "done" semantics
    `moment_scan.scan` already reports windows with, so a consumer reads
    both without knowing which produced it. Additive and defaulting to
    None, exactly like `cancel`: every existing caller and every existing
    test keeps working untouched, and without one this function behaves as
    it did before the parameter existed.

    Three things about WHEN it fires, all deliberate:

    - a chunk served from the CACHE counts, and so does one whose decode
      FAILED. Both are chunks nobody will look at again on this run, and a
      reading that stalled over a long cached stretch would read as a hung
      job.
    - nothing is reported before the first chunk is accounted for. The
      chunk count is only known once the audio has been downloaded (itself
      minutes of work this reports nothing for), and "chunk 0 of 50" reads
      as a job working on a chunk numbered zero.
    - a chunk the stop check never reached is never reported, because the
      `Stopped` is raised at the TOP of the iteration, before the work.
      Reporting it would claim a chunk had been looked at that nothing
      looked at.

    It is called plainly, exactly as `moment_scan.scan` calls its own: this
    function makes no attempt to survive a callback that raises. THIS
    function's own production callback is built in exactly one place today
    (`studio.worker.Worker._progress_reporter`, which cannot raise by
    construction - see its own docstring) - there is no CLI caller for
    `transcribe_stream`'s progress the way `bin/yt-shorts detect` builds one
    for `moment_scan.scan`'s. That is a fact about THIS function's own
    callers, not a claim that every producer in this codebase has only one:
    `moment_scan.scan` has two independently-guarded production callbacks
    (the studio's reporter and the CLI's own), a gap this file used to state
    more broadly than it should have - see CLAUDE.md's "A reading must never
    cost the run" for the measured incident that corrected it.
    """
    stream_dir = _stream_dir(Path(workspace_dir), video_id)
    chunks_dir = stream_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    try:
        audio = downloader(video_id, stream_dir)
    except Exception as error:
        raise StreamTranscribeError(
            f"Could not download audio for {video_id}: {error}") from error

    words: list[dict] = []
    missing: list[int] = []
    windows = chunk_windows(audio.duration_seconds, chunk_seconds)
    decoder_extra = cancel_kwargs(cancel)

    def report(done: int) -> None:
        # Three call sites below, one per way a chunk stops being outstanding
        # (cached, failed, decoded) - the same three `moment_scan.scan` has,
        # for the same reason: a single call in a `finally` would also fire
        # for the chunk a stop never reached.
        if progress is not None:
            progress(done, len(windows))

    for index, start, length in windows:
        if cancel is not None and cancel.stop_requested:
            # Between chunks is the one place stopping costs nothing: every
            # chunk decoded so far is already cached, so a re-run picks up at
            # the first missing one. Raise rather than return a short
            # transcript - a partial result that LOOKS complete is how a
            # stream ends up detected against half its own audio.
            raise Stopped(f"stopped after {index} chunk(s)")
        chunk_path = chunks_dir / f"{index:03d}.json"
        cached = _read_cached_chunk(chunk_path, video_id, start, length)
        if cached is not None:
            words.extend(cached)
            report(index + 1)
            continue
        try:
            chunk_words = offset_words(
                decoder(audio.path, start, length, glossary=glossary,
                        **decoder_extra), start)
        except Stopped:
            # A hard stop killed the decode worker. That is the operator's
            # own instruction arriving mid-chunk, not a chunk that failed -
            # swallowing it here would record the chunk in missing_chunks
            # (telling the operator it was looked at and could not be heard)
            # and carry on decoding the rest of the stream.
            raise
        except Exception as error:  # noqa: BLE001 - a bad chunk leaves a gap, never aborts the run
            # Record WHY, not just that. A bare `missing.append(index)` is what
            # made a whole stream decode to zero words with no explanation
            # anywhere - the failure this logging exists to end. shorten_urls
            # elides any signed googlevideo URL a yt-dlp/ffmpeg failure message
            # can carry (sig/lsig live in the query string) before it is logged.
            _logger.warning("chunk %d (%.0fs-%.0fs) failed: %s: %s",
                            index, start, start + length,
                            type(error).__name__, shorten_urls(str(error)))
            missing.append(index)
            report(index + 1)
            continue
        atomicwrite.write_text(chunk_path, json.dumps({
            "stream": video_id, "index": index, "start": start,
            "length": length, "words": chunk_words,
        }, indent=2))
        words.extend(chunk_words)
        report(index + 1)

    # Corrections run on the assembled list, not per chunk - see the
    # docstring above on why the cache stays raw.
    words = _glossary.apply(words, glossary)

    decoded = len(windows) - len(missing)
    if not words:
        _logger.warning(
            "%s: transcript is EMPTY - 0 words from %d chunk(s), %d failed: %s",
            video_id, decoded, len(missing), missing)
    else:
        _logger.info("%s: decoded %d chunk(s), %d words, %d missing %s",
                     video_id, decoded, len(words), len(missing), missing or "")

    transcript = StreamTranscript(
        video_id=video_id, audio_path=audio.path,
        duration_seconds=audio.duration_seconds, words=words, missing_chunks=missing)
    atomicwrite.write_text(stream_dir / "transcript.json", json.dumps({
        "video_id": video_id, "duration_seconds": audio.duration_seconds,
        "chunk_seconds": chunk_seconds, "words": words, "missing_chunks": missing,
    }, indent=2))
    return transcript
