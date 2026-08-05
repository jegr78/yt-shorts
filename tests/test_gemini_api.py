"""What is true of GEMINI specifically, and nothing the contract already pins.

`tests/test_provider_contract.py` runs nine behavioural properties against
every registered provider - the three key-secrecy wrapping points, the usage
accounting, the JSON handling, the key file. None of that is repeated here.
What is left is the vendor's own shape: the exact payload this module sends,
and the two failure modes that exist because `Interaction.status` and
`Interaction.output_text` are named and nullable the way they are.

No network, no key, no cost: every test drives an injected fake.
"""

from __future__ import annotations

import importlib.util
import warnings

import pytest

from yt_shorts import providers
from yt_shorts.providers import gemini_api


class _Usage:
    def __init__(self, reported_in=None, reported_out=None, reported_thought=None):
        self.total_input_tokens = reported_in
        self.total_output_tokens = reported_out
        self.total_thought_tokens = reported_thought


class _Interaction:
    def __init__(self, text, status="completed", usage=None):
        self.output_text = text
        self.status = status
        self.usage = usage if usage is not None else _Usage()


class _FakeInteractions:
    def __init__(self, interaction):
        self._interaction = interaction
        self.calls: list[dict] = []

    def create(self, **payload):
        self.calls.append(payload)
        return self._interaction


class _FakeSDK:
    """Stands in for the `google.genai` module. No network, no key, no cost."""

    def __init__(self, interaction):
        self.interactions = _FakeInteractions(interaction)
        self.last_api_key = None

    def Client(self, api_key=None):        # noqa: N802 - mirrors the SDK name
        self.last_api_key = api_key
        return self


class TestThePayload:
    """The four keyword names and the two nested ones were read off the
    installed SDK (`_gaos.google_genai._CREATE_BODY_KEYS` plus
    `GenerationConfig.model_fields`) and confirmed by serialising a real
    request offline. Pinning them here is what catches a rename in a later
    google-genai: an unknown keyword is a TypeError from the SDK's own
    `_normalize_create_body`, which this module would report as a
    type-name-only ModelError - true, but useless for finding out why."""

    def test_sends_the_schema_the_system_and_the_input_where_the_sdk_wants_them(self):
        sdk = _FakeSDK(_Interaction("{}"))
        call = gemini_api.make_caller("key", model="gemini-2.5-flash", sdk=sdk)
        call("sys", "the window", {"type": "object", "x": 1})
        payload = sdk.interactions.calls[0]
        assert payload["model"] == "gemini-2.5-flash"
        assert payload["system_instruction"] == "sys"
        assert payload["input"] == "the window"
        assert payload["response_format"] == {
            "type": "text", "mime_type": "application/json",
            "schema": {"type": "object", "x": 1}}

    def test_max_tokens_rides_in_generation_config_not_at_the_top_level(self):
        # Anthropic takes `max_tokens=` as its own keyword; this API has no
        # such top-level key at all, and a top-level `max_output_tokens` is
        # rejected outright by the SDK. The bound would silently vanish.
        sdk = _FakeSDK(_Interaction("{}"))
        call = gemini_api.make_caller("key", max_tokens=1234, sdk=sdk)
        call("sys", "user", {"type": "object"})
        payload = sdk.interactions.calls[0]
        assert payload["generation_config"] == {"max_output_tokens": 1234}
        assert "max_tokens" not in payload
        assert "max_output_tokens" not in payload

    def test_the_key_reaches_the_client_and_nothing_else(self):
        sdk = _FakeSDK(_Interaction("{}"))
        gemini_api.make_caller("the-secret-key", sdk=sdk)
        assert sdk.last_api_key == "the-secret-key"


