"""The one place this project talks to the Google Gemini API.

`google.genai` is imported LAZILY inside `_sdk()`, exactly as `anthropic` is in
`anthropic_api` and the Google upload libraries are in `google_oauth.py` - so
`import yt_shorts.providers` costs nothing and works in a venv that never
installed it. That matters more here than for the other two providers: the
package shares the `google` NAMESPACE with this project's YouTube upload stack
(`google-api-python-client`, `google-auth`, `google-auth-oauthlib`), and a
module-scope import would pull a second, much larger tree into every CLI
invocation for no reason.

The API key is read from `<workspace>/auth/gemini.json`, alongside
`client_secret.json` and `token-<id>.json`, under the same gitignored,
never-logged rules; TWO shapes are accepted (a bare key string or a JSON object
with an `api_key` field), because `providers.load_api_key` is shared with every
other provider rather than reimplemented here.

The eight contract names below (see `providers.CONTRACT`) are what makes this
module swappable; `tests/test_provider_contract.py` holds every provider to
them, this one included.

WHAT THE INSTALLED SDK ACTUALLY DOES (google-genai 2.16.0, established
2026-07-31)
---------------------------------------------------------------------------
The call shape - `client.interactions.create(model=, system_instruction=,
input=, response_format={"type": "text", "mime_type": "application/json",
"schema": <raw JSON Schema dict>})`, answer at `interaction.output_text` - is
Google's documented Interactions API and was CONFIRMED against the installed
package rather than taken on trust: `_gaos.google_genai._CREATE_BODY_KEYS`
lists every accepted keyword, and a probe that monkeypatched `httpx.Client.send`
(so no socket was ever opened and no key was ever needed) showed the SDK
serialising exactly that body to `POST /v1beta/interactions` and handing back a
`types.interactions.Interaction`.

Three things the documentation pages do not state, each pinned the same way -
by reading `Interaction.model_fields` / `GenerationConfig.model_fields` in the
installed package and then exercising them through that offline probe:

1. **Token counts** live at `interaction.usage.total_input_tokens` and
   `.total_output_tokens` (both `int | None`), NOT at Anthropic's
   `usage.input_tokens`/`.output_tokens`. `Usage` on this API is a wide record -
   `total_tokens`, `total_cached_tokens`, `total_thought_tokens`,
   `total_tool_use_tokens`, plus per-modality breakdowns - and the two totals
   above are the ones that correspond to what `providers.Usage` accumulates and
   what `PRICES` below is denominated in.
   **Thought tokens are a SEPARATE line item, not already folded into
   `total_output_tokens`, and this module adds them in.** This was gotten wrong
   once: an earlier draft of this docstring claimed the opposite, reasoning
   from the pricing page's per-model note that the output rate "includes
   thinking tokens". That note is about the RATE thinking is billed at (the
   same price as ordinary output), not about the COUNT reported for it -
   Google's own thinking documentation says response pricing is the SUM of
   output tokens and thinking tokens, two distinct figures, and the legacy
   `GenerateContentResponseUsageMetadata.total_token_count` in this same SDK
   documents itself as `prompt + candidates + tool_use + thoughts`, i.e. the
   candidates (output) figure excludes thoughts by construction. There is no
   way to turn thinking off on this call shape either -
   `GenerationConfig.thinking_level` is `'minimal'|'low'|'medium'|'high'|None`
   with no equivalent of the legacy `thinking_budget=0` - so every call to
   every model in `PRICES` bills thinking tokens this module must count itself
   or silently under-report cost. `total_output_tokens + total_thought_tokens`
   is what `Usage.record`'s extractor computes below, so the two are summed
   into the one output figure `providers.Usage` and `PRICES` both expect.
   **Confirmed against the live API on 2026-07-31, twice.** Directly, by a
   16-token ping: `max_output_tokens: 16` came back `status=incomplete` with
   `total_output_tokens=0` and `total_thought_tokens=12`. If thought tokens
   were already folded into the output count, that count could not be 0 while
   12 were spent. And end to end, by the bake-off run beside `DEFAULT_MODEL`:
   `estimate.estimate_run` predicted 1000 output tokens and $0.0212, the run
   reported 5262 and $0.0590 - 2.8x on cost - and the gap IS the thinking.
   That is also a second, independent confirmation of `estimate.py`'s own
   note that it runs low, on a different vendor and with the cause named
   rather than inferred.
   Every field is NULLABLE, and that is handled rather than guarded: the sum is
   computed with `or 0` on each half BEFORE it reaches `Usage.record`, because
   `record`'s own tolerance catches whatever its callable raises but cannot
   repair a `None + int` that raises TypeError and loses both counts, not just
   the missing one. A response that reports no usage at all still costs the
   bookkeeping and not the window.

2. **Bounding the answer's length** is `generation_config={"max_output_tokens":
   n}` (`interactions.GenerationConfig.max_output_tokens`), which is why
   `max_tokens` is threaded into the payload there rather than as a top-level
   keyword the way Anthropic takes it. There IS an equivalent, so the model's
   own default never applies here.

3. **Refusal and truncation** are reported by `interaction.status`, NOT by a
   `finish_reason` - that name exists in this SDK only on the older
   `client.models.generate_content` surface, which this module does not use.
   The literal is `InteractionStatus`: `in_progress`, `requires_action`,
   `completed`, `failed`, `cancelled`, `incomplete`, `budget_exceeded`,
   `queued`. For the call this module makes - non-streaming, non-background, no
   tools - `completed` is the ONLY success state: `queued`/`in_progress` need
   `background=True`, `requires_action` needs tools, and `incomplete` is this
   API's spelling of Anthropic's `max_tokens` truncation. So anything other
   than `completed` is a ModelError naming the status, checked BEFORE
   `output_text` is read, for exactly the reason Anthropic's `stop_reason`
   check comes first: on a non-completed interaction `output_text` is `None`,
   and `json.loads(None)` raises a TypeError that says nothing whatever about
   the cause.
   Which status a SAFETY refusal specifically produces could not be pinned
   without a paid call, so it is not claimed here; the check catches it either
   way, and the message quotes the status the API actually returned rather than
   a guess at what it means.

All of the above was read from the installed package offline. It has since
been exercised against the real service - one paid detection run and two
16-token pings on 2026-07-31, see `VERIFIED` and the bake-off beside
`DEFAULT_MODEL` - which confirmed Facts 1 and 2 and the `incomplete` spelling
of truncation in Fact 3. Still NOT established, and still not invented: which
status a SAFETY refusal produces. Provoking one was not attempted, so Fact 3
continues to claim nothing about it.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from ._shared import ModelError, Usage, require

PROVIDER_ID = "gemini"
KEY_FILENAME = "gemini.json"
PACKAGE = "google.genai"
INSTALL = ".venv/bin/pip install google-genai"
# Measured against the real API on 2026-07-31: one paid detection run over
# this workspace's own 98-minute qualifying (the bake-off beside DEFAULT_MODEL)
# plus two 16-token pings that established which models the Interactions API
# actually serves (see PRICES). That run is also what confirmed the docstring's
# Fact 1 - thought tokens counted separately - against the API's own numbers
# rather than against a reading of the SDK's field list.
VERIFIED = True

# model -> (USD per 1M input tokens, USD per 1M output tokens).
# A snapshot of the published rates at ai.google.dev/gemini-api/docs/pricing on
# 2026-07-31; re-check there rather than trusting these numbers. Every id was
# additionally checked against this SDK's own `Interaction.model` literal - a
# necessary check that turned out NOT to be sufficient, which is why two rows
# that were here are gone.
#
# `gemini-2.5-flash` and `gemini-2.5-pro` were priced here and were REMOVED on
# 2026-07-31, not merely annotated: each answers NotFoundError on the
# Interactions API. They exist only on the legacy
# `client.models.generate_content` surface, which this module deliberately does
# not use - the same split Fact 3 above records for `finish_reason`, and the
# reason the SDK's `Interaction.model` literal accepting an id says nothing
# about the API serving it. Measured by a 16-token ping - `make_caller(key,
# model=..., max_tokens=16)` called once with a one-line prompt, a few
# hundredths of a cent - which is also how to RE-CHECK them, and how to check
# any id before adding it: a model absent today may be servable later, and one
# servable today may go. Both rows below answered that same ping on that date.
#
# Leaving them priced was operator-visible, not cosmetic, which is why removing
# them mattered: the studio's provider picker quotes this table's CHEAPEST
# priced model as the comparison (`priceSentence` in the web client's
# `providers.ts`), so `gemini-2.5-flash` at 0.30/2.50 was held up as the bargain
# while being one of the two ids every single window raises ModelError on.
#
# What that actually costs an operator is NOT what this comment used to say
# ("silently degrade the run to the lexicon engine") - it is neither silent nor
# a lexicon run, and the wrong version understated it. No model name is read
# until the first request, so `detect._caller_from_config` builds a caller
# fine, the run commits to `engine = "model:gemini-2.5-flash"`, every window
# fails into `missing_windows` with its cause logged, and the analysis comes
# out EMPTY. Reproduced on the detect path against a fake SDK that raises on
# the request - see `profile._validate_detect`'s docstring for the same
# statement at the config end, and CLAUDE.md's "One engine per run" for why
# NOT falling back per-window is deliberate.
#
# Other Gemini models exist that this table does not price at all, and that
# path is already correct rather than guessed: an unpriced model still
# reports its token counts and says the cost is UNKNOWN, never zero - see
# `detect._report_usage` and `estimate.estimate_run`'s `priced` flag. An
# unpriced model is the SAFE gap; a priced one the API will not serve is not,
# and that asymmetry is the whole of why the two rows went rather than staying
# with a warning beside them.
#
# These are the PAID (standard-tier) rates, including for Flash models that
# have a free tier: 0.0 would be wrong for anyone paying, and 0.0 is also what
# an unpriced model looks like. Read a number from here as a FLOOR wherever
# tiering applies, not an upper bound - this flat two-number table cannot
# express batch, cached, long-context or service-tier pricing. The two concrete
# examples this caveat used to name (gemini-2.5-flash's higher audio-input rate,
# gemini-2.5-pro's roughly doubled rate above a 200k-token prompt) went with the
# removed rows; whether EITHER surviving row is tiered has not been checked, so
# read the floor as a caution rather than as an established fact about these two.
PRICES: dict[str, tuple[float, float]] = {
    "gemini-3.6-flash": (1.50, 7.50),
    "gemini-3.5-flash": (1.50, 9.00),
}

# Measured, not assumed - but on ONE stream, and against Anthropic's own run
# over that same stream rather than against a fixed answer key. A bake-off on
# 2026-07-31 over this workspace's 98-minute qualifying (video V9nVNEQNdR4,
# "ERF 24H Nürburgring | Qualifying 1 | Superpole", 5574 transcript words, 2
# scan windows - the stream anthropic_api's own bake-off used):
#
#   gemini-3.6-flash   6 moments, 0 failed windows, 2 API calls,
#                      13052 input + 5262 output tokens, $0.0590 measured
#
#   reference: claude-opus-5 over the same stream (2026-07-29, this
#   workspace's production analysis) found 11 moments, 0 failed windows.
#
# THE REFERENCE MOVES, so read the agreement figures below as approximate.
# claude-opus-5 has since been run a third time over this identical stream
# and returned 10 rather than 11 - see anthropic_api's own comment for the
# three counts (7, 10, 11) and what settled them. The strong moments are
# stable across all of them, including both of the two named below; it is the
# weakly-scored tail that comes and goes.
#
# Agreement, not merely a count: every one of the 6 Gemini moments overlaps an
# Anthropic moment by more than 50% of the shorter window, 3 of the 6 by 100%,
# and the top three scores are identical on identical windows (8.0 / 7.5 /
# 6.0). It found a strict SUBSET - lower recall, no disagreement - and that
# subset includes BOTH moments the Anthropic comment above credits to Opus
# alone (the Speed Hunter lap at 3053.8s and the pole lap at ~5625-5716s).
#
# It is not as cheap as a Flash model sounds: on this material it spends most
# of its answer thinking (5262 output tokens against the 1000
# `estimate.estimate_run` predicts). This comment used to call that a tie -
# "$0.0590 against Opus's ~$0.062 ... a difference of a fifth of a cent" - and
# that was wrong, because ~$0.062 was never a measurement. Opus's MEASURED
# cost on this same stream is $0.1362 (see anthropic_api's comment), so
# Gemini is well under half of it here. What separates the two is recall as
# well as price - which is why Anthropic stays the project default and this is
# only what a channel gets when it asks for Gemini.
#
# One stream, one 98-minute qualifying session, two windows, one run per
# model. It says nothing about an eight-hour endurance race, where the windows
# are the same length but there are eight times as many of them and the
# commentary is a different register; do not read the recall gap or the cost
# as established there. Re-measure.
#
# gemini-3.5-flash was NOT measured. It is servable (its 16-token ping
# answered on 2026-07-31, unlike the two ids named in PRICES above) and its
# published output rate is HIGHER - 9.00 against 7.50 - so on a model family
# that spends most of its tokens on the output side it is the more expensive
# choice by exactly the axis this run turned out to be dominated by. Whether
# it buys any of the missing recall back is unknown. Measure it before
# switching to it rather than reasoning from the version number - that is the
# reasoning the Anthropic bake-off overturned for its own vendor.
DEFAULT_MODEL = "gemini-3.6-flash"


def _sdk():
    # Four plain strings rather than this module itself: `require` lives in
    # `_shared`, which deliberately knows nothing about what a provider module
    # looks like, so there is nothing to hand it but this provider's own facts.
    require(PACKAGE, INSTALL, "moment detection", PROVIDER_ID)
    from google import genai
    return genai


def make_caller(api_key: str, *, model: str = DEFAULT_MODEL,
                max_tokens: int = 4096, sdk=None,
                usage: Usage | None = None) -> Callable[[str, str, dict], dict]:
    """Returns `call(system, user, schema) -> dict`.

    `sdk` is injected so the whole path tests without the package, a key, a
    network or a cent. Production passes nothing and gets the real module.

    `usage`, when given, accumulates the API's OWN reported token counts across
    every call this caller makes - see `providers.Usage` for why that is worth
    having and why it is an out-parameter rather than a return value.

    Deliberately absent from the payload: `thinking_level` (a
    `generation_config` key on this API) and `service_tier`. Both are real
    knobs, neither has been measured on this project's transcripts, and the
    Anthropic provider's own note is the precedent - name what was measured,
    not what looked plausible.
    """
    module = sdk if sdk is not None else _sdk()
    try:
        client = module.Client(api_key=api_key)
    except Exception as error:
        # The SDK's constructor takes the API KEY as an argument, so its own
        # exception message can embed it - on a malformed key, a bad base URL
        # or a config error. Never let str(error) reach a caller; the type name
        # is the only part of it that is safe to keep, and it is chained with
        # `from` so the real cause still shows up in a traceback.
        #
        # HAZARD (applies to every `from error`/`from None` wrap in this
        # function, not just this one): chaining keeps the SDK's original
        # message alive as `__cause__`, on purpose - a traceback still needs to
        # show the real cause during debugging. Nothing in src/ calls
        # `logging.exception`/`logger.exception` or passes `exc_info=True`
        # today, so `error`'s own text never reaches a log THAT WAY. But the
        # day a handler here (or a caller of `call`/`make_caller`) adds
        # `exc_info=True` to a `log.warning(...)` or switches to
        # `logger.exception(...)`, the logging module walks `__cause__` and
        # prints the ORIGINAL exception's text into that log line - including,
        # potentially, the API key this whole function exists to keep out of
        # one. See anthropic_api's identical note; it is repeated rather than
        # cross-referenced because it is the reason this handler exists at all.
        raise ModelError(
            f"the Gemini SDK raised {type(error).__name__} building the client"
        ) from error

    def call(system: str, user: str, schema: dict) -> dict:
        try:
            interaction = client.interactions.create(
                model=model,
                system_instruction=system,
                input=user,
                response_format={"type": "text",
                                 "mime_type": "application/json",
                                 "schema": schema},
                generation_config={"max_output_tokens": max_tokens},
            )
        except Exception as error:
            # Anything the SDK raises here - auth, rate limit, connection - may
            # embed the request in its own message, and that request carries the
            # API key. Never let str(error) reach a caller; the type name is the
            # only part of it that is safe to keep, and it is chained with
            # `from` so the real cause still shows up in a traceback.
            raise ModelError(
                f"the Gemini SDK raised {type(error).__name__} calling the model"
            ) from error
        # BEFORE any of the reads below, all of which can raise: the tokens
        # were spent the moment this interaction came back, whether or not it
        # turns out to be a refusal, truncated, or unparseable. Recording them
        # only on the success path would under-report a run's real cost by
        # exactly the windows that went wrong - the ones an operator most wants
        # explained when the bill does not match the estimate.
        if usage is not None:
            # A callable, so the reads below are inside Usage.record's own
            # tolerance rather than outside every handler (see Usage.record) -
            # which matters here because every one of these fields is
            # `int | None` and lives two attributes deep.
            #
            # Thought tokens are billed and reported SEPARATELY from output
            # tokens on this API (see the module docstring, Fact 1) and are
            # summed in here rather than left for a caller to notice missing.
            # The `or 0` on EACH half happens INSIDE this lambda, not left to
            # `Usage.record`'s own None-tolerance: `record` catches whatever
            # `extract()` raises, but a bare `None + int` raises TypeError
            # from inside this callable, which `record` then swallows and
            # counts as "usage unreadable" for BOTH tokens - silently zeroing
            # the input count too, not just the missing thought count.
            usage.record(lambda: (
                interaction.usage.total_input_tokens,
                (interaction.usage.total_output_tokens or 0)
                + (interaction.usage.total_thought_tokens or 0)))
        # Everything from here on reads the Interaction the SDK handed back,
        # and that object was built from the request we just sent - so anything
        # it raises on (a missing attribute, a future SDK shape this code does
        # not expect) can carry the same risk `interactions.create` itself
        # does: the key, by way of the request, showing up in a third-party
        # exception's own text. On the first provider this block sat outside
        # any handler and a fake SDK whose content attribute raised with the
        # key in its text reached the caller intact; it is inside one here from
        # the start.
        try:
            # status is checked BEFORE output_text is read: `completed` is the
            # only success state for this call shape (see the module
            # docstring), and on any other one `output_text` is None, so
            # json.loads would raise a TypeError that says nothing about the
            # real cause.
            status = getattr(interaction, "status", None)
            if status != "completed":
                raise ModelError(
                    f"the model returned no usable answer (status={status})")
            text = interaction.output_text
            if text is None or text == "":
                # A completed interaction whose output carried no text at all -
                # `output_text` is genuinely `str | None` on this API, set only
                # when a text part was produced. Saying so beats letting
                # json.loads(None) raise a TypeError about its argument types.
                raise ModelError("the model returned an empty answer")
            if not isinstance(text, str):
                # Not a shape the SDK's own types admit (`output_text` is
                # `str | None`), but this reads a third-party object handed
                # back over the wire, and a wrong-typed value here is not an
                # EMPTY answer - it earns its own honest message rather than
                # being folded into "empty", which would say nothing true
                # about what was actually wrong.
                raise ModelError(
                    f"the model returned a non-text answer ({type(text).__name__})")
            try:
                return json.loads(text)
            except json.JSONDecodeError as error:
                raise ModelError(f"the answer was not valid JSON: {error}") from None
        except ModelError:
            # Already sanitised by this function itself - the status, empty-
            # answer and JSONDecodeError raises above, whose messages quote the
            # API's own status or the MODEL'S OWN ANSWER (never the request, so
            # they cannot carry the key). Pass through unchanged rather than
            # re-wrapping into the vaguer type-name-only message below.
            raise
        except Exception as error:
            # Anything else here - a malformed Interaction, a future SDK shape
            # this code does not expect - is not something this function has
            # already vetted as safe. Never let str(error) reach a caller; the
            # type name is the only part of it that is safe to keep, and it is
            # chained with `from` so the real cause still shows up in a
            # traceback.
            raise ModelError(
                f"the Gemini SDK raised {type(error).__name__} reading the response"
            ) from error

    return call
