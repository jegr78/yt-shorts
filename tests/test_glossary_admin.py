import json
import os

import pytest

from yt_shorts import glossary as glossary_module
from yt_shorts import glossary_admin


@pytest.fixture
def root(tmp_path):
    (tmp_path / "channels" / "erf" / "events" / "race").mkdir(parents=True)
    return tmp_path


# glossary.DEFAULT_LAYER is EMPTY by design now (see glossary.py's module
# docstring and DEFAULT_LAYER's own comment) - the shipped Nordschleife
# vocabulary moved to tracks.PACKS. The tests below exist to exercise this
# module's "default" SOURCE mechanism itself (visibility), which is
# independent of what the default happens to contain - so they monkeypatch a
# small stand-in default layer rather than assert on real shipped content
# that no longer lives here.
FAKE_DEFAULT = glossary_module.parse_layer({
    "terms": ["Karussell"],
    "replacements": {"kessichen": "Kesselchen"},
})


@pytest.fixture
def fake_default(monkeypatch):
    monkeypatch.setattr(glossary_module, "DEFAULT_LAYER", FAKE_DEFAULT)


class TestScopeResolution:
    def test_workspace_scope(self, root):
        assert glossary_admin.read(root)["scope"] == "workspace"

    def test_channel_scope(self, root):
        assert glossary_admin.read(root, channel="erf")["scope"] == "channel"

    def test_event_scope(self, root):
        assert glossary_admin.read(root, channel="erf", event="race")["scope"] == "event"

    def test_unsafe_channel_segment_is_bad_name(self, root):
        with pytest.raises(glossary_admin.GlossaryAdminError) as excinfo:
            glossary_admin.read(root, channel="../etc")
        assert excinfo.value.kind == "bad_name"

    def test_unsafe_event_segment_is_bad_name(self, root):
        with pytest.raises(glossary_admin.GlossaryAdminError) as excinfo:
            glossary_admin.read(root, channel="erf", event="..")
        assert excinfo.value.kind == "bad_name"

    def test_segment_is_validated_before_existence(self, root):
        """bad_name must win over not_found, so a traversal attempt never
        reaches a filesystem check - the same order event_brand_admin keeps."""
        with pytest.raises(glossary_admin.GlossaryAdminError) as excinfo:
            glossary_admin.read(root, channel="../nope", event="..")
        assert excinfo.value.kind == "bad_name"

    def test_unknown_channel_is_not_found(self, root):
        with pytest.raises(glossary_admin.GlossaryAdminError) as excinfo:
            glossary_admin.read(root, channel="nope")
        assert excinfo.value.kind == "not_found"

    def test_unknown_event_is_not_found(self, root):
        with pytest.raises(glossary_admin.GlossaryAdminError) as excinfo:
            glossary_admin.read(root, channel="erf", event="nope")
        assert excinfo.value.kind == "not_found"


