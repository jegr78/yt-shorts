
from yt_shorts.glossary import (
    EMPTY, HOTWORD_BUDGET_CHARS, Glossary, apply, hotwords, hotwords_at_risk)


def w(start, end, text):
    return {"start": start, "end": end, "text": text}


class TestHotwords:
    def test_no_terms_means_no_hotwords(self):
        assert hotwords(EMPTY) is None

    def test_terms_are_joined_for_the_decoder(self):
        assert hotwords(Glossary(terms=["Rei Racing", "Nordschleife"],
                                 replacements={})) == "Rei Racing, Nordschleife"


class TestHotwordsAtRisk:
    def test_empty_glossary_is_not_at_risk(self):
        assert hotwords_at_risk(EMPTY) is False

    def test_a_short_glossary_is_not_at_risk(self):
        assert hotwords_at_risk(
            Glossary(terms=["Rei Racing", "Nordschleife"], replacements={})) is False

    def test_a_glossary_at_the_budget_is_at_risk(self):
        # One term whose own length alone reaches the budget.
        long_term = "x" * HOTWORD_BUDGET_CHARS
        assert hotwords_at_risk(Glossary(terms=[long_term], replacements={})) is True

    def test_just_under_the_budget_is_not_at_risk(self):
        term = "x" * (HOTWORD_BUDGET_CHARS - 1)
        assert hotwords_at_risk(Glossary(terms=[term], replacements={})) is False


class TestApply:
    def test_an_empty_glossary_changes_nothing(self):
        words = [w(0.0, 0.5, " very"), w(0.5, 1.0, " very")]
        assert apply(words, EMPTY) == words

    def test_a_two_word_phrase_becomes_two_words(self):
        words = [w(0.0, 0.5, " very"), w(0.5, 1.0, " very")]
        result = apply(words, Glossary(terms=[],
                                       replacements={"very very": "Rei Racing"}))
        assert [x["text"] for x in result] == [" Rei", " Racing"]
        assert result[0]["start"] == 0.0
        assert result[-1]["end"] == 1.0

    def test_two_words_collapsing_to_one_span_the_whole_range(self):
        words = [w(0.0, 0.5, " very"), w(0.5, 1.0, " very")]
        result = apply(words, Glossary(terms=[], replacements={"very very": "Rei"}))
        assert [x["text"] for x in result] == [" Rei"]
        assert result[0]["start"] == 0.0
        assert result[0]["end"] == 1.0

    def test_one_word_expanding_to_two_splits_the_range(self):
        words = [w(0.0, 1.0, " Fullsend")]
        result = apply(words, Glossary(terms=[],
                                       replacements={"Fullsend": "Team Fullsend"}))
        assert [x["text"] for x in result] == [" Team", " Fullsend"]
        assert result[0]["start"] == 0.0
        assert result[-1]["end"] == 1.0
        assert result[0]["end"] == result[1]["start"]

    def test_matching_ignores_case_and_punctuation_spacing(self):
        words = [w(0.0, 0.5, " Very"), w(0.5, 1.0, " VERY")]
        result = apply(words, Glossary(terms=[], replacements={"very very": "Rei"}))
        assert result[0]["text"] == " Rei"

    def test_surrounding_words_are_untouched(self):
        words = [w(0.0, 0.4, " and"), w(0.4, 0.9, " very"),
                 w(0.9, 1.4, " very"), w(1.4, 1.8, " wins")]
        result = apply(words, Glossary(terms=[], replacements={"very very": "Rei"}))
        assert [x["text"] for x in result] == [" and", " Rei", " wins"]
        assert result[0] == words[0]
        assert result[-1] == words[-1]

    def test_a_longer_key_wins_over_a_shorter_overlapping_one(self):
        words = [w(0.0, 0.5, " very"), w(0.5, 1.0, " very")]
        result = apply(words, Glossary(
            terms=[], replacements={"very": "X", "very very": "Rei"}))
        assert [x["text"] for x in result] == [" Rei"]

    def test_a_leading_space_is_preserved_from_the_matched_run(self):
        # Whisper marks word starts with a leading space; a replacement that
        # dropped it would run into the previous word when joined.
        words = [w(0.0, 0.4, " and"), w(0.4, 1.0, " Fullsend")]
        result = apply(words, Glossary(terms=[], replacements={"Fullsend": "Team"}))
        assert result[1]["text"].startswith(" ")

    def test_timestamps_stay_monotonic(self):
        words = [w(0.0, 1.2, " Fullsend")]
        result = apply(words, Glossary(
            terms=[], replacements={"Fullsend": "Team Fullsend Racing"}))
        starts = [x["start"] for x in result]
        ends = [x["end"] for x in result]
        assert starts == sorted(starts)
        assert all(s <= e for s, e in zip(starts, ends, strict=True))

    def test_the_real_very_comma_very_pair_becomes_rei_racing(self):
        # Pinned from the actual cached transcript for
        # erf/community-clips-back-catalogue/rei-got-sliced: Whisper
        # attaches the comma to the first word, not the second. This exact
        # pair - not a stylised one without punctuation - is the defect
        # this feature exists to fix.
        words = [w(0.0, 0.5, " very,"), w(0.5, 1.0, " very")]
        result = apply(words, Glossary(
            terms=[], replacements={"very very": "Rei Racing"}))
        assert [x["text"] for x in result] == [" Rei", " Racing"]
        assert result[0]["start"] == 0.0
        assert result[-1]["end"] == 1.0

    def test_a_comma_inside_the_matched_phrase_is_ignored(self):
        words = [w(0.0, 0.5, " very,"), w(0.5, 1.0, " very")]
        result = apply(words, Glossary(terms=[], replacements={"very very": "Rei"}))
        assert [x["text"] for x in result] == [" Rei"]

    def test_an_apostrophe_inside_the_matched_phrase_is_ignored(self):
        words = [w(0.0, 0.5, " it's"), w(0.5, 1.0, " Rei")]
        result = apply(words, Glossary(
            terms=[], replacements={"its rei": "Rei Racing"}))
        assert [x["text"] for x in result] == [" Rei", " Racing"]

    def test_a_full_stop_between_the_words_refuses_the_match(self):
        # "very. Very" is two sentences, not the phrase "very very" - a
        # comma normalises away, but a full stop must not.
        words = [w(0.0, 0.5, " very."), w(0.5, 1.0, " Very")]
        result = apply(words, Glossary(terms=[], replacements={"very very": "Rei"}))
        assert [x["text"] for x in result] == [" very.", " Very"]
