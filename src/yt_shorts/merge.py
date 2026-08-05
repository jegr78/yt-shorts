"""Deep merge for profile dictionaries: event overrides channel, key by key.

Used by yt_shorts.profile to layer an event's brand.json over its channel's
brand.json. Only leaf values are replaced - a nested dict is merged
recursively, not overwritten wholesale, so an event naming 'colors.accent'
keeps the channel's 'colors.base'. Lists are replaced wholesale on purpose:
there is no list in the profile format today, and merging lists is the kind
of cleverness that surprises people later.
"""

from __future__ import annotations


def deep_merge(base: dict, override: dict) -> dict:
    """Returns a new dict: base overlaid with override, recursively per key.

    Neither input is mutated. A key present in both, with dict values on
    both sides, is merged recursively. Any other type (str, int, list, ...)
    in override simply replaces the corresponding value in base, even if
    base held a dict there.
    """
    result = dict(base)
    for key, value in override.items():
        base_value = result.get(key)
        if isinstance(base_value, dict) and isinstance(value, dict):
            result[key] = deep_merge(base_value, value)
        else:
            result[key] = value
    return result
