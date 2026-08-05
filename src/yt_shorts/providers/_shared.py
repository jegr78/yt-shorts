"""Everything the providers share, and nothing that knows which they are.

This module exists to break one import cycle, once, rather than in three
places. `providers/__init__.py` imports the three provider modules at module
scope (so a syntax error in a provider nobody has a key for fails in the suite
rather than for whoever first pastes that key), and each provider module needs
the shared exception types, the key-file helpers and the SDK guard. If those
lived in `__init__` the providers would have to import their own package while
it is still executing.

The split is by what a name needs to KNOW: everything here is independent of
the provider list, and `__init__` keeps only what is not - `CONTRACT`,
`PROVIDERS`, `DEFAULT_PROVIDER`, `UnknownProvider`, `get`, `ordered`. It
re-exports every name below, so `providers.ModelError` and
`providers.load_api_key` keep working and no consumer has to know this file
exists.

Nothing here imports a vendor SDK. `sdk_installed` deliberately answers with
`importlib.util.find_spec` rather than a try/import, so a settings page cannot
pull three vendor SDKs into the studio process just by being rendered.
"""

from __future__ import annotations

import importlib.util
import json
import os
from dataclasses import dataclass
from pathlib import Path

# A key file is written 0600 and never longer than this. The bound is a sanity
# check against a pasted FILE, not a vendor fact - every real key is far
# shorter, and key formats change.
MAX_KEY_LENGTH = 4096


class MissingKey(RuntimeError):
    """No usable API key at <workspace>/auth/<provider>.json."""


class ModelError(RuntimeError):
    """The model did not return a usable answer. Never carries the API key."""


class SdkUnavailable(RuntimeError):
    """The provider's optional SDK is not installed in this venv."""


@dataclass
class Usage:
    """What a run actually cost, in the API's own numbers.

    Exists because this project spent a day believing an estimate. The cost
    preview in `estimate.py` counts characters and divides by four; measured
    against a real invoice it was roughly half the truth, and its own comment
    claimed it ran ~12% HIGH. Nothing here could have caught that, because
    nothing here ever looked at what the API reported - so this records it.

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

        `extract` is a zero-argument callable returning `(input, output)`. It
        is a CALLABLE rather than a pair of field names because vendors name
        and nest their token counts differently - and because reading them can
        itself raise. An earlier version took the response's `usage` object,
        which meant the `getattr` that produced it sat OUTSIDE any handler: a
        property raising anything but AttributeError escaped unwrapped, the
        same class of hole a review already found on Anthropic's `.content`.
        One callable, caught in one place.

        `calls` counts BEFORE the extraction, because a call happened whether
        or not its bookkeeping could be read. A response whose usage cannot be
        read still cost money and still produced an answer: losing the
        bookkeeping is acceptable, losing the window over the bookkeeping is
        not. This runs inside `call`'s response-reading path, where an
        exception would become a ModelError and cost the operator a window of
        their stream.
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

    EVERY unusable key is MissingKey - `detect._caller_from_config` relies on
    exactly that to degrade to the lexicon engine instead of letting a run
    abort. Never str()s the file's content; only the path and an exception's
    own (content-free) message.
    """
    path = Path(auth_dir) / filename
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as error:
        # UnicodeDecodeError alongside OSError, not just OSError: a key file
        # containing one stray non-UTF-8 byte is unusable in exactly the same
        # way a missing or unreadable file is, but read_text raises a
        # ValueError subclass for that case, not an OSError - and this
        # function's whole contract is "every unusable key is MissingKey".
        raise MissingKey(f"cannot read {path}: {error}") from None
    if not raw:
        raise MissingKey(f"{path} is empty")
    if raw.startswith("{"):
        try:
            value = json.loads(raw).get("api_key")
        except json.JSONDecodeError:
            raise MissingKey(f"{path} is neither a raw key nor valid JSON") from None
        except AttributeError:
            # Valid JSON that is not an object - `[1, 2]`, `"x"`, `3` - parses
            # fine and then has no `.get`. DEFENSIVE, and knowingly so: only a
            # leading `{` reaches this branch, and every JSON document that
            # starts with `{` and parses at all is an object, so no file can
            # reach here today. It is kept because the guard above it is a
            # cheap string test that a future change (accepting a leading
            # `[`, trying json.loads first) could widen without anyone
            # noticing that `.get` then vanishes - and the cost of being
            # wrong is an AttributeError escaping a function whose whole
            # contract is "every unusable key is MissingKey".
            raise MissingKey(f"{path} is not a JSON object") from None
        # `.get` returning None (missing OR an explicit `"api_key": null`) and a
        # non-string value (a number, a bool) are both "no usable key" - not
        # `str(value)`, which would turn `null` into the literal, truthy string
        # "None" and hand it back as if it were a real key.
        if not isinstance(value, str) or not value.strip():
            raise MissingKey(f"{path} has no non-empty 'api_key'")
        return value.strip()
    return raw


