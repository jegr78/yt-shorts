"""The one place this project talks to the Anthropic API.

`anthropic` is imported LAZILY inside `_sdk()`, exactly as `google_oauth.py`
treats the Google libraries - so `import yt_shorts.providers` costs nothing and
works in a venv that never installed it.

The API key is read from `<workspace>/auth/anthropic.json`, alongside
`client_secret.json` and `token-<id>.json`, under the same gitignored,
never-logged rules. TWO shapes are accepted: a bare `sk-ant-...` string or a
JSON object with an `api_key` field. The file as an operator first creates it
holds the raw key despite its extension, and an "Expecting value: line 1
column 1" traceback explains nothing to someone who did what they were asked.
Reading it is `providers.load_api_key(auth_dir, KEY_FILENAME)`, shared with
every other provider rather than reimplemented here.

The eight contract names below (see `providers.CONTRACT`) are what makes this
module swappable; `tests/test_provider_contract.py` holds every provider to
them, this one included.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from ._shared import ModelError, Usage, require

PROVIDER_ID = "anthropic"
KEY_FILENAME = "anthropic.json"
PACKAGE = "anthropic"
INSTALL = ".venv/bin/pip install anthropic"
# Measured against the real API (see the bake-off beside DEFAULT_MODEL).
VERIFIED = True

# model -> (USD per 1M input tokens, USD per 1M output tokens).
# A snapshot of the published rates on 2026-07-29; re-check at
# anthropic.com/pricing rather than trusting these numbers.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
}

# Measured, not assumed - and the measurement overturned this plan's own
# starting assumption that Haiku would do. A bake-off over this workspace's
# 98-minute qualifying (2026-07-29, all three models, same corrected prompt)
# against the operator's four known-good moments:
#
#   claude-haiku-4-5   2 moments, 0 of the 4   ~$0.012  ESTIMATE
#   claude-sonnet-5    7 moments, 1 of the 4   ~$0.037  ESTIMATE
#   claude-opus-5      7 moments, 3 of the 4   ~$0.062  ESTIMATE
#
# THOSE THREE COSTS ARE ESTIMATES, NOT MEASUREMENTS, and the estimate runs
# low. Every `~$0.0xx` above came from the character-counting calculation
# `estimate.py` uses, which that module's own comment now records as
# CIRCULAR - it was calibrated against a bake-off script that measured itself
# the same way, so the two agreeing proved only that two character counts
# agree. The moment counts and the known-good hits ARE observations; the
# money column is not.
#
# The one number since taken from the API's OWN reported token counts, via
# `providers.Usage` (2026-07-31, claude-opus-5, the same stream and the same
# two windows): 2 calls, 15369 input + 2374 output tokens, $0.1362. That
# checks against the rates in PRICES above - 15369/1e6*5.00 +
# 2374/1e6*25.00 = 0.13618 - and it is MORE THAN DOUBLE the ~$0.062 this
# comment used to carry. It also changes what the cross-provider comparison
# says rather than only a decimal: Opus is not "about the same as Gemini
# Flash", it is over twice the cost and over forty times gpt-5.6-luna's. See
# README.md's "How far each provider has actually been measured" for that
# table; it is held there rather than restated per module.
#
# Opus alone found the purple flying lap and "what a lap", and gave the most
# specific reasons. The price gap is a few cents per stream - which does not
# buy back a detector that misses the moments the channel exists to publish.
# (The "roughly 31 cents for an eight-hour endurance race against 6 for
# Haiku" this comment used to extrapolate was built on the estimates above;
# scaled from the measured figure the Opus end is nearer 70 cents, and the
# Haiku end has never been measured at all, so the ratio is the part to keep
# and the absolute numbers are not.) A channel may still override this with
# config["detect"]["model"]; none does.
#
# THE SAME MODEL ON THE SAME INPUT DOES NOT RETURN THE SAME COUNT. This
# comment records 7 moments for claude-opus-5 while the analysis stored in
# this workspace for the same stream on the same day has 11, which looked
# for a while like a bookkeeping error. It is not, and the question is
# settled: the workspace's central log for 2026-07-29 carries
# `scanned 2 window(s): 11 moment(s), 0 failed` at 17:41:20, and
# `moments.json`'s own `created_at` is 15:41:20+00:00 - the same second - so
# the 11 is a logged `bin/yt-shorts detect` run. The 7 came from a throwaway
# bake-off script that wrote nothing to the log and no longer exists in the
# repo or in git history. A THIRD run on 2026-07-31 over the identical
# stream returned 10 moments, 0 failed windows. So: 7, 10 and 11, same model,
# same input.
#
# What varies is the BORDERLINE TAIL, not the result. The two logged runs
# agree on 9 of 10: the 2026-07-29 run had two moments the re-run did not
# (scored 5.0 and 4.5), the re-run found one the earlier run did not (scored
# 7.0). The strong moments are stable. Read any single count in this file -
# or in any other provider's bake-off comment, which are all one run each -
# as one sample of a number that moves, and read cross-provider "agreement"
# figures as approximate for the same reason: the reference itself moves.
#
# Re-measure before changing it. Haiku's showing here is not a fixed property
# of the model: on the FIRST run it scored 0 moments outright, because the
# prompt stated the length limit only in seconds while the answer is in line
# numbers (see moment_scan's module docstring). Correcting the prompt took it
# from 0 to 2. Another prompt defect could be hiding a similar gain.
DEFAULT_MODEL = "claude-opus-5"


def _sdk():
    # Four plain strings rather than this module itself: `require` lives in
    # `_shared`, which deliberately knows nothing about what a provider module
    # looks like, so there is nothing to hand it but this provider's own facts.
    require(PACKAGE, INSTALL, "moment detection", PROVIDER_ID)
    import anthropic
    return anthropic


def make_caller(api_key: str, *, model: str = DEFAULT_MODEL,
                max_tokens: int = 4096, sdk=None,
                usage: Usage | None = None) -> Callable[[str, str, dict], dict]:
    """Returns `call(system, user, schema) -> dict`.

    `sdk` is injected so the whole path tests without the package, a key, a
    network or a cent. Production passes nothing and gets the real module.

    `usage`, when given, accumulates the API's OWN reported token counts
    across every call this caller makes - see `providers.Usage` for why that
    is worth having and why it is an out-parameter rather than a return value.

    Deliberately absent from the payload: `effort` (rejected outright by
    claude-haiku-4-5) and `thinking` (omitted means off on that model). Adding
    either would turn every window into a 400 on Haiku. The default model has
    since changed (see DEFAULT_MODEL above); neither claim was measured on
    claude-opus-5, so name Haiku explicitly here rather than "the default
    model" - do not assume either statement carries over.
    """
    module = sdk if sdk is not None else _sdk()
    try:
        client = module.Anthropic(api_key=api_key)
    except Exception as error:
        # The SDK's constructor can raise on a malformed key, bad base URL, or
        # config error. Its message may embed the API key. Never let str(error)
        # reach a caller; the type name is the only part of it that is safe to
        # keep, and it is chained with `from` so the real cause still shows up
        # in a traceback.
        #
        # HAZARD (applies to every `from error`/`from None` wrap in this
        # function, not just this one): chaining keeps the SDK's original
        # message alive as `__cause__`, on purpose - a traceback still needs
        # to show the real cause during debugging. Nothing in src/ calls
        # `logging.exception`/`logger.exception` or passes `exc_info=True`
        # today, so `error`'s own text never reaches a log THAT WAY. But the
        # day a handler here (or a caller of `call`/`make_caller`) adds
        # `exc_info=True` to a `log.warning(...)` or switches to
        # `logger.exception(...)`, the logging module walks `__cause__` and
        # prints the ORIGINAL exception's text into that log line -
        # including, potentially, the API key this whole function exists to
        # keep out of one. Sanitising the message we raise is not enough by
        # itself if a future log call unwinds the chain we deliberately kept.
        raise ModelError(
            f"the Anthropic SDK raised {type(error).__name__} building the client"
        ) from error

    def call(system: str, user: str, schema: dict) -> dict:
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except Exception as error:
            # Anything the SDK raises here - auth, rate limit, connection - may
            # embed the request in its own message, and that request carries the
            # API key. Never let str(error) reach a caller; the type name is the
            # only part of it that is safe to keep, and it is chained with
            # `from` so the real cause still shows up in a traceback.
            raise ModelError(
                f"the Anthropic SDK raised {type(error).__name__} calling the model"
            ) from error
        # BEFORE any of the reads below, all of which can raise: the tokens
        # were spent the moment this response came back, whether or not it
        # turns out to be a refusal, truncated, or unparseable. Recording
        # them only on the success path would under-report a run's real cost
        # by exactly the windows that went wrong - the ones an operator most
        # wants explained when the bill does not match the estimate.
        if usage is not None:
            # A callable, so the reads below are inside Usage.record's own
            # tolerance rather than outside every handler (see Usage.record).
            usage.record(lambda: (response.usage.input_tokens,
                                  response.usage.output_tokens))
        # Everything from here on reads the response object the SDK handed
        # back, and that object was built from the request we just sent - so
        # anything it raises on (a missing attribute, a malformed `.content`,
        # a future SDK shape this code does not expect) can carry the same
        # risk `messages.create` itself does: the key, by way of the request,
        # showing up in a third-party exception's own text. This block used
        # to sit outside any handler; a fake SDK whose `response.content`
        # raised `AttributeError("... sk-ant-XXXX ...")` reached the caller
        # as that bare AttributeError, text and all.
        try:
            # stop_reason is checked BEFORE content is read: on a refusal the
            # content list is empty or partial, and indexing it would raise
            # something that says nothing about the real cause.
            stop = getattr(response, "stop_reason", None)
            if stop == "refusal":
                raise ModelError("the model refused this window (stop_reason=refusal)")
            if stop == "max_tokens":
                raise ModelError("the answer was cut off (stop_reason=max_tokens)")
            text = "".join(
                b.text for b in response.content if getattr(b, "type", "") == "text"
            )
            try:
                return json.loads(text)
            except json.JSONDecodeError as error:
                raise ModelError(f"the answer was not valid JSON: {error}") from None
        except ModelError:
            # Already sanitised by this function itself - the refusal/
            # max_tokens raises above, or the JSONDecodeError branch, whose
            # message quotes the MODEL'S OWN ANSWER (never the request, so it
            # cannot carry the key). Pass it through unchanged rather than
            # re-wrapping it into the vaguer type-name-only message below.
            raise
        except Exception as error:
            # Anything else here - a malformed response object, a future SDK
            # shape this code does not expect - is not something this
            # function has already vetted as safe. Never let str(error)
            # reach a caller; the type name is the only part of it that is
            # safe to keep, and it is chained with `from` so the real cause
            # still shows up in a traceback.
            raise ModelError(
                f"the Anthropic SDK raised {type(error).__name__} reading the response"
            ) from error

    return call