class TestTheStatusIsCheckedFirst:
    """`status` is this API's `stop_reason`, and `completed` is its only
    success value for the non-streaming, tool-less call this module makes."""

    @pytest.mark.parametrize("status", ["incomplete", "failed", "cancelled",
                                        "budget_exceeded", "requires_action",
                                        "in_progress", "queued"])
    def test_any_status_but_completed_is_a_model_error_naming_it(self, status):
        # `output_text` is None on all of these, exactly as the real API
        # returns it - which is the whole reason the check comes first.
        sdk = _FakeSDK(_Interaction(None, status=status))
        call = gemini_api.make_caller("key", sdk=sdk)
        with pytest.raises(providers.ModelError) as error:
            call("sys", "user", {"type": "object"})
        # The status the API actually returned, not this module's reading of
        # what it means - `incomplete` is truncation and `failed` may be a
        # safety refusal, but neither is claimed without a measurement.
        assert status in str(error.value)

    def test_the_status_check_beats_the_json_error_on_a_truncated_answer(self):
        # A truncated answer is BOTH a bad status and unparseable JSON. The
        # status wins because it explains the cause; "Expecting ',' delimiter"
        # does not. Reversing the two reads would swap the message silently.
        sdk = _FakeSDK(_Interaction('{"moments": [', status="incomplete"))
        call = gemini_api.make_caller("key", sdk=sdk)
        with pytest.raises(providers.ModelError) as error:
            call("sys", "user", {"type": "object"})
        assert "incomplete" in str(error.value)
        assert "not valid JSON" not in str(error.value)

    def test_a_missing_status_attribute_is_reported_not_ignored(self):
        # getattr(..., None) rather than a bare attribute read: a future SDK
        # that drops or renames `status` must fail loudly here rather than
        # skip the check and hand an unvalidated answer to json.loads.
        class _NoStatus:
            output_text = '{"moments": []}'
            usage = _Usage()

        call = gemini_api.make_caller("key", sdk=_FakeSDK(_NoStatus()))
        with pytest.raises(providers.ModelError) as error:
            call("sys", "user", {"type": "object"})
        # Not just "None" anywhere in the message (many unrelated messages
        # would contain that substring) - the exact status-check phrasing
        # naming the missing attribute's getattr default, which is what
        # distinguishes "the status check fired on a missing attribute" from
        # any other failure this function can raise.
        assert "the model returned no usable answer (status=None)" in str(error.value)


class TestAnEmptyAnswer:
    @pytest.mark.parametrize("text", [None, ""])
    def test_a_completed_interaction_with_no_text_says_so(self, text):
        # `output_text` is `str | None` on this API - it is populated only when
        # the model produced a text part, so an image-only or empty completion
        # reaches here as None. Without this branch json.loads(None) raises a
        # TypeError about argument types, which the outer handler would report
        # as "the Gemini SDK raised TypeError reading the response".
        sdk = _FakeSDK(_Interaction(text))
        call = gemini_api.make_caller("key", sdk=sdk)
        with pytest.raises(providers.ModelError) as error:
            call("sys", "user", {"type": "object"})
        assert "empty answer" in str(error.value)


class TestAWronglyTypedAnswer:
    """`output_text=7` is not documented as possible by the SDK's own types
    (`str | None`), but this module reads a third-party object, and a
    non-string, non-empty value is a WRONG-TYPED answer, not an EMPTY one -
    misfiled under TestAnEmptyAnswer in an earlier draft of this suite. The
    branch in gemini_api.py is likewise now its own `if`, with its own
    message, rather than sharing "empty answer" with a case that isn't."""

    def test_a_non_string_answer_says_so_and_names_the_type(self):
        sdk = _FakeSDK(_Interaction(7))
        call = gemini_api.make_caller("key", sdk=sdk)
        with pytest.raises(providers.ModelError) as error:
            call("sys", "user", {"type": "object"})
        message = str(error.value)
        assert "non-text answer" in message
        assert "int" in message
        assert "empty" not in message