def has_api_key(auth_dir: str | Path, filename: str) -> bool:
    """Whether a USABLE key is on file. Reads it and throws it away rather than
    merely stat()ing the path, because an empty or malformed file is what an
    operator most needs told apart from a working one."""
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
    # auth/ holds this key alongside client_secret.json and token-<id>.json,
    # so it must not be world/group-listable - mirrors auth.TokenStore.save,
    # which states the same invariant: mkdir's own `mode` is umask-masked (a
    # looser process umask than 0o077 would still leave it group/world
    # readable), and exist_ok=True never repairs an ALREADY-existing
    # directory's mode - so both the mkdir mode and the explicit chmod are
    # needed, the second to cover a directory created before this code ran
    # (or created world-readable by anything else).
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
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


def sdk_installed(package: str) -> bool:
    """Whether an optional SDK is importable, WITHOUT importing IT - though a
    dotted PACKAGE like "google.genai" does import its parent NAMESPACE
    package ("google") as a side effect of find_spec's own lookup (see the
    comment below). The vendor SDK itself is never imported either way, which
    is the property this function exists to guarantee.

    Takes the package NAME (`PACKAGE`, e.g. "anthropic" or "google.genai"),
    never a provider module, so this file stays independent of the provider
    list and a provider can call it about itself (`sdk_installed(PACKAGE)`)
    with no import back into its own package. Passing a module instead of the
    string fails loudly with AttributeError rather than silently answering
    wrong - a call site writes `sdk_installed(module.PACKAGE)`, not
    `sdk_installed(module)`.

    `find_spec` is used rather than a try/import so a settings page cannot pull
    three vendor SDKs into the studio process just by being rendered.
    """
    try:
        return importlib.util.find_spec(package) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        # find_spec imports PARENT packages (so `google.genai` imports
        # `google`) and raises ValueError for some half-installed states.
        # ModuleNotFoundError is technically redundant here - it is an
        # ImportError subclass, so the first clause already catches it - but
        # it is named explicitly anyway because it is the concrete exception
        # find_spec raises for exactly the dotted-PACKAGE case above (a
        # parent that does not exist at all), and spelling it out is clearer
        # than relying on a reader to know the subclass relationship. Either
        # way: not usable.
        return False


def require(package: str, install: str, purpose: str, provider_id: str) -> None:
    """Raises SdkUnavailable with an actionable message, or returns.

    Four plain strings rather than a provider module: `_shared` must not know
    what a provider looks like, and a provider calling `require(PACKAGE,
    INSTALL, "moment detection", PROVIDER_ID)` needs no reference to itself.
    `package` is the same PACKAGE string `sdk_installed` takes, never a
    provider module - passing one fails loudly with AttributeError rather
    than silently answering wrong.
    """
    if not sdk_installed(package):
        raise SdkUnavailable(
            f"{purpose} needs the {provider_id} SDK, which is not installed in "
            f"this venv.\nInstall it with: {install}\n"
            f"Every other command works without it.")
