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

Everything the providers themselves need lives in `_shared.py`, not here, and
is re-exported below. That is what makes the module-scope imports above
possible with no cycle: `_shared` knows nothing about which providers exist,
so a provider importing it never imports this file while it is still running.
"""

from __future__ import annotations

from . import anthropic_api, gemini_api, openai_api
from ._shared import (
    MAX_KEY_LENGTH,
    MissingKey,
    ModelError,
    SdkUnavailable,
    Usage,
    forget_api_key,
    has_api_key,
    load_api_key,
    require,
    save_api_key,
    sdk_installed,
)

__all__ = [
    "CONTRACT", "DEFAULT_PROVIDER", "MAX_KEY_LENGTH", "PROVIDERS", "MissingKey",
    "ModelError", "SdkUnavailable", "UnknownProvider", "Usage", "forget_api_key",
    "get", "has_api_key", "load_api_key", "ordered", "require", "save_api_key",
    "sdk_installed",
]

# The eight names every provider module must expose. Named here rather than
# only in prose so the conformance suite can iterate it.
CONTRACT = ("PROVIDER_ID", "KEY_FILENAME", "DEFAULT_MODEL", "PRICES",
            "PACKAGE", "INSTALL", "VERIFIED", "make_caller")

_MODULES = (anthropic_api, gemini_api, openai_api)
PROVIDERS = {module.PROVIDER_ID: module for module in _MODULES}
DEFAULT_PROVIDER = "anthropic"


class UnknownProvider(ValueError):
    """No provider by that id."""


def ordered() -> list:
    """The provider modules in a stable display order: the default first, then
    the rest alphabetically. The Settings screen renders them in this order, so
    it must not depend on dict insertion order changing."""
    rest = sorted((m for m in _MODULES if m.PROVIDER_ID != DEFAULT_PROVIDER),
                  key=lambda m: m.PROVIDER_ID)
    return [PROVIDERS[DEFAULT_PROVIDER], *rest]


def get(provider_id: str):
    """The provider module, or UnknownProvider. Never guesses a default: a typo
    must be reported, not silently answered with Anthropic."""
    try:
        return PROVIDERS[provider_id]
    except (KeyError, TypeError):
        known = ", ".join(sorted(PROVIDERS))
        raise UnknownProvider(
            f"unknown model provider {provider_id!r}; known: {known}") from None
