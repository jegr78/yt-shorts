"""The studio's moments-lexicon routes: read/write the workspace, channel and
event layers via lexicon_admin, plus the adopt-default action - thin routes
over the already-tested pure module (see lexicon_admin.py's own docstring)."""

import json

import pytest
from fastapi.testclient import TestClient

from yt_shorts import lexicon, workspace
from yt_shorts.studio import api as studio_api
from yt_shorts.studio import jobs as studio_jobs
from yt_shorts.studio.api import create_app


@pytest.fixture
def root(tmp_path):
    return tmp_path


@pytest.fixture
def client(root, monkeypatch):
    # Same three-name patch tests/test_studio_logs_api.py uses: workspace.py,
    # api.py and jobs.py each bind their own `_resolve_workspace` via a
    # from-import at import time, so reassigning workspace.resolve alone
    # would leave api.py and jobs.py resolving through the real workspace.
    ws = workspace.Workspace(root=root, channels_dir=root / "channels",
                             origin="test")

    def _resolve(*_a, **_k):
        return ws

    monkeypatch.setattr(workspace, "resolve", _resolve)
    monkeypatch.setattr(studio_api, "_resolve_workspace", _resolve)
    monkeypatch.setattr(studio_jobs, "_resolve_workspace", _resolve)
    (root / "channels").mkdir(exist_ok=True)
    return TestClient(create_app())


def _make_channel(root, name):
    channel_dir = root / "channels" / name
    channel_dir.mkdir(parents=True, exist_ok=True)
    (channel_dir / "channel.json").write_text(
        json.dumps({"id": name, "name": name}), encoding="utf-8")
    return channel_dir


def _make_event(root, channel, event):
    event_dir = _make_channel(root, channel) / "events" / event
    event_dir.mkdir(parents=True, exist_ok=True)
    return event_dir


def test_get_workspace_moments_reports_the_default(client):
    body = client.get("/api/moments").json()
    assert body["scope"] == "workspace"
    assert body["own"] == {}
    assert body["effective"]["crash"] == {"weight": 3.0, "source": "default"}


def test_put_workspace_moments_writes_and_returns_the_new_state(client, root):
    response = client.put("/api/moments", json={"markers": {"crash": 9.0}})
    assert response.status_code == 200
    body = response.json()
    assert body["scope"] == "workspace"
    assert body["own"] == {"crash": 9.0}
    assert body["effective"]["crash"] == {"weight": 9.0, "source": "workspace"}
    on_disk = json.loads(workspace.moments_path(root).read_text(encoding="utf-8"))
    assert on_disk == {"markers": {"crash": 9.0}}


def test_get_channel_moments_shows_the_workspace_as_inherited(client, root):
    _make_channel(root, "erf")
    workspace.moments_path(root).write_text(
        json.dumps({"markers": {"crash": 9.0}}), encoding="utf-8")
    body = client.get("/api/channels/erf/moments").json()
    assert body["scope"] == "channel"
    assert body["own"] == {}
    assert body["effective"]["crash"] == {"weight": 9.0, "source": "workspace"}


def test_put_channel_moments_writes_only_the_channel_layer(client, root):
    channel_dir = _make_channel(root, "erf")
    response = client.put("/api/channels/erf/moments",
                          json={"markers": {"overtake": 5.0}})
    assert response.status_code == 200
    body = response.json()
    assert body["scope"] == "channel"
    assert body["own"] == {"overtake": 5.0}
    assert body["effective"]["overtake"] == {"weight": 5.0, "source": "channel"}
    # the workspace layer is untouched
    assert not workspace.moments_path(root).exists()
    on_disk = json.loads((channel_dir / "moments.json").read_text(encoding="utf-8"))
    assert on_disk == {"markers": {"overtake": 5.0}}


def test_get_event_moments_shows_the_channel_as_inherited(client, root):
    channel_dir = _make_channel(root, "erf")
    _make_event(root, "erf", "quali")
    (channel_dir / "moments.json").write_text(
        json.dumps({"markers": {"overtake": 5.0}}), encoding="utf-8")
    body = client.get("/api/channels/erf/events/quali/moments").json()
    assert body["scope"] == "event"
    assert body["own"] == {}
    assert body["effective"]["overtake"] == {"weight": 5.0, "source": "channel"}


def test_put_event_moments_writes_only_the_event_layer(client, root):
    channel_dir = _make_channel(root, "erf")
    event_dir = _make_event(root, "erf", "quali")
    response = client.put("/api/channels/erf/events/quali/moments",
                          json={"markers": {"pole": 4.0}})
    assert response.status_code == 200
    body = response.json()
    assert body["scope"] == "event"
    assert body["own"] == {"pole": 4.0}
    assert body["effective"]["pole"] == {"weight": 4.0, "source": "event"}
    assert not (channel_dir / "moments.json").exists()
    on_disk = json.loads((event_dir / "moments.json").read_text(encoding="utf-8"))
    assert on_disk == {"markers": {"pole": 4.0}}


