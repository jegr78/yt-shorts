"""The six glossary routes (GET/PUT at three scopes), the track selector
(GET /api/tracks) and the track field on the PUT routes - a thin layer over
glossary_admin and tracks, so these tests assert the HTTP contract (status
codes, the segment guard, which layer a PUT touches), not the merge logic
tests/test_glossary_admin.py already covers."""

import json

import pytest
from fastapi.testclient import TestClient

from yt_shorts import glossary as glossary_module
from yt_shorts import profile as profile_module
from yt_shorts.studio.api import create_app
from yt_shorts.workspace import Workspace

# glossary.DEFAULT_LAYER is EMPTY by design (see glossary.py's module
# docstring - the shipped Nordschleife vocabulary moved to tracks.PACKS).
# `TestGet.test_workspace_scope_returns_the_default` exists to exercise the
# "default" source HTTP contract itself, independent of what the default
# happens to contain, so it uses this small stand-in default rather than
# assert on real shipped content.
FAKE_DEFAULT = glossary_module.parse_layer({
    "terms": ["Karussell"],
    "replacements": {"kessichen": "Kesselchen"},
})


@pytest.fixture
def fake_default(monkeypatch):
    monkeypatch.setattr(glossary_module, "DEFAULT_LAYER", FAKE_DEFAULT)


@pytest.fixture
def client(tmp_path, monkeypatch):
    channels = tmp_path / "channels"
    (channels / "erf" / "events" / "race").mkdir(parents=True)
    monkeypatch.setattr(profile_module, "CHANNELS_DIR", channels)
    workspace = Workspace(root=tmp_path, channels_dir=channels, origin="test")
    import yt_shorts.studio.api as api
    import yt_shorts.studio.jobs as jobs
    monkeypatch.setattr(api, "_resolve_workspace", lambda: workspace)
    monkeypatch.setattr(jobs, "_resolve_workspace", lambda: workspace)
    app = create_app()
    with TestClient(app) as test_client:
        test_client.root = tmp_path
        yield test_client


class TestGet:
    def test_workspace_scope_returns_the_default(self, client, fake_default):
        body = client.get("/api/glossary").json()
        assert body["scope"] == "workspace"
        assert body["effective"]["terms"]["karussell"]["source"] == "default"
        assert body["own"] == {"terms": {}, "replacements": {}}
        assert body["own_keys"] == {"terms": [], "replacements": []}
        assert body["problems"] == []

    def test_channel_scope(self, client):
        body = client.get("/api/channels/erf/glossary").json()
        assert body["scope"] == "channel"

    def test_event_scope(self, client):
        body = client.get("/api/channels/erf/events/race/glossary").json()
        assert body["scope"] == "event"

    def test_unknown_channel_is_404(self, client):
        assert client.get("/api/channels/nope/glossary").status_code == 404

    def test_unknown_event_is_404(self, client):
        assert client.get("/api/channels/erf/events/nope/glossary").status_code == 404

    def test_a_traversal_segment_never_reaches_disk(self, client):
        # Starlette normalises "..", so send an encoded segment: the guard,
        # not the router, is what must refuse it.
        response = client.get("/api/channels/%2E%2E/glossary")
        assert response.status_code in (400, 404)


class TestPut:
    def test_writes_only_the_addressed_layer(self, client):
        response = client.put("/api/channels/erf/glossary", json={
            "terms": {"Rei Racing": True},
            "replacements": {"very very": "Rei Racing"},
        })

        assert response.status_code == 200
        assert response.json()["own"]["terms"] == {"Rei Racing": True}
        written = json.loads(
            (client.root / "channels" / "erf" / "glossary.json").read_text(encoding="utf-8"))
        assert written["terms"] == {"Rei Racing": True}
        assert not (client.root / "glossary.json").exists()

    def test_returns_the_freshly_read_state(self, client):
        body = client.put("/api/glossary", json={
            "terms": {"Workspace Term": True}, "replacements": {}}).json()
        assert body["effective"]["terms"]["workspace term"]["source"] == "workspace"

    def test_own_keys_reports_the_servers_own_normalised_keys(self, client):
        # Fix 3's contract: the client derives ownership from THIS field, not
        # from re-normalising `own`'s raw keys itself - so it must carry
        # exactly what glossary.normalise_term/normalise_key produced for
        # this scope's own layer.
        body = client.put("/api/glossary", json={
            "terms": {"Workspace Term": True},
            "replacements": {"Kessichen,": "Kesselchen"},
        }).json()
        assert body["own_keys"] == {"terms": ["workspace term"],
                                    "replacements": ["kessichen"]}

    def test_null_disables_a_replacement(self, client):
        body = client.put("/api/glossary", json={
            "terms": {}, "replacements": {"carousel": None}}).json()
        assert body["effective"]["replacements"]["carousel"]["value"] is None

    def test_an_invalid_payload_is_400(self, client):
        response = client.put("/api/glossary", json={
            "terms": {}, "replacements": {"carousel": ""}})
        assert response.status_code == 400
        assert not (client.root / "glossary.json").exists()

    def test_an_unknown_event_is_404(self, client):
        response = client.put("/api/channels/erf/events/nope/glossary",
                              json={"terms": {}, "replacements": {}})
        assert response.status_code == 404

    def test_a_malformed_other_layer_does_not_500_a_good_write(self, client):
        (client.root / "glossary.json").write_text("{not json", encoding="utf-8")
        response = client.put("/api/channels/erf/glossary",
                              json={"terms": {"Ok": True}, "replacements": {}})
        assert response.status_code == 200
        assert len(response.json()["problems"]) == 1


