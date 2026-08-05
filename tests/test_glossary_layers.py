import json

import pytest

from yt_shorts.glossary import (
    DEFAULT_LAYER, EMPTY_LAYER,
    MAX_ENTRY_LENGTH, GlossaryLayer, load, normalise_key, normalise_term,
    parse_layer)


class TestNormalisation:
    def test_term_key_is_stripped_and_lowercased(self):
        assert normalise_term("  Karussell ") == "karussell"

    def test_replacement_key_strips_punctuation_per_token(self):
        # Must agree with what apply() matches on: _normalized per token.
        assert normalise_key("Kleine, Carousel") == "kleine carousel"

    def test_replacement_key_collapses_whitespace(self):
        assert normalise_key("kleine   carousel") == "kleine carousel"


class TestParseTerms:
    def test_list_form_enables_every_term(self):
        layer = parse_layer({"terms": ["Karussell", "Galgenkopf"]})
        assert layer.terms == {"karussell": ("Karussell", True),
                               "galgenkopf": ("Galgenkopf", True)}

    def test_map_form_false_disables(self):
        layer = parse_layer({"terms": {"Karussell": True, "carousel": False}})
        assert layer.terms == {"karussell": ("Karussell", True),
                               "carousel": ("carousel", False)}

    def test_null_disables_a_term(self):
        layer = parse_layer({"terms": {"carousel": None}})
        assert layer.terms == {"carousel": ("carousel", False)}

    def test_missing_terms_key_is_empty(self):
        assert parse_layer({"replacements": {}}).terms == {}

    def test_terms_wrong_type_is_a_defect(self):
        with pytest.raises(ValueError, match="'terms' must be a list or an object"):
            parse_layer({"terms": "Karussell"})

    def test_non_string_term_is_a_defect(self):
        with pytest.raises(ValueError, match="each term must be a non-empty string"):
            parse_layer({"terms": ["fine", 42]})

    def test_blank_term_is_a_defect(self):
        with pytest.raises(ValueError, match="each term must be a non-empty string"):
            parse_layer({"terms": ["   "]})

    def test_duplicate_after_lowercasing_is_a_defect(self):
        with pytest.raises(ValueError, match="duplicate term 'karussell'"):
            parse_layer({"terms": ["Karussell", "karussell"]})

    def test_non_boolean_term_flag_is_a_defect(self):
        with pytest.raises(ValueError, match="must map to true or false"):
            parse_layer({"terms": {"Karussell": 1.5}})

    def test_over_long_term_is_a_defect(self):
        with pytest.raises(ValueError, match=f"over the {MAX_ENTRY_LENGTH}-character cap"):
            parse_layer({"terms": ["x" * (MAX_ENTRY_LENGTH + 1)]})

    def test_control_character_in_term_is_a_defect(self):
        with pytest.raises(ValueError, match="must not contain control characters"):
            parse_layer({"terms": ["kar\nussell"]})


class TestParseReplacements:
    def test_string_value_is_a_correction(self):
        layer = parse_layer({"replacements": {"Kessichen": "Kesselchen"}})
        assert layer.replacements == {"kessichen": ("Kessichen", "Kesselchen")}

    def test_null_disables_a_replacement(self):
        layer = parse_layer({"replacements": {"carousel": None}})
        assert layer.replacements == {"carousel": ("carousel", None)}

    def test_false_disables_a_replacement(self):
        layer = parse_layer({"replacements": {"carousel": False}})
        assert layer.replacements == {"carousel": ("carousel", None)}

    def test_replacements_wrong_type_is_a_defect(self):
        with pytest.raises(ValueError, match="'replacements' must be an object"):
            parse_layer({"replacements": ["kessichen"]})

    def test_non_string_value_is_a_defect(self):
        with pytest.raises(ValueError, match="must be a string, or null to disable"):
            parse_layer({"replacements": {"very very": 42}})

    def test_empty_value_is_refused_with_a_pointer_to_null(self):
        with pytest.raises(ValueError, match="use null to disable"):
            parse_layer({"replacements": {"very very": "  "}})

    def test_punctuation_only_key_is_a_defect(self):
        with pytest.raises(ValueError, match="is only punctuation"):
            parse_layer({"replacements": {"...": "Kesselchen"}})

    def test_duplicate_after_normalisation_is_a_defect(self):
        with pytest.raises(ValueError, match="duplicate replacement key"):
            parse_layer({"replacements": {"Kessichen": "Kesselchen",
                                          "kessichen,": "Kesselchen"}})

    def test_over_long_replacement_text_is_a_defect(self):
        with pytest.raises(ValueError, match=f"over the {MAX_ENTRY_LENGTH}-character cap"):
            parse_layer({"replacements": {"k": "x" * (MAX_ENTRY_LENGTH + 1)}})


