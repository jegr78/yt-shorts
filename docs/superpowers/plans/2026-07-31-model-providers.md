# Model providers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the model that scores moments a choice — Anthropic (default,
measured), Gemini (measured), OpenAI (shipped, labelled unverified) — behind
the one `caller` seam `moment_scan.scan` already takes.

**Architecture:** A new `src/yt_shorts/providers/` package holds a registry
plus one module per provider, each exposing the same eight names. `detect`,
`estimate`, `profile`, `brand_admin` and the studio look providers up through
the registry instead of importing `claude_client`. One parameterised
conformance suite asserts the same nine properties for every registered
provider, so a fourth inherits them.

**Tech Stack:** Python 3 (stdlib + optional vendor SDKs, all lazily
imported), FastAPI (studio routes only), React 19 + Mantine + TypeScript,
pytest, Vitest.

## Global Constraints

Copied from CLAUDE.md and the spec. Every task's requirements include these.

- **No test may reach the network, read a real API key, import a vendor SDK,
  run a real Whisper decode or render, or spend money.** A test that would
  spend money is a defect.
- **The API key never reaches a log line, an exception message, a route
  response, or a test fixture.** `ModelError` and `MissingKey` messages may
  be logged in full — they are built from a type name or the model's own
  answer. Anything else is logged by TYPE NAME only.
- **Every optional vendor SDK is imported lazily**, never at module scope.
  `import yt_shorts.providers` must pull in no `anthropic`, no `google.genai`,
  no `openai`. `create_app()` must likewise pull none of them.
- **Files under `<workspace>/auth/` are gitignored and must never be logged,
  echoed or committed.**
- `moment_scan.py` must not change. Not one line.
- `PYTHONPATH=src` is mandatory for every Python invocation.
- Run `python3 tools/lint.py` (exit 0) before every commit; the tree stays
  green.
- Frontend: run `npm test` before committing a frontend change, and
  `npm run build` — bare `npx tsc --noEmit` is INERT in this project and
  proves nothing.
- The six SHA-256 overlay hashes in
  `tests/test_event_layer_no_regression.py` must never be re-pinned. Nothing
  in this plan should touch them; if one moves, something is wrong.
- Anthropic stays `DEFAULT_PROVIDER`. No existing profile changes behaviour.

## Deviations from the spec, decided here

Both are refinements found while writing the plan. They are deliberate.

1. **The contract is eight names, not five.** `PACKAGE` (the import name),
   `INSTALL` (the pip line) and `VERIFIED` (whether this provider has been
   measured against the real service) join the five in the spec. Without
   them the Settings screen cannot say "SDK not installed, install it with
   …" or mark OpenAI unverified, which are both required behaviour.
2. **`Usage.record` takes an extraction callable**, not fixed field names.
   Vendors name their token fields differently, and today's
   `usage.record(getattr(response, "usage", None))` performs that `getattr`
   OUTSIDE any handler — a `usage` property that raised something other than
   `AttributeError` would escape unwrapped, the same class of hole a review
   already found on `.content`. One callable, caught in one place, fixes
   both.

## File Structure

| File | Responsibility |
|---|---|
| `src/yt_shorts/providers/__init__.py` | registry, `Usage`, errors, key load/save, SDK availability |
| `src/yt_shorts/providers/anthropic_api.py` | Anthropic (moved from `claude_client.py`) |
| `src/yt_shorts/providers/gemini_api.py` | Gemini |
| `src/yt_shorts/providers/openai_api.py` | OpenAI |
| `src/yt_shorts/detect.py` | provider-aware caller construction and cost report |
| `src/yt_shorts/estimate.py` | takes a price table instead of owning one |
| `src/yt_shorts/profile.py` | validates the `detect` section |
| `src/yt_shorts/brand_admin.py` | accepts `detect` in a brand patch |
| `src/yt_shorts/studio/api.py` | provider key routes, settings block, estimate wiring |
| `src/yt_shorts/studio/web/src/providers.ts` | pure display/selection helpers |
| `tests/test_provider_contract.py` | the parameterised conformance suite |

Deleted: `src/yt_shorts/_anthropic.py`, `src/yt_shorts/claude_client.py`
(moved), `tests/test_claude_client.py` (moved).

---

### Task 1: The providers package, the registry, and the conformance suite

**Files:**
- Create: `src/yt_shorts/providers/__init__.py`
- Move: `src/yt_shorts/claude_client.py` → `src/yt_shorts/providers/anthropic_api.py` (`git mv`)
- Move: `tests/test_claude_client.py` → `tests/test_anthropic_api.py` (`git mv`)
- Delete: `src/yt_shorts/_anthropic.py`
- Create: `tests/test_provider_contract.py`
- Modify: `src/yt_shorts/detect.py`, `src/yt_shorts/moment_scan.py` (import line only), `src/yt_shorts/studio/api.py` (import + one reference), `tests/test_detect.py`, `tests/test_moment_scan.py`, `tests/test_studio_api.py` (imports only)

**Interfaces:**
- Consumes: nothing new.
- Produces: `providers.PROVIDERS: dict[str, module]`, `providers.get(id) -> module`,
  `providers.DEFAULT_PROVIDER = "anthropic"`, `providers.UnknownProvider`,
  `providers.Usage` (with `record(extract)`), `providers.MissingKey`,
  `providers.ModelError`, `providers.SdkUnavailable`,
  `providers.load_api_key(auth_dir, filename) -> str`,
  `providers.save_api_key(auth_dir, filename, api_key) -> None`,
  `providers.forget_api_key(auth_dir, filename) -> bool`,
  `providers.has_api_key(auth_dir, filename) -> bool`,
  `providers.sdk_installed(module) -> bool`,
  `providers.require(module, purpose) -> None`,
  `providers.ordered() -> list[module]`.
  The eight-name contract each provider module exposes: `PROVIDER_ID`,
  `KEY_FILENAME`, `DEFAULT_MODEL`, `PRICES`, `PACKAGE`, `INSTALL`,
  `VERIFIED`, `make_caller`.

This task moves one provider and stands up the suite that will hold the
other two to the same bar. It is deliberately the largest task: the move and
the registry cannot be reviewed apart.

- [ ] **Step 1: Move the two files with git, so history survives**

```bash
mkdir -p src/yt_shorts/providers
git mv src/yt_shorts/claude_client.py src/yt_shorts/providers/anthropic_api.py
git mv tests/test_claude_client.py tests/test_anthropic_api.py
git rm src/yt_shorts/_anthropic.py
```

- [ ] **Step 2: Write `src/yt_shorts/providers/__init__.py`**