class TestRoutesPrecedeTheSpaFallback:
    def test_a_glossary_route_is_not_shadowed_by_index_html(self, client):
        # Asserting only the content-type is vacuous: an /api/* GET that
        # falls through to spa_fallback's own "genuine 404" branch also
        # answers with an application/json body (HTTPException(404)'s
        # default renderer). Assert the status AND the real payload shape
        # so this only passes when the actual glossary route fired.
        response = client.get("/api/glossary")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["scope"] == "workspace"


class TestTracksRoute:
    def test_lists_every_pack(self, client):
        from yt_shorts import tracks
        body = client.get("/api/tracks").json()
        assert len(body["tracks"]) == len(tracks.PACKS)

    def test_carries_id_and_name_only(self, client):
        for row in client.get("/api/tracks").json()["tracks"]:
            assert set(row) == {"id", "name"}

    def test_is_not_shadowed_by_the_spa_fallback(self, client):
        # Same trap as TestRoutesPrecedeTheSpaFallback above: a 404 from
        # spa_fallback's own HTTPException also carries an
        # application/json content-type, so that assertion alone would
        # still pass with the route deleted (proven by mutation: deleting
        # GET /api/tracks entirely left this test green). Assert the
        # status AND the real payload shape instead.
        response = client.get("/api/tracks")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert "tracks" in response.json()


class TestTrackOnTheGlossaryRoutes:
    def test_put_writes_the_track_at_event_scope(self, client):
        body = client.put("/api/channels/erf/events/race/glossary", json={
            "terms": {}, "replacements": {}, "track": "monza"}).json()
        assert body["track"] == "monza"
        assert body["effective"]["terms"]["lesmo"]["source"] == "track"

    def test_omitting_the_track_clears_it(self, client):
        first = client.put("/api/channels/erf/events/race/glossary", json={
            "terms": {}, "replacements": {}, "track": "monza"}).json()
        # Without this assertion the test passed even with `track=` deleted
        # from the second PUT's body entirely - the first PUT never set a
        # track for it to clear, so `body["track"] is None` was trivially
        # true either way. Confirm the track was actually set before
        # checking that omitting it on the next save actually clears it.
        assert first["track"] == "monza"
        body = client.put("/api/channels/erf/events/race/glossary", json={
            "terms": {}, "replacements": {}}).json()
        assert body["track"] is None

    def test_an_unknown_track_is_400(self, client):
        response = client.put("/api/channels/erf/events/race/glossary", json={
            "terms": {}, "replacements": {}, "track": "nope"})
        assert response.status_code == 400

    def test_a_track_at_channel_scope_is_400(self, client):
        response = client.put("/api/channels/erf/glossary", json={
            "terms": {}, "replacements": {}, "track": "monza"})
        assert response.status_code == 400

    def test_a_track_at_workspace_scope_is_400(self, client):
        response = client.put("/api/glossary", json={
            "terms": {}, "replacements": {}, "track": "monza"})
        assert response.status_code == 400


class TestAdoptDefaultRouteIsGone:
    def test_the_route_404s(self, client):
        assert client.post("/api/glossary/adopt-default").status_code == 404


class TestCatchAllDoesNotShadowRealRoutes:
    """api.py's unconditional POST/PUT/PATCH/DELETE /api/{full_path}
    catch-all is registered LAST, on purpose, so every real route above it
    wins first. The failure this guards against: a future real route
    registered AFTER the catch-all would silently never fire - a clean 404
    in every environment (including one with no built static/, where this
    used to be a 405 instead - see api.py's own comment on the catch-all)
    instead of ever reaching its own handler. One representative real route
    per verb the catch-all covers, asserting a response only that route's
    OWN logic could produce, not just "not a 404"."""

    def test_post_reaches_the_real_channel_create_route(self, client):
        # A body missing every required field must reach create_channel's
        # own pydantic validation (422), not the catch-all's blanket 404.
        response = client.post("/api/channels", json={})
        assert response.status_code == 422

    def test_put_reaches_the_real_glossary_route(self, client):
        response = client.put("/api/glossary", json={"terms": {}, "replacements": {}})
        assert response.status_code == 200
        assert response.json()["scope"] == "workspace"

    def test_post_reaches_the_real_adopt_default_moments_route(self, client):
        response = client.post("/api/moments/adopt-default")
        assert response.status_code == 200
        assert "effective" in response.json()

    def test_delete_reaches_the_real_channel_delete_route(self, client):
        response = client.delete("/api/channels/erf")
        assert response.status_code == 200
        assert response.json() == {"deleted": "erf"}
