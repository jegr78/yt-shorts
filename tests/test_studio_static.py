"""Tests for the studio's static-file serving (see the SPA fallback at the
bottom of yt_shorts.studio.api's create_app): the built React/Vite/Mantine
page under src/yt_shorts/studio/static/, committed so the tool runs from a
clone with no npm install.

A test that only asserts GET "/" returns 200 proves almost nothing - a
stray placeholder file would pass it too. These tests instead confirm the
page served IS the real Vite build (it references an asset filename that
actually exists on disk, content-addressed by its build hash) and that the
referenced asset is itself served with a usable content type - i.e. the
fallback actually wires index.html to its own built assets, not just to some
HTML file. Since G1 the app is workspace-level (create_app() takes no
profile) and the build emits ABSOLUTE asset refs (/assets/...), because the
router puts real client-side paths like /erf/studio-test in the address bar
and a relative base would resolve assets against that deep path.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from yt_shorts.studio import api as studio_api
from yt_shorts.studio.api import create_app


def test_the_built_bundle_is_committed_to_the_repository():
    assert studio_api.STATIC_DIR.is_dir(), (
        f"{studio_api.STATIC_DIR} is missing - run `npm run build` in "
        f"studio/web/ and commit its output (see README.md)"
    )
    assert (studio_api.STATIC_DIR / "index.html").is_file()
    assets = list((studio_api.STATIC_DIR / "assets").glob("*"))
    assert assets, "static/assets/ has no built files"


def test_root_serves_the_real_built_page_referencing_an_asset_that_exists_on_disk():
    client = TestClient(create_app())

    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]

    built_asset_names = {p.name for p in (studio_api.STATIC_DIR / "assets").glob("*")}
    referenced = set(re.findall(r'assets/([\w.-]+)', response.text))
    assert referenced, "served index.html references no /assets/ files at all"
    # Every asset index.html points at must be one the real Vite build
    # produced - not a stale reference to a file that no longer exists.
    assert referenced <= built_asset_names
    # And the reverse must not be vacuously true: the build must have
    # produced at least the JS bundle the page actually loads.
    assert any(name.endswith(".js") for name in referenced)


def test_a_referenced_built_asset_is_served_with_a_script_or_style_content_type():
    client = TestClient(create_app())

    index_response = client.get("/")
    # The build emits absolute asset refs (/assets/...) since G1's router.
    referenced = re.findall(r'href="(/assets/[\w./-]+)"|src="(/assets/[\w./-]+)"',
                            index_response.text)
    asset_paths = [href or src for href, src in referenced]
    assert asset_paths, "no asset paths found in the served index.html"

    for asset_path in asset_paths:
        response = client.get(asset_path)
        assert response.status_code == 200, f"{asset_path} was not served"
        content_type = response.headers["content-type"]
        assert (
            "javascript" in content_type
            or "css" in content_type
        ), f"unexpected content-type for {asset_path}: {content_type}"


def test_an_unknown_client_side_route_serves_the_page_so_a_reload_survives():
    """G1's SPA fallback serves index.html for any non-/api path that is not a
    built asset, so a deep link or reload on a client-side route (/, /{channel},
    /{channel}/{event}) lands on the real page instead of a 404 - the opposite
    of the pre-G1 StaticFiles mount, which 404'd unknown paths."""
    client = TestClient(create_app())

    response = client.get("/erf/some-event")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    # It is the real built page, not an empty shell.
    assert re.search(r'/assets/[\w.-]+\.js', response.text)


def test_api_routes_still_take_priority_over_the_spa_fallback():
    """The fallback is registered last and refuses any /api/* path, so an
    unknown /api/... is still a JSON 404 from the API surface, never the HTML
    page - the fallback must not shadow the API."""
    client = TestClient(create_app())

    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/json"
