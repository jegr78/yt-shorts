from yt_shorts import glossary, tracks


class TestRegistryIntegrity:
    def test_every_pack_is_keyed_by_its_own_id(self):
        for key, pack in tracks.PACKS.items():
            assert key == pack.track_id

    def test_every_pack_has_a_display_name(self):
        for pack in tracks.PACKS.values():
            assert pack.name.strip()

    def test_ids_are_safe_lowercase_slugs(self):
        """The id is written into a glossary.json and read back, so keep it to
        a shape that survives a hand edit and a URL without quoting."""
        import re
        for track_id in tracks.PACKS:
            assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", track_id), track_id

    def test_every_pack_converts_to_a_layer(self):
        """as_layer routes through glossary.parse_layer, so a pack that would
        be refused as a hand-written glossary.json fails here instead of at an
        operator's next run."""
        for pack in tracks.PACKS.values():
            layer = tracks.as_layer(pack)
            assert isinstance(layer, glossary.GlossaryLayer)
            assert len(layer.terms) == len(pack.terms)

    def test_no_pack_has_duplicate_terms_after_normalisation(self):
        for pack in tracks.PACKS.values():
            keys = [glossary.normalise_term(t) for t in pack.terms]
            assert len(keys) == len(set(keys)), pack.track_id

    def test_no_pack_is_empty(self):
        """A venue with no named corners still carries its own name - an empty
        pack would be a data gap wearing the costume of a feature."""
        for pack in tracks.PACKS.values():
            assert pack.terms, pack.track_id


class TestHotwordBudget:
    def test_no_pack_approaches_the_truncation_budget(self):
        """faster-whisper silently truncates the hotword prompt at 224 tokens.
        A shipped pack that filled the budget on its own would leave no room
        for the operator's own team and driver names - and the truncation
        drops the LAST terms, which after merge_glossaries' most-specific-first
        ordering are exactly theirs. This is the assertion that keeps a future
        oversized pack out of an operator's transcript."""
        for pack in tracks.PACKS.values():
            bias = glossary.hotwords(glossary.Glossary(terms=list(pack.terms),
                                                        replacements={}))
            assert bias is not None
            assert len(bias) < glossary.HOTWORD_BUDGET_CHARS, (
                f"{pack.track_id}: {len(bias)} chars, at or over the "
                f"{glossary.HOTWORD_BUDGET_CHARS}-character budget")

    def test_the_largest_pack_still_leaves_room_for_operator_terms(self):
        largest = max(tracks.PACKS.values(),
                      key=lambda p: len(glossary.hotwords(
                          glossary.Glossary(terms=list(p.terms), replacements={})) or ""))
        with_own = glossary.Glossary(
            terms=list(largest.terms) + ["Rei Racing", "Team Fullsend"],
            replacements={})
        assert not glossary.hotwords_at_risk(with_own), largest.track_id


class TestLookup:
    def test_get_returns_the_pack(self):
        assert tracks.get("spa-francorchamps").name == "Circuit de Spa-Francorchamps"

    def test_get_on_an_unknown_id_is_none(self):
        assert tracks.get("nope") is None

    def test_get_is_not_case_insensitive(self):
        """Ids are written by the studio's own selector and validated on the
        way in; accepting a stray case would hide a typo rather than report
        it."""
        assert tracks.get("Spa-Francorchamps") is None


class TestListing:
    def test_covers_every_pack(self):
        assert len(tracks.listing()) == len(tracks.PACKS)

    def test_carries_id_and_name_only(self):
        for row in tracks.listing():
            assert set(row) == {"id", "name"}

    def test_is_sorted_by_name(self):
        names = [row["name"] for row in tracks.listing()]
        assert names == sorted(names)