```python
"""The registry of model providers, and everything they share.

Moment detection is the only feature in this project that talks to a
commercial model API. `moment_scan.scan` takes an UNCONSTRAINED
`caller(system, user, schema) -> dict`, so which vendor answers is a
choice - this package is where that choice is made concrete.

Every provider module exposes the same eight names (see CONTRACT below) and
nothing else. `tests/test_provider_contract.py` asserts all of it for every
registered provider, so a fourth provider inherits the whole bar the moment
it enters PROVIDERS - including the key-secrecy rules, which took two review
rounds to get right on the first provider.

The three provider modules are imported HERE, at module scope, and that is
safe: each imports its own vendor SDK lazily, inside `make_caller`. The
benefit is that a syntax error in a provider nobody has a key for fails in
the suite rather than for whoever first pastes that key. A test pins that
importing this package pulls in no vendor SDK.
"""

from __future__ import annotations

import importlib.util
import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import anthropic_api, gemini_api, openai_api

__all__ = [
    "CONTRACT", "DEFAULT_PROVIDER", "PROVIDERS", "MissingKey", "ModelError",
    "SdkUnavailable", "UnknownProvider", "Usage", "forget_api_key", "get",
    "has_api_key", "load_api_key", "ordered", "require", "save_api_key",
    "sdk_installed",
]

# The eight names every provider module must expose. Named here rather than
# only in prose so the conformance suite can iterate it.
CONTRACT = ("PROVIDER_ID", "KEY_FILENAME", "DEFAULT_MODEL", "PRICES",
            "PACKAGE", "INSTALL", "VERIFIED", "make_caller")

_MODULES = (anthropic_api, gemini_api, openai_api)
PROVIDERS = {module.PROVIDER_ID: module for module in _MODULES}
DEFAULT_PROVIDER = "anthropic"

# A key file is written 0600 and never longer than this. The bound is a
# sanity check against a pasted FILE, not a vendor fact - every real key is
# far shorter, and key formats change.
MAX_KEY_LENGTH = 4096


class MissingKey(RuntimeError):
    """No usable API key at <workspace>/auth/<provider>.json."""


class ModelError(RuntimeError):
    """The model did not return a usable answer. Never carries the API key."""


class SdkUnavailable(RuntimeError):
    """The provider's optional SDK is not installed in this venv."""


class UnknownProvider(ValueError):
    """No provider by that id."""


def ordered() -> list:
    """The provider modules in a stable display order: the default first,
    then the rest alphabetically. The Settings screen renders them in this
    order, so it must not depend on dict insertion order changing."""
    rest = sorted((m for m in _MODULES if m.PROVIDER_ID != DEFAULT_PROVIDER),
                  key=lambda m: m.PROVIDER_ID)
    return [PROVIDERS[DEFAULT_PROVIDER], *rest]


def get(provider_id: str):
    """The provider module, or UnknownProvider. Never guesses a default:
    a typo must be reported, not silently answered with Anthropic."""
    try:
        return PROVIDERS[provider_id]
    except (KeyError, TypeError):
        known = ", ".join(sorted(PROVIDERS))
        raise UnknownProvider(
            f"unknown model provider {provider_id!r}; known: {known}") from None


@dataclass
class Usage:
    """What a run actually cost, in the API's own numbers.

    Exists because this project spent a day believing an estimate: the cost
    preview in `estimate.py` counts characters and divides by four, and
    measured against a real invoice it was roughly half the truth while its
    own comment claimed it ran ~12% HIGH. Nothing could have caught that,
    because nothing ever looked at what the API reported.

    Deliberately a plain accumulator the caller owns, not a return value:
    `make_caller`'s callable is passed to `moment_scan.scan`, which takes an
    unconstrained `caller` and must keep working with any function of the
    same shape. Widening that contract to "returns a (dict, usage) pair"
    would push token bookkeeping into every test double in the suite.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def record(self, extract) -> None:
        """Adds one response's usage. Tolerates anything, raises nothing.

        `extract` is a zero-argument callable returning `(input, output)`.
        It is a CALLABLE rather than a pair of field names because vendors
        name and nest their token counts differently - and because reading
        them can itself raise. An earlier version took the response's
        `usage` object, which meant the `getattr` that produced it sat
        OUTSIDE any handler: a property raising anything but AttributeError
        escaped unwrapped, the same class of hole a review already found on
        Anthropic's `.content`. One callable, caught in one place.

        `calls` counts before the extraction, because a call happened
        whether or not its bookkeeping could be read. A response whose usage
        cannot be read still cost money and still produced an answer: losing
        the bookkeeping is acceptable, losing the window over the
        bookkeeping is not.
        """
        self.calls += 1
        try:
            reported_in, reported_out = extract()
        except Exception:   # noqa: BLE001 - bookkeeping never costs a window
            return
        self.input_tokens += _count(reported_in)
        self.output_tokens += _count(reported_out)


def _count(value) -> int:
    """A non-negative int, or 0. `True` is rejected despite being an int
    subclass: a bool here means the extraction read the wrong field."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if value >= 0 else 0


def load_api_key(auth_dir: str | Path, filename: str) -> str:
    """The key in <auth_dir>/<filename>, or MissingKey.

    TWO shapes are accepted: a bare key string or a JSON object with an
    `api_key` field. The file as an operator first creates it holds the raw
    key despite its extension, and an "Expecting value: line 1 column 1"
    traceback explains nothing to someone who did what they were asked.

    EVERY unusable key is MissingKey - `detect._caller_from_config` relies
    on exactly that to degrade to the lexicon engine instead of aborting a
    run. Never str()s the file's content; only the path and an exception's
    own (content-free) message.
    """
    path = Path(auth_dir) / filename
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as error:
        # UnicodeDecodeError alongside OSError: a key file with one stray
        # non-UTF-8 byte is unusable exactly as a missing one is, but
        # read_text raises a ValueError subclass for that, not an OSError.
        raise MissingKey(f"cannot read {path}: {error}") from None
    if not raw:
        raise MissingKey(f"{path} is empty")
    if raw.startswith("{"):
        try:
            value = json.loads(raw).get("api_key")
        except json.JSONDecodeError:
            raise MissingKey(f"{path} is neither a raw key nor valid JSON") from None
        except AttributeError:
            raise MissingKey(f"{path} is not a JSON object") from None
        # `.get` returning None (missing OR an explicit `"api_key": null`) and
        # a non-string value are both "no usable key" - not `str(value)`,
        # which would turn `null` into the truthy literal "None".
        if not isinstance(value, str) or not value.strip():
            raise MissingKey(f"{path} has no non-empty 'api_key'")
        return value.strip()
    return raw


def has_api_key(auth_dir: str | Path, filename: str) -> bool:
    """Whether a USABLE key is on file. Reads it and throws it away rather
    than merely stat()ing the path, because an empty or malformed file is
    what an operator most needs told apart from a working one."""
    try:
        load_api_key(auth_dir, filename)
    except MissingKey:
        return False
    return True


def save_api_key(auth_dir: str | Path, filename: str, api_key: str) -> None:
    """Writes <auth_dir>/<filename> as {"api_key": ...}, mode 0600, atomically.

    Written to a scratch SIBLING whose mode is set at creation and then moved
    into place with os.replace - so the file appears complete or not at all,
    and never exists world-readable for a moment. Setting the mode after
    writing would leave exactly that window open. Same write-aside mechanic
    `render.compose` uses, for the same reason.

    Raises ValueError for an unusable key; the message names the constraint
    that failed and NEVER the value.
    """
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("the API key must be a non-empty string")
    key = api_key.strip()
    if len(key) > MAX_KEY_LENGTH:
        raise ValueError(f"the API key must be at most {MAX_KEY_LENGTH} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in key):
        raise ValueError("the API key must not contain control characters")
    directory = Path(auth_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    scratch = target.with_name(target.name + ".part")
    # Unlink first: O_CREAT does NOT re-apply the mode to a file that already
    # exists, so a stale scratch with loose permissions would survive.
    scratch.unlink(missing_ok=True)
    descriptor = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"api_key": key}, handle)
            handle.write("\n")
    except BaseException:
        scratch.unlink(missing_ok=True)
        raise
    scratch.replace(target)


def forget_api_key(auth_dir: str | Path, filename: str) -> bool:
    """Removes the key file. True if one was there, False if not. Mirrors
    `auth.forget_credentials` - reversible by pasting the key again."""
    path = Path(auth_dir) / filename
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def sdk_installed(module) -> bool:
    """Whether this provider's optional SDK is importable, WITHOUT importing
    it. `find_spec` is used rather than a try/import so a settings page
    cannot pull three vendor SDKs into the studio process just by being
    rendered."""
    try:
        return importlib.util.find_spec(module.PACKAGE) is not None
    except (ImportError, ValueError):
        # find_spec imports parent packages and raises ValueError for some
        # half-installed states. Either way: not usable.
        return False


def require(module, purpose: str) -> None:
    """Raises SdkUnavailable with an actionable message, or returns."""
    if not sdk_installed(module):
        raise SdkUnavailable(
            f"{purpose} needs the {module.PROVIDER_ID} SDK, which is not "
            f"installed in this venv.\nInstall it with: {module.INSTALL}\n"
            f"Every other command works without it.")
```

- [ ] **Step 3: Adapt `anthropic_api.py` to the contract**

Keep every comment and docstring in that file — they record measurements
this project paid for. Make exactly these changes:

Replace the imports and add the four new contract names near the top:

```python
from . import ModelError, Usage, require   # noqa: F401 - Usage re-exported for callers
```

That import is circular (`__init__` imports this module, this module imports
`__init__`). Resolve it the way Python allows: import the names INSIDE the
functions that use them, or `from yt_shorts import providers` lazily. Use
the simplest form that works and say in a comment which one and why.

Add:

```python
PROVIDER_ID = "anthropic"
PACKAGE = "anthropic"
INSTALL = ".venv/bin/pip install anthropic"
# Measured against the real API (see the bake-off beside DEFAULT_MODEL).
VERIFIED = True
```

Move the price table in from `estimate.py`:

```python
# model -> (USD per 1M input tokens, USD per 1M output tokens).
# A snapshot of the published rates on 2026-07-29; re-check at
# anthropic.com/pricing rather than trusting these numbers.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
}
```

Delete `load_api_key` and `KEY_FILENAME`'s old use; add
`KEY_FILENAME = "anthropic.json"` and let `providers.load_api_key` do the
reading. Delete the local `Usage`/`_count`/`MissingKey`/`ModelError`
definitions — they now live in `__init__`.