class TestRead:
    def test_the_default_is_visible_with_its_source(self, root, fake_default):
        state = glossary_admin.read(root)
        assert state["effective"]["terms"]["karussell"] == {
            "term": "Karussell", "enabled": True, "source": "default"}
        assert state["effective"]["replacements"]["kessichen"] == {
            "key": "kessichen", "value": "Kesselchen", "source": "default"}

    def test_own_is_empty_when_no_file_exists(self, root):
        state = glossary_admin.read(root)
        assert state["own"] == {"terms": {}, "replacements": {}}
        assert state["own_keys"] == {"terms": [], "replacements": []}
        assert state["problems"] == []

    def test_own_keys_matches_what_the_own_layer_actually_parsed(self, root):
        """Fix 3: own_keys must be the server's OWN already-normalised keys
        for this scope's own layer - exactly the keys glossary.parse_layer
        produced for it, not derived from `own`'s raw text a second time.
        This is what lets the studio client determine ownership without
        reproducing Python's normalisation itself."""
        glossary_admin.update(
            root, {"Rei Racing": True, "Karussell": False},
            {"Kessichen,": "Kesselchen", "carousel": None}, channel="erf")

        state = glossary_admin.read(root, channel="erf")
        own_layer = glossary_module.load(root / "channels" / "erf" / "glossary.json")

        assert set(state["own_keys"]["terms"]) == set(own_layer.terms.keys())
        assert set(state["own_keys"]["replacements"]) == set(own_layer.replacements.keys())
        assert state["own_keys"]["terms"] == ["rei racing", "karussell"]
        assert state["own_keys"]["replacements"] == ["kessichen", "carousel"]

    def test_a_channel_entry_is_own_at_channel_scope_and_absent_at_workspace(self, root):
        glossary_admin.update(root, {"Rei Racing": True}, {}, channel="erf")

        channel_state = glossary_admin.read(root, channel="erf")
        assert channel_state["own"]["terms"] == {"Rei Racing": True}
        assert channel_state["effective"]["terms"]["rei racing"]["source"] == "channel"

        workspace_state = glossary_admin.read(root)
        assert "rei racing" not in workspace_state["effective"]["terms"]

    def test_a_workspace_entry_is_inherited_at_channel_scope(self, root):
        glossary_admin.update(root, {"Workspace Term": True}, {})

        state = glossary_admin.read(root, channel="erf")
        assert state["effective"]["terms"]["workspace term"]["source"] == "workspace"
        assert "Workspace Term" not in state["own"]["terms"]

    def test_a_disabled_entry_is_KEPT_in_effective_with_its_source(self, root):
        """Deliberately NOT merge_glossaries, which drops a disabled entry so
        scoring never sees it - an editor must show that a layer disabled
        something, struck through, rather than make it vanish."""
        glossary_admin.update(root, {"Karussell": False}, {"carousel": None})

        state = glossary_admin.read(root, channel="erf")
        assert state["effective"]["terms"]["karussell"] == {
            "term": "Karussell", "enabled": False, "source": "workspace"}
        assert state["effective"]["replacements"]["carousel"] == {
            "key": "carousel", "value": None, "source": "workspace"}

    def test_the_most_specific_layer_wins(self, root):
        glossary_admin.update(root, {}, {"carousel": "Workspace"})
        glossary_admin.update(root, {}, {"carousel": "Channel"}, channel="erf")
        glossary_admin.update(root, {}, {"carousel": "Event"}, channel="erf", event="race")

        state = glossary_admin.read(root, channel="erf", event="race")
        assert state["effective"]["replacements"]["carousel"] == {
            "key": "carousel", "value": "Event", "source": "event"}

    def test_a_scope_never_sees_a_more_specific_layer(self, root):
        glossary_admin.update(root, {"Event Term": True}, {}, channel="erf", event="race")
        assert "event term" not in glossary_admin.read(root, channel="erf")["effective"]["terms"]

    def test_a_malformed_layer_is_a_problem_not_an_exception(self, root, fake_default):
        (root / "glossary.json").write_text("{not json", encoding="utf-8")

        state = glossary_admin.read(root, channel="erf")

        assert len(state["problems"]) == 1
        assert "not valid JSON" in state["problems"][0]
        # The other layers still load - one bad file must not 500 the route.
        assert state["effective"]["terms"]["karussell"]["source"] == "default"

    def test_a_malformed_own_layer_still_reads(self, root):
        (root / "channels" / "erf" / "glossary.json").write_text("{nope", encoding="utf-8")
        state = glossary_admin.read(root, channel="erf")
        assert state["own"] == {"terms": {}, "replacements": {}}
        assert len(state["problems"]) == 1


