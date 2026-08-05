"""Optional-dependency guard for the Google upload libraries.

The Google client is only needed for stage E (upload). Like FastAPI for the
studio, it is imported LAZILY so harvest/render/gallery/migrate run in a venv
that never installed it; this turns a missing library into a readable message.
"""

from __future__ import annotations

INSTALL = ".venv/bin/pip install google-api-python-client google-auth-oauthlib"


class GoogleUnavailable(Exception):
    """The Google upload libraries are not installed."""


def _import_google() -> None:
    import google_auth_oauthlib.flow  # noqa: F401
    import googleapiclient.discovery  # noqa: F401


def require(feature: str) -> None:
    """Raises GoogleUnavailable with an install hint if the libraries are absent."""
    try:
        _import_google()
    except ImportError as error:
        raise GoogleUnavailable(
            f"{feature} needs the Google libraries, which are not installed in "
            f"this venv.\nInstall them with: {INSTALL}\n"
            f"Every other command works without them.\n({error})"
        ) from error