Replace `_sdk()` with:

```python
def _sdk():
    require(_self(), "moment detection")
    import anthropic
    return anthropic
```

where `_self()` returns this module (`sys.modules[__name__]`), or restructure
so `require` gets what it needs — implementer's choice, documented.

Change the usage recording line from

```python
        if usage is not None:
            usage.record(getattr(response, "usage", None))
```

to

```python
        if usage is not None:
            # A callable, so the reads below are inside Usage.record's own
            # tolerance rather than outside every handler (see Usage.record).
            usage.record(lambda: (response.usage.input_tokens,
                                  response.usage.output_tokens))
```

Everything else in the file — the three-entry-point wrapping, the
`__cause__` hazard comment, the `stop_reason` checks, the bake-off comment —
stays byte-identical.

- [ ] **Step 4: Write minimal `gemini_api.py` and `openai_api.py` stubs**

They must exist for `__init__` to import. Tasks 2 and 3 fill them in. For
now, each carries its eight names and a `make_caller` that raises
`NotImplementedError`. The conformance suite (Step 5) will therefore FAIL
for both — that is the point, and it is what Tasks 2 and 3 fix.

```python
# gemini_api.py
PROVIDER_ID = "gemini"
KEY_FILENAME = "gemini.json"
DEFAULT_MODEL = "gemini-3.6-flash"   # provisional; pinned by Task 8's bake-off
PACKAGE = "google.genai"
INSTALL = ".venv/bin/pip install google-genai"
VERIFIED = False                      # flipped to True by Task 8
PRICES: dict[str, tuple[float, float]] = {}


def make_caller(api_key, *, model=DEFAULT_MODEL, max_tokens=4096, sdk=None,
                usage=None):
    raise NotImplementedError("Task 2")
```

```python
# openai_api.py
PROVIDER_ID = "openai"
KEY_FILENAME = "openai.json"
DEFAULT_MODEL = "gpt-5"              # pinned against the live model list in Task 3
PACKAGE = "openai"
INSTALL = ".venv/bin/pip install openai"
VERIFIED = False                      # stays False: never measured against the real API
PRICES: dict[str, tuple[float, float]] = {}


def make_caller(api_key, *, model=DEFAULT_MODEL, max_tokens=4096, sdk=None,
                usage=None):
    raise NotImplementedError("Task 3")
```

- [ ] **Step 5: Write the conformance suite**

Create `tests/test_provider_contract.py`. This is the centrepiece — it is
the executable form of the claim "the provider is swappable".

```python
"""One suite, every provider, the same nine properties.

This file is the proof behind README's claim that a provider is a file
rather than a rebuild: a fourth provider inherits all of it the moment it
enters `providers.PROVIDERS`. In particular it inherits the key-secrecy
rules, which took two review rounds to get right on the first provider -
the response-READING path was missed twice, and a fake SDK whose
`response.content` raised with the key in its text reached the caller with
the key intact.

Nothing here imports a vendor SDK, reads a real key, touches the network or
spends a cent. Every provider is exercised through its injected `sdk`.
"""

from __future__ import annotations

import json

import pytest

from yt_shorts import moment_scan, providers

KEY = "sk-secret-DO-NOT-LEAK-0123456789"
ALL_PROVIDERS = pytest.mark.parametrize(
    "provider", providers.ordered(), ids=lambda m: m.PROVIDER_ID)


class Boom(Exception):
    """Carries the key in its text, exactly as a real SDK's exception can:
    its message quotes the request, and the request carries the key."""

    def __init__(self, where: str) -> None:
        super().__init__(f"failed {where} with api_key={KEY} in the message")


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


@ALL_PROVIDERS
def test_importing_the_module_pulls_in_no_vendor_sdk(provider):
    # The whole point of lazy SDK imports: a venv with none of them installed
    # must still start, render and transcribe.
    import subprocess
    import sys
    code = (
        "import sys;"
        "import yt_shorts.providers;"
        "bad=[n for n in sys.modules if n.split('.')[0] in "
        "{'anthropic','openai','google'}];"
        "print(bad)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                            text=True, env={"PYTHONPATH": "src", "PATH": ""},
                            check=True)
    assert result.stdout.strip() == "[]", result.stdout
```

**Note for the implementer:** that last test does not need
parameterising — write it once, outside `ALL_PROVIDERS`. Fix the `env` so it
actually works in this repo (it needs at least `PATH` and possibly
`HOME`); verify by making it fail on purpose (add a module-scope
`import anthropic` to a provider) before trusting it.

Now the seven behavioural properties. Each needs a fake SDK **per provider**,
because the three SDKs have different shapes. Put those fakes in a
`FAKES: dict[str, Callable]` mapping `PROVIDER_ID` to a factory
`make_fake(*, fail_at=None, answer=None, usage=(11, 22), finish=None)` and
look the right one up per parameterised run. Tasks 2 and 3 add their entry.
The seven:

```python
@ALL_PROVIDERS
def test_a_failure_building_the_client_is_wrapped_without_the_key(provider):
    fake = FAKES[provider.PROVIDER_ID](fail_at="client")
    with pytest.raises(providers.ModelError) as caught:
        provider.make_caller(KEY, sdk=fake)
    assert KEY not in str(caught.value)
    assert "Boom" in str(caught.value)


@ALL_PROVIDERS
def test_a_failure_sending_the_request_is_wrapped_without_the_key(provider):
    call = provider.make_caller(KEY, sdk=FAKES[provider.PROVIDER_ID](fail_at="request"))
    with pytest.raises(providers.ModelError) as caught:
        call("system", "user", moment_scan.SCHEMA)
    assert KEY not in str(caught.value)


@ALL_PROVIDERS
def test_a_failure_reading_the_response_is_wrapped_without_the_key(provider):
    # The one that was missed twice. The response object was built from the
    # request, so anything it raises can carry the key.
    call = provider.make_caller(KEY, sdk=FAKES[provider.PROVIDER_ID](fail_at="response"))
    with pytest.raises(providers.ModelError) as caught:
        call("system", "user", moment_scan.SCHEMA)
    assert KEY not in str(caught.value)


@ALL_PROVIDERS
def test_a_non_json_answer_is_a_model_error(provider):
    call = provider.make_caller(KEY, sdk=FAKES[provider.PROVIDER_ID](answer="not json"))
    with pytest.raises(providers.ModelError):
        call("system", "user", moment_scan.SCHEMA)


@ALL_PROVIDERS
def test_the_caller_accepts_moment_scans_own_schema_and_returns_its_answer(provider):
    payload = {"moments": [{"start_line": 1, "end_line": 2, "category": "incident",
                            "score": 8.0, "reason": "contact"}]}
    call = provider.make_caller(KEY, sdk=FAKES[provider.PROVIDER_ID](
        answer=json.dumps(payload)))
    assert call("system", "user", moment_scan.SCHEMA) == payload


@ALL_PROVIDERS
def test_usage_is_recorded_before_the_response_is_read(provider):
    # The tokens were spent the moment the response came back, whether or not
    # reading it succeeds. Recording only on success would under-report a run
    # by exactly the windows that went wrong.
    usage = providers.Usage()
    call = provider.make_caller(KEY, usage=usage,
                                sdk=FAKES[provider.PROVIDER_ID](fail_at="response"))
    with pytest.raises(providers.ModelError):
        call("system", "user", moment_scan.SCHEMA)
    assert usage.calls == 1


@ALL_PROVIDERS
def test_unreadable_usage_costs_the_bookkeeping_and_not_the_answer(provider):
    usage = providers.Usage()
    call = provider.make_caller(KEY, usage=usage,
                                sdk=FAKES[provider.PROVIDER_ID](usage="explode"))
    assert call("system", "user", moment_scan.SCHEMA) == {"moments": []}
    assert usage.calls == 1 and usage.input_tokens == 0
```

Plus the key-file properties, which are shared code and need no fake:

```python
@ALL_PROVIDERS
@pytest.mark.parametrize("content", ["", "   ", "{}", '{"api_key": null}',
                                     '{"api_key": ""}', "{not json"])
def test_every_unusable_key_file_is_missing_key(provider, tmp_path, content):
    (tmp_path / provider.KEY_FILENAME).write_text(content, encoding="utf-8")
    with pytest.raises(providers.MissingKey):
        providers.load_api_key(tmp_path, provider.KEY_FILENAME)


@ALL_PROVIDERS
def test_a_saved_key_round_trips_and_is_owner_only(provider, tmp_path):
    providers.save_api_key(tmp_path, provider.KEY_FILENAME, KEY)
    path = tmp_path / provider.KEY_FILENAME
    assert providers.load_api_key(tmp_path, provider.KEY_FILENAME) == KEY
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert not list(tmp_path.glob("*.part"))
    assert providers.forget_api_key(tmp_path, provider.KEY_FILENAME) is True
    assert providers.forget_api_key(tmp_path, provider.KEY_FILENAME) is False
```

