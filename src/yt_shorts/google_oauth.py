"""Production Google adapter for yt_shorts.auth (imports Google lazily).

Every method imports the Google library at call time, so importing this module,
and constructing GoogleOAuth, needs nothing installed - the orchestration in
yt_shorts.auth stays usable and testable without google, and only an actual
consent/refresh/upload touches it. The orchestration this serves is tested in
tests/test_auth.py with a fake adapter; this thin wrapper is not itself unit
tested against the network.
"""

from __future__ import annotations

import json
from pathlib import Path

# Bound how long the browser consent may block. Without this, run_local_server
# waits FOREVER for the redirect - an abandoned sign-in, or two connect flows
# back-to-back in one studio process where the second local redirect server
# never gets its callback, left the connect job stuck "running" and needed a
# process restart to clear. A bounded wait fails cleanly instead (see the stage
# E connect-hang fix). Two minutes is comfortably longer than a real sign-in.
CONSENT_TIMEOUT_SECONDS = 120


class GoogleOAuth:
    def run_consent(self, client_secret_path: Path, scopes, *,
                    timeout_seconds: int = CONSENT_TIMEOUT_SECONDS):
        from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secret_path), scopes)
        try:
            return flow.run_local_server(port=0, timeout_seconds=timeout_seconds)
        except WSGITimeoutError as error:
            # No redirect arrived within the window - surface a clear, bounded
            # failure so the connect job records "failed" instead of hanging.
            raise RuntimeError(
                f"OAuth consent was not completed within {timeout_seconds}s - "
                "the browser sign-in did not finish, or the redirect never "
                "arrived. Start the connection again and complete Google's "
                "consent screen."
            ) from error

    def to_json(self, creds) -> str:
        return creds.to_json()

    def from_json(self, text: str):
        from google.oauth2.credentials import Credentials
        return Credentials.from_authorized_user_info(json.loads(text))

    def valid(self, creds) -> bool:
        return bool(getattr(creds, "valid", False))

    def ensure_fresh(self, creds):
        from google.auth.transport.requests import Request
        if getattr(creds, "expired", False) and getattr(creds, "refresh_token", None):
            creds.refresh(Request())
        return creds

    def authorized_channel(self, creds) -> tuple[str, str]:
        """The (id, title) of the channel this consent actually granted.

        Reads it from the API (channels.list mine=true) rather than trusting what
        the operator was asked to connect, so a consent granted as the wrong
        channel (e.g. a personal account instead of the brand channel) can be
        caught. Needs the youtube.readonly scope, which is requested alongside
        upload."""
        service = build_service(creds)
        response = service.channels().list(part="snippet", mine=True).execute()
        items = response.get("items", [])
        if not items:
            raise RuntimeError("the authorized account has no YouTube channel")
        channel = items[0]
        return channel["id"], channel.get("snippet", {}).get("title", channel["id"])


def build_service(credentials):
    """Builds the authenticated YouTube Data API v3 client."""
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=credentials)
