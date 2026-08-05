"""What is true of OPENAI specifically, and nothing the contract already pins.

`tests/test_provider_contract.py` runs the behavioural properties against every
registered provider - the three key-secrecy wrapping points, the usage
accounting, the JSON handling, the key file. None of that is repeated here.
What is left is the vendor's own shape: the schema adaptation (the one genuinely
provider-specific transformation in this project), the exact payload this module
sends, and the failure modes that exist because `Response.status`,
`Response.output_text` and `ResponseOutputRefusal` are shaped the way they are.

The provider HAS run against the real service since 2026-07-31 (`VERIFIED` is
True; see the bake-off beside `openai_api.DEFAULT_MODEL`), but that run only
ever exercised the happy path - no refusal, no truncation, no content filter
occurred. So this file and the fakes remain the only thing that exercises the
failure branches at all. No network, no key, no cost: every test drives an
injected fake or reads the installed package.
"""

from __future__ import annotations

import copy
import importlib.util

import pytest

from yt_shorts import moment_scan, providers
from yt_shorts.providers import openai_api

# --- The schema adaptation --------------------------------------------------
#
# The conformance suite only asserts that `moment_scan.SCHEMA` is ACCEPTED, not
# what it becomes. Strict mode's rules (structured-outputs guide, 2026-07-31)
# are that every property appears in `required`, that an optional property is
# expressed as a nullable union instead, and that every object carries
# `additionalProperties: false`.

# Snapshotted ONCE, at module import - i.e. before a single test in this file
# has run, so it cannot itself be built from an already-corrupted constant.
# `test_the_shared_schema_is_not_mutated` asserts against THIS, never against
# a `before` captured inside the test: a `before` taken there would, under a
# mutating `_adapt_schema`, be captured AFTER
# `test_every_property_becomes_required_and_the_optional_one_becomes_nullable`
# (an earlier test in this same class) has already corrupted
# `moment_scan.SCHEMA` in place - so the guard would snapshot the corrupted
# constant as its own baseline, re-adapt idempotently, see no further change,
# and pass while the mutation it exists to catch went undetected. Proven: with
# `copy.deepcopy` removed from `_adapt_schema`, a `before` captured inside the
# test still passed this way; only a baseline fixed at import time catches it
# when the file runs as a whole. See the review report appended to
# `.superpowers/sdd/task-3-report.md` for the whole-file revert proof.
_PRISTINE_SCHEMA = copy.deepcopy(moment_scan.SCHEMA)