class TestCoverage:
    """The registry covers every venue on Gran Turismo 7's official track
    list (gran-turismo.com/gb/gt7/tracklist/, read December 2025).

    That list's own header says "41 locations (121 layouts)", and this
    registry holds 41 packs - but the two 41s are NOT the same arithmetic,
    and a future reader comparing them will otherwise conclude a venue is
    missing. The list names 40 distinct PLACES; it reaches 41 by counting
    Sardegna twice (Road Track and Windmills are separate entries) while
    treating the whole Nurburgring complex as one. This registry does the
    opposite: the Nurburgring is two packs, because the GP circuit and the
    Nordschleife share no corners and together exceed the hotword budget
    (see TestHotwordBudget), and Sardegna is one, because its two areas are
    one venue's vocabulary. Net: every place named on that list has a pack.

    The set below is pinned by hand rather than fetched, because a test must
    not depend on the network - which does mean a venue added to GT7 after
    the date above is invisible here until someone looks.
    """

    def test_the_venues_this_task_ships_are_all_present(self):
        expected = {
            "alsace", "autopolis", "barcelona-catalunya", "bb-raceway",
            "blue-moon-bay", "brands-hatch", "colorado-springs", "daytona",
            "deep-forest", "dragon-trail", "eiger-nordwand", "fishermans-ranch",
            "fuji", "gilles-villeneuve", "goodwood", "grand-valley",
            "high-speed-ring", "interlagos", "kyoto-driving-park",
            "laguna-seca", "lago-maggiore", "lake-louise", "le-mans", "monza",
            "mount-panorama", "northern-isle", "red-bull-ring", "road-atlanta",
            "sainte-croix", "sardegna", "special-stage-route-x",
            "spa-francorchamps", "suzuka", "tokyo-expressway",
            "trial-mountain", "tsukuba", "watkins-glen", "willow-springs",
            "yas-marina",
        }
        assert expected <= set(tracks.PACKS), expected - set(tracks.PACKS)


