"""One suite, every provider, the same properties.

This file is the proof behind the claim that a provider is a file rather than
a rebuild: a fourth provider inherits all of it the moment it enters
`providers.PROVIDERS`. In particular it inherits the key-secrecy rules, which
took two review rounds to get right on the first provider - the
response-READING path was missed twice, and a fake SDK whose
`response.content` raised with the key in its text reached the caller with the
key intact.

Nothing here imports a vendor SDK, reads a real key, touches the network or
spends a cent. Every provider is exercised through its injected `sdk`.

CURRENT STATE: all three registered providers - `anthropic`, `gemini`,
`openai` - implement `make_caller`, and every parameterisation in this file is
expected to PASS. There is no skip, xfail or de-parameterisation hiding a red
result anywhere here; if a parameterisation goes red, that is a real
regression in that provider, not expected groundwork, and must not be worked
around by skipping it to make the run look clean. (Earlier in this project's
history, before Tasks 2 and 3 landed, `gemini` and `openai` were stubs and
their parameterisations failed with `NotImplementedError` on purpose - that
phase is over; do not reintroduce it.)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from yt_shorts import moment_scan, providers
from yt_shorts.providers import _shared

KEY = "sk-secret-DO-NOT-LEAK-0123456789"
ALL_PROVIDERS = pytest.mark.parametrize(
    "provider", providers.ordered(), ids=lambda m: m.PROVIDER_ID)

REPO_ROOT = Path(__file__).resolve().parent.parent


class Boom(Exception):
    """Carries the key in its text, exactly as a real SDK's exception can: its
    message quotes the request, and the request carries the key."""

    def __init__(self, where: str) -> None:
        super().__init__(f"failed {where} with api_key={KEY} in the message")


# --- The per-provider fakes -------------------------------------------------
#
# One factory per vendor, because the three SDKs have different shapes. Each
# takes the same five keyword arguments so the behavioural tests below can be
# written once and parameterised:
#
#   fail_at  "client" | "request" | "response" - where Boom is raised
#   answer   the text the model "returns" (default: a valid empty answer)
#   usage    (input, output), or the string "explode" for usage that raises
#   finish   the vendor's own stop/finish reason, when a test needs one
#
# All three registered providers have their entry below. A future FOURTH
# provider adds its own fake here, in this same shape, and nothing else in
# this file changes - `_fake_for` and `_NoFakeYet` below already handle a
# provider that has entered `providers.PROVIDERS` but has no entry here yet.


def _anthropic_fake(*, fail_at=None, answer=None, usage=(11, 22), finish=None):
    text = '{"moments": []}' if answer is None else answer

    class _Block:
        type = "text"

        def __init__(self, value):
            self.text = value

    class _Usage:
        def __init__(self, reported_in, reported_out):
            self.input_tokens = reported_in
            self.output_tokens = reported_out

    class _ExplodingUsage:
        @property
        def input_tokens(self):
            raise RuntimeError(f"no usage here, api_key={KEY}")

        @property
        def output_tokens(self):
            raise RuntimeError(f"no usage here, api_key={KEY}")

    class _Response:
        stop_reason = finish or "end_turn"

        def __init__(self):
            self.usage = (_ExplodingUsage() if usage == "explode"
                          else _Usage(*usage))

        @property
        def content(self):
            if fail_at == "response":
                raise Boom("reading the response")
            return [_Block(text)]

    class _Messages:
        def create(self, **payload):
            if fail_at == "request":
                raise Boom("sending the request")
            return _Response()

    class _SDK:
        messages = _Messages()

        def Anthropic(self, api_key=None):    # noqa: N802 - mirrors the SDK name
            if fail_at == "client":
                raise Boom("building the client")
            return self

    return _SDK()


def _gemini_fake(*, fail_at=None, answer=None, usage=(11, 22), finish=None):
    # Shaped like google-genai's Interactions API, not like Anthropic's: the
    # client is `module.Client(api_key=...)`, the call is
    # `client.interactions.create(...)`, the answer is `interaction.output_text`,
    # the token counts are `usage.total_input_tokens`/`.total_output_tokens`,
    # and the vendor's own stop reason is `interaction.status` (whose one
    # success value is "completed").
    text = '{"moments": []}' if answer is None else answer

    class _Usage:
        def __init__(self, reported_in, reported_out):
            self.total_input_tokens = reported_in
            self.total_output_tokens = reported_out
            # Real `google.genai.interactions.Usage` also carries
            # `total_thought_tokens` (int | None) - a separate line item
            # gemini_api sums into its output count (see that module's own
            # docstring, Fact 1). Zero here so this generic contract fixture,
            # shared across providers, keeps the plain `usage=(in, out)`
            # 2-tuple call sites unaffected; the gemini-specific accumulation
            # of a NON-zero thought count is pinned in tests/test_gemini_api.py
            # instead, against a fake that models this field on purpose.
            self.total_thought_tokens = 0

    class _ExplodingUsage:
        @property
        def total_input_tokens(self):
            raise RuntimeError(f"no usage here, api_key={KEY}")

        @property
        def total_output_tokens(self):
            raise RuntimeError(f"no usage here, api_key={KEY}")

        @property
        def total_thought_tokens(self):
            raise RuntimeError(f"no usage here, api_key={KEY}")

    class _Interaction:
        status = finish or "completed"

        def __init__(self):
            self.usage = (_ExplodingUsage() if usage == "explode"
                          else _Usage(*usage))

        @property
        def output_text(self):
            # A property, so it raises only when READ - inside `call`, after
            # `interactions.create` has already returned, and after the status
            # check has already passed. That is the attribute the provider
            # reads for the answer, so it is the one the response-reading leak
            # test has to come through.
            if fail_at == "response":
                raise Boom("reading the response")
            return text

    class _Interactions:
        def create(self, **payload):
            if fail_at == "request":
                raise Boom("sending the request")
            return _Interaction()

    class _SDK:
        interactions = _Interactions()

        def Client(self, api_key=None):        # noqa: N802 - mirrors the SDK name
            if fail_at == "client":
                raise Boom("building the client")
            return self

    return _SDK()


def _openai_fake(*, fail_at=None, answer=None, usage=(11, 22), finish=None):
    # Shaped like openai's Responses API, not like the other two: the client is
    # `module.OpenAI(api_key=...)`, the call is `client.responses.create(...)`,
    # the answer is `response.output_text`, the token counts are
    # `usage.input_tokens`/`.output_tokens`, and the vendor's own stop reason is
    # `response.status` (whose one success value is "completed").
    #
    # `output` is modelled as well as `output_text`, and not as decoration:
    # openai_api walks it looking for a `refusal` content part BEFORE it reads
    # `output_text`, because a refusal arrives inside a COMPLETED response and
    # `output_text` renders it as the empty string. A fake with no `output` at
    # all would let that walk pass by accident rather than by agreement.
    text = '{"moments": []}' if answer is None else answer

    class _Usage:
        def __init__(self, reported_in, reported_out):
            self.input_tokens = reported_in
            self.output_tokens = reported_out
            # Real `ResponseUsage` also carries `output_tokens_details.
            # reasoning_tokens`, which is a BREAKDOWN of `output_tokens` rather
            # than a line item beside it - so openai_api reads the plain figure
            # and must NOT add this in (the opposite of gemini_api's thought
            # tokens; see openai_api's docstring, Fact 1). Modelled with a
            # non-zero value on purpose: a provider that summed it would show
            # up here as a wrong total rather than passing unnoticed.
            self.output_tokens_details = _Details(7)

    class _Details:
        def __init__(self, reasoning):
            self.reasoning_tokens = reasoning

    class _ExplodingUsage:
        @property
        def input_tokens(self):
            raise RuntimeError(f"no usage here, api_key={KEY}")

        @property
        def output_tokens(self):
            raise RuntimeError(f"no usage here, api_key={KEY}")

        @property
        def output_tokens_details(self):
            raise RuntimeError(f"no usage here, api_key={KEY}")

    class _TextPart:
        type = "output_text"

        def __init__(self, value):
            self.text = value

    class _Message:
        type = "message"

        def __init__(self, value):
            self.content = [_TextPart(value)]

    class _Response:
        status = finish or "completed"
        incomplete_details = None

        def __init__(self):
            self.usage = (_ExplodingUsage() if usage == "explode"
                          else _Usage(*usage))
            self.output = [_Message(text)]

        @property
        def output_text(self):
            # A property, so it raises only when READ - inside `call`, after
            # `responses.create` has already returned, and after the status and
            # refusal checks have already passed. That is the attribute the
            # provider reads for the answer, so it is the one the
            # response-reading leak test has to come through.
            if fail_at == "response":
                raise Boom("reading the response")
            return text

    class _Responses:
        def create(self, **payload):
            if fail_at == "request":
                raise Boom("sending the request")
            return _Response()

    class _SDK:
        responses = _Responses()

        def OpenAI(self, api_key=None):        # noqa: N802 - mirrors the SDK name
            if fail_at == "client":
                raise Boom("building the client")
            return self

    return _SDK()


FAKES: dict[str, Callable] = {
    "anthropic": _anthropic_fake,
    "gemini": _gemini_fake,
    "openai": _openai_fake,
}


class _NoFakeYet:
    """Stands in where FAKES has no entry, and complains only when TOUCHED.

    Deliberately not an immediate failure: a stub `make_caller` raises
    NotImplementedError before it looks at `sdk` at all, so the test reports
    the real reason a not-yet-implemented provider is red - the module still
    has work to do - rather than a missing test fixture. A provider whose
    `make_caller` IS implemented reaches this object, and then says exactly
    what is missing: a FAKES entry the provider's own task never added. (This
    is what `gemini` and `openai` hit before Tasks 2 and 3 gave them fakes;
    kept as generic, provider-agnostic machinery for whichever provider is
    next, not as a description of either module's current state - both are
    implemented and have real fakes below.)
    """

    def __init__(self, provider_id: str) -> None:
        self._provider_id = provider_id

    def __getattr__(self, name):
        raise AssertionError(
            f"no fake SDK for {self._provider_id} in FAKES (asked for {name!r}) "
            f"- a registered provider must be exercised by this suite")


def _fake_for(provider, **kwargs):
    """The fake for this provider, or a placeholder that says so when used."""
    factory = FAKES.get(provider.PROVIDER_ID)
    if factory is None:
        return _NoFakeYet(provider.PROVIDER_ID)
    return factory(**kwargs)


# --- The contract itself ----------------------------------------------------


@ALL_PROVIDERS
def test_the_module_exposes_the_whole_contract(provider):
    for name in providers.CONTRACT:
        assert hasattr(provider, name), f"{provider.PROVIDER_ID} lacks {name}"
    assert isinstance(provider.PROVIDER_ID, str) and provider.PROVIDER_ID
    assert provider.KEY_FILENAME == f"{provider.PROVIDER_ID}.json"
    assert isinstance(provider.DEFAULT_MODEL, str) and provider.DEFAULT_MODEL
    assert isinstance(provider.PRICES, dict)
    for model, prices in provider.PRICES.items():
        assert isinstance(model, str)
        assert len(prices) == 2 and all(isinstance(p, float) for p in prices)
    assert isinstance(provider.PACKAGE, str) and provider.PACKAGE
    assert isinstance(provider.INSTALL, str) and provider.INSTALL
    assert isinstance(provider.VERIFIED, bool)
    assert callable(provider.make_caller)


def test_the_registry_is_the_three_modules_default_first():
    assert providers.DEFAULT_PROVIDER in providers.PROVIDERS
    assert [m.PROVIDER_ID for m in providers.ordered()][0] == providers.DEFAULT_PROVIDER
    # ordered() is the display order the Settings screen renders: the default
    # first, then the rest alphabetically - never dict insertion order.
    rest = [m.PROVIDER_ID for m in providers.ordered()][1:]
    assert rest == sorted(rest)
    assert set(m.PROVIDER_ID for m in providers.ordered()) == set(providers.PROVIDERS)
    # The SET equality above is not enough on its own: PROVIDERS is built by a
    # dict comprehension keyed on PROVIDER_ID, so a fourth module that
    # copy-pastes an existing id would silently COLLAPSE the registry - the
    # duplicate would shadow the real entry and every set-based assertion
    # would still pass. ordered() walks every module in the underlying tuple
    # with no such de-duplication, so its LENGTH catches what the set cannot:
    # a duplicate id makes len(ordered()) > len(PROVIDERS).
    assert len(providers.ordered()) == len(providers.PROVIDERS)


def test_an_unknown_id_is_reported_not_answered_with_the_default():
    with pytest.raises(providers.UnknownProvider) as caught:
        providers.get("anthropik")
    assert "anthropik" in str(caught.value)
    # An unhashable id is a typo too, not a TypeError escaping the registry.
    with pytest.raises(providers.UnknownProvider):
        providers.get(["anthropic"])


def test_usage_record_rejects_a_bool_and_clamps_a_negative():
    # `_count` (private to _shared, exercised here through the public
    # Usage.record) has two defensive branches with no coverage otherwise:
    # `isinstance(value, bool)` is rejected even though bool is an int
    # subclass - a bool here means the extraction read the wrong field - and
    # a negative int is clamped to 0 rather than recorded as reported. Both
    # are NEW in this task (the pre-move `_count` accepted True as 1); drop
    # either branch and the suite stays green without this test.
    usage = providers.Usage()
    usage.record(lambda: (True, -5))
    assert usage.calls == 1
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0


def test_importing_the_package_pulls_in_no_vendor_sdk():
    # The whole point of lazy SDK imports: a venv with none of them installed
    # must still start, render and transcribe. Written ONCE rather than per
    # provider - it is a property of importing the package, not of a module.
    #
    # The vendor set is DERIVED from providers.ordered() rather than
    # hardcoded, so a fourth provider inherits this check the instant it
    # enters PROVIDERS - which is exactly what this file's own module
    # docstring claims happens. A hardcoded {'anthropic','openai','google'}
    # would silently stop covering a later provider with a module-scope SDK
    # import; deriving it cannot.
    #
    # The environment is inherited and only PYTHONPATH is set: a stripped env
    # (no PATH, no HOME) does not run this interpreter reliably, and the check
    # is about what `import yt_shorts.providers` pulls in, not about what a
    # bare environment can do.
    vendors = {module.PACKAGE.split(".")[0] for module in providers.ordered()}
    code = (
        "import sys;"
        "import yt_shorts.providers;"
        f"bad=[n for n in sys.modules if n.split('.')[0] in {vendors!r}];"
        "print(bad)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        cwd=REPO_ROOT, env={**os.environ, "PYTHONPATH": "src"}, check=True)
    assert result.stdout.strip() == "[]", result.stdout


# --- The behavioural properties, once per provider --------------------------


@ALL_PROVIDERS
def test_a_failure_building_the_client_is_wrapped_without_the_key(provider):
    fake = _fake_for(provider, fail_at="client")
    with pytest.raises(providers.ModelError) as caught:
        provider.make_caller(KEY, sdk=fake)
    assert KEY not in str(caught.value)
    assert "Boom" in str(caught.value)


@ALL_PROVIDERS
def test_a_failure_sending_the_request_is_wrapped_without_the_key(provider):
    call = provider.make_caller(KEY, sdk=_fake_for(provider, fail_at="request"))
    with pytest.raises(providers.ModelError) as caught:
        call("system", "user", moment_scan.SCHEMA)
    assert KEY not in str(caught.value)
    assert "Boom" in str(caught.value)


@ALL_PROVIDERS
def test_a_failure_reading_the_response_is_wrapped_without_the_key(provider):
    # The one that was missed twice. The response object was built from the
    # request, so anything it raises can carry the key.
    call = provider.make_caller(KEY, sdk=_fake_for(provider, fail_at="response"))
    with pytest.raises(providers.ModelError) as caught:
        call("system", "user", moment_scan.SCHEMA)
    assert KEY not in str(caught.value)
    assert "Boom" in str(caught.value)


@ALL_PROVIDERS
def test_a_non_json_answer_is_a_model_error(provider):
    call = provider.make_caller(KEY, sdk=_fake_for(provider, answer="not json"))
    with pytest.raises(providers.ModelError):
        call("system", "user", moment_scan.SCHEMA)


@ALL_PROVIDERS
def test_the_caller_accepts_moment_scans_own_schema_and_returns_its_answer(provider):
    payload = {"moments": [{"start_line": 1, "end_line": 2, "category": "incident",
                            "score": 8.0, "reason": "contact"}]}
    call = provider.make_caller(
        KEY, sdk=_fake_for(provider, answer=json.dumps(payload)))
    assert call("system", "user", moment_scan.SCHEMA) == payload


@ALL_PROVIDERS
def test_usage_is_recorded_before_the_response_is_read(provider):
    # The tokens were spent the moment the response came back, whether or not
    # reading it succeeds. Recording only on success would under-report a run
    # by exactly the windows that went wrong.
    usage = providers.Usage()
    call = provider.make_caller(KEY, usage=usage,
                                sdk=_fake_for(provider, fail_at="response"))
    with pytest.raises(providers.ModelError):
        call("system", "user", moment_scan.SCHEMA)
    assert usage.calls == 1


@ALL_PROVIDERS
def test_the_apis_own_token_counts_are_accumulated(provider):
    usage = providers.Usage()
    call = provider.make_caller(KEY, usage=usage,
                                sdk=_fake_for(provider, usage=(9000, 1200)))
    call("system", "user", moment_scan.SCHEMA)
    call("system", "user", moment_scan.SCHEMA)
    assert (usage.calls, usage.input_tokens, usage.output_tokens) == (2, 18000, 2400)


@ALL_PROVIDERS
def test_unreadable_usage_costs_the_bookkeeping_and_not_the_answer(provider):
    usage = providers.Usage()
    call = provider.make_caller(KEY, usage=usage,
                                sdk=_fake_for(provider, usage="explode"))
    assert call("system", "user", moment_scan.SCHEMA) == {"moments": []}
    assert usage.calls == 1 and usage.input_tokens == 0


@ALL_PROVIDERS
def test_no_usage_argument_leaves_the_caller_working(provider):
    # `moment_scan.scan` takes an unconstrained caller; usage is optional
    # precisely so every existing call site and test double is unaffected.
    call = provider.make_caller(KEY, sdk=_fake_for(provider))
    assert call("system", "user", moment_scan.SCHEMA) == {"moments": []}


# --- The key file, shared code, no fake needed ------------------------------


@ALL_PROVIDERS
@pytest.mark.parametrize("content", ["", "   ", "{}", '{"api_key": null}',
                                     '{"api_key": ""}', "{not json", "{[1, 2]}",
                                     '{"api_key": 3}'])
def test_every_unusable_key_file_is_missing_key(provider, tmp_path, content):
    (tmp_path / provider.KEY_FILENAME).write_text(content, encoding="utf-8")
    with pytest.raises(providers.MissingKey):
        providers.load_api_key(tmp_path, provider.KEY_FILENAME)


@ALL_PROVIDERS
def test_a_missing_key_file_is_missing_key(provider, tmp_path):
    with pytest.raises(providers.MissingKey):
        providers.load_api_key(tmp_path, provider.KEY_FILENAME)
    assert providers.has_api_key(tmp_path, provider.KEY_FILENAME) is False


@ALL_PROVIDERS
def test_a_saved_key_round_trips_and_is_owner_only(provider, tmp_path):
    providers.save_api_key(tmp_path, provider.KEY_FILENAME, KEY)
    path = tmp_path / provider.KEY_FILENAME
    assert providers.load_api_key(tmp_path, provider.KEY_FILENAME) == KEY
    assert providers.has_api_key(tmp_path, provider.KEY_FILENAME) is True
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert not list(tmp_path.glob("*.part"))
    assert providers.forget_api_key(tmp_path, provider.KEY_FILENAME) is True
    assert providers.forget_api_key(tmp_path, provider.KEY_FILENAME) is False


@ALL_PROVIDERS
@pytest.mark.parametrize("bad", ["", "   ", None, 7, "a\nb", "x" * 5000])
def test_an_unusable_key_is_refused_without_quoting_it(provider, tmp_path, bad):
    with pytest.raises(ValueError) as caught:
        providers.save_api_key(tmp_path, provider.KEY_FILENAME, bad)
    assert str(bad) not in str(caught.value) or not str(bad).strip()
    assert not list(tmp_path.iterdir())


@ALL_PROVIDERS
def test_a_failed_write_leaves_no_trace_and_preserves_an_existing_key(
        provider, tmp_path, monkeypatch):
    # `save_api_key`'s docstring promises the file "appears complete or not
    # at all, and never exists world-readable for a moment". Proving that
    # needs a failure genuinely mid-write - patching `json.dump` (called
    # while the scratch file is open, before the replace) rather than
    # anything import-time or path-related.
    def _boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(_shared.json, "dump", _boom)

    # No pre-existing file: a failed write must leave the directory
    # completely empty - no half-written target, no leftover `.part` scratch.
    with pytest.raises(RuntimeError):
        providers.save_api_key(tmp_path, provider.KEY_FILENAME, KEY)
    assert list(tmp_path.iterdir()) == []

    # A pre-existing key file: a second, failing save must leave it
    # byte-for-byte untouched rather than half-overwritten - this is the
    # whole reason the write goes to a scratch SIBLING instead of the target.
    target = tmp_path / provider.KEY_FILENAME
    target.write_text('{"api_key": "already-here"}', encoding="utf-8")
    with pytest.raises(RuntimeError):
        providers.save_api_key(tmp_path, provider.KEY_FILENAME, KEY)
    assert target.read_text(encoding="utf-8") == '{"api_key": "already-here"}'
    assert [p.name for p in tmp_path.iterdir()] == [provider.KEY_FILENAME]


class _OsSpyNoChmodAfter:
    """Stands in for the `os` name INSIDE `_shared`'s own module namespace
    only - `monkeypatch.setattr(_shared, "os", ...)` rebinds `_shared.os`,
    not the real `os` module object, so nothing outside this one test's
    target function is affected and the process-wide `os` module is never
    touched. Records the mode every `open()` call is made with, and every
    `chmod()` call's target path - the KEY FILE must never be chmoded after
    writing (a rewrite of `save_api_key` to `write_text()` + a later
    `chmod()` would be caught even though it produces the same final mode
    `stat()` can see), but the auth DIRECTORY legitimately is: see F1 in
    the task-6 report, mirroring `auth.TokenStore.save`'s explicit
    `os.chmod(self.auth_dir, 0o700)`."""

    def __init__(self, real):
        self._real = real
        self.open_modes: list[int] = []
        self.chmod_paths: list = []

    def open(self, path, flags, mode=0o777):
        self.open_modes.append(mode)
        return self._real.open(path, flags, mode)

    def chmod(self, path, mode, *args, **kwargs):
        self.chmod_paths.append(Path(path))
        return self._real.chmod(path, mode, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


@ALL_PROVIDERS
def test_the_key_file_is_created_0600_not_chmoded_after(provider, tmp_path, monkeypatch):
    spy = _OsSpyNoChmodAfter(os)
    monkeypatch.setattr(_shared, "os", spy)
    providers.save_api_key(tmp_path, provider.KEY_FILENAME, KEY)
    assert spy.open_modes == [0o600]
    path = tmp_path / provider.KEY_FILENAME
    assert oct(path.stat().st_mode)[-3:] == "600"
    # The directory may legitimately be chmoded (F1); the key FILE must not.
    assert path not in spy.chmod_paths


@ALL_PROVIDERS
def test_a_fresh_auth_dir_is_created_0700(provider, tmp_path):
    # F1: mirrors auth.TokenStore.save's invariant - the directory that will
    # later hold client_secret.json and token-<id>.json must never be left
    # world/group-listable by this route, regardless of the process umask.
    auth_dir = tmp_path / "auth"
    providers.save_api_key(auth_dir, provider.KEY_FILENAME, KEY)
    assert oct(auth_dir.stat().st_mode)[-3:] == "700"


@ALL_PROVIDERS
def test_an_existing_loose_auth_dir_is_repaired_to_0700(provider, tmp_path):
    # mkdir(exist_ok=True) never repairs an ALREADY-existing directory's
    # mode - a workspace whose auth/ was created 0755 by something else (or
    # by this same route before the F1 fix) must come out 0700 too.
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir(mode=0o755)
    os.chmod(auth_dir, 0o755)  # mkdir's own mode arg is umask-masked too
    assert oct(auth_dir.stat().st_mode)[-3:] == "755"
    providers.save_api_key(auth_dir, provider.KEY_FILENAME, KEY)
    assert oct(auth_dir.stat().st_mode)[-3:] == "700"