class TestTheTokenCountsAreTheTotals:
    def test_the_totals_are_read_not_anthropics_field_names(self):
        usage = providers.Usage()
        sdk = _FakeSDK(_Interaction('{"ok": 1}', usage=_Usage(9000, 1200)))
        call = gemini_api.make_caller("key", sdk=sdk, usage=usage)
        call("sys", "user", {"type": "object"})
        assert (usage.calls, usage.input_tokens, usage.output_tokens) == (1, 9000, 1200)

    def test_null_token_counts_cost_the_bookkeeping_and_not_the_window(self):
        # Every field on this API's Usage is `int | None`, unlike Anthropic's.
        # A response reporting none of them must still return its answer.
        usage = providers.Usage()
        sdk = _FakeSDK(_Interaction('{"ok": 1}', usage=_Usage(None, None)))
        call = gemini_api.make_caller("key", sdk=sdk, usage=usage)
        assert call("sys", "user", {"type": "object"}) == {"ok": 1}
        assert (usage.calls, usage.input_tokens, usage.output_tokens) == (1, 0, 0)

    def test_thought_tokens_are_summed_into_the_output_count(self):
        # C1: thought tokens are a SEPARATE line item Google bills and reports
        # beside `total_output_tokens`, not folded into it - see the module
        # docstring's Fact 1. Reported: 1200 output + 300 thought must land as
        # 1500 in `usage.output_tokens`, not 1200 - a provider that dropped the
        # thought count would silently under-report every run's real cost.
        usage = providers.Usage()
        sdk = _FakeSDK(_Interaction(
            '{"ok": 1}', usage=_Usage(9000, 1200, reported_thought=300)))
        call = gemini_api.make_caller("key", sdk=sdk, usage=usage)
        call("sys", "user", {"type": "object"})
        assert (usage.calls, usage.input_tokens, usage.output_tokens) == (1, 9000, 1500)

    def test_a_null_thought_count_beside_a_real_output_count_costs_nothing(self):
        # None-tolerance, half 1: a response that reports ordinary output
        # tokens but no thought tokens at all (the field genuinely absent, not
        # zero) must not raise and must not lose the output count it DID get -
        # the whole reason the `or 0` sits inside the lambda rather than being
        # left to Usage.record's own tolerance (see the lambda's own comment).
        usage = providers.Usage()
        sdk = _FakeSDK(_Interaction(
            '{"ok": 1}', usage=_Usage(9000, 1200, reported_thought=None)))
        call = gemini_api.make_caller("key", sdk=sdk, usage=usage)
        call("sys", "user", {"type": "object"})
        assert (usage.calls, usage.input_tokens, usage.output_tokens) == (1, 9000, 1200)

    def test_a_null_output_count_beside_a_real_thought_count_still_counts_the_thoughts(self):
        # None-tolerance, half 2: the mirror case - no ordinary output tokens
        # reported, but thought tokens were. The thought count must still
        # reach `usage.output_tokens` rather than being lost alongside the
        # missing output figure.
        usage = providers.Usage()
        sdk = _FakeSDK(_Interaction(
            '{"ok": 1}', usage=_Usage(9000, None, reported_thought=300)))
        call = gemini_api.make_caller("key", sdk=sdk, usage=usage)
        call("sys", "user", {"type": "object"})
        assert (usage.calls, usage.input_tokens, usage.output_tokens) == (1, 9000, 300)


class TestThePricesAreRealModelIds:
    def test_the_default_model_is_priced(self):
        # An unpriced model reads as 0.0 downstream, which is indistinguishable
        # from free - the exact confusion PRICES' own comment exists to avoid.
        assert gemini_api.DEFAULT_MODEL in gemini_api.PRICES

    def test_no_price_is_zero(self):
        for model, (price_in, price_out) in gemini_api.PRICES.items():
            assert price_in > 0 and price_out > 0, model


@pytest.mark.skipif(importlib.util.find_spec("google.genai") is None,
                    reason="the google-genai SDK is an optional dependency")
def test_the_usage_fields_this_module_reads_exist_on_the_installed_sdk():
    """Reads the installed package. No network, no key, no cost.

    WHAT THIS GUARDS: `Usage.record` swallows anything its extractor raises, by
    design - bookkeeping must never cost an operator a window. The side effect
    is that a vendor renaming `total_output_tokens` would zero this provider's
    token accounting with no error anywhere. This asserts the three names
    `gemini_api` reads still exist.

    WHAT IT DOES NOT GUARD, and must not be described as guarding: anything
    SEMANTIC. It cannot tell whether a count is additive or already folded into
    another - the "are thought tokens included in total_output_tokens?"
    question this module answers in its own Fact 1 is settled by reading
    documentation and, eventually, an invoice. No field-presence check of any
    kind can answer it. It also proves nothing about what the live API returns,
    only about what this SDK version declares.
    """
    # Scoped rather than added to pytest.ini's filterwarnings: this is the only
    # place in the whole suite that imports `google.genai`, and importing it on
    # Python 3.14 emits a DeprecationWarning from inside `google/genai/types.py`
    # about `_UnionGenericAlias`. Muting it globally would widen a list whose
    # stated job is to stay narrow so anything NEW still surfaces; muting it
    # here mutes exactly the one import that causes it and nothing else. It is
    # a third-party notice about a package this project only imports lazily -
    # it says nothing about this suite, and it is not hidden from an operator,
    # who still sees it the first time a real detect run loads the SDK.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from google.genai.interactions import Usage
    for field in ("total_input_tokens", "total_output_tokens",
                  "total_thought_tokens"):
        assert field in Usage.model_fields, field