- [ ] **Step 6: Run the suite and confirm it fails for two providers, passes for one**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_provider_contract.py -q`
Expected: the `anthropic` parameterisations PASS; the `gemini` and `openai`
ones FAIL with `NotImplementedError`. That is the correct state at the end of
this task — Tasks 2 and 3 turn them green.

- [ ] **Step 7: Update every importer**

`src/yt_shorts/detect.py`: replace `from . import claude_client, moment_scan`
and `from ._anthropic import AnthropicUnavailable` with
`from . import moment_scan, providers`. Replace `claude_client.Usage()` with
`providers.Usage()`, `claude_client.MissingKey` with `providers.MissingKey`,
`claude_client.ModelError` with `providers.ModelError`, and
`AnthropicUnavailable` with `providers.SdkUnavailable`. Leave the provider
SELECTION for Task 4 — here, keep behaviour identical by using
`providers.get(providers.DEFAULT_PROVIDER)` in place of the old direct
`claude_client` calls.

`src/yt_shorts/moment_scan.py`: change **only** the import line
`from .claude_client import ModelError` to `from .providers import ModelError`.
The rest of the file, including its comments about `claude_client`, needs its
prose updated to say `providers.anthropic_api` — text only, no logic.

`src/yt_shorts/studio/api.py`: replace the `claude_client` import and its one
`claude_client.DEFAULT_MODEL` reference (line ~1513) with the registry
equivalent; full provider wiring is Task 4.

`tests/test_anthropic_api.py`, `tests/test_detect.py`,
`tests/test_moment_scan.py`, `tests/test_studio_api.py`: update imports and
symbol names. If a test needs more than an import change, say so in the
report — the move should not alter behaviour.

- [ ] **Step 8: Run lint and the whole suite**

Run: `python3 tools/lint.py && PYTHONPATH=src .venv/bin/pytest -q` (foreground, timeout 600000)
Expected: lint exit 0; the suite green EXCEPT the gemini/openai
parameterisations of `test_provider_contract.py`, which fail with
`NotImplementedError`.

- [ ] **Step 9: Verify `create_app()` still pulls no heavy imports**

```bash
PYTHONPATH=src .venv/bin/python -c "
import sys
BLOCKED = ('anthropic', 'openai', 'google', 'googleapiclient')
class Block:
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in BLOCKED:
            raise ImportError(f'blocked: {name}')
        return None
sys.meta_path.insert(0, Block())
from yt_shorts.studio.api import create_app
create_app()
leaked = [n for n in sys.modules if n.split('.')[0] in BLOCKED]
print('leaked:', leaked)"
```
Expected: `leaked: []`.

**Use `find_spec`, not `find_module`.** The `find_module` finder protocol was
removed in Python 3.12, so a `Block` class defining it is never consulted and
the check passes unconditionally — an earlier draft of this plan had exactly
that and would have proved nothing. The `leaked` list is the real assertion;
the raise is only there to fail loudly at the point of import.

- [ ] **Step 10: Commit**

```bash
git add -A src/yt_shorts tests/
git commit -m "refactor(providers): a registry, a shared contract, and one conformance suite

Moves claude_client.py to providers/anthropic_api.py with its history, folds
_anthropic.py's job into a parameterised helper, and stands up the suite that
will hold two further providers to the same bar - including the key-secrecy
rules that took two review rounds to get right on the first one.

Usage.record now takes an extraction callable rather than fixed field names.
Vendors nest their token counts differently, and the getattr that produced
them used to sit outside every handler."
```

---

### Task 2: The Gemini provider

**Files:**
- Modify: `src/yt_shorts/providers/gemini_api.py`
- Modify: `tests/test_provider_contract.py` (its `FAKES` entry)
- Test: `tests/test_gemini_api.py` (anything specific to this vendor)

**Interfaces:**
- Consumes: `providers.ModelError`, `providers.Usage`, `providers.require`,
  the eight-name contract, and `tests/test_provider_contract.py`'s `FAKES`
  registry (Task 1).
- Produces: a working `make_caller` for `PROVIDER_ID == "gemini"`.

**The conformance suite is your failing test.** It already exercises nine
properties against this module and currently fails with
`NotImplementedError`. Do not write a parallel copy of those tests.

- [ ] **Step 1: Pin the SDK shape against the real documentation**

Verified 2026-07-31 at `ai.google.dev/gemini-api/docs/text-generation` and
`.../structured-output`, and true as far as it goes:

```python
from google import genai                    # package: google-genai
client = genai.Client(api_key=api_key)
interaction = client.interactions.create(
    model=model,
    system_instruction=system,
    input=user,
    response_format={"type": "text", "mime_type": "application/json",
                     "schema": schema},     # raw JSON Schema dicts accepted
)
text = interaction.output_text
```

**Three things are NOT documented on those pages and you must pin them
yourself, from the installed SDK, and record what you found in the module
docstring with the date:**

1. **Where the token counts live.** Anthropic's are
   `response.usage.input_tokens` / `.output_tokens`. Gemini's are named
   differently and may be nested (a `usage_metadata`-shaped object with
   prompt/candidate counts is the likely shape). Find the real names.
2. **How to bound the answer's length**, the equivalent of Anthropic's
   `max_tokens=4096`. If the SDK exposes one, use it; if it does not, say so
   in the docstring and note that the model's own default applies.
3. **How truncation and refusal are reported.** Anthropic checks
   `stop_reason in ("refusal", "max_tokens")` BEFORE reading content,
   because on a refusal the content is empty or partial and indexing it
   raises something that says nothing about the cause. Find Gemini's
   equivalent (a `finish_reason`-shaped field) and check it in the same
   place, for the same reason.

Install it first: `.venv/bin/pip install google-genai`, then introspect.
This is research, not guesswork — if you cannot establish one of the three
from the SDK or its documentation, report it rather than inventing it.

- [ ] **Step 2: Write `make_caller`**

Mirror `anthropic_api.make_caller` exactly in structure. The three wrapping
points are not optional and not negotiable:

```python
def make_caller(api_key: str, *, model: str = DEFAULT_MODEL,
                max_tokens: int = 4096, sdk=None, usage=None):
    """Returns `call(system, user, schema) -> dict`.

    `sdk` is injected so the whole path tests without the package, a key, a
    network or a cent. Production passes nothing and gets the real module.
    """
    module = sdk if sdk is not None else _sdk()
    try:
        client = module.Client(api_key=api_key)
    except Exception as error:
        # The SDK's constructor takes the API KEY as an argument, so its own
        # exception message can embed it. The type name is the only part safe
        # to keep. Chained with `from` so a traceback still shows the cause -
        # see anthropic_api's __cause__ hazard note, which applies here too:
        # the day anyone adds exc_info=True on this path, logging walks the
        # chain and prints the original text.
        raise ModelError(
            f"the Gemini SDK raised {type(error).__name__} building the client"
        ) from error

    def call(system: str, user: str, schema: dict) -> dict:
        try:
            interaction = client.interactions.create(...)   # Step 1's shape
        except Exception as error:
            raise ModelError(
                f"the Gemini SDK raised {type(error).__name__} calling the model"
            ) from error
        # BEFORE any read below: the tokens were spent the moment this came
        # back, whether or not it turns out to be a refusal or unparseable.
        if usage is not None:
            usage.record(lambda: (..., ...))   # Step 1's field names
        try:
            <refusal / truncation check, from Step 1>
            text = interaction.output_text
            try:
                return json.loads(text)
            except json.JSONDecodeError as error:
                raise ModelError(f"the answer was not valid JSON: {error}") from None
        except ModelError:
            # Already sanitised by this function - pass through rather than
            # re-wrapping into the vaguer type-name-only message below.
            raise
        except Exception as error:
            raise ModelError(
                f"the Gemini SDK raised {type(error).__name__} reading the response"
            ) from error

    return call
