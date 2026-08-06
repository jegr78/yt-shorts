"""Every route with a path parameter refuses a traversal segment.

The 19 hand-written `%2e%2e` cases in test_studio_api.py each pin one route.
This walks the app's OWN route table instead, so a route added later is
covered the day it is added rather than the day someone remembers to write a
test for it. CodeQL reports 58 py/path-injection alerts across these routes
because it cannot see pathnames.validate_segment through the admin helpers
that call it; this is the evidence that the guard is nevertheless there.

Encoded (`%2e%2e`), never a literal `..`: httpx normalises literal dot
segments client-side, so the request that reaches the app would not carry
them (CLAUDE.md, "Tests").

The assertion is the filesystem, not the status code. A 4xx could mean the
guard fired or merely that a POST lacked its body; what cannot be argued with
is that nothing outside the workspace was created, changed or read away.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from yt_shorts.studio.api import create_app

TRAVERSAL = "%2e%2e"


def _parameterised_routes(app):
    """(method, path) for every route carrying a path parameter, minus the
    SPA catch-all (`/{full_path:path}`), which is a static-file server with
    its own resolve()-and-contain check rather than a workspace route."""
    out = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if "{" not in path or path.startswith("/{"):
            continue
        for method in sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}):
            out.append((method, path))
    return sorted(out)


def _fill(path: str) -> str:
    """Put the traversal segment in EVERY path parameter at once."""
    out, depth = [], 0
    for char in path:
        if char == "{":
            depth += 1
            if depth == 1:
                out.append(TRAVERSAL)
        elif char == "}":
            depth -= 1
        elif depth == 0:
            out.append(char)
    return "".join(out)


def _snapshot(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


@pytest.fixture
def outside(tmp_path, monkeypatch):
    """A workspace with a sibling directory that must never be touched."""
    root = tmp_path / "workspace"
    (root / "channels").mkdir(parents=True)
    neighbour = tmp_path / "private"
    neighbour.mkdir()
    (neighbour / "secret.txt").write_text("do not read, do not write", encoding="utf-8")
    monkeypatch.setenv("YT_SHORTS_DATA", str(root))
    return tmp_path, neighbour


def test_the_route_table_is_actually_being_walked(outside):
    # Guards the guard: if route discovery silently returned nothing, every
    # assertion below would pass vacuously.
    routes = _parameterised_routes(create_app())
    assert len(routes) > 40, f"only {len(routes)} parameterised routes found"


def test_no_route_with_a_path_parameter_escapes_the_workspace(outside):
    tmp_path, neighbour = outside
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)
    before = _snapshot(neighbour)

    succeeded = []
    for method, path in _parameterised_routes(app):
        target = _fill(path)
        response = client.request(method, target, json={}, headers={"host": "localhost"})
        if 200 <= response.status_code < 300:
            succeeded.append(f"{method} {target} -> {response.status_code}")

    assert _snapshot(neighbour) == before, "a traversal request changed a file outside the workspace"
    assert (neighbour / "secret.txt").read_text(encoding="utf-8") == "do not read, do not write"
    assert not succeeded, "a traversal segment was accepted:\n" + "\n".join(succeeded)
