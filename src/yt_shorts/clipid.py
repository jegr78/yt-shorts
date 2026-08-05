"""What a clip is called on disk, and why that name never changes.

A clip's identity is its source URL - that is what the clip *is*. Everything
else about it, the title above all, is an attribute a human may edit at any
time. The previous layout derived every filename from the title, so renaming
a clip orphaned its transcript, its raw download and its rendered short under
the old name, and a collision suffix shifted as soon as the order of the
source list changed.

The directory name therefore pairs a readable slug, frozen once at creation
from the harvested title, with a short hash of the canonical URL. The slug is
a label for humans browsing the workspace; the hash is the identity.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse, urlunparse

ID_LENGTH = 8
SLUG_MAX = 50


def canonical_url(url: str) -> str:
    """Strips the parts of a URL that do not change which clip it names.

    A query string (YouTube appends share parameters), a fragment and a
    trailing slash all address the same clip; treating them as different
    would give one clip several identities and several directories.

    Scheme and host names are case-insensitive per RFC 3986; they are
    normalized to lowercase. The path is preserved as-is, since clip IDs
    are case-sensitive.
    """
    cleaned = url.strip()
    for separator in ("#", "?"):
        cleaned = cleaned.split(separator, 1)[0]
    cleaned = cleaned.rstrip("/")

    # Normalize scheme and host to lowercase (case-insensitive per RFC 3986)
    # while preserving path case (clip IDs are case-sensitive)
    parsed = urlparse(cleaned)
    normalized = urlunparse((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        parsed.path,
        parsed.params,
        parsed.query,
        parsed.fragment
    ))
    return normalized


def require_http_url(url: str) -> str:
    """Return `url` unchanged if it is an http(s) URL; raise ValueError otherwise.

    A source/channel URL is handed to yt-dlp as a positional argument. Constrain
    the scheme so a 'file:///etc/passwd' (local-file read / SSRF) or a value
    starting with '-' (parsed by yt-dlp as an option such as --exec -> command
    execution) can never reach it. Call sites additionally place '--' before the
    URL so a leading-dash value can never be read as a flag.
    """
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"not an http(s) URL: {url!r}")
    return url


def clip_id(url: str) -> str:
    """A short, stable identity for a clip, derived from its URL.

    Raises ValueError if the URL canonicalizes to nothing usable (empty,
    query/fragment only, or only whitespace).
    """
    canonical = canonical_url(url)
    if not canonical:
        raise ValueError(f"empty or unusable URL: {url!r}")
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digest[:ID_LENGTH]


def slug(title: str) -> str:
    """A readable, filesystem-safe label. May be empty."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:SLUG_MAX].strip("-")


def directory_name(url: str, title: str) -> str:
    """The clip's directory name: '<slug>--<id>', or '<id>' with no slug."""
    label = slug(title)
    identity = clip_id(url)
    return f"{label}--{identity}" if label else identity