```

- [ ] **Step 3: Fill in `PRICES` and confirm `DEFAULT_MODEL`**

From `ai.google.dev/gemini-api/docs/pricing`, verified 2026-07-31 (record the
date in the comment, as `anthropic_api.PRICES` does):

```python
# model -> (USD per 1M input tokens, USD per 1M output tokens).
# The PAID rates, including for the Flash models that have a free tier: 0.0
# would be wrong for anyone paying, and 0.0 is also what an unpriced model
# looks like. Read a number from here as an honest upper bound.
PRICES: dict[str, tuple[float, float]] = {
    "gemini-3.6-flash": (1.50, 7.50),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
}
```

`DEFAULT_MODEL` stays the provisional `gemini-3.6-flash`; Task 8 pins it with
measured numbers and flips `VERIFIED`.

- [ ] **Step 4: Add the Gemini fake to `FAKES`**

A fake SDK matching Step 1's shape, honouring the same
`fail_at`/`answer`/`usage` switches the Anthropic fake does — including
`fail_at="response"` raising `Boom("reading")` from the attribute the real
code reads, and `usage="explode"` raising on the token-count access.

- [ ] **Step 5: Run the conformance suite**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_provider_contract.py -q -k gemini`
Expected: PASS, all parameterisations.

- [ ] **Step 6: Prove the suite would catch a leak**

Mutation check, not optional. Temporarily replace the response-reading
`type(error).__name__` with `error`, re-run, and confirm
`test_a_failure_reading_the_response_is_wrapped_without_the_key` FAILS. Put
it back. Report both outputs — a passing test on broken code is the failure
mode this project has hit repeatedly.

- [ ] **Step 7: Lint, full suite, commit**

Run: `python3 tools/lint.py && PYTHONPATH=src .venv/bin/pytest -q`

```bash
git add src/yt_shorts/providers/gemini_api.py tests/
git commit -m "feat(providers): Gemini, the second implementation behind the same seam"
```

---

### Task 3: The OpenAI provider

**Files:**
- Modify: `src/yt_shorts/providers/openai_api.py`
- Modify: `tests/test_provider_contract.py` (its `FAKES` entry)
- Test: `tests/test_openai_api.py` (the schema adaptation, at least)

**Interfaces:** identical to Task 2, for `PROVIDER_ID == "openai"`.

**This provider ships without ever having been run against the real
service**, by explicit decision. `VERIFIED` stays `False` and every surface
says so. That makes the fakes and the schema adaptation the only things
standing between a contributor and a broken first experience — take them
seriously.

- [ ] **Step 1: Pin the SDK shape against the real documentation**

Two APIs are plausible (chat completions and the responses API). Establish
which one supports strict JSON-schema structured output today, and use it.
Record the shape, the date and the source URL in the module docstring.
Establish the same three unknowns Task 2 lists: token-count field names,
the max-tokens parameter, and how refusal/truncation are reported (OpenAI
has an explicit refusal concept — find it and check it before reading
content, as the other two providers do).

- [ ] **Step 2: Write the schema adaptation**

This is the one genuinely provider-specific transformation in the project,
and it needs its own test file because the conformance suite only asserts
that `moment_scan.SCHEMA` is ACCEPTED, not what it becomes.

OpenAI's strict mode requires every property of every object to appear in
`required`; optionality is expressed as a nullable type union instead.
`moment_scan.SCHEMA` has one optional property, `hook_suggestion`.

```python
def _adapt_schema(schema: dict) -> dict:
    """`moment_scan.SCHEMA` in OpenAI's strict dialect.

    Strict mode requires EVERY property to appear in `required`; a property
    that is genuinely optional is expressed as a nullable union instead.
    `moment_scan.SCHEMA` has exactly one such property today
    (`hook_suggestion`), but this walks the whole schema rather than naming
    it, so a second optional field added upstream does not silently produce
    a 400 here.

    `moment_scan.SCHEMA` itself is NOT changed to suit this vendor: it is
    written for Anthropic's dialect, that dialect is measured, and adapting
    is this module's job. Deep-copied, never mutated in place - the caller
    passes the module-level constant.
    """
```

Walk objects recursively: for each `properties` dict, set `required` to all
its keys, and for keys that were not previously required, widen `"type": X`
to `"type": [X, "null"]`. Keep `additionalProperties: false`.

Test it directly in `tests/test_openai_api.py`:

```python
def test_every_property_becomes_required_and_the_optional_one_becomes_nullable():
    adapted = openai_api._adapt_schema(moment_scan.SCHEMA)
    item = adapted["properties"]["moments"]["items"]
    assert set(item["required"]) == set(item["properties"])
    assert item["properties"]["hook_suggestion"]["type"] == ["string", "null"]
    assert item["properties"]["reason"]["type"] == "string"


def test_the_shared_schema_is_not_mutated():
    before = copy.deepcopy(moment_scan.SCHEMA)
    openai_api._adapt_schema(moment_scan.SCHEMA)
    assert moment_scan.SCHEMA == before
```

- [ ] **Step 3: Write `make_caller`**

Same three wrapping points, same structure, same comments as Task 2. No
shortcuts because this one is unverified — if anything, it needs the
discipline more.

- [ ] **Step 4: Fill in `PRICES` and `DEFAULT_MODEL`**

From OpenAI's published pricing page, with the date and URL in the comment.
Pick a `DEFAULT_MODEL` that exists on the live model list and supports strict
structured output.

- [ ] **Step 5: Add the OpenAI fake, run the suite, run the mutation check**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_provider_contract.py tests/test_openai_api.py -q`
Expected: PASS. Then the same mutation check Task 2 Step 6 describes, on this
module, with both outputs reported.

- [ ] **Step 6: Lint, full suite, commit**

```bash
git add src/yt_shorts/providers/openai_api.py tests/
git commit -m "feat(providers): OpenAI, shipped and labelled unverified

Tested against a fake SDK, never against the real service - by decision, and
said so in the module, in Settings and in README. The schema adaptation is
real work: strict mode requires every property in \`required\`, and
moment_scan.SCHEMA has an optional one."
```

---

### Task 4: Provider selection in detect, estimate and profile

**Files:**
- Modify: `src/yt_shorts/detect.py`
- Modify: `src/yt_shorts/estimate.py`
- Modify: `src/yt_shorts/profile.py`
- Modify: `src/yt_shorts/studio/api.py` (the estimate route only)
- Test: `tests/test_detect.py`, `tests/test_estimate.py`, `tests/test_profile.py`

**Interfaces:**
- Consumes: the whole registry from Task 1.
- Produces: `estimate.estimate_run(words, lexicon, *, model, prices)`;
  `profile._validate_detect(brand, path) -> list[str]`; a `"provider"` field
  in `moments.json`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_profile.py`:

```python
class TestDetectValidation:
    def test_an_absent_detect_section_is_fine(self, tmp_path): ...
    def test_an_unknown_provider_is_a_reported_defect(self, tmp_path):
        # ... write a brand.json with {"detect": {"provider": "anthropc"}}
        with pytest.raises(profile.ProfileError) as caught:
            profile.load("erf/e")
        assert "detect.provider" in str(caught.value)
        assert "anthropc" in str(caught.value)

    def test_a_known_provider_loads(self, tmp_path): ...
    def test_a_non_object_detect_section_is_a_reported_defect(self, tmp_path): ...
    def test_an_empty_model_is_a_reported_defect(self, tmp_path): ...
    def test_the_defect_is_collected_with_the_others(self, tmp_path):
        # Two defects (a bad provider AND a missing color) must BOTH appear -
        # the module's whole point is that five typos take one run, not five.
```

In `tests/test_detect.py`:

```python
def test_the_configured_provider_is_the_one_used(tmp_path, monkeypatch): ...
def test_no_provider_configured_means_anthropic(tmp_path): ...
def test_the_analysis_records_which_provider_produced_it(tmp_path):
    # payload["provider"] == "gemini"; engine stays "model:<model>"
def test_an_unknown_provider_in_config_raises_rather_than_guessing(tmp_path):
    # profile.load already refuses it, but detect must not silently default
    # if handed one directly.
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_profile.py tests/test_detect.py -q -k "Detect or provider"`
Expected: FAIL.

- [ ] **Step 3: Add `profile._validate_detect`**

```python
def _validate_detect(brand: dict, path) -> list[str]:
    """The `detect` section: which provider scores moments, and with what
    model. Both optional - an absent section means Anthropic's default, which
    is what every profile written before this existed gets.

    An unknown provider is a REPORTED DEFECT, never a silent fall back to the
    default. A typo that quietly ran a different vendor than the operator
    asked for is exactly the silent degradation this project has paid for
    before.

    The MODEL is deliberately NOT checked against the provider's catalogue:
    that would mean carrying three vendors' model lists and re-checking them
    monthly. A model the vendor does not know fails at call time, is wrapped
    as ModelError, and falls back to the lexicon with the loud log a missing
    key already produces.
    """
    detect = brand.get("detect")
    if detect is None:
        return []
    if not isinstance(detect, dict):
        return [f"{path}: 'detect' must be an object"]
    problems = []
    provider = detect.get("provider")
    if provider is not None and provider not in providers.PROVIDERS:
        known = ", ".join(sorted(providers.PROVIDERS))
        problems.append(
            f"{path}: detect.provider must be one of {known}, not {provider!r}")
    model = detect.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        problems.append(f"{path}: detect.model must be a non-empty string")
    return problems
