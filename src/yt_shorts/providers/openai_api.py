"""The one place this project talks to the OpenAI API.

`openai` is imported LAZILY inside `_sdk()`, exactly as `anthropic` is in
`anthropic_api` and `google.genai` is in `gemini_api` - so `import
yt_shorts.providers` costs nothing and works in a venv that never installed it.

The API key is read from `<workspace>/auth/openai.json`, alongside
`client_secret.json` and `token-<id>.json`, under the same gitignored,
never-logged rules; TWO shapes are accepted (a bare key string or a JSON object
with an `api_key` field), because `providers.load_api_key` is shared with every
other provider rather than reimplemented here.

The eight contract names below (see `providers.CONTRACT`) are what makes this
module swappable; `tests/test_provider_contract.py` holds every provider to
them, this one included.

WHAT THE REAL SERVICE HAS CONFIRMED (2026-07-31)
----------------------------------------------------------------------------
`VERIFIED` is True. Every fact below was FIRST read off the INSTALLED SDK and
confirmed by driving it offline with the network monkeypatched out; on
2026-07-31 the module additionally ran against the real API - two paid
detection runs over this workspace's own 98-minute qualifying (the bake-off
beside `DEFAULT_MODEL`) plus a 16-token ping per priced model. Three things
that run established, stated as narrowly as it earned:

1. **The live API accepted the adapted strict schema.** `_adapt_schema`'s
   output - `moment_scan.SCHEMA` rewritten into OpenAI's strict dialect - was
   sent on every call of both runs, and both runs finished with **0 failed
   windows** (2 windows each, every call `completed`, every answer parsed).
   Until then that adaptation was pinned only against this project's own
   reading of the structured-outputs guide.
2. **The reported token counts price out at the published rates.** 11047 input
   tokens on both runs, 876 output on gpt-5.6-terra and 1575 on gpt-5.6-luna,
   which against `PRICES` give exactly the $0.0326 and $0.0041 recorded beside
   `DEFAULT_MODEL`. Read that for what it is: internal consistency between the
   API's own counters and this table's rates. It is NOT an invoice, and it
   cannot be - the costs were COMPUTED from those counters, so the arithmetic
   agreeing proves the rates were applied correctly and nothing about what
   OpenAI actually billed.
3. **Every id in `PRICES` is one the API serves.** All five answered a 16-token
   ping with `status=completed`. Unlike `gemini_api.PRICES`, which had two ids
   the Interactions API declines outright, this table has no dead entries. Ping
   any id before adding it here; a `Response.model` literal accepting a name is
   not the API agreeing to serve it.

What the run did NOT touch, and must not be read as covering: none of the
failure branches in Fact 3 below. No refusal, no truncation and no content
filter occurred, because nothing provoked one - those paths are still
read-from-the-SDK, and the fakes in `tests/test_provider_contract.py` and
`tests/test_openai_api.py` remain the only thing that exercises them at all.

WHICH API SURFACE, AND WHY (openai 2.51.0, established 2026-07-31)
----------------------------------------------------------------------------
The **Responses API** (`client.responses.create`), not Chat Completions. Both
support strict JSON-schema structured output in this version, so the tie is
broken on three things read off the installed package:

- **The length bound.** `chat.completions.create`'s `max_tokens` documents
  itself, in this SDK, as "now deprecated in favor of `max_completion_tokens`,
  and is not compatible with o-series models" - i.e. the reasoning models this
  provider's `DEFAULT_MODEL` comes from. `responses.create` has one unambiguous
  `max_output_tokens` with no deprecated sibling. A bound that silently does
  not apply is exactly the class of defect Task 2 hit with Gemini's nested
  `max_output_tokens`.
- **The usage record.** `responses.ResponseUsage` declares `input_tokens` and
  `output_tokens` as plain non-optional `int`; Chat Completions'
  `CompletionUsage` names them `prompt_tokens`/`completion_tokens` and hangs
  the reasoning breakdown off an OPTIONAL `completion_tokens_details`.
- **Refusal.** Both surfaces have the concept, but on Responses it is a typed
  content part (`ResponseOutputRefusal`) this module can find by walking
  `response.output`, with no dependence on a message index.

The call shape - `client.responses.create(model=, instructions=, input=,
max_output_tokens=, text={"format": {"type": "json_schema", "name": ...,
"schema": ..., "strict": True}})` - was CONFIRMED rather than taken on trust: a
probe that monkeypatched `httpx.Client.send` (so no socket was opened, no key
was needed and nothing was spent) showed the SDK serialising exactly that body
to `POST https://api.openai.com/v1/responses` and parsing the reply back into
`openai.types.responses.response.Response`. `instructions` is this API's
`system`; `input` is the user turn.

THE THREE FACTS, AND HOW EACH WAS ESTABLISHED
----------------------------------------------------------------------------
1. **Token counts** live at `response.usage.input_tokens` and
   `.output_tokens` - NOT Chat Completions' `prompt_tokens`/`completion_tokens`
   and not Gemini's `total_input_tokens`/`total_output_tokens`. Read off
   `openai.types.responses.response_usage.ResponseUsage.model_fields` in the
   installed package (`input_tokens`, `input_tokens_details`, `output_tokens`,
   `output_tokens_details`, `total_tokens`) and exercised through the offline
   probe above. `Response.usage` is itself `ResponseUsage | None`, so the read
   is two attributes deep and can fail - which is why it happens inside
   `Usage.record`'s callable.

   **Reasoning tokens are ALREADY INCLUDED in `output_tokens`, and this module
   deliberately does NOT sum them in.** This is the opposite answer to Gemini's
   and it was checked rather than assumed, because assuming it is what produced
   Task 2's Critical. Three independent statements in the installed SDK agree,
   and all three are about the reported COUNT, not merely the billed RATE - the
   distinction Gemini's pricing page blurred:
     - `OutputTokensDetails`, which holds `reasoning_tokens`, documents itself
       as "A detailed breakdown of the output tokens" - a breakdown OF the
       output figure, not a line item beside it. Structure agrees: on Gemini,
       `total_thought_tokens` is a SIBLING of `total_output_tokens` at the top
       level of `Usage`; here it is nested one level inside the output count.
     - `openai.types.completion_usage.CompletionTokensDetails`'s
       `rejected_prediction_tokens` says: "However, LIKE REASONING TOKENS,
       these tokens are still counted in the total completion tokens for
       purposes of billing, output, and context window limits." That is an
       explicit statement that reasoning tokens are counted IN the completion
       total, not added to it.
     - `responses.ResponseCreateParams.max_output_tokens` says: "An upper bound
       for the number of tokens that can be generated for a response,
       INCLUDING visible output tokens and reasoning tokens." A bound on
       "output tokens" that covers reasoning tokens is a bound on one figure,
       not two.
   So `output_tokens` is the whole billable output figure and adding
   `output_tokens_details.reasoning_tokens` to it would DOUBLE-COUNT every
   reasoning token. `tests/test_openai_api.py` pins that this module reads the
   plain figure and leaves the breakdown alone.
   Not established, and deliberately not claimed: none of this has been
   checked against a real INVOICE. Three SDK docstrings remain the whole of the
   evidence. The 2026-07-31 bake-off did not add to it - the costs recorded
   beside `DEFAULT_MODEL` were computed FROM `output_tokens`, so a
   double-counted reasoning token would appear in both halves of that sum and
   cancel. Only a bill can settle this one.

2. **Bounding the answer's length** is the top-level `max_output_tokens=`
   keyword (`ResponseCreateParamsBase.__annotations__` lists it; the offline
   probe shows it serialised at the top level of the request body). Unlike
   Gemini's it is not nested, and unlike Chat Completions' `max_tokens` it is
   not deprecated.
   A HAZARD that follows from Fact 1 and is worth knowing before an operator
   reports a mystery: because reasoning tokens count against this bound, a
   reasoning model can spend the entire budget thinking and return NO text at
   all. The API reports that as `status="incomplete"` with
   `incomplete_details.reason="max_output_tokens"`, which the status check
   below turns into a message naming both - rather than a JSON parse error
   about an empty string.

3. **Refusal and truncation are reported DIFFERENTLY from each other**, and the
   refusal half is the trap:
   - Truncation is `response.status == "incomplete"` plus
     `response.incomplete_details.reason` (a `Literal["max_output_tokens",
     "content_filter"]`). `Response.status` is
     `Literal["completed", "failed", "in_progress", "cancelled", "queued",
     "incomplete"] | None`; for the call this module makes - non-streaming,
     non-background, no tools - `completed` is the only success state.
   - A REFUSAL, though, arrives inside a COMPLETED response, as a content part
     of type `refusal` (`ResponseOutputRefusal`, carrying the model's own
     explanation). And `Response.output_text` is a convenience property that
     collects ONLY `output_text` parts: the offline probe confirms it returns
     the EMPTY STRING for a refusal-only response. So a refusal that is not
     checked for reaches `json.loads("")` and is reported as "Expecting value:
     line 1 column 1 (char 0)" - a message that says nothing whatever about the
     real cause. This is precisely why Anthropic's `stop_reason` check comes
     before its `content` read, and why `_refusal` below is consulted before
     `output_text` is ever touched.

Sources, all dated 2026-07-31: the installed `openai` 2.51.0 package itself
(authoritative wherever it and a docs page disagree), plus
https://developers.openai.com/api/docs/guides/structured-outputs for the strict
-mode rules `_adapt_schema` implements - and, for the three numbered points in
the opening section, the live API. The three FACTS below were established
without a paid call and are still stated on that evidence: where one says it
was read off the SDK, that is all it was, and the run confirmed the happy path
and the token accounting rather than every branch of them.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable

from ._shared import ModelError, Usage, require

PROVIDER_ID = "openai"
KEY_FILENAME = "openai.json"
PACKAGE = "openai"
INSTALL = ".venv/bin/pip install openai"
# Measured against the real API on 2026-07-31: two paid detection runs over
# this workspace's own 98-minute qualifying (the bake-off beside DEFAULT_MODEL)
# plus a 16-token ping per priced model, which established that every id in
# PRICES is one the API actually serves. What those runs did and did not
# establish is set out in the docstring's opening section; this flag claims
# nothing beyond it.
VERIFIED = True

# model -> (USD per 1M input tokens, USD per 1M output tokens).
# A snapshot of the published standard-tier rates at
# https://developers.openai.com/api/docs/pricing on 2026-07-31; re-check there
# rather than trusting these numbers. Every id was additionally checked against
# this SDK's own `Response.model` literal, so a typo here is not merely
# mispriced but a model the API does not know - and, on 2026-07-31, against the
# API itself: all five answered a 16-token ping with `status=completed`. That
# second check is not redundant. `gemini_api.PRICES` carried two ids its SDK's
# own literal accepted and the API then refused, and they had to be removed.
#
# These are the STANDARD-tier rates. Read a number from here as a FLOOR: the
# published table prices batch and cached input lower, priority/flex service
# tiers differently, and LONG CONTEXT differently again - and long context is
# the one of the three this project's own workload plausibly hits, since
# moment_scan sends hour-long transcript windows as a single call. Measured
# against the published pricing page on 2026-07-31: gpt-5.6-terra's
# long-context OUTPUT rate is 18.00, against the 12.00 stored below - a 50%
# understatement for exactly the calls this detector makes. The page does not
# state the short/long threshold, so the fix here is this caveat, not a second
# number; a channel whose windows cross it will still see PRICES undercount.
# This flat two-number table cannot express any of the three tiers.
# 0.0 is deliberately absent - an unpriced model reads as free downstream,
# which is the one thing worse than a slightly wrong number.
PRICES: dict[str, tuple[float, float]] = {
    "gpt-5.6-sol": (5.00, 30.00),
    "gpt-5.6-terra": (2.00, 12.00),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.4-mini": (0.75, 4.50),
}

# Measured, not assumed - but on ONE stream, and against Anthropic's own run
# over that same stream rather than against a fixed answer key. A bake-off on
# 2026-07-31 over this workspace's 98-minute qualifying (video V9nVNEQNdR4,
# "ERF 24H Nürburgring | Qualifying 1 | Superpole", 5574 transcript words, 2
# scan windows, 11047 input tokens on both runs - the stream anthropic_api's
# and gemini_api's own bake-offs used):
#
#   gpt-5.6-terra   5 moments, 0 failed windows,  876 output tokens, $0.0326
#   gpt-5.6-luna    7 moments, 0 failed windows, 1575 output tokens, $0.0041
#
#   reference: claude-opus-5 over the same stream (2026-07-29, this
#   workspace's production analysis) found 11 moments, 0 failed windows.
#
# THE REFERENCE MOVES, and its cost was wrong. This line used to end
# "~$0.062", which was never a measurement - the measured figure is $0.1362,
# more than double, so gpt-5.6-terra is a quarter of Opus here rather than a
# half. And claude-opus-5 over this identical stream has returned 7, 10 and 11
# moments across three runs; see anthropic_api's own comment for what settled
# that. Read the agreement figures below as approximate for that reason: they
# are scored against ONE run of a reference whose weakly-scored tail moves.
# Both of the moments named below are in the stable part of it.
#
# Agreement, not merely a count - a moment "agrees" when it overlaps a
# reference moment by more than 50% of the shorter window. gpt-5.6-terra: 5 of
# 5, including BOTH moments the Anthropic comment credits to Opus alone (the
# Speed Hunter lap at 3053.8s, at 100% overlap, and the pole lap at
# ~5625-5716s, at 86%). gpt-5.6-luna: 6 of 7.
#
# THE FINDING THAT DECIDES THIS, and it is subtler than "cheaper is worse":
# gpt-5.6-luna does not MISS the pole lap, it CUTS IT IN HALF. It proposes two
# windows - 5585.4-5658.4 and 5681.2-5715.8 - overlapping the reference moment
# by 49% and 33%, where gpt-5.6-terra and gemini-3.6-flash each return ONE
# window at 86%. For a Shorts pipeline the deliverable IS the window: a split
# moment is two clips that each begin or end in the wrong place, not one good
# one. So a model that finds MORE moments while framing the strongest one badly
# is not the cheaper choice, it is the wrong one - and that, not the count, is
# what keeps gpt-5.6-terra here at roughly eight times gpt-5.6-luna's measured
# cost ($0.0326 against $0.0041; the published rate gap is 10x, narrowed by
# luna spending more output tokens).
#
# That REPLACES the argument this comment used to carry, which was an ANALOGY
# and said so: the Anthropic bake-off had refuted the plan's assumption that
# the cheapest model would do, so the cheapest OpenAI model was not picked
# either. Reasoning from another vendor's measurement was the best available
# then and is not needed now. This is this vendor, this detector, this stream.
#
# Also seen, on the gpt-5.6-luna run, and worth recording because it had never
# been observed in production before: `validate_moment` DROPPED one malformed
# candidate and logged it, and the run finished with the other 7 - the "one bad
# moment costs one moment, never the run" rule doing exactly its job.
#
# AN OPEN QUESTION THIS RUN DID NOT SETTLE, and the most interesting thing it
# left behind: on identical input, Anthropic found 11 moments (10 on the
# re-run), Gemini 6 and OpenAI 5, with perfect agreement wherever they overlap
# - every non-Anthropic moment lands on an Anthropic one. Nobody has checked
# whether the other two engines SCORED the moments only Opus found (5 of the 11
# in Gemini's case, 6 in gpt-5.6-terra's) below threshold, or never PROPOSED
# them at all. It is not the cap: `moment_scan.MAX_PER_WINDOW` is 12 across 2
# windows, so no run came near it. Part of the gap is a tail that is not stable
# even within Opus itself, which narrows the question without answering it.
# Until someone looks, "lower recall" describes the counts rather than
# explaining them.
#
# ONE stream, ONE 98-minute qualifying session, TWO windows, ONE run per model.
# It says nothing about an eight-hour endurance race, which is this project's
# real workload - eight times as many windows and a different commentary
# register. The costs are a dated snapshot computed from the API's OWN reported
# token counts at PRICES' rates, never read off an invoice, and PRICES
# documents itself as a FLOOR. The other three priced entries (gpt-5.6-sol,
# gpt-5.4, gpt-5.4-mini) were NOT run; they have no numbers here at all.
# Re-measure before moving this in either direction.
#
# RE-MEASURED 2026-07-31 on the eight-hour workload the paragraph above says
# this run says nothing about: Esm9vv5-PdU, 8h19m, 41925 words, 9 windows.
# gpt-5.6-terra returned 33 moments, no failed window, 77673 input + 4890
# output tokens, $0.2140 measured, in 78 seconds - against claude-opus-5's 39
# moments at $0.7603 and gemini-3.6-flash's 30 at $0.3347 with ONE window lost.
# terra was the cheapest, the fastest and the only one of the three that was
# both complete and cheap.
#
# But the reason for keeping it CHANGED, and the old reason is now known to be
# wrong. On the qualifying, terra's rivals proposed only moments Anthropic had
# also found, which supported an argument that Anthropic's extras were noise.
# Over eight hours pairwise agreement falls to 38-67%, no model's list is a
# subset of another's, and each proposes strong moments the others miss
# entirely. Choosing terra therefore means accepting what the other two would
# have found - it is a cost/speed/completeness choice, not a "the others only
# add noise" one. Do not restate the old argument; it did not survive the
# larger stream.
DEFAULT_MODEL = "gpt-5.6-terra"

# The name the response format is registered under. `strict` mode requires one
# (`ResponseFormatTextJSONSchemaConfigParam.name` is `Required[str]`, a-z/A-Z/
# 0-9/underscore/dash, max 64) and the API rejects the call without it. It is
# a label for the schema, not a model instruction, so it never varies.
_SCHEMA_NAME = "moments"


def _sdk():
    # Four plain strings rather than this module itself: `require` lives in
    # `_shared`, which deliberately knows nothing about what a provider module
    # looks like, so there is nothing to hand it but this provider's own facts.
    require(PACKAGE, INSTALL, "moment detection", PROVIDER_ID)
    import openai
    return openai


def _adapt_schema(schema: dict) -> dict:
    """`moment_scan.SCHEMA` in OpenAI's strict dialect.

    Strict mode requires EVERY property to appear in `required`; a property
    that is genuinely optional is expressed as a nullable union instead. The
    structured-outputs guide states both halves outright - "all fields or
    function parameters must be specified as `required`", and "it is possible
    to emulate an optional parameter by using a union type with `null`" -
    alongside the third rule this function must not break, that every object
    carries `additionalProperties: false`. `moment_scan.SCHEMA` sets that key
    explicitly at every level today, so it is always PRESERVED here rather than
    exercised - but an object node that omits it gets `False` ADDED, matching
    OpenAI's own strict-mode normaliser (`openai/lib/_pydantic.py::
    _ensure_strict_json_schema` in the installed package, which does exactly
    this: `if typ == "object" and "additionalProperties" not in json_schema:
    json_schema["additionalProperties"] = False`). Read that SDK function
    yourself before changing this behaviour again.

    `moment_scan.SCHEMA` has exactly one such property today
    (`hook_suggestion`), but this walks the whole schema rather than naming it,
    so a second optional field added upstream does not silently produce a 400
    here. Anything that is not a `properties`-bearing object or an array is
    copied through untouched: this is an ADAPTER, not a validator, and a key it
    does not understand is not its business to drop.

    `moment_scan.SCHEMA` itself is NOT changed to suit this vendor: it is
    written for Anthropic's dialect, that dialect is measured, and adapting is
    this module's job. Deep-copied, never mutated in place - the caller passes
    the module-level constant, and mutating it would silently corrupt the
    schema every OTHER provider sends. `tests/test_openai_api.py` pins that.

    The real API has since accepted what this produces: both bake-off runs on
    2026-07-31 sent it on every call and finished with 0 failed windows (module
    docstring, opening section, point 1). That covers `moment_scan.SCHEMA` as it
    stands today and nothing more - the recursive walk below exists for a schema
    that CHANGES, and no changed schema has been sent to the API at all.

    Widening a type to a nullable union is done ONLY for a property that was
    not already required and whose `type` is a plain string. A type that is
    already a list (some other union) is left alone unless `"null"` is missing
    from it, and a property with no `type` at all - `$ref`, `anyOf`, `enum`
    alone - is left entirely alone, because there is no scalar there to widen
    and inventing one would be a guess.
    """
    return _adapt(copy.deepcopy(schema))


def _adapt(node):
    """The in-place half of `_adapt_schema`, on an ALREADY-COPIED node.

    Separate from `_adapt_schema` so the deep copy happens exactly once, at the
    top, rather than once per level of recursion.
    """
    if isinstance(node, list):
        for item in node:
            _adapt(item)
        return node
    if not isinstance(node, dict):
        return node

    # Matches the SDK's own strict-mode normaliser
    # (`openai/lib/_pydantic.py::_ensure_strict_json_schema`): an object node
    # that never set `additionalProperties` gets `False`, not a silent gap the
    # API would reject. `moment_scan.SCHEMA` sets it explicitly everywhere
    # today, so this is latent - but the recursive walk exists precisely so an
    # upstream schema change does not silently 400, and an absent key here
    # would defeat that for this one rule. Never OVERWRITES a value already
    # present, at any level - see `_adapt_schema`'s own docstring.
    if node.get("type") == "object" and "additionalProperties" not in node:
        node["additionalProperties"] = False

    properties = node.get("properties")
    if isinstance(properties, dict):
        # Every key becomes required. `sorted` rather than insertion order so
        # the adapted schema is stable to compare and to diff; the API does not
        # care about the order, a test reading it does.
        was_required = set(node.get("required") or ())
        node["required"] = sorted(properties)
        for name, definition in properties.items():
            if name not in was_required and isinstance(definition, dict):
                _make_nullable(definition)
            _adapt(definition)

    # Arrays, and any other nested schema keyword that holds a subschema or a
    # list of them. `items` is the only one moment_scan.SCHEMA uses today; the
    # rest are here so a schema that grows a union or a tuple-typed array does
    # not quietly skip adaptation of its branches.
    for keyword in ("items", "prefixItems", "anyOf", "allOf", "oneOf",
                    "additionalItems", "not"):
        if keyword in node:
            _adapt(node[keyword])
    # `$defs` is the modern keyword `moment_scan.SCHEMA` would use if it ever
    # grows a shared subschema; `definitions` is its draft-07 predecessor. The
    # SDK's own normaliser walks BOTH (`_ensure_strict_json_schema` reads
    # `$defs` and then, separately, `definitions`) - a schema authored, or
    # generated by a third-party tool, in the older dialect must not silently
    # skip adaptation of its shared definitions.
    for defs_keyword in ("$defs", "definitions"):
        definitions = node.get(defs_keyword)
        if isinstance(definitions, dict):
            for definition in definitions.values():
                _adapt(definition)
    return node


def _make_nullable(definition: dict) -> None:
    """Widens `{"type": X}` to `{"type": [X, "null"]}`, in place.

    Called only for a property that the ORIGINAL schema left out of `required`
    - i.e. one the model may genuinely omit. Under strict mode it can no longer
    be omitted, so `null` has to become a legal value for it or the schema
    would demand a `hook_suggestion` the model has nothing to say for.
    """
    declared = definition.get("type")
    if isinstance(declared, str):
        definition["type"] = [declared, "null"]
    elif isinstance(declared, list) and "null" not in declared:
        definition["type"] = [*declared, "null"]
    # No `type` at all ($ref, a bare enum, an anyOf): nothing to widen, and
    # guessing one would be inventing a constraint the author did not write.


def _refusal(response) -> str | None:
    """The model's refusal explanation, or None if it did not refuse.

    A refusal is a content PART of type `refusal` inside an otherwise
    `completed` response, and `Response.output_text` collects only
    `output_text` parts - so an unchecked refusal reaches `json.loads("")` and
    is reported as a JSON syntax error. Walked defensively with `getattr`
    rather than by index: this reads a third-party object, and the whole point
    of the walk is that no particular position may be assumed.
    """
    for item in getattr(response, "output", None) or ():
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", None) or ():
            if getattr(part, "type", None) == "refusal":
                text = getattr(part, "refusal", None)
                # The model's OWN words, never the request - safe to quote, the
                # same way the JSONDecodeError branch quotes the answer. An
                # empty or missing explanation still counts as a refusal.
                return text if isinstance(text, str) and text else "no reason given"
    return None


def make_caller(api_key: str, *, model: str = DEFAULT_MODEL,
                max_tokens: int = 4096, sdk=None,
                usage: Usage | None = None) -> Callable[[str, str, dict], dict]:
    """Returns `call(system, user, schema) -> dict`.

    `sdk` is injected so the whole path tests without the package, a key, a
    network or a cent. Production passes nothing and gets the real module.

    `usage`, when given, accumulates the API's OWN reported token counts across
    every call this caller makes - see `providers.Usage` for why that is worth
    having and why it is an out-parameter rather than a return value.

    The `schema` argument arrives in `moment_scan.SCHEMA`'s Anthropic dialect
    and is adapted here (see `_adapt_schema`); the caller's dict is never
    mutated.

    Deliberately absent from the payload: `reasoning` (this API's effort
    control), `service_tier`, `temperature` and `text.verbosity`. All four are
    real knobs, none has been measured on this project's transcripts, and the
    Anthropic provider's own note is the precedent - name what was measured,
    not what looked plausible. `reasoning` is still the one most likely to be
    worth adding, because reasoning tokens are billed as output tokens and
    count against `max_output_tokens` (see the module docstring, Facts 1 and
    2). The 2026-07-31 bake-off did not settle it either way: neither run
    recorded how its output tokens split between reasoning and visible answer,
    so what an effort setting would cost - or buy - here is unmeasured.
    """
    module = sdk if sdk is not None else _sdk()
    try:
        client = module.OpenAI(api_key=api_key)
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
        # one. See anthropic_api's and gemini_api's identical notes; it is
        # repeated rather than cross-referenced because it is the reason this
        # handler exists at all.
        raise ModelError(
            f"the OpenAI SDK raised {type(error).__name__} building the client"
        ) from error

    def call(system: str, user: str, schema: dict) -> dict:
        # OUTSIDE the try below, deliberately: `_adapt_schema` is this
        # project's own pure code operating on this project's own constant. It
        # cannot carry the API key, and wrapping it in the request handler
        # would report a bug in this file as "the OpenAI SDK raised KeyError
        # calling the model" - blaming the vendor for something that never
        # reached them.
        adapted = _adapt_schema(schema)
        try:
            response = client.responses.create(
                model=model,
                instructions=system,
                input=user,
                max_output_tokens=max_tokens,
                text={"format": {"type": "json_schema",
                                 "name": _SCHEMA_NAME,
                                 "schema": adapted,
                                 "strict": True}},
            )
        except Exception as error:
            # Anything the SDK raises here - auth, rate limit, connection - may
            # embed the request in its own message, and that request carries the
            # API key. Never let str(error) reach a caller; the type name is the
            # only part of it that is safe to keep, and it is chained with
            # `from` so the real cause still shows up in a traceback.
            raise ModelError(
                f"the OpenAI SDK raised {type(error).__name__} calling the model"
            ) from error
        # BEFORE any of the reads below, all of which can raise: the tokens
        # were spent the moment this response came back, whether or not it
        # turns out to be a refusal, truncated, or unparseable. Recording them
        # only on the success path would under-report a run's real cost by
        # exactly the windows that went wrong - the ones an operator most wants
        # explained when the bill does not match the estimate.
        if usage is not None:
            # A callable, so the reads below are inside Usage.record's own
            # tolerance rather than outside every handler (see Usage.record) -
            # which matters here because `Response.usage` is itself
            # `ResponseUsage | None`, so this is two attributes deep.
            #
            # `output_tokens` is read PLAIN. Reasoning tokens are already
            # counted in it on this API (module docstring, Fact 1) - the
            # opposite of Gemini, where `total_thought_tokens` is a separate
            # line item gemini_api has to add in. Adding
            # `output_tokens_details.reasoning_tokens` here would double-count
            # every reasoning token and overstate the bill.
            usage.record(lambda: (response.usage.input_tokens,
                                  response.usage.output_tokens))
        # Everything from here on reads the Response the SDK handed back, and
        # that object was built from the request we just sent - so anything it
        # raises on (a missing attribute, a future SDK shape this code does not
        # expect) can carry the same risk `responses.create` itself does: the
        # key, by way of the request, showing up in a third-party exception's
        # own text. On the first provider this block sat outside any handler
        # and a fake SDK whose content attribute raised with the key in its
        # text reached the caller intact; it is inside one here from the start.
        try:
            # status is checked BEFORE the answer is read: `completed` is the
            # only success state for this call shape (see the module
            # docstring), and on an `incomplete` one `output_text` is a
            # TRUNCATED fragment that fails json.loads with a syntax error
            # explaining nothing.
            status = getattr(response, "status", None)
            if status != "completed":
                raise ModelError(
                    "the model returned no usable answer "
                    f"(status={status}{_why(response)})")
            # And the refusal check comes before it too, for a sharper reason:
            # a refusal arrives in a COMPLETED response, so the check above
            # passes, and `output_text` collects only `output_text` parts - so
            # a refusal reads back as the empty string and json.loads reports
            # "Expecting value: line 1 column 1". The model's own explanation
            # is what an operator needs instead.
            refusal = _refusal(response)
            if refusal is not None:
                raise ModelError(f"the model refused this window: {refusal}")
            text = response.output_text
            if text is None or text == "":
                # A completed, non-refusing response that carried no text part
                # at all. Saying so beats letting json.loads("") raise a syntax
                # error about column 1 of nothing.
                raise ModelError("the model returned an empty answer")
            if not isinstance(text, str):
                # Not a shape the SDK's own types admit (`output_text` is
                # typed `str`), but this reads a third-party object handed back
                # over the wire, and a wrong-typed value here is not an EMPTY
                # answer - it earns its own honest message rather than being
                # folded into "empty", which would say nothing true about what
                # was actually wrong.
                raise ModelError(
                    f"the model returned a non-text answer ({type(text).__name__})")
            try:
                return json.loads(text)
            except json.JSONDecodeError as error:
                raise ModelError(f"the answer was not valid JSON: {error}") from None
        except ModelError:
            # Already sanitised by this function itself - the status, refusal,
            # empty-answer and JSONDecodeError raises above, whose messages
            # quote the API's own status or the MODEL'S OWN ANSWER (never the
            # request, so they cannot carry the key). Pass through unchanged
            # rather than re-wrapping into the vaguer type-name-only message
            # below.
            raise
        except Exception as error:
            # Anything else here - a malformed Response, a future SDK shape
            # this code does not expect - is not something this function has
            # already vetted as safe. Never let str(error) reach a caller; the
            # type name is the only part of it that is safe to keep, and it is
            # chained with `from` so the real cause still shows up in a
            # traceback.
            raise ModelError(
                f"the OpenAI SDK raised {type(error).__name__} reading the response"
            ) from error

    return call


def _why(response) -> str:
    """`, reason=max_output_tokens` when the API said why, else "".

    Truncation and a content filter are BOTH `status="incomplete"` on this API
    and are told apart only by `incomplete_details.reason` - which is exactly
    the difference between "raise max_tokens" and "this window will never
    work". Defensive `getattr`s: the whole value of this string is that it
    appears inside a message about a response that already went wrong, so it
    must never be the thing that raises. It is a `Literal` of the API's own
    vocabulary, never free text from the request, so quoting it cannot carry
    the key.
    """
    try:
        details = getattr(response, "incomplete_details", None)
        reason = getattr(details, "reason", None)
    except Exception:   # noqa: BLE001 - a diagnostic string never costs a window
        return ""
    return f", reason={reason}" if reason else ""
