import json

import pytest

from yt_shorts import providers
from yt_shorts.providers import anthropic_api


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Usage:
    def __init__(self, input_tokens, output_tokens):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _Response:
    def __init__(self, text, stop_reason="end_turn", usage=None):
        self.content = [_Block(text)]
        self.stop_reason = stop_reason
        self.stop_details = None
        if usage is not None:
            self.usage = usage


class _FakeMessages:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def create(self, **payload):
        self.calls.append(payload)
        return self._response


class _FakeSDK:
    """Stands in for the `anthropic` module. No network, no key, no cost."""

    def __init__(self, response):
        self.messages = _FakeMessages(response)
        self.last_api_key = None

    def Anthropic(self, api_key=None):        # noqa: N802 - mirrors the SDK name
        self.last_api_key = api_key
        return self


class TestLoadApiKey:
    def test_reads_a_raw_key(self, tmp_path):
        # The file as the operator created it: a bare key, despite the .json name.
        (tmp_path / "anthropic.json").write_text("sk-ant-api03-abc\n")
        assert providers.load_api_key(tmp_path, anthropic_api.KEY_FILENAME) == "sk-ant-api03-abc"

    def test_reads_a_json_object(self, tmp_path):
        (tmp_path / "anthropic.json").write_text(json.dumps({"api_key": "sk-ant-api03-xyz"}))
        assert providers.load_api_key(tmp_path, anthropic_api.KEY_FILENAME) == "sk-ant-api03-xyz"

    def test_missing_file_raises_missing_key(self, tmp_path):
        with pytest.raises(providers.MissingKey):
            providers.load_api_key(tmp_path, anthropic_api.KEY_FILENAME)

    def test_empty_file_raises_missing_key(self, tmp_path):
        (tmp_path / "anthropic.json").write_text("   \n")
        with pytest.raises(providers.MissingKey):
            providers.load_api_key(tmp_path, anthropic_api.KEY_FILENAME)

    def test_a_null_api_key_raises_missing_key(self, tmp_path):
        # `.get("api_key", "")` returns None here (the key IS present), so the
        # ""-default never applies. Without the isinstance check this used to
        # come back as the truthy literal string "None".
        (tmp_path / "anthropic.json").write_text(json.dumps({"api_key": None}))
        with pytest.raises(providers.MissingKey):
            providers.load_api_key(tmp_path, anthropic_api.KEY_FILENAME)

    def test_a_non_utf8_key_file_raises_missing_key(self, tmp_path):
        # read_text(encoding="utf-8") raises UnicodeDecodeError - a ValueError
        # subclass, not an OSError - on one stray non-UTF-8 byte. Without
        # catching it explicitly this escaped as a bare UnicodeDecodeError,
        # which _caller_from_config's `except MissingKey` does not catch, and
        # a corrupt key file aborted the whole detect run instead of
        # degrading to the lexicon engine.
        (tmp_path / "anthropic.json").write_bytes(b"\x80\x81\x82")
        with pytest.raises(providers.MissingKey) as excinfo:
            providers.load_api_key(tmp_path, anthropic_api.KEY_FILENAME)
        # The message names the path and the codec complaint, never file content.
        assert "\x80" not in str(excinfo.value)