```

Call it from wherever `_validate_subtitles` is called, appending to the same
collected list. `from . import providers` at module scope is safe — the
package imports no vendor SDK.

- [ ] **Step 4: Make `detect` provider-aware**

```python
    settings = config.get("detect", {}) or {}
    provider = providers.get(settings.get("provider", providers.DEFAULT_PROVIDER))
    model = settings.get("model", provider.DEFAULT_MODEL)
```

`_caller_from_config` takes the provider module:

```python
def _caller_from_config(workspace_dir: Path, provider, model: str, log,
                        usage=None) -> object | None:
    """A model caller, or None with the reason logged. Never raises."""
    try:
        key = providers.load_api_key(Path(workspace_dir) / "auth",
                                     provider.KEY_FILENAME)
    except providers.MissingKey as error:
        log.warning("no %s API key (%s) - falling back to the lexicon engine, "
                    "whose results are markedly weaker", provider.PROVIDER_ID, error)
        return None
    try:
        return provider.make_caller(key, model=model, usage=usage)
    except (providers.ModelError, providers.SdkUnavailable) as error:
        # OUR exception types, carrying OUR messages - safe to log in full.
        log.warning("cannot reach the model (%s) - falling back to the lexicon "
                    "engine, whose results are markedly weaker", error)
        return None
    except Exception as error:      # noqa: BLE001 - must degrade, not abort
        # An exception the provider did NOT wrap is one whose text this project
        # has made no promise about, and the request it describes carries the
        # API key. Log the type name and nothing else.
        log.warning("cannot reach the model (%s) - falling back to the lexicon "
                    "engine, whose results are markedly weaker",
                    type(error).__name__)
        return None
```

`_report_usage(usage, provider, model, log)` reads `provider.PRICES` instead
of the imported `PRICES`; drop `from .estimate import PRICES`.

**`engine` keeps its exact current format**, `f"model:{model}"`. The provider
goes in a NEW payload field beside it:

```python
        "engine": engine,
        "configured_provider": provider.PROVIDER_ID,
```

Additive, so nothing that reads `engine` today changes meaning. Do not fold
the provider into the `engine` string.

**The name is `configured_provider`, not `provider`** — Task 4's review
earned that rename. The field records what the PROFILE ASKED FOR, not what
produced the moments: it is written even when `engine` is `"none"` (a
zero-word transcript), where no caller is built and no key is ever consulted,
so a bare `provider` invites a badge that credits a vendor which was never
attempted. `engine` says what ran; `configured_provider` says what was asked
for. A UI that renders one without the other is the silent-degradation shape
this project keeps paying for.

Two consequences for Task 7: `StreamAnalysis` in `web/src/api.ts` gains
`configured_provider: string | null`, and the SAME change must add
`"configured_provider": None` to the synthesised never-analysed payload in
`api.py` (~line 1468) — otherwise a never-analysed stream hands the client
`undefined` for a non-optional field.

- [ ] **Step 5: Make `estimate` take its prices**

`estimate_run(words, lexicon, *, model, prices)`; delete the module-level
`PRICES` and the paragraph of its docstring that describes it, replacing it
with one line saying the table now belongs to the provider. `estimate.py`
must keep importing nothing from `providers` — it stays pure.

The studio's estimate route:

```python
        settings = profile.config.get("detect", {}) or {}
        provider = providers.get(settings.get("provider", providers.DEFAULT_PROVIDER))
        model = body.model or settings.get("model", provider.DEFAULT_MODEL)
        return estimate.estimate_run(transcript["words"],
                                     profile.config.get("lexicon", LEXICON_EMPTY),
                                     model=model, prices=provider.PRICES)
```

- [ ] **Step 6: Run the tests, then lint and the full suite**

Run: `PYTHONPATH=src .venv/bin/pytest -q` (foreground, timeout 600000)
Expected: PASS, everything, including all three providers' conformance runs.

- [ ] **Step 7: Commit**

```bash
git add src/yt_shorts tests/
git commit -m "feat(detect): the provider is a validated config key, defaulting to anthropic"
```

---

### Task 5: The brand editor's detect section, server side

**Files:**
- Modify: `src/yt_shorts/brand_admin.py`
- Modify: `src/yt_shorts/studio/api.py` (`BrandPatchBody`)
- Test: `tests/test_brand_admin.py`, `tests/test_studio_api.py`

**Interfaces:**
- Consumes: `profile._validate_detect` (Task 4).
- Produces: `detect` accepted in a brand patch and in `PUT …/brand`.

- [ ] **Step 1: Write the failing tests**

```python
class TestDetectSection:
    def test_a_valid_detect_section_is_stored(self, tmp_path):
        brand_admin.update_brand(channels_dir, "erf",
                                 {"detect": {"provider": "gemini", "model": "x"}})
        assert brand_admin.read_brand(channels_dir, "erf")["detect"]["provider"] == "gemini"

    def test_an_unknown_provider_is_refused(self, tmp_path):
        with pytest.raises(brand_admin.BrandAdminError) as caught:
            brand_admin.update_brand(channels_dir, "erf", {"detect": {"provider": "nope"}})
        assert caught.value.kind == "bad_detect"

    def test_a_patch_this_accepts_is_one_profile_load_accepts(self, tmp_path):
        # The module's standing invariant. Save a detect section, then load
        # the profile and assert it does not raise.
```

And in `tests/test_studio_api.py`, that `PUT …/brand` with a bad provider is
a 400 and with a good one is a 200 whose response carries it back.

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_brand_admin.py -q -k Detect`
Expected: FAIL — the section is dropped, not stored.

- [ ] **Step 3: Accept and validate the section**

In `update_brand`, add `"detect"` to the tuple, keeping the comment above it
truthful (it currently enumerates the sections in prose):

```python
    for key in ("colors", "fonts", "subtitles", "logo", "output", "upload",
                "bands", "detect"):
```

In `_validate`, beside the existing `profile._validate_subtitles` call:

```python
    # Same borrowing as subtitles above: profile owns the rule, so a detect
    # section this accepts is one profile.load accepts.
    problems = profile._validate_detect(brand, brand_path)
    if problems:
        raise BrandAdminError(problems[0], kind="bad_detect")
```

Add `"bad_detect"` to `BrandAdminError`'s documented kinds and map it to 400
in `api._brand_status` (check whether the existing mapping already defaults
unknown kinds to 400; if it does, say so rather than adding a redundant
branch).

In `BrandPatchBody`, add `detect: dict | None = None` and thread it into the
patch the same way the other sections are threaded.

- [ ] **Step 4: Run tests, lint, full suite, commit**

```bash
git add src/yt_shorts tests/
git commit -m "feat(studio): the brand editor may set the detection provider"
```

---

### Task 6: The provider key routes and the settings block

**Files:**
- Modify: `src/yt_shorts/studio/api.py`
- Test: `tests/test_studio_api.py`

**Interfaces:**
- Consumes: `providers.save_api_key`, `forget_api_key`, `has_api_key`,
  `sdk_installed`, `ordered`, `get`, `UnknownProvider` (Task 1).
- Produces: `PUT /api/providers/{provider_id}/key`,
  `DELETE /api/providers/{provider_id}/key`, and a `providers` array in
  `GET /api/settings`.

- [ ] **Step 1: Write the failing tests**