class TestParseLayerShape:
    def test_not_an_object_is_a_defect(self):
        with pytest.raises(ValueError, match="glossary must be an object, found list"):
            parse_layer(["not", "an", "object"])

    def test_empty_object_is_the_empty_layer(self):
        assert parse_layer({}) == EMPTY_LAYER


class TestLoad:
    def test_missing_file_is_the_empty_layer(self, tmp_path):
        assert load(tmp_path / "nope.json") == EMPTY_LAYER

    def test_reads_a_file(self, tmp_path):
        path = tmp_path / "glossary.json"
        path.write_text(json.dumps({"terms": ["Karussell"]}), encoding="utf-8")
        assert load(path).terms == {"karussell": ("Karussell", True)}

    def test_invalid_json_says_so(self, tmp_path):
        path = tmp_path / "glossary.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError, match="is not valid JSON"):
            load(path)

    def test_a_defect_does_not_double_the_path(self, tmp_path):
        # load() itself no longer appends the path to a parse_layer defect -
        # every caller (profile._load_glossary, glossary_admin's own loader)
        # already prefixes it, and doing it here too doubled it in a
        # ProfileError line and the studio's problems Alert. The defect
        # message itself is still surfaced unchanged.
        path = tmp_path / "glossary.json"
        path.write_text(json.dumps({"terms": [42]}), encoding="utf-8")
        with pytest.raises(ValueError, match="each term must be a non-empty string") as excinfo:
            load(path)
        assert str(path) not in str(excinfo.value)


class TestBuiltInDefaultIsEmpty:
    """The built-in default used to carry the Nordschleife. It does not any
    more: there is no proper noun that is correct on every circuit, so an
    always-on layer has nothing to hold, and a track-specific rule in it fired
    on the wrong track (`carousel` -> `Karussell` on any circuit with its own
    Carousel). The Nordschleife lives in tracks.PACKS now."""

    def test_the_default_layer_is_empty(self):
        assert DEFAULT_LAYER == EMPTY_LAYER

    def test_the_old_default_constants_are_gone(self):
        import yt_shorts.glossary as g
        assert not hasattr(g, "DEFAULT_TERMS")
        assert not hasattr(g, "DEFAULT_REPLACEMENTS")


class TestEmptyLayerIsNotShared:
    def test_empty_layer_is_not_mutated_by_a_parse(self):
        parse_layer({"terms": ["Karussell"]})
        assert EMPTY_LAYER == GlossaryLayer(terms={}, replacements={})


class TestWorkspaceGlossaryPath:
    def test_path_is_the_workspace_root_file(self, tmp_path):
        from yt_shorts import workspace
        assert workspace.glossary_path(tmp_path) == tmp_path / "glossary.json"

    def test_nothing_is_created(self, tmp_path):
        from yt_shorts import workspace
        workspace.glossary_path(tmp_path)
        assert list(tmp_path.iterdir()) == []


class TestTrackKey:
    def test_absent_track_is_none(self):
        assert parse_layer({"terms": ["Karussell"]}).track is None

    def test_a_track_is_parsed(self):
        assert parse_layer({"track": "spa-francorchamps"}).track == "spa-francorchamps"

    def test_a_track_is_stripped(self):
        assert parse_layer({"track": "  monza  "}).track == "monza"

    def test_an_empty_track_is_a_defect(self):
        with pytest.raises(ValueError, match="'track' must be a non-empty string"):
            parse_layer({"track": "   "})

    def test_a_non_string_track_is_a_defect(self):
        with pytest.raises(ValueError, match="'track' must be a non-empty string"):
            parse_layer({"track": 42})

    def test_a_null_track_is_the_same_as_absent(self):
        """The studio's selector sends null for "no track", and that must mean
        the same thing as never having chosen one."""
        assert parse_layer({"track": None}).track is None

    def test_a_control_character_in_a_track_is_a_defect(self):
        """_parse_track delegates to _check_text for this, exactly as the term
        and replacement parsers do. Pinned so a future edit that
        re-implemented the check inline could not quietly drop the guard - the
        sibling parsers each have this case, and this one was missing it."""
        with pytest.raises(ValueError, match="must not contain control characters"):
            parse_layer({"track": "mo\x07nza"})

    def test_an_over_long_track_is_a_defect(self):
        with pytest.raises(ValueError, match=f"over the {MAX_ENTRY_LENGTH}-character cap"):
            parse_layer({"track": "x" * (MAX_ENTRY_LENGTH + 1)})

    def test_the_empty_layer_has_no_track(self):
        assert EMPTY_LAYER.track is None

    def test_load_reads_a_track(self, tmp_path):
        path = tmp_path / "glossary.json"
        path.write_text(json.dumps({"track": "monza", "terms": ["Lesmo"]}),
                        encoding="utf-8")
        layer = load(path)
        assert layer.track == "monza"
        assert layer.terms == {"lesmo": ("Lesmo", True)}