class TestTheSchemaAdaptation:
    def test_every_property_becomes_required_and_the_optional_one_becomes_nullable(self):
        adapted = openai_api._adapt_schema(moment_scan.SCHEMA)
        item = adapted["properties"]["moments"]["items"]
        assert set(item["required"]) == set(item["properties"])
        assert item["properties"]["hook_suggestion"]["type"] == ["string", "null"]
        assert item["properties"]["reason"]["type"] == "string"

    def test_the_shared_schema_is_not_mutated(self):
        # `moment_scan.SCHEMA` is a module-level constant every OTHER provider
        # sends verbatim. Mutating it here would silently corrupt Anthropic's
        # and Gemini's payloads too, in the same process, with nothing local to
        # notice. Compared against `_PRISTINE_SCHEMA` (snapshotted at import,
        # above) rather than a `before` captured in this test - see that
        # constant's own comment for why a locally-captured baseline is order-
        # dependent and can hide exactly the mutation this test exists to
        # catch.
        openai_api._adapt_schema(moment_scan.SCHEMA)
        assert moment_scan.SCHEMA == _PRISTINE_SCHEMA

    def test_the_top_level_object_is_adapted_too_not_only_the_items(self):
        adapted = openai_api._adapt_schema(moment_scan.SCHEMA)
        assert set(adapted["required"]) == set(adapted["properties"]) == {"moments"}

    def test_additional_properties_false_survives_at_every_level(self):
        adapted = openai_api._adapt_schema(moment_scan.SCHEMA)
        assert adapted["additionalProperties"] is False
        assert adapted["properties"]["moments"]["items"]["additionalProperties"] is False

    def test_a_second_optional_field_added_upstream_is_adapted_without_being_named(self):
        # The whole reason `_adapt_schema` walks the schema instead of naming
        # `hook_suggestion`: a property added to moment_scan.SCHEMA later and
        # left out of `required` would otherwise reach the API as a strict-mode
        # violation - a 400 on every window, with nothing here to catch it.
        grown = copy.deepcopy(moment_scan.SCHEMA)
        item = grown["properties"]["moments"]["items"]
        item["properties"]["driver"] = {"type": "string"}
        adapted = openai_api._adapt_schema(grown)
        adapted_item = adapted["properties"]["moments"]["items"]
        assert "driver" in adapted_item["required"]
        assert adapted_item["properties"]["driver"]["type"] == ["string", "null"]

    def test_an_already_required_property_is_left_alone(self):
        # Widening a required property to nullable would tell the model it may
        # answer `null` for a `reason` the prompt insists on.
        adapted = openai_api._adapt_schema(moment_scan.SCHEMA)
        item = adapted["properties"]["moments"]["items"]
        assert item["properties"]["score"]["type"] == "number"
        assert item["properties"]["category"]["type"] == "string"
        assert item["properties"]["category"]["enum"] == list(moment_scan.CATEGORIES)

    def test_nesting_deeper_than_moment_scan_uses_today_is_still_walked(self):
        schema = {
            "type": "object",
            "properties": {
                "outer": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "inner": {
                                "type": "object",
                                "properties": {"deep": {"type": "integer"}},
                                "required": [],
                                "additionalProperties": False,
                            },
                        },
                        "required": ["inner"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["outer"],
            "additionalProperties": False,
        }
        adapted = openai_api._adapt_schema(schema)
        deep = adapted["properties"]["outer"]["items"]["properties"]["inner"]
        assert deep["required"] == ["deep"]
        assert deep["properties"]["deep"]["type"] == ["integer", "null"]

    def test_an_existing_union_gains_null_once_and_only_once(self):
        schema = {"type": "object",
                  "properties": {"a": {"type": ["string", "integer"]},
                                 "b": {"type": ["string", "null"]}},
                  "required": [], "additionalProperties": False}
        adapted = openai_api._adapt_schema(schema)
        assert adapted["properties"]["a"]["type"] == ["string", "integer", "null"]
        # Already nullable: not "null" twice.
        assert adapted["properties"]["b"]["type"] == ["string", "null"]

    def test_a_property_with_no_type_is_left_entirely_alone(self):
        # A `$ref`, a bare `enum`, an `anyOf`: there is no scalar type to
        # widen, and inventing one would be a constraint the author did not
        # write. It still becomes required, which is what strict mode demands.
        schema = {"type": "object",
                  "properties": {"a": {"$ref": "#/$defs/thing"},
                                 "b": {"enum": ["x", "y"]}},
                  "required": [], "additionalProperties": False}
        adapted = openai_api._adapt_schema(schema)
        assert adapted["required"] == ["a", "b"]
        assert adapted["properties"]["a"] == {"$ref": "#/$defs/thing"}
        assert adapted["properties"]["b"] == {"enum": ["x", "y"]}

    def test_additional_properties_false_is_added_when_absent_on_an_object(self):
        # Mirrors the SDK's own strict-mode normaliser
        # (`openai/lib/_pydantic.py::_ensure_strict_json_schema` in the
        # installed package): `if typ == "object" and "additionalProperties"
        # not in json_schema: json_schema["additionalProperties"] = False`.
        # `moment_scan.SCHEMA` sets the key explicitly at every level today, so
        # this is latent there - but the recursive walk exists precisely so an
        # upstream schema change does not silently 400, and an object node
        # that omitted the key used to sail through untouched and fail at the
        # API instead of here.
        schema = {"type": "object",
                  "properties": {"a": {"type": "string"}},
                  "required": ["a"]}
        adapted = openai_api._adapt_schema(schema)
        assert adapted["additionalProperties"] is False

    def test_additional_properties_already_set_is_never_overwritten(self):
        # The SDK's own check is `not in json_schema` - presence, not value -
        # so an explicit `True` (unusual, but not this function's call to
        # second-guess) must survive exactly as written.
        schema = {"type": "object", "properties": {}, "required": [],
                  "additionalProperties": True}
        adapted = openai_api._adapt_schema(schema)
        assert adapted["additionalProperties"] is True

    def test_a_non_object_type_does_not_get_additional_properties_invented(self):
        # Only `type == "object"` triggers the SDK's rule; an array or a
        # scalar has no `additionalProperties` concept at all, and inventing
        # the key on one would be a constraint neither the author nor OpenAI's
        # own normaliser would write.
        schema = {"type": "object",
                  "properties": {"a": {"type": "array",
                                       "items": {"type": "string"}}},
                  "required": ["a"], "additionalProperties": False}
        adapted = openai_api._adapt_schema(schema)
        assert "additionalProperties" not in adapted["properties"]["a"]

    def test_draft_07_definitions_is_walked_like_defs(self):
        # OpenAI's own normaliser walks `$defs` AND, separately, the older
        # draft-07 `definitions` keyword. `moment_scan.SCHEMA` only ever uses
        # `items` today, so this - like the other keywords in the recursion
        # loop - exists for a schema this project has not written yet rather
        # than one it has.
        schema = {
            "type": "object",
            "properties": {"a": {"$ref": "#/definitions/thing"}},
            "required": ["a"],
            "additionalProperties": False,
            "definitions": {
                "thing": {"type": "object",
                         "properties": {"x": {"type": "string"}},
                         "required": []},
            },
        }
        adapted = openai_api._adapt_schema(schema)
        inner = adapted["definitions"]["thing"]
        assert inner["required"] == ["x"]
        assert inner["properties"]["x"]["type"] == ["string", "null"]
        # And the same latent additionalProperties fix applies inside a
        # definition, not only at the top level.
        assert inner["additionalProperties"] is False


# --- The payload ------------------------------------------------------------


class _Details:
    def __init__(self, reasoning=0):
        self.reasoning_tokens = reasoning


class _Usage:
    def __init__(self, reported_in=0, reported_out=0, reasoning=0):
        self.input_tokens = reported_in
        self.output_tokens = reported_out
        self.output_tokens_details = _Details(reasoning)


class _TextPart:
    type = "output_text"

    def __init__(self, value):
        self.text = value


class _RefusalPart:
    type = "refusal"

    def __init__(self, value):
        self.refusal = value


class _Message:
    type = "message"

    def __init__(self, parts):
        self.content = parts


class _Response:
    def __init__(self, text, status="completed", usage=None,
                 incomplete_details=None, output=None):
        self.output_text = text
        self.status = status
        self.usage = usage if usage is not None else _Usage()
        self.incomplete_details = incomplete_details
        # Mirrors the real API: `output_text` is derived from `output`, so a
        # fake that sets only one of the two is describing a response the
        # service cannot produce.
        self.output = ([_Message([_TextPart(text)])] if output is None
                       else output)


class _Incomplete:
    def __init__(self, reason):
        self.reason = reason


class _FakeResponses:
    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []

    def create(self, **payload):
        self.calls.append(payload)
        return self._response


class _FakeSDK:
    """Stands in for the `openai` module. No network, no key, no cost."""

    def __init__(self, response):
        self.responses = _FakeResponses(response)
        self.last_api_key = None

    def OpenAI(self, api_key=None):        # noqa: N802 - mirrors the SDK name
        self.last_api_key = api_key
        return self


class TestThePayload:
    """The keyword names and the nested `text.format` shape were read off the
    installed SDK (`ResponseCreateParamsBase.__annotations__` plus
    `ResponseFormatTextJSONSchemaConfigParam`) and confirmed by serialising a
    real request offline against a monkeypatched `httpx.Client.send`. Pinning
    them here is what catches a rename in a later openai release: an unknown
    keyword is a TypeError from the SDK's own signature, which this module
    would report as a type-name-only ModelError - true, but useless for finding
    out why."""

    def test_sends_the_system_as_instructions_and_the_window_as_input(self):
        sdk = _FakeSDK(_Response("{}"))
        call = openai_api.make_caller("key", model="gpt-5.4", sdk=sdk)
        call("sys", "the window", {"type": "object"})
        payload = sdk.responses.calls[0]
        assert payload["model"] == "gpt-5.4"
        # This API has no `system` keyword and no `messages` list - the system
        # prompt is `instructions` and the user turn is `input`.
        assert payload["instructions"] == "sys"
        assert payload["input"] == "the window"
        assert "system" not in payload
        assert "messages" not in payload

    def test_max_tokens_rides_as_max_output_tokens_at_the_top_level(self):
        # Anthropic takes `max_tokens=`; Gemini nests `max_output_tokens` in a
        # `generation_config`. This API takes `max_output_tokens` at the top
        # level, and `max_tokens` is a Chat Completions name that does not
        # exist here at all - sending it would be a TypeError, and omitting the
        # bound would let the model's own default apply.
        sdk = _FakeSDK(_Response("{}"))
        call = openai_api.make_caller("key", max_tokens=1234, sdk=sdk)
        call("sys", "user", {"type": "object"})
        payload = sdk.responses.calls[0]
        assert payload["max_output_tokens"] == 1234
        assert "max_tokens" not in payload
        assert "max_completion_tokens" not in payload

    def test_the_schema_goes_in_strict_mode_under_text_format_with_a_name(self):
        sdk = _FakeSDK(_Response("{}"))
        call = openai_api.make_caller("key", sdk=sdk)
        call("sys", "user", moment_scan.SCHEMA)
        text_config = sdk.responses.calls[0]["text"]["format"]
        assert text_config["type"] == "json_schema"
        assert text_config["strict"] is True
        # `name` is Required[str] on the SDK's own param type; without it the
        # API rejects the call outright.
        assert text_config["name"] == openai_api._SCHEMA_NAME
        assert "response_format" not in sdk.responses.calls[0]

    def test_the_schema_that_is_actually_sent_is_the_adapted_one(self):
        # The adaptation is worthless if `call` sends the caller's dict anyway.
        sdk = _FakeSDK(_Response("{}"))
        call = openai_api.make_caller("key", sdk=sdk)
        call("sys", "user", moment_scan.SCHEMA)
        sent = sdk.responses.calls[0]["text"]["format"]["schema"]
        item = sent["properties"]["moments"]["items"]
        assert set(item["required"]) == set(item["properties"])
        assert item["properties"]["hook_suggestion"]["type"] == ["string", "null"]
        assert sent is not moment_scan.SCHEMA

    def test_the_key_reaches_the_client_and_nothing_else(self):
        sdk = _FakeSDK(_Response("{}"))
        openai_api.make_caller("the-secret-key", sdk=sdk)
        assert sdk.last_api_key == "the-secret-key"


# --- Status, refusal, and the order the two are checked in ------------------


class TestTheStatusIsCheckedFirst:
    @pytest.mark.parametrize("status", ["incomplete", "failed", "cancelled",
                                        "in_progress", "queued"])
    def test_any_status_but_completed_is_a_model_error_naming_it(self, status):
        sdk = _FakeSDK(_Response(None, status=status))
        call = openai_api.make_caller("key", sdk=sdk)
        with pytest.raises(providers.ModelError) as error:
            call("sys", "user", {"type": "object"})
        assert status in str(error.value)

    def test_a_truncated_answer_names_the_reason_the_api_gave(self):
        # `incomplete` is both truncation and a content filter; only
        # `incomplete_details.reason` tells them apart, and that difference is
        # the difference between "raise max_tokens" and "this window will never
        # work". A truncated answer is ALSO unparseable JSON - the status must
        # win, because "Expecting ',' delimiter" explains nothing.
        sdk = _FakeSDK(_Response('{"moments": [', status="incomplete",
                                 incomplete_details=_Incomplete("max_output_tokens")))
        call = openai_api.make_caller("key", sdk=sdk)
        with pytest.raises(providers.ModelError) as error:
            call("sys", "user", {"type": "object"})
        message = str(error.value)
        assert "incomplete" in message
        assert "max_output_tokens" in message
        assert "not valid JSON" not in message

    def test_a_missing_incomplete_details_costs_the_detail_and_not_the_message(self):
        sdk = _FakeSDK(_Response(None, status="incomplete"))
        call = openai_api.make_caller("key", sdk=sdk)
        with pytest.raises(providers.ModelError) as error:
            call("sys", "user", {"type": "object"})
        assert "status=incomplete" in str(error.value)

    def test_a_missing_status_attribute_is_reported_not_ignored(self):
        # getattr(..., None) rather than a bare attribute read: a future SDK
        # that drops or renames `status` must fail loudly here rather than skip
        # the check and hand an unvalidated answer to json.loads.
        class _NoStatus:
            output_text = '{"moments": []}'
            usage = _Usage()
            output = []
            incomplete_details = None

        call = openai_api.make_caller("key", sdk=_FakeSDK(_NoStatus()))
        with pytest.raises(providers.ModelError) as error:
            call("sys", "user", {"type": "object"})
        assert "the model returned no usable answer (status=None)" in str(error.value)


class TestARefusalIsFoundBeforeTheAnswerIsRead:
    """A refusal on this API arrives inside a COMPLETED response, as a content
    part, and `Response.output_text` collects only `output_text` parts - so it
    renders as the empty string. Confirmed offline against the real SDK's own
    `output_text` property. Unchecked, a refusal is reported as a JSON syntax
    error at column 1, which says nothing about the real cause."""

    def test_a_refusal_is_reported_with_the_models_own_explanation(self):
        sdk = _FakeSDK(_Response(
            "", output=[_Message([_RefusalPart("I can't help with that.")])]))
        call = openai_api.make_caller("key", sdk=sdk)
        with pytest.raises(providers.ModelError) as error:
            call("sys", "user", {"type": "object"})
        message = str(error.value)
        assert "refused" in message
        assert "I can't help with that." in message
        # NOT the message an unchecked refusal would produce.
        assert "not valid JSON" not in message
        assert "empty answer" not in message

    def test_a_refusal_with_no_explanation_is_still_a_refusal(self):
        sdk = _FakeSDK(_Response("", output=[_Message([_RefusalPart("")])]))
        call = openai_api.make_caller("key", sdk=sdk)
        with pytest.raises(providers.ModelError) as error:
            call("sys", "user", {"type": "object"})
        assert "refused" in str(error.value)

    def test_a_refusal_beside_a_text_part_still_wins(self):
        # A response that carried both would otherwise parse the text half and
        # report success for a window the model declined to answer.
        sdk = _FakeSDK(_Response(
            '{"moments": []}',
            output=[_Message([_TextPart('{"moments": []}'),
                              _RefusalPart("policy")])]))
        call = openai_api.make_caller("key", sdk=sdk)
        with pytest.raises(providers.ModelError) as error:
            call("sys", "user", {"type": "object"})
        assert "refused" in str(error.value)

    def test_an_ordinary_answer_is_not_mistaken_for_a_refusal(self):
        sdk = _FakeSDK(_Response('{"moments": []}'))
        call = openai_api.make_caller("key", sdk=sdk)
        assert call("sys", "user", {"type": "object"}) == {"moments": []}

    def test_a_response_with_no_output_list_at_all_does_not_raise_in_the_walk(self):
        # `_refusal` reads a third-party object; a missing or None `output` is
        # not a refusal and must not become one, nor an AttributeError.
        class _NoOutput:
            output_text = '{"moments": []}'
            status = "completed"
            usage = _Usage()
            incomplete_details = None

        call = openai_api.make_caller("key", sdk=_FakeSDK(_NoOutput()))
        assert call("sys", "user", {"type": "object"}) == {"moments": []}


class TestAnEmptyAnswer:
    @pytest.mark.parametrize("text", [None, ""])
    def test_a_completed_non_refusing_response_with_no_text_says_so(self, text):
        # Without this branch, json.loads("") raises a syntax error about
        # column 1 of nothing, which the outer handler would report as "the
        # answer was not valid JSON: Expecting value" - true and useless.
        sdk = _FakeSDK(_Response(text, output=[_Message([])]))
        call = openai_api.make_caller("key", sdk=sdk)
        with pytest.raises(providers.ModelError) as error:
            call("sys", "user", {"type": "object"})
        assert "empty answer" in str(error.value)


class TestAWronglyTypedAnswer:
    def test_a_non_string_answer_says_so_and_names_the_type(self):
        # `output_text` is typed `str` by the SDK, but this module reads a
        # third-party object handed back over the wire, and a non-string,
        # non-empty value is a WRONG-TYPED answer, not an EMPTY one.
        sdk = _FakeSDK(_Response(7, output=[_Message([])]))
        call = openai_api.make_caller("key", sdk=sdk)
        with pytest.raises(providers.ModelError) as error:
            call("sys", "user", {"type": "object"})
        message = str(error.value)
        assert "non-text answer" in message
        assert "int" in message
        assert "empty" not in message


# --- The token counts -------------------------------------------------------


class TestTheTokenCountsAreThePlainFigures:
    def test_the_responses_api_field_names_are_read_not_chat_completions(self):
        usage = providers.Usage()
        sdk = _FakeSDK(_Response('{"ok": 1}', usage=_Usage(9000, 1200)))
        call = openai_api.make_caller("key", sdk=sdk, usage=usage)
        call("sys", "user", {"type": "object"})
        assert (usage.calls, usage.input_tokens, usage.output_tokens) == (1, 9000, 1200)

    def test_reasoning_tokens_are_not_added_because_they_are_already_included(self):
        # The mirror image of gemini_api's thought-token test, and the answer
        # is the OPPOSITE - established from the installed SDK, not assumed
        # (see openai_api's docstring, Fact 1): `output_tokens_details` is a
        # BREAKDOWN of `output_tokens`, so 1200 output with 300 of them
        # reasoning must land as 1200, not 1500. Summing would overstate every
        # reasoning model's bill by the whole reasoning figure.
        usage = providers.Usage()
        sdk = _FakeSDK(_Response('{"ok": 1}', usage=_Usage(9000, 1200, reasoning=300)))
        call = openai_api.make_caller("key", sdk=sdk, usage=usage)
        call("sys", "user", {"type": "object"})
        assert (usage.calls, usage.input_tokens, usage.output_tokens) == (1, 9000, 1200)

    def test_a_missing_usage_object_costs_the_bookkeeping_and_not_the_window(self):
        # `Response.usage` is `ResponseUsage | None` on this API, so the read is
        # two attributes deep and can raise. It must not cost the answer.
        class _NoUsage:
            output_text = '{"ok": 1}'
            status = "completed"
            usage = None
            output = []
            incomplete_details = None

        usage = providers.Usage()
        call = openai_api.make_caller("key", sdk=_FakeSDK(_NoUsage()), usage=usage)
        assert call("sys", "user", {"type": "object"}) == {"ok": 1}
        assert (usage.calls, usage.input_tokens, usage.output_tokens) == (1, 0, 0)


class TestThePricesAreRealModelIds:
    def test_the_default_model_is_priced(self):
        # An unpriced model reads as 0.0 downstream, which is indistinguishable
        # from free - the exact confusion PRICES' own comment exists to avoid.
        assert openai_api.DEFAULT_MODEL in openai_api.PRICES

    def test_no_price_is_zero(self):
        for model, (price_in, price_out) in openai_api.PRICES.items():
            assert price_in > 0 and price_out > 0, model

    def test_this_provider_is_labelled_verified(self):
        # This asserted `is False` until 2026-07-31, with a note saying a flip
        # must mean the bake-off ran. It did (two paid runs over this
        # workspace's own qualifying, recorded beside DEFAULT_MODEL), so this
        # is where that shows up. Flipping it back would mean the runs were
        # withdrawn, not that the flag was tidied.
        assert openai_api.VERIFIED is True


# --- The tripwire against a silent field rename -----------------------------


@pytest.mark.skipif(importlib.util.find_spec("openai") is None,
                    reason="the openai SDK is an optional dependency")
def test_the_usage_fields_this_module_reads_exist_on_the_installed_sdk():
    """Reads the installed package. No network, no key, no cost.

    WHAT THIS GUARDS: `Usage.record` swallows anything its extractor raises, by
    design - bookkeeping must never cost an operator a window. The side effect
    is that a vendor renaming `output_tokens` would zero this provider's token
    accounting with no error anywhere. This asserts the names still exist.

    WHAT IT DOES NOT GUARD, and must not be described as guarding: anything
    SEMANTIC. It cannot tell whether a count is additive or already folded into
    another - the "are reasoning tokens included in output_tokens?" question
    this module answers in its own Fact 1 is settled by reading documentation
    and, eventually, an invoice. No field-presence check of any kind can
    answer it. It also proves nothing about what the live API returns, only
    about what this SDK version declares.
    """
    from openai.types.responses.response_usage import (
        OutputTokensDetails,
        ResponseUsage,
    )
    assert "input_tokens" in ResponseUsage.model_fields
    assert "output_tokens" in ResponseUsage.model_fields
    # Read by NOBODY in openai_api, deliberately - asserted here so that if a
    # later SDK moves the reasoning figure OUT of the output breakdown (which
    # would change the answer to Fact 1), this test is the thing that fails.
    assert "output_tokens_details" in ResponseUsage.model_fields
    assert "reasoning_tokens" in OutputTokensDetails.model_fields
