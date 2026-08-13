import shutil
from pathlib import Path
from unittest import mock

import pytest

from yt_shorts import workspaces
from yt_shorts.cancel import CancelToken, Stopped


def test_read_config_missing_returns_empty(tmp_path):
    assert workspaces.read_config(tmp_path) == {"current": None, "recent": []}


def test_write_then_read_roundtrip(tmp_path):
    workspaces.write_config(tmp_path, {"current": "/w/a", "recent": ["/w/a"]})
    assert workspaces.config_path(tmp_path).is_file()
    assert workspaces.read_config(tmp_path) == {"current": "/w/a", "recent": ["/w/a"]}


def test_read_config_malformed_is_empty(tmp_path):
    p = workspaces.config_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    assert workspaces.read_config(tmp_path) == {"current": None, "recent": []}


def test_push_recent_dedups_and_caps_newest_first():
    assert workspaces.push_recent(["/a", "/b"], "/a") == ["/a", "/b"]
    assert workspaces.push_recent(["/a", "/b", "/c"], "/d") == ["/d", "/a", "/b"]
    assert workspaces.push_recent([], "/a") == ["/a"]


def _make_ws(root: Path) -> Path:
    (root / "channels").mkdir(parents=True)
    return root


def test_is_workspace_by_manifest_or_channels(tmp_path):
    legacy = _make_ws(tmp_path / "legacy")
    assert workspaces.is_workspace(legacy) is True          # has channels/
    plain = tmp_path / "plain"
    plain.mkdir()
    assert workspaces.is_workspace(plain) is False
    workspaces.write_manifest(plain, "Plain", "2026-07-24T00:00:00")
    assert workspaces.is_workspace(plain) is True           # now has manifest


def test_adopt_writes_manifest_for_legacy_only(tmp_path):
    legacy = _make_ws(tmp_path / "legacy")
    workspaces.adopt(legacy, "2026-07-24T00:00:00")
    assert (legacy / workspaces.MANIFEST_NAME).is_file()
    # idempotent: adopting again keeps the original name
    workspaces.write_manifest(legacy, "Kept", "2026-07-24T00:00:00")
    workspaces.adopt(legacy, "2026-07-25T00:00:00")
    assert workspaces.read_manifest(legacy)["name"] == "Kept"


def test_workspace_name_prefers_manifest(tmp_path):
    d = _make_ws(tmp_path / "erf-data")
    assert workspaces.workspace_name(d) == "erf-data"       # basename fallback
    workspaces.write_manifest(d, "ERF Prod", "2026-07-24T00:00:00")
    assert workspaces.workspace_name(d) == "ERF Prod"


def test_list_dir_lists_subdirs_with_workspace_flag(tmp_path):
    _make_ws(tmp_path / "a-ws")
    (tmp_path / "b-plain").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "file.txt").write_text("x")
    out = workspaces.list_dir(tmp_path)
    names = {e["name"]: e["is_workspace"] for e in out["entries"]}
    assert names == {"a-ws": True, "b-plain": False}        # no hidden, no files
    assert out["parent"] == str(tmp_path.parent)


def test_create_workspace_scaffolds(tmp_path):
    ws = workspaces.create_workspace(tmp_path, "new-ws", "2026-07-24T00:00:00")
    assert ws == tmp_path / "new-ws"
    assert (ws / "channels").is_dir()
    assert workspaces.read_manifest(ws)["name"] == "new-ws"


def test_create_workspace_rejects_bad_name(tmp_path):
    with pytest.raises(workspaces.WorkspaceError) as e:
        workspaces.create_workspace(tmp_path, "../evil", "2026-07-24T00:00:00")
    assert e.value.kind == "bad_name"


def test_create_workspace_refuses_existing(tmp_path):
    (tmp_path / "dup").mkdir()
    with pytest.raises(workspaces.WorkspaceError) as e:
        workspaces.create_workspace(tmp_path, "dup", "2026-07-24T00:00:00")
    assert e.value.kind == "exists"


