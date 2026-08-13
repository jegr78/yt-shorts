"""Detect moments in a stream and write an ANALYSIS - never a clip.

The change of contract is the point. While detection wrote clip directories
unprompted, its precision had to be high or an operator's clip list filled with
work they then had to clear out; a fixed `top_n` also imposed a count the
material does not share - one stream holds five worthwhile moments, another a
hundred. Detection now only DISPLAYS, so it may be generous: a weak suggestion
costs a glance instead of a cleanup. A clip exists when the operator picks a
window and asks for one, and at no other time (see clip_from_moment.py).

**Two callers, two deliberate policies for `transcriber`.** `detect_moments`
keeps its injected `transcriber` parameter and its default
(`stream_transcribe.transcribe_stream`) - `bin/yt-shorts detect` passes
nothing and behaves exactly as it always has: one command that transcribes
(or reuses the chunk cache) and then scores, because a CLI operator watching
a terminal can see two hours pass and choose to wait through it. The
studio's background detect job is a different situation - nobody is watching
it, an eight-hour stream is over two hours of decode, and detection then
spends money on top - so bundling the two means an operator who only wanted
a transcript pays for a scan, and one who only wanted a scan waits two hours
first. The queue (see `studio.jobs.start_transcribe_job`) exists precisely to
make those two plannable, separate jobs; the studio's detect job passes
`require_cached_transcript`, which reads a transcript already on disk and
REFUSES (`TranscriptNotCached`) rather than silently falling back to a
two-hour decode nobody asked that job to start. This is not a change to the
CLI's contract - only to what the studio does before scoring.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from . import atomicwrite, moment_scan, providers
from .cancel import CancelToken, Stopped, cancel_kwargs
from .glossary import EMPTY as GLOSSARY_EMPTY
from .lexicon import EMPTY as LEXICON_EMPTY
from .moments import Moment, activity_curve, lexicon_moments
from .pathnames import validate_segment
from .stream_transcribe import StreamTranscript, transcribe_stream

_logger = logging.getLogger("ytshorts.detect")

ANALYSIS_FILENAME = "moments.json"
TRANSCRIPT_FILENAME = "transcript.json"


def stream_dir(workspace_dir, video_id: str) -> Path:
    """Where a stream's derived data lives - the one place that name is
    built, and the one place `video_id` is validated for it.

    `workspace_dir` comes FIRST, matching `analysis_path`, `windows_dir` and
    `stream_transcribe._stream_dir`. Raises ValueError for an id that is not
    one safe path segment, which is what lets the helpers below build on it
    without borrowing anyone's guard.

    That order has no compiler behind it, and getting it wrong is now SILENT
    where it used to be loud: a swapped `has_cached_transcript("vid", root)`
    hands a Path in as the id, `validate_segment` refuses a non-string as
    ValueError (it used to leak a TypeError), and `_has` catches ValueError -
    so the call answers False instead of raising. That is the right answer
    for the case the tolerance exists for, because a non-string id really
    does arrive as DATA here (`youtube.py` accepts an entry whose `id` is
    truthy without checking it is a string, so a yt-dlp line reading
    `{"id": 12345}` yields an int). It is the wrong answer for a swapped
    call, and nothing distinguishes the two at this depth. Check the order
    when you add a caller; no test can.
    """
    validate_segment(video_id, what="video id")
    return Path(workspace_dir) / "streams" / video_id


def has_cached_transcript(workspace_dir, video_id: str) -> bool:
    """Whether a `transcribe` job has already produced this stream's
    transcript. `is_file`, not `exists`: a directory of that name is not a
    transcript. An id that is not a safe segment answers False rather than
    raising - this is asked once per video over a whole catalogue, and one
    odd id must not sink a list of 99."""
    return _has(workspace_dir, video_id, TRANSCRIPT_FILENAME)


def has_analysis(workspace_dir, video_id: str) -> bool:
    """Whether a `detect` job has already written this stream's moments.
    Same tolerance as has_cached_transcript, for the same reason."""
    return _has(workspace_dir, video_id, ANALYSIS_FILENAME)


def _has(workspace_dir, video_id: str, filename: str) -> bool:
    try:
        directory = stream_dir(workspace_dir, video_id)
    except ValueError:
        return False  # names no file this workspace could hold
    return (directory / filename).is_file()


WINDOWS_DIRNAME = "windows"


class TranscriptNotCached(RuntimeError):
    """Raised by `require_cached_transcript` (never by `transcribe_stream`
    itself) when a caller has asked to use only a transcript that already
    exists, and none does. See the module docstring - this is the studio's
    background-job policy, not a new failure mode of detection in general.
    """


def require_cached_transcript(video_id, workspace_dir, *, glossary=None,
                              cancel: CancelToken | None = None) -> StreamTranscript:
    """A `transcriber` (see `detect_moments`'s own parameter of that name)
    that reads `streams/<video_id>/transcript.json` and never decodes.

    This is the STUDIO's policy - see the module docstring on why detection
    has two callers with two different transcriber defaults. `glossary` and
    `cancel` are accepted only so this matches the call shape every other
    transcriber in this project is invoked with (`detect_moments` always
    passes `glossary=`, and passes `cancel=` whenever it has a token - see
    `cancel_kwargs`); a transcript already written to disk was already
    decoded and corrected by whichever job wrote it, so there is no chunk to
    stop between and no glossary left to apply here.

    Raises `TranscriptNotCached` - naming both the stream and the missing
    file, never a bare "detection failed" - instead of silently falling
    through to a two-hour decode nobody asked THIS call to start.
    """
    path = stream_dir(workspace_dir, video_id) / TRANSCRIPT_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TranscriptNotCached(
            f"{video_id}: no cached transcript for this stream at {path} - "
            f"run a transcribe job for {video_id} first "
            f"({type(error).__name__}: {error})") from error
    return StreamTranscript(
        video_id=video_id,
        # Not used by detect_moments (it only reads .words/.duration_seconds/
        # .missing_chunks off what a transcriber returns) - filled in only
        # because StreamTranscript requires it, and pointed at the stream's
        # own directory rather than left meaningless.
        audio_path=path.parent,
        duration_seconds=payload.get("duration_seconds", 0.0),
        words=payload.get("words", []),
        missing_chunks=list(payload.get("missing_chunks", []) or []))


def analysis_path(workspace_dir: str | Path, video_id: str) -> Path:
    # Built on `stream_dir`, which validates - NOT borrowing a caller's
    # guard, which is the thing CLAUDE.md's rule forbids. video_id becomes
    # a path segment and arrives from the URL path of
    # POST …/streams/{video_id}/detect, which does not validate it itself;
    # the check still runs inside this call, before any filesystem touch,
    # exactly as it did when this function spelled it out. What changed is
    # that the rule now lives in ONE place instead of three, so the three
    # cannot drift apart. Raises ValueError. (stream_transcribe._stream_dir
    # is a fourth copy and stays one: `detect` imports `stream_transcribe`,
    # so it cannot import back without a cycle.)
    return stream_dir(workspace_dir, video_id) / ANALYSIS_FILENAME


def windows_dir(workspace_dir: str | Path, video_id: str) -> Path:
    """Where a stream's SCORED windows are cached, beside its chunks.

    Validates through `stream_dir` for exactly the reason `analysis_path`
    does, and by the same mechanism: the guard runs inside this call rather
    than being assumed of a caller. Raises ValueError.
    """
    return stream_dir(workspace_dir, video_id) / WINDOWS_DIRNAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WindowCache:
    """`moment_scan.scan`'s `get`/`put` contract, backed by
    `streams/<video_id>/windows/NNN.json`.

    Deliberately symmetric with the chunk cache one stage earlier
    (`streams/<video_id>/chunks/`), which is what already makes transcription
    free to stop and resume; this is the same property for the half that
    costs money. `moment_scan.py` stays filesystem-free, so this lives here -
    it is also the only layer that knows WHAT the model was asked, which is
    what the fingerprint below is for.

    THE FINGERPRINT is the whole safety of the thing. A window file is a
    scored answer to one specific question, and three things change that
    question: the model that answered it, the marker vocabulary named in the
    system prompt, and the transcript the window's text was rendered from. A
    cache keyed on the window INDEX alone would happily serve last week's
    scores for text that has since been re-decoded - quietly, since a moment
    carries absolute times and would still look plausible. So each file
    records the fingerprint it was written under and a mismatch is a miss.
    The cost of that strictness is real (a single re-decoded chunk
    invalidates every window of the stream) and it is the right way round:
    re-scoring is money, serving a score for text that no longer exists is
    a wrong answer nobody can see.

    Every failure here is a MISS, never an exception. A corrupt or
    unwritable cache must cost money, never the run - the same stance the
    chunk cache takes (`_read_cached_chunk` returns None on anything it
    cannot read).
    """

    def __init__(self, directory: Path, fingerprint: str, log):
        self.directory = Path(directory)
        self.fingerprint = fingerprint
        self._log = log

    def _path(self, index: int) -> Path:
        return self.directory / f"{index:03d}.json"

    def get(self, index: int) -> list[Moment] | None:
        try:
            payload = json.loads(self._path(index).read_text(encoding="utf-8"))
            if payload.get("fingerprint") != self.fingerprint:
                return None
            return [Moment(**raw) for raw in payload["moments"]]
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            # Unreadable, corrupt, or written by an older shape of this file:
            # score the window again rather than raising. A cache is an
            # optimisation, and one that can fail a run is not one.
            return None

    def put(self, index: int, moments) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            atomicwrite.write_text(self._path(index), json.dumps({
                "index": index,
                "fingerprint": self.fingerprint,
                "moments": [vars(moment) for moment in moments],
            }, indent=2))
        except OSError as error:
            # Best-effort, like every other write in this project's logging
            # and caching paths: the scan already has the answer in memory,
            # and a full disk must not throw away an hour that was paid for.
            self._log.warning("could not cache window %d: %s: %s",
                              index, type(error).__name__, error)


def window_fingerprint(engine: str, lexicon, words) -> str:
    """What the model was asked, condensed - see `WindowCache`.

    Only markers with a weight above zero are included, because those are
    exactly the ones `moment_scan.build_system_prompt` names; a weight change
    that does not cross zero never reaches the model, so it must not
    invalidate a cache either (CLAUDE.md states the same asymmetry between
    the two engines' sensitivity to a lexicon edit).
    """
    payload = json.dumps({
        "engine": engine,
        "markers": sorted(marker for marker, weight in lexicon.markers.items()
                          if weight > 0),
        "words": [[word["start"], word["end"], word["text"]] for word in words],
    }, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _report_usage(usage, provider, model: str, log) -> None:
    """Logs what the run actually cost, in the API's own token counts.

    Logged even when it is zero calls, and logged as MEASURED, because the
    reason this exists is that an estimate was believed for a day:
    `estimate.py` counts characters and divides by four, which measured
    against a real invoice came out roughly half the truth while its comment
    claimed it ran ~12% HIGH. An operator who reads this line has the number
    the preview could not give them.

    The prices come from the PROVIDER that ran, not from one hard-wired
    table: this used to read `estimate.PRICES`, which held Anthropic's rates,
    so a gemini or openai run would have been priced against the wrong
    vendor - and silently, since every number involved still looked like a
    plausible dollar figure.

    A model the provider's PRICES has no entry for reports its tokens and
    says the cost is unknown - never 0.00, which is what a free run would
    look like. Same stance as `estimate.estimate_run`'s `priced` flag.
    """
    if usage is None or usage.calls == 0:
        return
    prices = provider.PRICES.get(model)
    if prices is None:
        log.info("%s: %d call(s), %d input + %d output token(s) - no price on "
                 "record for this model, so the cost is unknown, not zero",
                 model, usage.calls, usage.input_tokens, usage.output_tokens)
        return
    price_in, price_out = prices
    cost = usage.input_tokens / 1e6 * price_in + usage.output_tokens / 1e6 * price_out
    log.info("%s: %d call(s), %d input + %d output token(s), $%.4f measured",
             model, usage.calls, usage.input_tokens, usage.output_tokens, cost)


def _caller_from_config(workspace_dir: Path, provider, model: str, log,
                        usage=None) -> object | None:
    """A model caller, or None with the reason logged. Never raises.

    `provider` is the module the profile selected (see `detect_moments`), so
    the key file, the SDK and the wire format are all that provider's own.
    Every failure here degrades to the lexicon rather than aborting the run,
    and every log line NAMES the provider - "no API key" is useless
    diagnosis when three of them could be the one meant.
    """
    try:
        key = providers.load_api_key(Path(workspace_dir) / "auth",
                                     provider.KEY_FILENAME)
    except providers.MissingKey as error:
        log.warning("no %s API key (%s) - falling back to the lexicon "
                    "engine, whose results are markedly weaker",
                    provider.PROVIDER_ID, error)
        return None
    try:
        return provider.make_caller(key, model=model, usage=usage)
    except (providers.ModelError, providers.SdkUnavailable) as error:
        # OUR exception types, carrying OUR messages - safe to log in full, and
        # the useful half of the diagnosis lives in that text.
        log.warning("cannot reach the %s model %s (%s) - falling back to the "
                    "lexicon engine, whose results are markedly weaker",
                    provider.PROVIDER_ID, model, error)
        return None
    except Exception as error:      # noqa: BLE001 - an unavailable SDK must degrade, not abort
        # An exception the provider did NOT wrap is one whose text this project
        # has made no promise about, and the request it describes carries the
        # API key. Log the type name and nothing else. Widening this to
        # str(error) for a nicer message is exactly how the key gets into a log.
        # The provider id and the model name beside it are OUR OWN strings -
        # they come from the registry and the profile, never from the SDK.
        log.warning("cannot reach the %s model %s (%s) - falling back to the "
                    "lexicon engine, whose results are markedly weaker",
                    provider.PROVIDER_ID, model, type(error).__name__)
        return None


def detect_moments(video_id, workspace_dir, config, *, stream_title,
                   transcriber=transcribe_stream, caller=None, now=None,
                   logger=None, progress=None,
                   cancel: CancelToken | None = None) -> Path:
    """Transcribes (or reuses the cache), scans, and writes moments.json.

    Returns the path to the analysis. `caller` and `now` are injected so the
    whole path tests with no network, no key and no cost.

    No `event_dir` parameter: detection writes only under
    `workspace_dir/streams/<video_id>/`, never into an event directory (see
    the module docstring - a clip is a separate, explicit step), so it has
    nothing to do with one. An earlier version of this signature carried
    `event_dir` anyway, unused, threaded through the CLI, the studio job and
    a dozen tests for nothing.

    `stream_title` IS used: it is recorded in the written analysis (see
    below) so a future picker can call `clip_from_moment.create_clip(...,
    source_title=...)` without re-fetching the stream list to look the title
    up again.

    WHICH VENDOR answers is `config["detect"]["provider"]`, defaulting to
    `providers.DEFAULT_PROVIDER`, and `config["detect"]["model"]` defaults to
    THAT provider's own `DEFAULT_MODEL`. Both reads use `settings.get(key) or
    default`, not `settings.get(key, default)` - a key present with value
    `None` (`{"detect": {"provider": null}}`, which `profile._validate_detect`
    deliberately blesses as equivalent to an absent key, same as `config.get(
    "detect", {}) or {}` already does for a null SECTION one level up) is
    exactly the case `dict.get`'s default argument does not cover, and
    `None or default` is `default`. `profile._validate_detect` refuses an
    unknown, non-null provider by the time a profile reaches here via
    `profile.load` - but that is not a guarantee this function can lean on
    unconditionally: a profile can be built in-process with no validation at
    all (a hand-written config, or a caller that skips `profile.load`
    entirely), so an unknown provider arriving anyway still raises
    `providers.UnknownProvider` here rather than being answered with the
    default.

    ONE ENGINE PER RUN. A window that fails mid-run is recorded in
    `missing_windows`; it does NOT fall back to the lexicon for that window,
    because two scoring scales inside one list would make the ranking
    meaningless in a way the operator could not see.

    ``cancel`` (see yt_shorts.cancel) makes the whole run stoppable, and
    stopping it COSTS NOTHING: each window is written to `WindowCache` as it
    is scored, so a re-run pays only for the hours nobody got to. The stop
    is honoured at two levels - it is handed to the transcriber (which halts
    between chunks) and turned into `moment_scan.scan`'s `should_stop`
    predicate (which halts between windows).

    A stopped scan RAISES `Stopped` and writes NO analysis. The alternative
    was tried on paper and refused: a moments.json covering three of nine
    windows reads as a complete detection - `engine` names a model,
    `missing_windows` is empty (those hours were never attempted, see
    `ScanResult`), and six hours of stream silently hold no moments. That is
    the same silent-degradation shape this project has already paid for
    twice; the cache is what makes raising cheap enough to be the right
    answer.
    """
    log = logger if logger is not None else _logger
    clock = now if now is not None else _now
    settings = config.get("detect", {}) or {}
    # `providers.get` RAISES UnknownProvider for a typo rather than answering
    # it with the default - and this is the one place in detection that can
    # raise at all. profile.load already refuses an unknown provider as a
    # collected defect, so an operator never reaches here with one; a
    # hand-built config that does must be told, not quietly billed to another
    # vendor. The default lives in the registry, not here.
    provider = providers.get(settings.get("provider") or providers.DEFAULT_PROVIDER)
    # The MODEL default is the selected provider's own. Reading it from the
    # default provider instead would pair, say, gemini's key with a claude
    # model name and fail at call time for a reason no log line explains.
    model = settings.get("model") or provider.DEFAULT_MODEL
    lexicon = config.get("lexicon", LEXICON_EMPTY)

    # The cancel kwarg is sent ONLY when there is a token, so a transcriber
    # written before this parameter existed (every fake in the suite) is
    # still called with exactly the keywords it accepts - the same rule
    # `subprocess_decoder` applies to its own runner one layer down.
    transcriber_extra = cancel_kwargs(cancel)
    transcript = transcriber(video_id, Path(workspace_dir),
                             glossary=config.get("glossary", GLOSSARY_EMPTY),
                             **transcriber_extra)
    words = transcript.words
    missing_chunks = list(getattr(transcript, "missing_chunks", []) or [])
    if missing_chunks:
        log.warning("%s: %d chunk(s) missing from the transcript: %s - moments in "
                    "those windows cannot be found", video_id, len(missing_chunks),
                    missing_chunks)

    if not words:
        log.warning("%s: the transcript has 0 words (%d chunk(s) failed: %s). "
                    "Per-chunk decode causes are logged by stream_transcribe.",
                    video_id, len(missing_chunks), missing_chunks)

    usage = providers.Usage()
    if caller is None and words:
        caller = _caller_from_config(Path(workspace_dir), provider, model, log, usage)

    if caller is not None and words:
        engine = f"model:{model}"
        cache = WindowCache(windows_dir(workspace_dir, video_id),
                            window_fingerprint(engine, lexicon, words), log)
        result = moment_scan.scan(
            words, lexicon, caller=caller, logger=log, progress=progress,
            window_cache=cache,
            should_stop=(None if cancel is None
                         else lambda: cancel.stop_requested))
        if result.stopped:
            # Nothing is written: see the docstring above on why a partial
            # analysis is worse than none. Every window that WAS scored is
            # already in the cache, so the re-run is free.
            raise Stopped(
                f"{video_id}: stopped with {len(result.unscanned_windows)} "
                f"window(s) not scanned; the scored ones are cached")
        found, missing_windows = result.moments, result.missing_windows
    elif words:
        engine = "lexicon"
        log.warning("%s: detected with the lexicon engine - reduced quality",
                    video_id)
        found, missing_windows = lexicon_moments(words, lexicon), []
    else:
        # `caller is None and words` guards _caller_from_config above, so an
        # empty transcript never reaches either engine - lexicon_moments([],
        # ...) would trivially return [] too, but recording "engine":
        # "lexicon" here would be untrue: no engine ran at all. The 0-words
        # warning already fired above, so this is not a silent gap - just a
        # field that used to claim something that did not happen.
        engine = "none"
        found, missing_windows = [], []

    payload = {
        "video_id": video_id,
        "stream_title": stream_title,
        "engine": engine,
        # ADDITIVE, beside `engine` and never folded into it: `engine` keeps
        # its exact `model:<model>` / `lexicon` / `none` format, which the
        # frontend and several tests already read. `configured_provider`
        # names the vendor the run was CONFIGURED for - which is still the
        # useful half of the diagnosis when the run fell back to the lexicon,
        # because it says whose key was missing, and is written even when
        # `engine` is "none" (zero words - _caller_from_config never ran and
        # no key was ever consulted). Named `configured_provider`, not
        # `provider`, precisely so a payload never reads
        # `{"engine": "none", "provider": "anthropic"}` - which names a
        # vendor that was never even attempted. `engine` remains the
        # authority on what actually produced the moments.
        "configured_provider": provider.PROVIDER_ID,
        "created_at": clock(),
        "duration_seconds": getattr(transcript, "duration_seconds", 0.0),
        "activity": activity_curve(words, lexicon),
        "moments": [vars(moment) for moment in found],
        "missing_windows": missing_windows,
        "missing_chunks": missing_chunks,
    }
    path = analysis_path(workspace_dir, video_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomicwrite.write_text(path, json.dumps(payload, indent=2))
    log.info("%s: %d words, %d moment(s), engine=%s, %d window(s) failed",
             video_id, len(words), len(found), engine, len(missing_windows))
    _report_usage(usage, provider, model, log)
    return path