```python
class TestProviderKeys:
    def test_settings_lists_every_provider_with_its_state(self, client):
        body = client.get("/api/settings").json()
        ids = [row["id"] for row in body["providers"]]
        assert ids[0] == "anthropic"          # the default sorts first
        assert set(ids) == {"anthropic", "gemini", "openai"}
        row = body["providers"][0]
        assert row["key_present"] is False and isinstance(row["sdk_installed"], bool)
        assert row["verified"] is True

    def test_openai_is_marked_unverified(self, client):
        rows = {r["id"]: r for r in client.get("/api/settings").json()["providers"]}
        assert rows["openai"]["verified"] is False

    def test_saving_a_key_makes_it_present(self, client, workspace):
        assert client.put("/api/providers/gemini/key",
                          json={"api_key": "abc123"}).status_code == 200
        rows = {r["id"]: r for r in client.get("/api/settings").json()["providers"]}
        assert rows["gemini"]["key_present"] is True

    def test_the_key_is_never_returned_anywhere(self, client, workspace):
        secret = "sk-DO-NOT-LEAK-987654321"
        put = client.put("/api/providers/gemini/key", json={"api_key": secret})
        assert secret not in put.text
        assert secret not in client.get("/api/settings").text

    def test_the_key_file_is_owner_only(self, client, workspace): ...
    def test_an_empty_key_is_400(self, client): ...
    def test_a_key_with_a_newline_is_400(self, client): ...
    def test_an_over_long_key_is_400(self, client): ...
    def test_a_rejected_key_is_not_echoed_in_the_error(self, client):
        secret = "bad\nkey-SECRET-123"
        response = client.put("/api/providers/gemini/key", json={"api_key": secret})
        assert response.status_code == 400 and "SECRET" not in response.text

    def test_an_unknown_provider_is_404_and_touches_no_file(self, client, workspace):
        assert client.put("/api/providers/../../etc/passwd/key",
                          json={"api_key": "x"}).status_code in (404, 405)
        assert client.put("/api/providers/nope/key",
                          json={"api_key": "x"}).status_code == 404

    def test_deleting_forgets_the_key(self, client, workspace): ...
    def test_deleting_a_key_that_is_not_there_is_404(self, client): ...
    def test_each_channel_row_reports_its_provider_and_model(self, client): ...
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q -k ProviderKeys`
Expected: FAIL — 404 for the routes, `KeyError: 'providers'` for settings.

- [ ] **Step 3: Add the routes**

```python
class ProviderKeyBody(BaseModel):
    api_key: str


def _provider_or_404(provider_id: str):
    """The provider module, or 404.

    Validated against the REGISTRY, not against a name pattern: the set of
    valid providers is closed, which is stronger than pathnames.
    validate_segment and means an unknown id cannot reach the filesystem at
    all. The key filename comes from the module, never from the URL.
    """
    try:
        return providers.get(provider_id)
    except providers.UnknownProvider as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.put("/api/providers/{provider_id}/key")
def put_provider_key(provider_id: str, body: ProviderKeyBody) -> dict:
    """Stores this provider's API key at <workspace>/auth/<provider>.json.

    The key is written 0600 and atomically (see providers.save_api_key) and
    is NEVER returned, logged, or quoted in an error message - the response
    and GET /api/settings both carry booleans only, exactly as the YouTube
    connection state does.
    """
    provider = _provider_or_404(provider_id)
    auth_dir = _resolve_workspace().root / "auth"
    try:
        providers.save_api_key(auth_dir, provider.KEY_FILENAME, body.api_key)
    except ValueError as error:
        # save_api_key's messages name the constraint, never the value.
        raise HTTPException(status_code=400, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(
            status_code=500,
            detail=f"cannot write the key file: {type(error).__name__}") from error
    return {"provider": provider.PROVIDER_ID, "key_present": True}


@app.delete("/api/providers/{provider_id}/key")
def delete_provider_key(provider_id: str) -> dict:
    """Forgets this provider's key. Reversible by pasting it again - the same
    shape as disconnecting a YouTube channel, and equally never touching
    anything else in auth/."""
    provider = _provider_or_404(provider_id)
    auth_dir = _resolve_workspace().root / "auth"
    if not providers.forget_api_key(auth_dir, provider.KEY_FILENAME):
        raise HTTPException(
            status_code=404,
            detail=f"no key was stored for {provider.PROVIDER_ID}")
    return {"provider": provider.PROVIDER_ID, "key_present": False}
```

Register both BEFORE the SPA fallback, like every other `/api` route.

- [ ] **Step 4: Add the settings block**

In `get_settings`, beside `"workspace"` and `"channels"`:

```python
        "providers": [
            {"id": module.PROVIDER_ID,
             "default_model": module.DEFAULT_MODEL,
             "key_present": providers.has_api_key(auth_dir, module.KEY_FILENAME),
             "sdk_installed": providers.sdk_installed(module),
             "install": module.INSTALL,
             "verified": module.VERIFIED}
            for module in providers.ordered()
        ],
```

And per channel row, read-only:

```python
                detect = _channel_config(info.name).get("detect", {}) or {}
                row["detect_provider"] = detect.get("provider", providers.DEFAULT_PROVIDER)
                row["detect_model"] = detect.get("model", "")
```

Guard that read the way the neighbouring reads in this loop are guarded — a
malformed `brand.json` must produce a row with a reason, never a 500 for the
whole page.

- [ ] **Step 5: Add both routes to the docstring route list** at the top of
`api.py`, in the same style as the existing entries.

- [ ] **Step 6: Verify `create_app()` still pulls no vendor SDK**

Re-run Task 1 Step 9's command. Expected: `ok`. `sdk_installed` uses
`find_spec` precisely so rendering a settings page does not import three
vendor SDKs into the studio process.

- [ ] **Step 7: Run tests, lint, full suite, commit**

```bash
git add src/yt_shorts/studio/api.py tests/test_studio_api.py
git commit -m "feat(studio): store and forget a provider's API key from Settings"
```

---

### Task 7: The frontend — Settings block and brand-editor section

**Files:**
- Create: `src/yt_shorts/studio/web/src/providers.ts`
- Create: `src/yt_shorts/studio/web/src/providers.test.ts`
- Modify: `src/yt_shorts/studio/web/src/api.ts`
- Modify: `src/yt_shorts/studio/web/src/brandForm.ts` (+ its test)
- Modify: `src/yt_shorts/studio/web/src/components/SettingsScreen.tsx`
- Modify: `src/yt_shorts/studio/web/src/components/BrandEditor.tsx`
- Rebuild: `src/yt_shorts/studio/static/` (committed)

**Interfaces:**
- Consumes: `GET /api/settings`'s `providers` array, `PUT`/`DELETE
  /api/providers/{id}/key`, `PUT …/brand`'s `detect` section (Tasks 5, 6).

**Pure logic goes in `providers.ts`, not in a component** — Vite's
fast-refresh boundary stays component-only, the same convention
`words.ts`/`trim.ts`/`settings.ts` already follow, and it is what makes the
logic unit-testable without rendering.

- [ ] **Step 1: Write `providers.ts` and its failing tests**

```typescript
export interface ProviderState {
  id: string
  default_model: string
  key_present: boolean
  sdk_installed: boolean
  install: string
  verified: boolean
}

/** The display name for a provider id. Falls back to the id itself for one
 * this build does not know, so a server ahead of this client shows something
 * honest rather than blank. */
export function providerLabel(id: string): string

/** Why this provider cannot be used right now, or null when it can.
 * Ordered by what the operator must do FIRST: an absent SDK cannot be fixed
 * by pasting a key. */
export function providerBlocker(state: ProviderState): string | null

/** The model field's value when the operator switches provider: that
 * provider's default, never the previous provider's model - carrying a
 * model name across vendors is how you find out three hours into a run. */
export function modelOnProviderChange(states: ProviderState[], id: string): string
```

Tests to write first, in `providers.test.ts`:

```typescript
describe('providerBlocker', () => {
  it('names the missing SDK before the missing key', ...)
  it('names the missing key when the SDK is installed', ...)
  it('returns null when both are in place', ...)
})

describe('modelOnProviderChange', () => {
  it('returns the target provider default', ...)
  it('returns an empty string for an unknown provider rather than guessing', ...)
})