class TestUpdate:
    def test_writes_the_layer_and_nothing_else(self, root):
        glossary_admin.update(root, {"Rei Racing": True}, {"very very": "Rei Racing"},
                              channel="erf")

        written = json.loads(
            (root / "channels" / "erf" / "glossary.json").read_text(encoding="utf-8"))
        assert written == {"terms": {"Rei Racing": True},
                           "replacements": {"very very": "Rei Racing"}}
        assert not (root / "glossary.json").exists()

    def test_an_empty_update_still_writes_the_file(self, root):
        """"I cleared this layer" is an explicit, re-editable state, not a
        deletion - same contract lexicon_admin.update keeps."""
        glossary_admin.update(root, {}, {})
        assert json.loads((root / "glossary.json").read_text(encoding="utf-8")) == {
            "terms": {}, "replacements": {}}

    def test_an_accepted_update_is_one_profile_load_accepts(self, root):
        """The invariant every admin module keeps: what update() writes,
        glossary.load reads back without a defect - and glossary.load is
        exactly what profile._load_glossary calls per layer."""
        glossary_admin.update(root, {"Rei Racing": True}, {"very very": "Rei Racing"},
                              channel="erf")
        layer = glossary_module.load(root / "channels" / "erf" / "glossary.json")
        assert layer.terms["rei racing"] == ("Rei Racing", True)
        assert layer.replacements["very very"] == ("very very", "Rei Racing")

    def test_a_bad_payload_never_reaches_disk(self, root):
        with pytest.raises(glossary_admin.GlossaryAdminError) as excinfo:
            glossary_admin.update(root, {"Karussell": 1.5}, {})
        assert excinfo.value.kind == "bad_glossary"
        assert not (root / "glossary.json").exists()

    def test_an_empty_replacement_is_refused(self, root):
        with pytest.raises(glossary_admin.GlossaryAdminError) as excinfo:
            glossary_admin.update(root, {}, {"carousel": ""})
        assert excinfo.value.kind == "bad_glossary"


class TestTheWriteIsAtomic:
    """A save must be invisible until it is complete. Whoever reads this file
    - profile._load_glossary on every render, `read` above, a test polling it
    after a save - must find either the whole old layer or the whole new one,
    never the empty file a truncate-in-place leaves behind for the length of
    a write. Measured, not feared: with `Path.write_text` here,
    test_studio_e2e.py's glossary round-trip read a ZERO-BYTE glossary.json
    on a CI runner and died in json.loads.
    """

    def test_a_failed_save_leaves_the_previous_layer_complete(self, root, monkeypatch):
        glossary_admin.update(root, {"Karussell": True}, {}, channel="erf")
        path = root / "channels" / "erf" / "glossary.json"
        before = path.read_bytes()

        # The failure has to land AFTER the new bytes are on disk and BEFORE
        # they are in place - that is the only moment a truncating write
        # cannot survive. Patching json.dumps (as test_provider_contract.py
        # does for the same guarantee) would not: it runs before either
        # implementation touches the filesystem.
        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", _boom)
        with pytest.raises(OSError):
            glossary_admin.update(root, {"Boxengasse": True}, {}, channel="erf")

        assert path.read_bytes() == before

    def test_a_successful_save_leaves_no_scratch_file_behind(self, root):
        """test_quota.py:20 pins the same half for the same mechanic. It
        cannot fail today - os.replace consumes the scratch - which is the
        point: it is what a future retry or fallback path would break
        silently."""
        glossary_admin.update(root, {"Karussell": True}, {}, channel="erf")
        assert sorted(p.name for p in (root / "channels" / "erf").iterdir()) == [
            "events", "glossary.json"]

    def test_a_failed_save_leaves_no_scratch_file_behind(self, root, monkeypatch):
        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", _boom)
        with pytest.raises(OSError):
            glossary_admin.update(root, {"Boxengasse": True}, {}, channel="erf")

        assert sorted(p.name for p in (root / "channels" / "erf").iterdir()) == ["events"]