class TestNoHeavyImports:
    def test_module_imports_no_web_framework_or_google(self):
        """CLAUDE.md's rule for every pure module. Checked over the AST's
        import statements, not the source text, so the docstring stays free to
        NAME the constraint it upholds."""
        import ast
        from pathlib import Path

        tree = ast.parse(Path(tracks.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
        for banned in ("fastapi", "google", "googleapiclient", "google_auth_oauthlib"):
            assert banned not in imported, f"{banned} must not be imported here"


class TestPacksAreReadOnly:
    """PACKS is a module-level singleton shared by every channel and event for
    the life of the process. `frozen=True` on the dataclass blocks rebinding a
    field, NOT mutating the collection a field points at - so without these
    guards a single stray write would poison the shipped data for everyone
    until the studio restarts. glossary.DEFAULT_LAYER carries the same hazard
    in its own comment; this pins the fix rather than the intention."""

    def test_a_packs_replacements_cannot_be_mutated(self):
        import pytest
        # Every pack shipped so far has an EMPTY replacements map (only a
        # venue with an observed mis-hearing gets one), and an empty proxy
        # refuses a write exactly as a populated one would.
        pack = tracks.PACKS["monza"]
        with pytest.raises(TypeError):
            pack.replacements["carousel"] = "POISONED"

    def test_a_packs_terms_cannot_be_mutated(self):
        import pytest
        pack = tracks.PACKS["monza"]
        with pytest.raises((TypeError, AttributeError)):
            pack.terms.append("Injected")

    def test_a_field_cannot_be_rebound(self):
        import pytest
        import dataclasses
        with pytest.raises(dataclasses.FrozenInstanceError):
            tracks.PACKS["monza"].track_id = "elsewhere"

    def test_as_layer_hands_out_a_fresh_layer_each_time(self):
        """A caller may hold and edit its own layer; two callers must not end
        up sharing one."""
        pack = tracks.PACKS["monza"]
        first, second = tracks.as_layer(pack), tracks.as_layer(pack)
        assert first == second
        assert first.terms is not second.terms


class TestShippedDataIsValidatedAtImport:
    def test_as_layer_rejects_a_pack_with_a_control_character(self):
        """Not a test of import timing (see the class's other test for that)
        - this only exercises as_layer directly, on a pack the registry would
        never contain, to pin what "malformed" means for a pack. Proven by
        mutation: deleting the import-time loop at the bottom of tracks.py
        leaves this test green, because it never touches that loop."""
        import pytest
        bad = tracks.TrackPack(track_id="bad", name="Bad",
                               terms=("fine", "also\nfine"), replacements={})
        with pytest.raises(ValueError, match="control characters"):
            tracks.as_layer(bad)

    def test_a_malformed_shipped_pack_fails_the_import_itself(self):
        """Pins the timing property the loop at the bottom of tracks.py
        exists for: a defect in a SHIPPED pack must fail at first import,
        not only later when an operator happens to select that pack. The
        test above proves as_layer rejects bad data, but calling as_layer
        directly does not exercise the import-time loop at all - deleting
        that loop leaves it (and, before this test existed, the whole suite)
        green.

        Re-running the module's top-level code is what is actually needed,
        so this reloads tracks.py rather than calling any of its functions
        directly. Poisoning `tracks._ALL` first and then reloading would NOT
        work: `_ALL` is rebuilt from literal `_pack(...)` calls every time
        the module executes, so the reload's own top-level code immediately
        overwrites any pre-reload poison. What DOES survive reload is a
        patch to `glossary.parse_layer` itself - `tracks.py` re-imports that
        name from the (unpatched) `glossary` MODULE OBJECT on every reload,
        so patching the attribute on that object, rather than on `tracks`,
        is what a reload actually picks up. `as_layer` then routes every
        shipped pack, real or poisoned, through the patched function via the
        loop under test.

        If the import-time loop were deleted, PACKS would still build fine
        from the (entirely real, unpoisoned) `_ALL`, no call into the
        patched `parse_layer` would happen during reload, and this test
        would fail with "DID NOT RAISE" - confirmed by hand: temporarily
        deleting the loop at tracks.py's `for _shipped in _ALL: ...` made
        this test fail exactly that way, and restoring the loop made it
        pass again.
        """
        import importlib

        import pytest

        from yt_shorts import glossary as glossary_module
        from yt_shorts import tracks as tracks_module

        def always_raises(*_args, **_kwargs):
            raise ValueError("control characters (poisoned for this test)")

        original_parse_layer = glossary_module.parse_layer
        glossary_module.parse_layer = always_raises
        try:
            with pytest.raises(ValueError, match="control characters"):
                importlib.reload(tracks_module)
        finally:
            # Restore BEFORE reloading, or the reload would just re-poison
            # itself the same way.
            glossary_module.parse_layer = original_parse_layer
            importlib.reload(tracks_module)


class TestNurburgring:
    def test_both_nurburgring_packs_exist(self):
        assert tracks.get("nurburgring-nordschleife") is not None
        assert tracks.get("nurburgring-gp") is not None

    def test_the_nordschleife_pack_carries_the_measured_replacements(self):
        """These ten keys were OBSERVED in the real V9nVNEQNdR4 transcript and
        used to live in glossary's built-in default. They move here, which is
        what stops `carousel` firing on a circuit that has its own Carousel."""
        pack = tracks.get("nurburgring-nordschleife")
        for decoded, correct in [
            ("schwab schwanz", "Schwalbenschwanz"),
            ("shriver schwanz", "Schwalbenschwanz"),
            ("kleine carousel", "Kleines Karussell"),
            ("kleinica or sell", "Kleines Karussell"),
            ("carousel", "Karussell"),
            ("galgen cop", "Galgenkopf"),
            ("galbenkopf", "Galgenkopf"),
            ("geigenkop", "Galgenkopf"),
            ("kessichen", "Kesselchen"),
            ("boyacht", "Hohe Acht"),
        ]:
            assert pack.replacements[decoded] == correct

    def test_the_two_nurburgring_packs_do_not_share_corners(self):
        nords = {t.lower() for t in tracks.get("nurburgring-nordschleife").terms}
        gp = {t.lower() for t in tracks.get("nurburgring-gp").terms}
        assert not (nords & gp)

    def test_combining_them_would_exceed_the_budget(self):
        """The measurement that made them two packs rather than one: 249
        tokens together, over faster-whisper's 224-token truncation point
        before the operator adds a single name of their own. If a future
        change merges them, this fails."""
        both = (list(tracks.get("nurburgring-nordschleife").terms)
                + list(tracks.get("nurburgring-gp").terms))
        bias = glossary.hotwords(glossary.Glossary(terms=both, replacements={}))
        assert len(bias) > glossary.HOTWORD_BUDGET_CHARS
