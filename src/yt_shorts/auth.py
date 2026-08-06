"""OAuth credentials and per-channel token storage for uploads.

All Google interaction is behind an injected `oauth` adapter (production impl in
yt_shorts.google_oauth, which imports the Google library lazily), so this module
is pure orchestration over file I/O and that adapter and tests need no network,
no Google, no real consent. Tokens are keyed by YouTube channel id: authorizing
'switches account' by granting the right Google account's consent for a channel.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import ownermode
from .pathnames import validate_segment

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

CLIENT_SECRET_FILE = "client_secret.json"


class AuthError(Exception):
    """Understandable message about a failed or missing authorization."""


class TokenStore:
    def __init__(self, auth_dir):
        self.auth_dir = Path(auth_dir)

    def path(self, channel_id: str) -> Path:
        # channel_id becomes a filename (token-<id>.json). It reaches here from an
        # operator-editable HTTP field (studio connect/disconnect), so validate it
        # as one safe segment before it can traverse - a '../..' id must never read
        # or unlink a file outside auth/. Real YouTube ids are [A-Za-z0-9_-].
        try:
            validate_segment(channel_id, what="channel id")
        except ValueError as error:
            raise AuthError(str(error)) from error
        return self.auth_dir / f"token-{channel_id}.json"

    def load(self, channel_id: str) -> str | None:
        path = self.path(channel_id)
        return path.read_text(encoding="utf-8") if path.exists() else None

    def save(self, channel_id: str, text: str) -> None:
        # The token file holds a long-lived OAuth refresh token = standing upload
        # credentials, so it must not be world/group readable. Create auth/ as 0700
        # and write the token 0600, regardless of the process umask (mkdir's mode is
        # umask-masked, so chmod explicitly).
        path = self.path(channel_id)
        self.auth_dir.mkdir(parents=True, exist_ok=True, mode=ownermode.DIR_MODE)
        ownermode.restrict(self.auth_dir)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, ownermode.FILE_MODE)
        try:
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)
        ownermode.restrict(path)


def _client_secret_path(auth_dir: Path) -> Path:
    path = Path(auth_dir) / CLIENT_SECRET_FILE
    if not path.exists():
        raise AuthError(
            f"No {CLIENT_SECRET_FILE} in {auth_dir}. Create an OAuth client in your "
            "Google Cloud project and place its client_secret.json there (see "
            "README, 'Upload')."
        )
    return path


def load_credentials(channel_id, *, auth_dir, oauth, store=None):
    """Fresh credentials for the channel, refreshing a stale token, or None."""
    store = store or TokenStore(auth_dir)
    text = store.load(channel_id)
    if text is None:
        return None
    creds = oauth.from_json(text)
    if not oauth.valid(creds):
        creds = oauth.ensure_fresh(creds)
        store.save(channel_id, oauth.to_json(creds))
    return creds


def forget_credentials(channel_id, *, auth_dir, store=None) -> bool:
    """Delete the stored OAuth token for ``channel_id`` (the studio's
    "disconnect"). Removes only ``auth/token-<channel_id>.json`` - never the
    client secret or the quota file - and is reversible by connecting again.
    Returns True if a token file was removed, False if there was none. Imports
    no google: forgetting a token is a local file op, unlike authorize()."""
    store = store or TokenStore(auth_dir)
    path = store.path(channel_id)
    if path.exists():
        path.unlink()
        return True
    return False


def authorize(channel_id, *, auth_dir, oauth, store=None, force=False):
    """Valid credentials for the channel, running consent and storing on first use.

    After consent, the channel the operator actually granted is read back from the
    API (``oauth.authorized_channel``) and must match ``channel_id``. Consenting as
    a different channel - the classic footgun of picking a personal account in
    Google's channel chooser instead of the brand channel - is REFUSED and nothing
    is stored, rather than silently saving the wrong account's token under this
    channel's id and later uploading to the wrong place.

    ``force`` re-runs consent even when a valid token is already stored - the way
    to switch which account a channel is connected as, or to recover from a token
    stored (before this check existed) for the wrong account, without deleting the
    file by hand.
    """
    store = store or TokenStore(auth_dir)
    if not force:
        existing = load_credentials(channel_id, auth_dir=auth_dir, oauth=oauth, store=store)
        if existing is not None:
            return existing
    client_secret = _client_secret_path(Path(auth_dir))
    creds = oauth.run_consent(client_secret, SCOPES)
    actual_id, actual_title = oauth.authorized_channel(creds)
    if actual_id != channel_id:
        raise AuthError(
            f"You authorized {actual_title} ({actual_id}), but connecting this "
            f"channel needs {channel_id}. Re-run and pick the right channel in "
            "Google's consent screen (for a brand channel, choose it from the "
            "channel list rather than your personal account). Nothing was stored."
        )
    store.save(channel_id, oauth.to_json(creds))
    return creds