class TestTrack:
    def test_read_reports_no_track_by_default(self, root):
        assert glossary_admin.read(root, channel="erf", event="race")["track"] is None

    def test_update_writes_the_track(self, root):
        glossary_admin.update(root, {}, {}, track="monza", channel="erf", event="race")
        written = json.loads(
            (root / "channels" / "erf" / "events" / "race" / "glossary.json")
            .read_text(encoding="utf-8"))
        assert written["track"] == "monza"

    def test_read_reports_the_track_back(self, root):
        glossary_admin.update(root, {}, {}, track="monza", channel="erf", event="race")
        assert glossary_admin.read(root, channel="erf", event="race")["track"] == "monza"

    def test_the_pack_appears_as_an_inherited_layer(self, root):
        glossary_admin.update(root, {}, {}, track="monza", channel="erf", event="race")
        state = glossary_admin.read(root, channel="erf", event="race")
        assert state["effective"]["terms"]["lesmo"] == {
            "term": "Lesmo", "enabled": True, "source": "track"}

    def test_a_saved_row_edit_PRESERVES_the_track(self, root):
        """The data-loss risk this feature carries: the editor overwrites the
        whole own layer on every save, so a track dropped anywhere in
        read -> row -> payload -> write disappears on the next unrelated
        edit."""
        glossary_admin.update(root, {}, {}, track="monza", channel="erf", event="race")
        state = glossary_admin.read(root, channel="erf", event="race")

        glossary_admin.update(root, {"Rei Racing": True}, {},
                              track=state["track"], channel="erf", event="race")

        assert glossary_admin.read(root, channel="erf", event="race")["track"] == "monza"

    def test_clearing_the_track_removes_the_key(self, root):
        glossary_admin.update(root, {}, {}, track="monza", channel="erf", event="race")
        glossary_admin.update(root, {}, {}, track=None, channel="erf", event="race")
        written = json.loads(
            (root / "channels" / "erf" / "events" / "race" / "glossary.json")
            .read_text(encoding="utf-8"))
        assert "track" not in written
        assert glossary_admin.read(root, channel="erf", event="race")["track"] is None

    def test_an_unknown_track_is_refused(self, root):
        with pytest.raises(glossary_admin.GlossaryAdminError) as excinfo:
            glossary_admin.update(root, {}, {}, track="nope", channel="erf", event="race")
        assert excinfo.value.kind == "bad_glossary"

    def test_a_track_at_channel_scope_is_refused(self, root):
        with pytest.raises(glossary_admin.GlossaryAdminError) as excinfo:
            glossary_admin.update(root, {}, {}, track="monza", channel="erf")
        assert excinfo.value.kind == "bad_glossary"

    def test_a_track_at_workspace_scope_is_refused(self, root):
        with pytest.raises(glossary_admin.GlossaryAdminError) as excinfo:
            glossary_admin.update(root, {}, {}, track="monza")
        assert excinfo.value.kind == "bad_glossary"

    def test_event_without_channel_does_not_bypass_the_track_guard(self, root):
        """Fix 1: `_resolve`'s own docstring documents that an `event` given
        WITHOUT a `channel` is validated and then silently ignored - the
        scope resolves to WORKSPACE. Gating on `event is None` (the
        parameter) rather than the RESOLVED scope let this call slip past
        the guard and write a workspace-wide track, which then made
        profile.load raise for every event of every channel in the
        workspace. The guard must gate on the resolved scope."""
        with pytest.raises(glossary_admin.GlossaryAdminError) as excinfo:
            glossary_admin.update(root, {}, {}, track="monza", event="race")
        assert excinfo.value.kind == "bad_glossary"
        assert not (root / "glossary.json").exists()

    def test_the_track_pack_sits_between_default_and_workspace(self, root):
        """Fix 3: proven by mutation - moving the pack's insertion point to
        sit between channel and event (the shipped pack overriding the
        operator's own entries, an exact inversion of the constraint) leaves
        the rest of the suite green. A workspace-level disable of a term the
        pack also defines must win, which only holds if the pack sits
        BEFORE the workspace layer in `_layers`."""
        glossary_admin.update(root, {"Lesmo": False}, {})
        glossary_admin.update(root, {}, {}, track="monza", channel="erf", event="race")

        state = glossary_admin.read(root, channel="erf", event="race")
        assert state["effective"]["terms"]["lesmo"]["source"] == "workspace"
        assert state["effective"]["terms"]["lesmo"]["enabled"] is False

    def test_an_unknown_track_hand_written_on_disk_is_a_problem_on_read(self, root):
        """Fix 4: `update` refuses a bad id before it reaches disk, so this
        branch is otherwise dead on the write path - only a hand-written
        file reaches it. The message lists the valid ids, the way
        profile._load_glossary's does, since this string is what the studio
        shows an operator."""
        event_glossary = root / "channels" / "erf" / "events" / "race" / "glossary.json"
        event_glossary.write_text(json.dumps({"track": "nope"}), encoding="utf-8")

        state = glossary_admin.read(root, channel="erf", event="race")

        assert len(state["problems"]) == 1
        assert "unknown track 'nope'" in state["problems"][0]
        assert "valid ids:" in state["problems"][0]
        assert "monza" in state["problems"][0]

    def test_a_track_hand_written_at_channel_scope_is_a_problem_not_echoed(self, root):
        """Fix 2: a hand-edited channel glossary.json carrying a `track`
        must not be echoed back by `read` - `profile._load_glossary` reports
        it as a defect and ignores the selection, and update's docstring
        promises the client "sends `read`'s `track` back with every save",
        so echoing it here would make the very next channel-scope save
        raise bad_glossary."""
        channel_glossary = root / "channels" / "erf" / "glossary.json"
        channel_glossary.write_text(json.dumps({"track": "monza"}), encoding="utf-8")

        state = glossary_admin.read(root, channel="erf")

        assert state["track"] is None
        assert "track" not in state["own"]
        assert len(state["problems"]) == 1
        assert "only an event selects a track" in state["problems"][0]
        assert "'monza'" in state["problems"][0]
        assert str(channel_glossary) in state["problems"][0]

    def test_a_track_hand_written_at_workspace_scope_is_a_problem_not_echoed(self, root):
        workspace_glossary = root / "glossary.json"
        workspace_glossary.write_text(json.dumps({"track": "monza"}), encoding="utf-8")

        state = glossary_admin.read(root)

        assert state["track"] is None
        assert "track" not in state["own"]
        assert len(state["problems"]) == 1
        assert "only an event selects a track" in state["problems"][0]