describe('providerLabel', () => {
  it('falls back to the id for an unknown provider', ...)
})
```

- [ ] **Step 2: Run them and watch them fail**

Run: `cd src/yt_shorts/studio/web && npm test -- providers`
Expected: FAIL.

- [ ] **Step 3: Implement, and watch them pass**

Run: `cd src/yt_shorts/studio/web && npm test -- providers`
Expected: PASS.

- [ ] **Step 4: Add the api.ts functions**

`putProviderKey(id, apiKey)` and `deleteProviderKey(id)`, plus the
`ProviderState` field on the settings response type. Follow the file's
existing error-handling convention exactly.

- [ ] **Step 5: The Settings block**

A "Model providers" section above or below the channels table, one row per
provider from `providers.ordered()`'s order, showing: the label, the default
model, a badge for the key state, `providerBlocker`'s sentence when there is
one (with the `install` line when the SDK is missing), and — for a provider
with `verified === false` — a plainly worded warning that it has not been
measured against the real service. A `PasswordInput` plus Save, and a Remove
with a confirmation, only when a key is present.

**The field must never be pre-filled**, because the server never returns a
key: an input that looks populated would imply the studio holds something it
does not.

**Scrolling is a mandatory acceptance criterion here.** Verify at a short
viewport that every row and both buttons are reachable, and that adding this
block did not starve any existing scroll area — the hit list's 120px floor
exists because exactly that happened before.

- [ ] **Step 6: The brand editor's Moment-detection section**

A `Select` over the provider ids and a `TextInput` for the model, seeded
from the loaded brand and defaulting to the provider's default. On provider
change, set the model field from `modelOnProviderChange`. Extend
`brandForm.ts`'s `BrandEditorForm` with the two fields and its reader/writer
with the `detect` section, extending `brandForm.test.ts` in the same pass.

Show, next to an unverified provider in the select, the same plainly worded
caveat Settings shows — an operator choosing it in the editor must not have
to have visited Settings to learn it.

**And show what it costs, at the moment of choosing.** Beside the caveat,
render the selected provider's own `PRICES` row for the selected model, plus
that provider's cheapest priced model for comparison — so picking OpenAI
reads as "unverified, $2.00/$12.00 per 1M input/output, the cheapest here is
$0.20/$1.20" rather than being discovered on a bill.

This exists because Task 3's review found a real hole: `openai_api.
DEFAULT_MODEL` is deliberately not the cheapest entry (picking cheapest is
the argument the Anthropic bake-off refuted on this project's own
transcripts), but OpenAI is never measured, so "provisional" has no
scheduled end and the price gap has no evidence behind it either way. The
reviewer offered two remedies — measure OpenAI in Task 8, or disclose the
exposure at the point of choice. The operator's standing decision is that
OpenAI stays unmeasured, so this is the one being taken. It is the same move
`VERIFIED` already makes for correctness: turn a hidden risk into a
disclosed one.

Do not present these numbers as a bill. They are a per-million rate from a
dated snapshot, and both `gemini_api` and `openai_api` document that tiering
makes their tables a FLOOR for long windows — which this project's endurance
streams reach. Say "from" or "at least", never a total.

- [ ] **Step 7: Type-check, test and build**

```bash
cd src/yt_shorts/studio/web
npm test
npm run build
```

Expected: both green. `npm run build` is the real type-check — a clean bare
`npx tsc --noEmit` is no signal at all in this project.

- [ ] **Step 8: Commit the rebuilt bundle with the sources**

```bash
git add src/yt_shorts/studio/web src/yt_shorts/studio/static
git commit -m "feat(studio-web): choose a detection provider, and manage its key"
```

---

### Task 8: The measurement, and the documentation

**Files:**
- Modify: `src/yt_shorts/providers/gemini_api.py` (`DEFAULT_MODEL`, `VERIFIED`, the bake-off comment)
- Modify: `README.md`
- Modify: `CLAUDE.md`

**This task needs the operator's Gemini API key and costs a few cents.** It
cannot be done by a subagent without one. Stop and hand back if the key is
not present at `<workspace>/auth/gemini.json`.

On cost, precisely: a Flash model at $1.50/$7.50 per 1M tokens over a
98-minute qualifying is roughly a third of what the same run cost on
`claude-opus-5` (~$0.06 measured), so expect a few cents, not a euro. The
free tier does NOT make it zero here — Google's terms restrict free-tier use
for the EEA, Switzerland and the UK, where billing must be enabled even for
free-tier-eligible models (see the spec's decision 3). Budget cents, not
nothing.

- [ ] **Step 1: Confirm the key is in place**

The operator places it, or pastes it in the studio's new Settings block —
which is also a live check that Task 6 and Task 7 actually work.

Getting one: `aistudio.google.com/apikey`, accept the terms, and a default
Google Cloud project plus key is created automatically; then "Create API
key". In the EEA/Switzerland/UK, billing must additionally be enabled on
that project — if the first call fails with a quota or permission error,
that is the cause, not the code.

- [ ] **Step 2: Run detection with Gemini over the reference stream**

The same 98-minute qualifying the Anthropic bake-off used, so the numbers are
comparable. Set the channel's provider to `gemini` (in the studio, again
exercising Task 7), run `bin/yt-shorts detect`, and record from the run's own
log line: moments found, how many of the operator's four known-good moments
were among them, the measured token counts, and the cost.

Use a free-tier Flash model first. Only if it falls down badly, try one paid
model and record both.

- [ ] **Step 3: Pin `DEFAULT_MODEL` and flip `VERIFIED`**

Write the bake-off comment in the same form `anthropic_api.py`'s already
takes: the date, the stream, every model tried, moments found, how many of
the four it caught, and the cost. Set `VERIFIED = True`.

If the measurement shows Gemini performing markedly worse than Anthropic,
that is a RESULT, not a failure — record it honestly. Anthropic stays the
default either way.

- [ ] **Step 4: README**

A "Model providers" section: how to choose one, where the keys live, how to
set one from Settings, and the three-way gradation of claims, in exactly this
order and no stronger than this:

- **Anthropic** — the default; measured, with the existing bake-off table.
- **Gemini** — measured, with Step 3's table and its date.
- **OpenAI** — ships and is tested against a fake SDK; **not verified against
  the real service**.

On Gemini's free tier, state the caveat rather than the headline: it exists
on the Flash models, but Google's terms restrict free-tier use for the EEA,
Switzerland and the UK, where billing must be enabled even for
free-tier-eligible models. Do not write "try it for free" without that
sentence beside it — a European reader following an unqualified claim hits a
billing error and concludes the tool is broken.

Plus the eight-name contract and a pointer to
`tests/test_provider_contract.py`, so a contributor adding a fourth provider
knows exactly what they must satisfy.

- [ ] **Step 5: CLAUDE.md**

- The moment-detection section: five modules become a package; name the
  registry, the contract and the conformance suite.
- The key-secrecy paragraph: it is now a shared, tested rule covering three
  providers, and the three entry points are pinned by
  `test_provider_contract.py` rather than by one module's own tests.
- The optional-dependency list: `google-genai` and `openai` join `anthropic`,
  all lazy, none required.
- The `detect` config section: `provider` and `model`, validated by
  `profile._validate_detect`.
- **Correct the existing error:** the file still says `brand_admin` edits
  "`colors`, `fonts` and `subtitles` only" and never takes `output` from the
  patch. It has accepted seven sections including `output` for some time, and
  eight after Task 5.

- [ ] **Step 6: Full suite, lint, commit**

```bash
python3 tools/lint.py && PYTHONPATH=src .venv/bin/pytest -q
git add src/yt_shorts/providers/gemini_api.py README.md CLAUDE.md
git commit -m "docs: three providers, measured where measured and labelled where not"
```

---

## Self-Review

**Spec coverage.** Package and contract → Task 1. Gemini → Task 2. OpenAI and
the schema dialect → Task 3. Config key, validation, `estimate`, `detect`,
the `provider` payload field → Task 4. Brand editor server side → Task 5. Key
routes and Settings → Task 6. Both UI surfaces → Task 7. Measurement, README
gradation, CLAUDE.md → Task 8. The conformance suite's nine properties →
Task 1 Step 5, extended by Tasks 2 and 3. Every spec section has a task.

**Type consistency.** `make_caller(api_key, *, model, max_tokens, sdk, usage)`
is identical in Tasks 1, 2 and 3. `Usage.record(extract)` is used the same way
in all three. `estimate_run(words, lexicon, *, model, prices)` is defined in
Task 4 Step 5 and called with exactly those names in Task 4 Step 5's route.
`providers.get`/`ordered`/`has_api_key`/`sdk_installed` are defined in Task 1
and used with the same signatures in Tasks 4 and 6. `ProviderState`'s six
fields (Task 7 Step 1) match the six keys the settings route emits (Task 6
Step 4).

**Known soft spots, flagged rather than hidden.**

- Task 2 Step 1 and Task 3 Step 1 require the implementer to establish three
  SDK facts from the installed package. That is research with a stated method
  and a stated deliverable, not a placeholder — but it is the part most
  likely to come back as BLOCKED, and the right response is to report what
  could not be established rather than to invent it.
- Task 1 is large. It cannot be split: the move and the registry are one
  reviewable change, and splitting them would leave the tree with two import
  graphs.
- Task 8 needs a real key and real money. It is last for that reason, and
  everything before it is complete and useful without it.