class TestMergeGlossaries:
    def _layer(self, terms=None, replacements=None):
        return parse_layer({"terms": terms or [], "replacements": replacements or {}})

    def test_all_four_layers_combine(self):
        # MOST-SPECIFIC-FIRST, not the layer order passed in - see
        # merge_glossaries's own docstring for why: faster-whisper truncates
        # its hotword prompt from the END of this list, so the operator's
        # own (most specific) terms must sit at the FRONT, not the built-in
        # default.
        from yt_shorts.profile import merge_glossaries
        merged = merge_glossaries([
            self._layer(terms=["Default"]),
            self._layer(terms=["Workspace"]),
            self._layer(terms=["Channel"]),
            self._layer(terms=["Event"]),
        ])
        assert merged.terms == ["Event", "Channel", "Workspace", "Default"]

    def test_own_terms_precede_less_specific_terms_a_truncation_survival_property(self):
        # This is NOT cosmetic ordering - it is what keeps an operator's own
        # terms inside faster-whisper's 224-token hotword budget when the
        # combined list is long enough to be truncated (see
        # glossary.HOTWORD_BUDGET_CHARS and merge_glossaries's docstring).
        # Do not "simplify" merge_glossaries back to a single forward pass;
        # that would silently put the less specific layer back in front and
        # make truncation eat the operator's own entries first. (Formerly
        # exercised with the built-in default itself, before it was emptied
        # - see TestBuiltInDefaultIsEmpty - so a plain stand-in layer plays
        # the same "less specific" role here now.)
        from yt_shorts.profile import merge_glossaries
        merged = merge_glossaries([
            self._layer(terms=["Nordschleife"]),
            self._layer(terms=["Mutkurve Racing", "Team Fullsend"]),
        ])
        own_index = merged.terms.index("Mutkurve Racing")
        less_specific_index = merged.terms.index("Nordschleife")
        assert own_index < less_specific_index

    def test_a_more_specific_layer_wins_a_terms_spelling(self):
        from yt_shorts.profile import merge_glossaries
        merged = merge_glossaries([
            self._layer(terms=["karussell"]),
            self._layer(terms=["Karussell"]),
        ])
        assert merged.terms == ["Karussell"]

    def test_a_more_specific_layer_wins_a_replacement(self):
        from yt_shorts.profile import merge_glossaries
        merged = merge_glossaries([
            self._layer(replacements={"carousel": "Karussell"}),
            self._layer(replacements={"carousel": "Carousel corner"}),
        ])
        assert merged.replacements == {"carousel": "Carousel corner"}

    def test_a_disabled_term_is_dropped_from_the_merge(self):
        from yt_shorts.profile import merge_glossaries
        merged = merge_glossaries([
            self._layer(terms=["Karussell"]),
            self._layer(terms={"Karussell": False}),
        ])
        assert merged.terms == []

    def test_a_disabled_replacement_is_dropped_from_the_merge(self):
        from yt_shorts.profile import merge_glossaries
        merged = merge_glossaries([
            self._layer(replacements={"carousel": "Karussell"}),
            self._layer(replacements={"carousel": None}),
        ])
        assert merged.replacements == {}

    def test_a_disabled_entry_stays_in_its_raw_layer(self):
        """What lets the editor strike it through instead of losing it."""
        layer = self._layer(terms={"Karussell": False})
        assert layer.terms == {"karussell": ("Karussell", False)}

    def test_a_more_specific_layer_can_re_enable_a_disabled_term(self):
        from yt_shorts.profile import merge_glossaries
        merged = merge_glossaries([
            self._layer(terms=["Karussell"]),
            self._layer(terms={"Karussell": False}),
            self._layer(terms=["Karussell"]),
        ])
        assert merged.terms == ["Karussell"]

    def test_no_layers_is_the_empty_glossary(self):
        from yt_shorts.glossary import EMPTY
        from yt_shorts.profile import merge_glossaries
        assert merge_glossaries([]) == EMPTY

    def test_the_empty_default_alone_produces_nothing(self):
        # The built-in default is EMPTY by design now (see
        # TestBuiltInDefaultIsEmpty) - merging it alone must still work and
        # must not manufacture hotwords out of nothing.
        from yt_shorts.glossary import EMPTY, hotwords
        from yt_shorts.profile import merge_glossaries
        merged = merge_glossaries([DEFAULT_LAYER])
        assert merged == EMPTY
        assert hotwords(merged) is None