class TestMakeCaller:
    def test_returns_the_parsed_json_object(self):
        sdk = _FakeSDK(_Response('{"moments": [{"start_line": 3}]}'))
        call = anthropic_api.make_caller("sk-ant-test", model="claude-haiku-4-5", sdk=sdk)
        assert call("sys", "user", {"type": "object"}) == {"moments": [{"start_line": 3}]}

    def test_sends_the_schema_and_no_effort(self):
        # `effort` is rejected outright by claude-haiku-4-5; sending it would
        # turn every window into a 400.
        sdk = _FakeSDK(_Response("{}"))
        call = anthropic_api.make_caller("sk-ant-test", model="claude-haiku-4-5", sdk=sdk)
        call("sys", "user", {"type": "object", "x": 1})
        payload = sdk.messages.calls[0]
        assert payload["model"] == "claude-haiku-4-5"
        assert payload["system"] == "sys"
        assert payload["output_config"]["format"]["schema"] == {"type": "object", "x": 1}
        assert "effort" not in json.dumps(payload.get("output_config", {}))
        assert "thinking" not in payload

    def test_records_the_apis_own_token_counts(self):
        usage = providers.Usage()
        sdk = _FakeSDK(_Response('{"moments": []}', usage=_Usage(9000, 1200)))
        call = anthropic_api.make_caller("sk-ant-test", model="claude-haiku-4-5",
                                        sdk=sdk, usage=usage)
        call("sys", "user", {"type": "object"})
        call("sys", "user", {"type": "object"})
        assert (usage.calls, usage.input_tokens, usage.output_tokens) == (2, 18000, 2400)

    def test_records_the_tokens_of_a_window_that_then_failed(self):
        # The whole point of recording before the response is READ: a refusal
        # still cost money. Recording on the success path only would
        # under-report a run by exactly the windows that went wrong - the
        # ones an operator most wants explained when a bill surprises them.
        usage = providers.Usage()
        sdk = _FakeSDK(_Response("", stop_reason="refusal", usage=_Usage(9000, 40)))
        call = anthropic_api.make_caller("sk-ant-test", model="claude-haiku-4-5",
                                        sdk=sdk, usage=usage)
        with pytest.raises(providers.ModelError):
            call("sys", "user", {"type": "object"})
        assert (usage.calls, usage.input_tokens) == (1, 9000)

    def test_an_unreadable_usage_costs_the_bookkeeping_not_the_window(self):
        # A response with no usage attribute at all, and one whose usage
        # raises on access. Both must still return the parsed answer: losing
        # a count is acceptable, losing a window over a count is not.
        class _Exploding:
            @property
            def input_tokens(self):
                raise RuntimeError("no usage here")

        for response in (_Response('{"ok": 1}'),
                         _Response('{"ok": 1}', usage=_Exploding())):
            usage = providers.Usage()
            call = anthropic_api.make_caller("sk-ant-test", model="claude-haiku-4-5",
                                            sdk=_FakeSDK(response), usage=usage)
            assert call("sys", "user", {"type": "object"}) == {"ok": 1}
            assert (usage.calls, usage.input_tokens) == (1, 0)

    def test_no_usage_argument_leaves_the_caller_working(self):
        # moment_scan.scan takes an unconstrained caller; usage is optional
        # precisely so every existing call site and test double is unaffected.
        sdk = _FakeSDK(_Response('{"ok": 1}', usage=_Usage(5, 5)))
        call = anthropic_api.make_caller("sk-ant-test", model="claude-haiku-4-5", sdk=sdk)
        assert call("sys", "user", {"type": "object"}) == {"ok": 1}

    def test_a_refusal_raises_before_content_is_read(self):
        sdk = _FakeSDK(_Response("", stop_reason="refusal"))
        call = anthropic_api.make_caller("sk-ant-test", model="claude-haiku-4-5", sdk=sdk)
        with pytest.raises(providers.ModelError) as error:
            call("sys", "user", {"type": "object"})
        assert "refusal" in str(error.value)

    def test_a_truncated_response_raises_model_error(self):
        sdk = _FakeSDK(_Response('{"moments": [', stop_reason="max_tokens"))
        call = anthropic_api.make_caller("sk-ant-test", model="claude-haiku-4-5", sdk=sdk)
        with pytest.raises(providers.ModelError):
            call("sys", "user", {"type": "object"})

    def test_the_key_never_appears_in_an_error_message(self):
        secret = "sk-ant-api03-SUPERSECRET"
        sdk = _FakeSDK(_Response("", stop_reason="refusal"))
        call = anthropic_api.make_caller(secret, model="claude-haiku-4-5", sdk=sdk)
        with pytest.raises(providers.ModelError) as error:
            call("sys", "user", {"type": "object"})
        assert secret not in str(error.value)

    def test_an_unwrapped_sdk_exception_is_wrapped_without_the_key(self):
        # An auth error, a rate limit, a connection failure - anything the SDK
        # raises that this module does not specifically expect - must still
        # come out as a ModelError, and the SDK's own exception text (which
        # can embed the request, and therefore the key) must not survive into
        # the new message. Only the exception's type name may.
        secret = "sk-ant-api03-SUPERSECRET"

        class _FakeMessagesThatRaise:
            def create(self, **payload):
                raise RuntimeError(f"401 unauthorized for key {secret}")

        class _FakeSDKThatRaises:
            def Anthropic(self, api_key=None):        # noqa: N802 - mirrors the SDK name
                return self

            messages = _FakeMessagesThatRaise()

        sdk = _FakeSDKThatRaises()
        call = anthropic_api.make_caller(secret, model="claude-haiku-4-5", sdk=sdk)
        with pytest.raises(providers.ModelError) as error:
            call("sys", "user", {"type": "object"})
        assert secret not in str(error.value)
        assert "RuntimeError" in str(error.value)
        assert error.value.__cause__ is not None
        assert isinstance(error.value.__cause__, RuntimeError)

    def test_an_unwrapped_response_reading_exception_is_wrapped_without_the_key(self):
        # Everything after messages.create() - the stop_reason read, the
        # response.content join, json.loads - used to sit outside any
        # handler. Reproduced with a fake SDK whose `response.content` raises
        # on attribute access: the raw AttributeError text reached the caller
        # verbatim, key-shaped substring and all, as a bare AttributeError.
        secret = "sk-ant-api03-SUPERSECRET"

        # `.content` is a property so it raises only when READ (inside `call`,
        # after messages.create() has already returned), not at construction.
        class _ResponseThatExplodesOnContent:
            stop_reason = "end_turn"
            stop_details = None

            @property
            def content(self):
                raise AttributeError(f"sekrit {secret} in some sdk message")

        class _FakeMessagesResponseExplodes:
            def create(self, **payload):
                return _ResponseThatExplodesOnContent()

        class _FakeSDKResponseExplodes:
            def Anthropic(self, api_key=None):        # noqa: N802 - mirrors the SDK name
                return self
            messages = _FakeMessagesResponseExplodes()

        sdk = _FakeSDKResponseExplodes()
        call = anthropic_api.make_caller(secret, model="claude-haiku-4-5", sdk=sdk)
        with pytest.raises(providers.ModelError) as error:
            call("sys", "user", {"type": "object"})
        assert secret not in str(error.value)
        assert "sk-ant-" not in str(error.value)
        assert "AttributeError" in str(error.value)

    def test_the_constructor_exception_is_wrapped_without_the_key(self):
        # If the Anthropic() constructor raises (bad key, bad config, etc.) its
        # exception message may embed the API key. This must be wrapped into a
        # ModelError the same way the messages.create() call is, so the key never
        # reaches a logger that catches exceptions at a higher level.
        secret = "sk-ant-api03-SUPERSECRET"

        class _FakeSDKConstructorRaises:
            def Anthropic(self, api_key=None):        # noqa: N802 - mirrors the SDK name
                raise ValueError(f"bad config for key {secret}")

        sdk = _FakeSDKConstructorRaises()
        with pytest.raises(providers.ModelError) as error:
            anthropic_api.make_caller(secret, model="claude-haiku-4-5", sdk=sdk)
        assert secret not in str(error.value)
        assert "ValueError" in str(error.value)
        assert "building the client" in str(error.value)
        assert error.value.__cause__ is not None
        assert isinstance(error.value.__cause__, ValueError)