def test_copy_workspace_clones_including_auth(tmp_path):
    src = _make_ws(tmp_path / "src")
    (src / "channels" / "erf").mkdir()
    (src / "auth").mkdir()
    (src / "auth" / "token-UC1.json").write_text("secret")
    dest = workspaces.copy_workspace(src, tmp_path / "dests", "clone",
                                     "2026-07-24T00:00:00")
    assert (dest / "channels" / "erf").is_dir()
    assert (dest / "auth" / "token-UC1.json").read_text() == "secret"   # true clone
    assert workspaces.read_manifest(dest)["name"] == "clone"


def test_copy_workspace_stops_after_the_current_file(tmp_path):
    """KINDS["copy"] promises a stop "after the current file". This is that
    promise: the token is checked before EVERY file, so a stop asked for
    while the first one is being copied leaves the rest uncopied - and the
    half-copied target is removed, because a directory with a `channels/` in
    it already reads as a workspace to `is_workspace` and offering the
    operator a workspace missing an unknowable part of itself is worse than
    offering none."""
    src = _make_ws(tmp_path / "src")
    for i in range(5):
        (src / "channels" / f"file{i}.json").write_text("x" * 10)
    token = CancelToken()
    copied = []

    real_copy2 = shutil.copy2

    def counting_copy2(source, destination, **kwargs):
        copied.append(Path(source).name)
        token.request_stop()          # the operator clicks during the first file
        return real_copy2(source, destination, **kwargs)

    target = tmp_path / "dests" / "clone"
    with pytest.raises(Stopped):
        with mock.patch.object(shutil, "copy2", counting_copy2):
            workspaces.copy_workspace(src, tmp_path / "dests", "clone",
                                      "2026-07-24T00:00:00", cancel=token)

    assert len(copied) == 1, f"files kept being copied after the stop: {copied}"
    assert not target.exists(), "a half-copied workspace was left behind"


def test_copy_workspace_without_a_token_copies_everything(tmp_path):
    # No `cancel` argument at all - which is what this test's name claims
    # and what it did NOT do: it passed a fresh CancelToken, so the
    # `cancel is None` branch (a plain copytree, no copy_function of ours)
    # went uncovered while the test read as though it were the one covering
    # it. Every caller that never asks to stop takes that branch.
    src = _make_ws(tmp_path / "src")
    for i in range(5):
        (src / "channels" / f"file{i}.json").write_text("x" * 10)
    dest = workspaces.copy_workspace(src, tmp_path / "dests", "clone",
                                     "2026-07-24T00:00:00")
    assert sorted(p.name for p in (dest / "channels").iterdir()) == [
        f"file{i}.json" for i in range(5)]


def test_copy_workspace_with_an_unstopped_token_copies_everything(tmp_path):
    # The other half of the above: a token that is never stopped changes
    # nothing either - the per-file check runs and lets every file through.
    src = _make_ws(tmp_path / "src")
    for i in range(5):
        (src / "channels" / f"file{i}.json").write_text("x" * 10)
    dest = workspaces.copy_workspace(src, tmp_path / "dests", "clone",
                                     "2026-07-24T00:00:00", cancel=CancelToken())
    assert sorted(p.name for p in (dest / "channels").iterdir()) == [
        f"file{i}.json" for i in range(5)]


def test_copy_workspace_refuses_non_workspace_src(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(workspaces.WorkspaceError) as e:
        workspaces.copy_workspace(plain, tmp_path, "x", "2026-07-24T00:00:00")
    assert e.value.kind == "not_found"


def test_a_failed_config_write_leaves_the_previous_config_readable(tmp_path, monkeypatch):
    """The CLI reads this on every invocation to find the workspace - an empty
    read here means `read_config` answers "nothing configured" and the tool
    looks at the wrong data directory."""
    import os

    workspaces.write_config(tmp_path, {"current": "/w/a", "recent": ["/w/a"]})
    before = workspaces.config_path(tmp_path).read_bytes()

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        workspaces.write_config(tmp_path, {"current": "/w/b", "recent": ["/w/b"]})

    assert workspaces.config_path(tmp_path).read_bytes() == before
    assert workspaces.read_config(tmp_path) == {"current": "/w/a", "recent": ["/w/a"]}