class TestAdoptDefaultIsGone:
    def test_the_function_no_longer_exists(self):
        """With an empty built-in default it would adopt nothing. Per-row
        Override and Disable already let an operator own any pack entry."""
        assert not hasattr(glossary_admin, "adopt_default")


class TestNoFastAPI:
    def test_module_imports_no_web_framework(self):
        """CLAUDE.md's rule for every pure admin module. Checked over the
        module's IMPORT STATEMENTS via the AST, not over its source text: a
        substring search would also forbid the docstring from NAMING the
        constraint it upholds, which in a codebase whose whole comment style
        is explaining WHY is precisely backwards. And sys.modules would prove
        nothing either - some other test in the same session has almost
        certainly imported FastAPI already."""
        import ast
        from pathlib import Path

        tree = ast.parse(Path(glossary_admin.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
        # Every top-level package name the two optional google dependencies
        # actually import as, not just the ones that look google-shaped:
        # google-api-python-client is `googleapiclient` and
        # google-auth-oauthlib is `google_auth_oauthlib` - a DISTINCT top-level
        # name from `google`, which is why listing only the latter left the
        # guard defeatable by a copy-paste of google_oauth.py's own import.
        for banned in ("fastapi", "google", "googleapiclient", "google_auth_oauthlib"):
            assert banned not in imported, f"{banned} must not be imported here"