class TestRequire:
    """`providers.require` replaced `_anthropic.require`, and takes four plain
    strings rather than a module - so an absent package is expressed by NAMING
    one that is not installed, with nothing monkeypatched."""

    def test_raises_with_an_install_message_when_missing(self):
        with pytest.raises(providers.SdkUnavailable) as error:
            providers.require("yt_shorts_no_such_sdk", anthropic_api.INSTALL,
                              "moment detection", anthropic_api.PROVIDER_ID)
        assert "pip install" in str(error.value)
        assert "moment detection" in str(error.value)
        assert "anthropic" in str(error.value)

    def test_returns_quietly_when_present(self):
        # `json` stands in for an installed SDK: `require` only asks whether a
        # package is importable, and asking about the stdlib keeps this test
        # honest whether or not `anthropic` is in this venv.
        providers.require("json", anthropic_api.INSTALL, "moment detection",
                          anthropic_api.PROVIDER_ID)   # no raise

    def test_sdk_installed_answers_without_importing(self):
        import sys
        assert providers.sdk_installed("json") is True
        assert providers.sdk_installed("yt_shorts_no_such_sdk") is False
        # A dotted name whose PARENT does not exist must answer False rather
        # than escaping as a ModuleNotFoundError from find_spec's own import
        # of that parent - the shape `google.genai` has in a venv with no
        # google package at all.
        assert providers.sdk_installed("yt_shorts_no_such_sdk.sub") is False
        assert "yt_shorts_no_such_sdk" not in sys.modules