def test_a_disabled_marker_is_reported_with_weight_zero(client, root):
    _make_channel(root, "erf")
    response = client.put("/api/channels/erf/moments",
                          json={"markers": {"crash": 0.0}})
    body = response.json()
    assert body["effective"]["crash"] == {"weight": 0.0, "source": "channel"}


def test_adopt_default_writes_the_workspace_layer(client, root):
    response = client.post("/api/moments/adopt-default")
    assert response.status_code == 200
    body = response.json()
    assert body["scope"] == "workspace"
    assert body["own"] == dict(lexicon.DEFAULT_MARKERS)
    on_disk = json.loads(workspace.moments_path(root).read_text(encoding="utf-8"))
    assert on_disk == {"markers": dict(lexicon.DEFAULT_MARKERS)}


def test_adopt_default_is_idempotent(client, root):
    client.post("/api/moments/adopt-default")
    first = workspace.moments_path(root).read_text(encoding="utf-8")
    response = client.post("/api/moments/adopt-default")
    assert response.status_code == 200
    second = workspace.moments_path(root).read_text(encoding="utf-8")
    assert first == second


def test_a_bad_weight_is_400_and_nothing_is_written(client, root):
    response = client.put("/api/moments", json={"markers": {"crash": 99.0}})
    assert response.status_code == 400
    assert not workspace.moments_path(root).exists()


def test_an_unknown_channel_is_404(client):
    assert client.get("/api/channels/nope/moments").status_code == 404
    assert client.put("/api/channels/nope/moments",
                      json={"markers": {}}).status_code == 404


def test_an_unsafe_channel_segment_is_400(client):
    assert client.get("/api/channels/.hidden/moments").status_code == 400


def test_an_unsafe_event_segment_is_400(client, root):
    _make_channel(root, "erf")
    assert client.get("/api/channels/erf/events/.hidden/moments").status_code == 400


def test_get_workspace_moments_reports_problems_for_a_malformed_layer_but_still_200s(client, root):
    workspace.moments_path(root).write_text("not json", encoding="utf-8")
    response = client.get("/api/moments")
    assert response.status_code == 200
    body = response.json()
    assert body["own"] == {}
    assert body["effective"]["crash"]["source"] == "default"
    assert len(body["problems"]) == 1


def test_get_channel_moments_reports_problems_for_a_malformed_layer_but_still_200s(client, root):
    channel_dir = _make_channel(root, "erf")
    (channel_dir / "moments.json").write_text("not json", encoding="utf-8")
    response = client.get("/api/channels/erf/moments")
    assert response.status_code == 200
    body = response.json()
    assert body["own"] == {}
    assert len(body["problems"]) == 1


def test_get_event_moments_reports_problems_for_a_malformed_layer_but_still_200s(client, root):
    _make_channel(root, "erf")
    event_dir = _make_event(root, "erf", "quali")
    (event_dir / "moments.json").write_text("not json", encoding="utf-8")
    response = client.get("/api/channels/erf/events/quali/moments")
    assert response.status_code == 200
    body = response.json()
    assert body["own"] == {}
    assert len(body["problems"]) == 1


def test_get_workspace_moments_reports_no_problems_when_healthy(client, root):
    response = client.get("/api/moments")
    assert response.status_code == 200
    assert response.json()["problems"] == []


def test_put_event_moments_still_200s_with_problems_when_the_channel_layer_is_broken(client, root):
    # A write to the EVENT layer must not be blocked, nor turned into a 500
    # on the post-write re-read, just because a layer it merely inherits
    # from (the channel's) happens to be malformed - see lexicon_admin.read's
    # own docstring.
    channel_dir = _make_channel(root, "erf")
    _make_event(root, "erf", "quali")
    (channel_dir / "moments.json").write_text("not json", encoding="utf-8")
    response = client.put("/api/channels/erf/events/quali/moments",
                          json={"markers": {"pole": 4.0}})
    assert response.status_code == 200
    body = response.json()
    assert body["own"] == {"pole": 4.0}
    assert len(body["problems"]) == 1


def test_adopt_default_route_is_not_shadowed_by_a_channel_pattern(client, root):
    # "/api/moments/adopt-default" could in principle be matched by some
    # future GET/PUT "/api/moments/{something}"-shaped route if one were
    # registered before it; there is no such route today, but this pins
    # that the literal path reaches the adopt handler (workspace layer
    # written) rather than falling through to the SPA fallback or a 404.
    response = client.post("/api/moments/adopt-default")
    assert response.status_code == 200
    assert response.json()["own"] == dict(lexicon.DEFAULT_MARKERS)
    assert workspace.moments_path(root).exists()
