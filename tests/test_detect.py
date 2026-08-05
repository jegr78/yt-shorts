import json

import pytest

from yt_shorts import detect, providers
from yt_shorts.providers import anthropic_api, gemini_api, openai_api
from yt_shorts.glossary import EMPTY as GLOSSARY_EMPTY
from yt_shorts.glossary import Glossary
from yt_shorts.lexicon import Lexicon
from yt_shorts.stream_transcribe import StreamTranscript


def transcript(words, *, video_id="vid123", tmp_path=None):
    def fake(video_id_, workspace_dir, *, glossary=None):
        return StreamTranscript(video_id=video_id_,
                                audio_path=(tmp_path or workspace_dir) / "audio.webm",
                                duration_seconds=600.0, words=words)
    return fake


def some_words(seconds=600):
    return [{"start": t, "end": t + 0.8, "text": " word"} for t in range(seconds)]


def config(**over):
    base = {"lexicon": Lexicon(markers={"crash": 3.0})}
    base.update(over)
    return base


class TestWritesAnalysis:
    def test_writes_moments_json_and_returns_its_path(self, tmp_path):
        def caller(system, user, schema):
            return {"moments": [{"start_line": 2, "end_line": 3, "category": "incident",
                                 "score": 8.0, "reason": "A crash at Pflanzgarten.",
                                 "hook_suggestion": "BIG ONE"}]}
        path = detect.detect_moments("vid123", tmp_path, config(),
                                     stream_title="Race", caller=caller,
                                     transcriber=transcript(some_words(), tmp_path=tmp_path),
                                     now=lambda: "2026-07-27T10:00:00+00:00")
        assert path.name == "moments.json"
        data = json.loads(path.read_text())
        # Against the CONSTANT, not the model name of the day: what this pins
        # is that the engine is recorded and that a config naming no model
        # falls back to the shipped default - not which default won the last
        # bake-off (see anthropic_api.DEFAULT_MODEL's own note).
        assert data["engine"] == f"model:{anthropic_api.DEFAULT_MODEL}"
        assert data["moments"][0]["category"] == "incident"
        assert data["created_at"] == "2026-07-27T10:00:00+00:00"
        assert data["activity"]

    def test_records_the_stream_title_for_a_future_picker(self, tmp_path):
        # MINOR-1: a picker turning a candidate into a clip needs
        # source_title for clip_from_moment.create_clip without re-fetching
        # the stream list to look the title up again.
        def caller(system, user, schema):
            return {"moments": []}
        path = detect.detect_moments("vid123", tmp_path, config(),
                                     stream_title="ERF Qualifying Part 1",
                                     caller=caller,
                                     transcriber=transcript(some_words(), tmp_path=tmp_path))
        assert json.loads(path.read_text())["stream_title"] == "ERF Qualifying Part 1"

    def test_creates_no_clip_directories(self, tmp_path):
        # The whole point: detection stops producing artifacts nobody asked for.
        event = tmp_path / "event"
        event.mkdir()
        def caller(system, user, schema):
            return {"moments": [{"start_line": 2, "end_line": 3, "category": "incident",
                                 "score": 8.0, "reason": "x"}]}
        detect.detect_moments("vid123", tmp_path, config(), stream_title="Race",
                              caller=caller,
                              transcriber=transcript(some_words(), tmp_path=tmp_path))
        assert list(event.iterdir()) == []

    def test_re_running_overwrites_rather_than_accumulating(self, tmp_path):
        def caller(system, user, schema):
            return {"moments": []}
        args = dict(stream_title="Race", caller=caller,
                    transcriber=transcript(some_words(), tmp_path=tmp_path))
        first = detect.detect_moments("vid123", tmp_path, config(), **args)
        second = detect.detect_moments("vid123", tmp_path, config(), **args)
        assert first == second


class TestUsageIsReported:
    """A run says what it actually cost, in the API's own numbers.

    This exists because the estimate was believed for a day: estimate.py
    counts characters and came out at roughly half a real invoice while its
    own comment claimed it ran ~12% HIGH. Nothing measured the truth, so
    nothing could contradict it.
    """

    def _run(self, tmp_path, caplog, logger_name, model, usage_values):
        import logging

        logger = logging.getLogger(logger_name)

        def fake_caller(workspace_dir, provider, model_, log, usage=None):
            # Stands in for a real make_caller: fills the accumulator the way
            # a real response would, without an SDK, a key or a cent.
            assert provider.PROVIDER_ID == "anthropic"   # nothing selected one
            if usage is not None:
                usage.calls = usage_values[0]
                usage.input_tokens = usage_values[1]
                usage.output_tokens = usage_values[2]
            return lambda system, user, schema: {"moments": []}

        original = detect._caller_from_config
        detect._caller_from_config = fake_caller
        try:
            with caplog.at_level(logging.INFO, logger=logger_name):
                detect.detect_moments("vid123", tmp_path,
                                      config(detect={"model": model}),
                                      stream_title="Race", logger=logger,
                                      transcriber=transcript(some_words(60),
                                                             tmp_path=tmp_path))
        finally:
            detect._caller_from_config = original
        return caplog.text

    def test_logs_the_measured_tokens_and_cost(self, tmp_path, caplog):
        text = self._run(tmp_path, caplog, "ytshorts.test.usage1",
                         "claude-opus-5", (2, 20000, 3000))
        assert "2 call(s)" in text
        assert "20000 input" in text and "3000 output" in text
        # 20000/1e6*5 + 3000/1e6*25 = 0.10 + 0.075
        assert "$0.1750 measured" in text

    def test_an_unpriced_model_reports_tokens_and_refuses_to_say_zero(
            self, tmp_path, caplog):
        # 0.00 is what a free run looks like. An unpriced model must not
        # borrow that appearance - same stance as estimate_run's `priced`.
        text = self._run(tmp_path, caplog, "ytshorts.test.usage2",
                         "some-unlisted-model", (1, 10, 10))
        assert "unknown, not zero" in text
        assert "$0.0000" not in text

    def test_the_lexicon_engine_reports_no_cost_line_at_all(self, tmp_path, caplog):
        import logging

        logger = logging.getLogger("ytshorts.test.usage3")
        with caplog.at_level(logging.INFO, logger="ytshorts.test.usage3"):
            detect.detect_moments("vid123", tmp_path, config(),
                                  stream_title="Race", caller=None, logger=logger,
                                  transcriber=transcript(some_words(60),
                                                         tmp_path=tmp_path))
        assert "call(s)" not in caplog.text


class TestEngineSelection:
    def test_falls_back_to_the_lexicon_when_no_key_is_configured(self, tmp_path):
        words = some_words() + [{"start": 300.0, "end": 300.5, "text": " crash"}]
        path = detect.detect_moments("vid123", tmp_path, config(),
                                     stream_title="Race", caller=None,
                                     transcriber=transcript(words, tmp_path=tmp_path))
        data = json.loads(path.read_text())
        assert data["engine"] == "lexicon"

    def test_the_fallback_is_announced_on_the_logger(self, tmp_path, caplog):
        # A silent downgrade to the weaker engine is exactly the defect this
        # rewrite exists to remove.
        import logging
        logger = logging.getLogger("ytshorts.test.detect")
        with caplog.at_level(logging.WARNING, logger="ytshorts.test.detect"):
            detect.detect_moments("vid123", tmp_path, config(),
                                  stream_title="Race", caller=None, logger=logger,
                                  transcriber=transcript(some_words(), tmp_path=tmp_path))
        assert "lexicon" in caplog.text.lower()

    def test_an_empty_transcript_is_reported_loudly(self, tmp_path, caplog):
        import logging
        logger = logging.getLogger("ytshorts.test.detect2")
        with caplog.at_level(logging.WARNING, logger="ytshorts.test.detect2"):
            path = detect.detect_moments("vid123", tmp_path, config(),
                                         stream_title="Race", caller=None, logger=logger,
                                         transcriber=transcript([], tmp_path=tmp_path))
        assert json.loads(path.read_text())["moments"] == []
        assert "0 words" in caplog.text

    def test_an_empty_transcript_records_engine_none_not_lexicon(self, tmp_path):
        # MINOR-4: `if caller is None and words` means a 0-word transcript
        # fell through to the lexicon branch and wrote "engine": "lexicon"
        # even though no engine ran at all - untrue, though not silent (the
        # 0-words warning still fires). Reproduce with caller=None...
        path = detect.detect_moments("vid123", tmp_path, config(),
                                     stream_title="Race", caller=None,
                                     transcriber=transcript([], tmp_path=tmp_path))
        assert json.loads(path.read_text())["engine"] == "none"

    def test_an_empty_transcript_records_engine_none_even_with_a_configured_caller(
            self, tmp_path):
        # ...and again with a model key configured (a real caller passed in),
        # which is the exact case the finding's title names: "even when a
        # model key is configured".
        def caller(system, user, schema):
            raise AssertionError("must not be called for an empty transcript")
        path = detect.detect_moments("vid123", tmp_path, config(),
                                     stream_title="Race", caller=caller,
                                     transcriber=transcript([], tmp_path=tmp_path))
        assert json.loads(path.read_text())["engine"] == "none"

    def test_an_unknown_failure_logs_its_type_and_not_its_text(self, tmp_path, caplog):
        # The provider wraps every SDK exception, but this handler must not
        # DEPEND on that: an exception whose text this project has made no
        # promise about describes a request that carries the API key.
        import logging

        from yt_shorts.providers import anthropic_api as cc
        secret = "sk-ant-api03-SUPERSECRET"
        (tmp_path / "auth").mkdir()
        (tmp_path / "auth" / "anthropic.json").write_text(secret)

        def exploding_caller(*args, **kwargs):
            raise RuntimeError(f"connection failed for key {secret}")

        original = cc.make_caller
        cc.make_caller = exploding_caller
        try:
            logger = logging.getLogger("ytshorts.test.detect3")
            with caplog.at_level(logging.WARNING, logger="ytshorts.test.detect3"):
                detect.detect_moments("vid123", tmp_path, config(),
                                      stream_title="Race", logger=logger,
                                      transcriber=transcript(some_words(),
                                                             tmp_path=tmp_path))
        finally:
            cc.make_caller = original
        assert secret not in caplog.text
        assert "RuntimeError" in caplog.text

    def test_a_corrupt_non_utf8_key_file_degrades_to_the_lexicon(self, tmp_path):
        # Reproduces IMPORTANT-1: a key file containing one stray non-UTF-8
        # byte used to raise UnicodeDecodeError out of load_api_key, which
        # _caller_from_config's `except MissingKey` does not catch - so the
        # whole detect run aborted instead of falling back. This is the
        # property that actually matters: the run must still produce an
        # analysis, engine=lexicon, with no exception escaping.
        words = some_words() + [{"start": 300.0, "end": 300.5, "text": " crash"}]
        (tmp_path / "auth").mkdir()
        (tmp_path / "auth" / "anthropic.json").write_bytes(b"\x80\x81\x82")
        path = detect.detect_moments("vid123", tmp_path, config(),
                                     stream_title="Race", caller=None,
                                     transcriber=transcript(words, tmp_path=tmp_path))
        assert json.loads(path.read_text())["engine"] == "lexicon"

    def test_the_model_name_comes_from_the_config(self, tmp_path):
        def caller(system, user, schema):
            return {"moments": []}
        path = detect.detect_moments(
            "vid123", tmp_path,
            config(detect={"model": "claude-sonnet-5"}),
            stream_title="Race", caller=caller,
            transcriber=transcript(some_words(), tmp_path=tmp_path))
        assert json.loads(path.read_text())["engine"] == "model:claude-sonnet-5"


class TestProviderSelection:
    """`config["detect"]["provider"]` decides which vendor answers.

    Before this, `_caller_from_config` resolved `providers.DEFAULT_PROVIDER`
    unconditionally, so a registry of three providers had exactly one that
    could ever run. Every test here fails if the configured provider is
    ignored and the default used instead.
    """

    def _record_make_caller(self, monkeypatch, module, seen):
        def fake(api_key, *, model, usage=None):
            seen.append((module.PROVIDER_ID, api_key, model, usage))
            return lambda system, user, schema: {"moments": []}
        monkeypatch.setattr(module, "make_caller", fake)

    def _key(self, tmp_path, filename, value="sk-test-000"):
        (tmp_path / "auth").mkdir(exist_ok=True)
        (tmp_path / "auth" / filename).write_text(value, encoding="utf-8")

    def _explode(self, monkeypatch, module):
        def fake(*args, **kwargs):
            raise AssertionError(f"{module.PROVIDER_ID}.make_caller must not be called")
        monkeypatch.setattr(module, "make_caller", fake)

    def test_the_configured_provider_is_the_one_used(self, tmp_path, monkeypatch):
        seen = []
        self._key(tmp_path, "gemini.json", "gemini-key")
        self._key(tmp_path, "anthropic.json", "sk-ant-key")
        self._record_make_caller(monkeypatch, gemini_api, seen)
        self._explode(monkeypatch, anthropic_api)
        detect.detect_moments("vid123", tmp_path,
                              config(detect={"provider": "gemini"}),
                              stream_title="Race",
                              transcriber=transcript(some_words(), tmp_path=tmp_path))
        assert [entry[0] for entry in seen] == ["gemini"]
        # ...and the key came from THAT provider's own file, not anthropic's.
        assert seen[0][1] == "gemini-key"

    def test_no_provider_configured_means_anthropic(self, tmp_path, monkeypatch):
        seen = []
        self._key(tmp_path, "anthropic.json", "sk-ant-key")
        self._record_make_caller(monkeypatch, anthropic_api, seen)
        self._explode(monkeypatch, gemini_api)
        path = detect.detect_moments("vid123", tmp_path, config(),
                                     stream_title="Race",
                                     transcriber=transcript(some_words(),
                                                            tmp_path=tmp_path))
        assert [entry[0] for entry in seen] == ["anthropic"]
        assert json.loads(path.read_text())["configured_provider"] == "anthropic"

    def test_the_default_model_comes_from_the_selected_provider(self, tmp_path, monkeypatch):
        # Not anthropic's default with gemini's key - the pairing that would
        # fail at call time for a reason no log line explains.
        seen = []
        self._key(tmp_path, "gemini.json")
        self._record_make_caller(monkeypatch, gemini_api, seen)
        path = detect.detect_moments("vid123", tmp_path,
                                     config(detect={"provider": "gemini"}),
                                     stream_title="Race",
                                     transcriber=transcript(some_words(),
                                                            tmp_path=tmp_path))
        assert seen[0][2] == gemini_api.DEFAULT_MODEL
        assert json.loads(path.read_text())["engine"] == f"model:{gemini_api.DEFAULT_MODEL}"

    def test_a_configured_model_still_wins_over_the_providers_default(
            self, tmp_path, monkeypatch):
        seen = []
        self._key(tmp_path, "gemini.json")
        self._record_make_caller(monkeypatch, gemini_api, seen)
        detect.detect_moments("vid123", tmp_path,
                              config(detect={"provider": "gemini",
                                             "model": "gemini-2.5-flash"}),
                              stream_title="Race",
                              transcriber=transcript(some_words(), tmp_path=tmp_path))
        assert seen[0][2] == "gemini-2.5-flash"

    def test_the_analysis_records_which_provider_produced_it(self, tmp_path, monkeypatch):
        seen = []
        self._key(tmp_path, "gemini.json")
        self._record_make_caller(monkeypatch, gemini_api, seen)
        path = detect.detect_moments("vid123", tmp_path,
                                     config(detect={"provider": "gemini"}),
                                     stream_title="Race",
                                     transcriber=transcript(some_words(),
                                                            tmp_path=tmp_path))
        data = json.loads(path.read_text())
        assert data["configured_provider"] == "gemini"
        # ADDITIVE: `engine` keeps its exact format. The frontend and several
        # tests read it, and folding the provider into that string would
        # change what every one of them sees.
        assert data["engine"] == f"model:{gemini_api.DEFAULT_MODEL}"
        assert "gemini" not in data["engine"].split(":")[0]

    def test_the_provider_field_names_the_configured_one_even_on_a_fallback(
            self, tmp_path):
        # With no key at all the lexicon runs, and `engine` says so - that
        # field stays the authority on WHAT produced the analysis.
        # `configured_provider` records which vendor the run was configured
        # for, which is the piece of diagnosis an operator needs to know
        # whose key is missing.
        path = detect.detect_moments("vid123", tmp_path,
                                     config(detect={"provider": "openai"}),
                                     stream_title="Race", caller=None,
                                     transcriber=transcript(some_words(),
                                                            tmp_path=tmp_path))
        data = json.loads(path.read_text())
        assert data["engine"] == "lexicon"
        assert data["configured_provider"] == "openai"

    def test_an_unknown_provider_in_config_raises_rather_than_guessing(self, tmp_path):
        # profile.load already refuses this, but detect must not silently
        # answer a typo with Anthropic if handed one directly (the studio's
        # job runner and a hand-written config both reach here).
        with pytest.raises(providers.UnknownProvider):
            detect.detect_moments("vid123", tmp_path,
                                  config(detect={"provider": "anthropc"}),
                                  stream_title="Race",
                                  transcriber=transcript(some_words(),
                                                         tmp_path=tmp_path))

    def test_a_missing_key_for_the_selected_provider_names_it_in_the_log(
            self, tmp_path, caplog):
        # An anthropic key on disk must not make a gemini run look configured:
        # each provider reads its OWN file, and the warning has to say whose
        # key is missing or the operator has nothing to act on.
        import logging

        self._key(tmp_path, "anthropic.json", "sk-ant-key")
        logger = logging.getLogger("ytshorts.test.provider1")
        with caplog.at_level(logging.WARNING, logger="ytshorts.test.provider1"):
            path = detect.detect_moments("vid123", tmp_path,
                                         config(detect={"provider": "gemini"}),
                                         stream_title="Race", logger=logger,
                                         transcriber=transcript(some_words(),
                                                                tmp_path=tmp_path))
        assert json.loads(path.read_text())["engine"] == "lexicon"
        assert "gemini" in caplog.text
        assert "anthropic" not in caplog.text

    def test_an_unavailable_sdk_for_the_selected_provider_degrades(
            self, tmp_path, monkeypatch, caplog):
        import logging

        self._key(tmp_path, "openai.json")

        def unavailable(*args, **kwargs):
            raise providers.SdkUnavailable("openai is not installed in this venv")
        monkeypatch.setattr(openai_api, "make_caller", unavailable)
        logger = logging.getLogger("ytshorts.test.provider2")
        with caplog.at_level(logging.WARNING, logger="ytshorts.test.provider2"):
            path = detect.detect_moments("vid123", tmp_path,
                                         config(detect={"provider": "openai"}),
                                         stream_title="Race", logger=logger,
                                         transcriber=transcript(some_words(),
                                                                tmp_path=tmp_path))
        assert json.loads(path.read_text())["engine"] == "lexicon"
        assert "not installed" in caplog.text

    def test_an_unwrapped_failure_from_any_provider_logs_only_its_type(
            self, tmp_path, monkeypatch, caplog):
        # The key-secrecy rule is per-PROVIDER, not per-Anthropic: an
        # exception the provider did not wrap describes a request that
        # carries the key.
        import logging

        secret = "AIzaSy-SUPERSECRET-GEMINI"
        self._key(tmp_path, "gemini.json", secret)

        def exploding(*args, **kwargs):
            raise RuntimeError(f"connection failed for key {secret}")
        monkeypatch.setattr(gemini_api, "make_caller", exploding)
        logger = logging.getLogger("ytshorts.test.provider3")
        with caplog.at_level(logging.WARNING, logger="ytshorts.test.provider3"):
            detect.detect_moments("vid123", tmp_path,
                                  config(detect={"provider": "gemini"}),
                                  stream_title="Race", logger=logger,
                                  transcriber=transcript(some_words(),
                                                         tmp_path=tmp_path))
        assert secret not in caplog.text
        assert "RuntimeError" in caplog.text

    def test_the_cost_line_prices_against_the_selected_providers_table(
            self, tmp_path, monkeypatch, caplog):
        # gemini-3.6-flash exists in gemini_api.PRICES and in NO other
        # provider's, so a _report_usage still reading anthropic's table
        # reports "unknown, not zero" here instead of a figure.
        import logging

        self._key(tmp_path, "gemini.json")

        def fake(api_key, *, model, usage=None):
            if usage is not None:
                usage.calls, usage.input_tokens, usage.output_tokens = 2, 20000, 4000
            return lambda system, user, schema: {"moments": []}
        monkeypatch.setattr(gemini_api, "make_caller", fake)
        logger = logging.getLogger("ytshorts.test.provider4")
        with caplog.at_level(logging.INFO, logger="ytshorts.test.provider4"):
            detect.detect_moments("vid123", tmp_path,
                                  config(detect={"provider": "gemini",
                                                 "model": "gemini-3.6-flash"}),
                                  stream_title="Race", logger=logger,
                                  transcriber=transcript(some_words(60),
                                                         tmp_path=tmp_path))
        # 20000/1e6*1.50 + 4000/1e6*7.50 = 0.03 + 0.03
        assert "$0.0600 measured" in caplog.text
        assert "unknown, not zero" not in caplog.text

    # --- I1/I2: an explicit null passes profile validation as "absent" -----
    # (`profile._validate_detect`'s own docstring), so `dict.get(key,
    # default)` - which does NOT substitute the default for a key present
    # with value None - must not be what resolves it here.

    def test_an_explicit_null_provider_falls_back_to_the_default(
            self, tmp_path, monkeypatch):
        seen = []
        self._key(tmp_path, "anthropic.json", "sk-ant-key")
        self._record_make_caller(monkeypatch, anthropic_api, seen)
        self._explode(monkeypatch, gemini_api)
        path = detect.detect_moments("vid123", tmp_path,
                                     config(detect={"provider": None}),
                                     stream_title="Race",
                                     transcriber=transcript(some_words(),
                                                            tmp_path=tmp_path))
        assert [entry[0] for entry in seen] == ["anthropic"]
        assert json.loads(path.read_text())["configured_provider"] == "anthropic"

    def test_an_explicit_null_model_falls_back_to_the_providers_default(
            self, tmp_path, monkeypatch):
        seen = []
        self._key(tmp_path, "gemini.json")
        self._record_make_caller(monkeypatch, gemini_api, seen)
        path = detect.detect_moments("vid123", tmp_path,
                                     config(detect={"provider": "gemini",
                                                    "model": None}),
                                     stream_title="Race",
                                     transcriber=transcript(some_words(),
                                                            tmp_path=tmp_path))
        assert seen[0][2] == gemini_api.DEFAULT_MODEL
        assert json.loads(path.read_text())["engine"] == f"model:{gemini_api.DEFAULT_MODEL}"

    def test_engine_never_contains_the_string_none(self, tmp_path, monkeypatch):
        # The binding invariant I2 violated: `model=None` reaching
        # f"model:{model}" wrote "engine": "model:None" to moments.json -
        # syntactically the required format, semantically nonsense, and the
        # frontend renders it literally.
        self._key(tmp_path, "anthropic.json")
        seen = []
        self._record_make_caller(monkeypatch, anthropic_api, seen)
        for i, detect_settings in enumerate(
                [{}, {"provider": None}, {"model": None},
                 {"provider": None, "model": None}]):
            path = detect.detect_moments(f"vid{i}", tmp_path,
                                         config(detect=detect_settings),
                                         stream_title="Race",
                                         transcriber=transcript(some_words(),
                                                                tmp_path=tmp_path))
            assert "None" not in json.loads(path.read_text())["engine"]


class TestGlossaryIsPassedToTheTranscriber:
    """`detect_moments` hands `config.get("glossary", GLOSSARY_EMPTY)` straight
    to the transcriber. This is the exact class of regression CLAUDE.md warns
    about: `_decode_worker.main` once accepted a glossary path that
    `subprocess_decoder` never passed, so every stream chunk in this project
    decoded with an EMPTY glossary while ERF's glossary.json sat on disk doing
    nothing. Pin the hand-off directly rather than trusting it stays correct
    by accident."""

    def capturing_transcriber(self, captured, tmp_path):
        def fake(video_id_, workspace_dir, *, glossary=None):
            captured["glossary"] = glossary
            return StreamTranscript(video_id=video_id_,
                                    audio_path=(tmp_path or workspace_dir) / "audio.webm",
                                    duration_seconds=600.0, words=some_words())
        return fake

    def test_a_configured_glossary_reaches_the_transcriber(self, tmp_path):
        captured = {}
        configured = Glossary(terms=["Nordschleife"],
                              replacements={"karusel": "Karussell"})
        def caller(system, user, schema):
            return {"moments": []}
        detect.detect_moments("vid123", tmp_path,
                              config(glossary=configured),
                              stream_title="Race", caller=caller,
                              transcriber=self.capturing_transcriber(captured, tmp_path))
        assert captured["glossary"] is configured

    def test_a_config_with_no_glossary_key_passes_the_documented_empty_default(self, tmp_path):
        # No KeyError, and - the point - the transcriber must receive the
        # actual documented default (glossary.EMPTY), not merely "something".
        captured = {}
        bare_config = {"lexicon": Lexicon(markers={"crash": 3.0})}
        def caller(system, user, schema):
            return {"moments": []}
        detect.detect_moments("vid123", tmp_path, bare_config,
                              stream_title="Race", caller=caller,
                              transcriber=self.capturing_transcriber(captured, tmp_path))
        assert captured["glossary"] is GLOSSARY_EMPTY


class TestAnalysisPathValidatesVideoId:
    """MINOR-2: video_id arrives unvalidated from the URL path of
    POST …/streams/{video_id}/detect. analysis_path must not borrow its
    safety from stream_transcribe._stream_dir running first - it validates
    on its own, the same rule CLAUDE.md states for every write op."""

    def test_a_traversal_video_id_is_refused(self, tmp_path):
        with pytest.raises(ValueError):
            detect.analysis_path(tmp_path, "../../auth")

    def test_a_traversal_video_id_never_touches_the_filesystem(self, tmp_path):
        # Reproduced by the reviewer: analysis_path("/tmp/ws", "../../auth")
        # used to return .../streams/../../auth/moments.json, and
        # detect_moments would mkdir(parents=True) and write there. Raising
        # before returning a path is what keeps a caller from ever getting
        # that path to write to in the first place.
        with pytest.raises(ValueError):
            detect.analysis_path(tmp_path, "../../auth")
        assert not (tmp_path.parent.parent / "auth").exists()

    def test_an_ordinary_video_id_still_works(self, tmp_path):
        path = detect.analysis_path(tmp_path, "vid123")
        assert path == tmp_path / "streams" / "vid123" / "moments.json"


def cancellable_transcript(words, *, tmp_path=None, seen=None):
    """A transcriber that accepts (and records) the cancel token, which the
    production `transcribe_stream` now does."""
    def fake(video_id_, workspace_dir, *, glossary=None, cancel=None):
        if seen is not None:
            seen.append(cancel)
        return StreamTranscript(video_id=video_id_,
                                audio_path=(tmp_path or workspace_dir) / "audio.webm",
                                duration_seconds=7200.0, words=words)
    return fake


def window_caller(calls, *, score=8.0):
    """Answers one moment per window, addressed by that window's OWN first
    line number (read back out of the rendered prompt), and records which
    window it was asked about - so a test can tell a cache hit from a miss
    without counting anonymous calls."""
    def caller(system, user, schema):
        first = int(user.splitlines()[0].split("\t")[0])
        calls.append(first)
        return {"moments": [{"start_line": first + 2, "end_line": first + 3,
                             "category": "incident", "score": score,
                             "reason": "Contact at the chicane.",
                             "hook_suggestion": "CONTACT"}]}
    return caller


class TestStoppingDetection:
    """Stopping a detection costs nothing because its windows are cached on
    disk at streams/<video_id>/windows/, exactly symmetric with the chunk
    cache at streams/<video_id>/chunks/ one stage earlier."""

    def test_the_cancel_token_reaches_the_transcriber(self, tmp_path):
        from yt_shorts.cancel import CancelToken
        token = CancelToken()
        seen = []
        detect.detect_moments("vid123", tmp_path, config(), stream_title="Race",
                              caller=window_caller([]),
                              transcriber=cancellable_transcript(
                                  some_words(7200), tmp_path=tmp_path, seen=seen),
                              cancel=token)
        assert seen == [token]

    def test_a_stopped_scan_writes_no_analysis(self, tmp_path):
        # A moments.json covering three of nine windows would read as a
        # complete detection: `engine` names a model, `missing_windows` is
        # empty, and hours of the stream silently hold no moments. Raise
        # instead - the scored windows are on disk, so the re-run is free.
        from yt_shorts.cancel import CancelToken, Stopped
        token = CancelToken()
        token.request_stop()
        with pytest.raises(Stopped):
            detect.detect_moments("vid123", tmp_path, config(), stream_title="Race",
                                  caller=window_caller([]),
                                  transcriber=cancellable_transcript(
                                      some_words(7200), tmp_path=tmp_path),
                                  cancel=token)
        assert not (tmp_path / "streams" / "vid123" / "moments.json").exists()

    def test_the_window_cache_lives_beside_the_chunk_cache(self, tmp_path):
        calls = []
        detect.detect_moments("vid123", tmp_path, config(), stream_title="Race",
                              caller=window_caller(calls),
                              transcriber=transcript(some_words(7200), tmp_path=tmp_path))
        windows_dir = tmp_path / "streams" / "vid123" / "windows"
        assert sorted(p.name for p in windows_dir.glob("*.json")) == [
            "000.json", "001.json", "002.json"]
        assert len(calls) == 3

    def test_a_scored_window_is_cached_on_disk_and_not_paid_for_twice(self, tmp_path):
        first = []
        path = detect.detect_moments(
            "vid123", tmp_path, config(), stream_title="Race",
            caller=window_caller(first),
            transcriber=transcript(some_words(7200), tmp_path=tmp_path))
        before = json.loads(path.read_text())["moments"]
        assert len(first) == 3

        def refusing_caller(system, user, schema):
            raise AssertionError("a cached window must never be paid for again")

        path = detect.detect_moments(
            "vid123", tmp_path, config(), stream_title="Race",
            caller=refusing_caller,
            transcriber=transcript(some_words(7200), tmp_path=tmp_path))
        after = json.loads(path.read_text())
        assert after["moments"] == before
        assert after["missing_windows"] == []

    def test_a_changed_transcript_re_scores_every_window(self, tmp_path):
        # The cached scores were produced from different text; serving them
        # against a re-decoded transcript would be quietly wrong.
        detect.detect_moments("vid123", tmp_path, config(), stream_title="Race",
                              caller=window_caller([]),
                              transcriber=transcript(some_words(7200), tmp_path=tmp_path))
        second = []
        detect.detect_moments("vid123", tmp_path, config(), stream_title="Race",
                              caller=window_caller(second),
                              transcriber=transcript(some_words(7201), tmp_path=tmp_path))
        assert len(second) == 3

    def test_a_changed_model_re_scores_every_window(self, tmp_path):
        detect.detect_moments("vid123", tmp_path, config(), stream_title="Race",
                              caller=window_caller([]),
                              transcriber=transcript(some_words(7200), tmp_path=tmp_path))
        second = []
        detect.detect_moments("vid123", tmp_path,
                              config(detect={"model": "some-other-model"}),
                              stream_title="Race", caller=window_caller(second),
                              transcriber=transcript(some_words(7200), tmp_path=tmp_path))
        assert len(second) == 3

    def test_an_unreadable_cache_file_is_re_scored_rather_than_raised(self, tmp_path):
        detect.detect_moments("vid123", tmp_path, config(), stream_title="Race",
                              caller=window_caller([]),
                              transcriber=transcript(some_words(7200), tmp_path=tmp_path))
        (tmp_path / "streams" / "vid123" / "windows" / "001.json").write_text("{ not json")
        second = []
        detect.detect_moments("vid123", tmp_path, config(), stream_title="Race",
                              caller=window_caller(second),
                              transcriber=transcript(some_words(7200), tmp_path=tmp_path))
        assert second == [290], "only the corrupt window should have been re-scored"

    def test_the_lexicon_engine_writes_no_window_cache(self, tmp_path):
        # Only a paid window is worth caching; the lexicon engine costs
        # nothing and never goes through moment_scan at all.
        detect.detect_moments("vid123", tmp_path, config(), stream_title="Race",
                              transcriber=transcript(some_words(7200), tmp_path=tmp_path))
        assert not (tmp_path / "streams" / "vid123" / "windows").exists()


class TestRequireCachedTranscript:
    """`require_cached_transcript` is the STUDIO's policy (see this module's
    docstring on why detect has two callers): a transcriber that reads
    streams/<video_id>/transcript.json and refuses rather than spending two
    hours of Whisper decode a background job nobody is watching."""

    def test_detect_refuses_when_no_transcript_is_cached(self, tmp_path):
        # No streams/vid123/transcript.json anywhere under tmp_path.
        with pytest.raises(detect.TranscriptNotCached) as excinfo:
            detect.detect_moments(
                "vid123", tmp_path, config(), stream_title="Race",
                caller=window_caller([]),
                transcriber=detect.require_cached_transcript)
        # The reason must NAME the missing transcript and the stream it
        # belongs to - "detection failed" is exactly the useless message
        # this project keeps working to eliminate.
        message = str(excinfo.value)
        assert "vid123" in message
        assert "transcript" in message
        assert not (tmp_path / "streams" / "vid123" / "moments.json").exists()

    def test_detect_uses_the_cached_transcript_without_decoding(self, tmp_path, monkeypatch):
        from yt_shorts import stream_transcribe

        stream_dir = tmp_path / "streams" / "vid123"
        stream_dir.mkdir(parents=True)
        words = some_words(7200)
        (stream_dir / "transcript.json").write_text(json.dumps({
            "video_id": "vid123", "duration_seconds": 7200.0,
            "words": words, "missing_chunks": [],
        }), encoding="utf-8")

        def refusing(*args, **kwargs):
            raise AssertionError("require_cached_transcript must never decode")

        # Neither the downloader nor the decoder may be reached at all.
        monkeypatch.setattr(stream_transcribe, "ytdlp_downloader", refusing)
        monkeypatch.setattr(stream_transcribe, "subprocess_decoder", refusing)

        calls = []
        path = detect.detect_moments(
            "vid123", tmp_path, config(), stream_title="Race",
            caller=window_caller(calls),
            transcriber=detect.require_cached_transcript)
        data = json.loads(path.read_text())
        assert data["engine"].startswith("model:")
        assert len(calls) == 3, "the cached words should still be scanned normally"


class TestWhatAStreamAlreadyHas:
    """The two questions the studio's stream list asks per row, and the
    worker asks about a queued detect. One `Path.exists` each, so the list
    route can ask them for 99 videos on every response."""

    def test_nothing_exists_for_an_untouched_stream(self, tmp_path):
        assert detect.has_cached_transcript(tmp_path, "vid123") is False
        assert detect.has_analysis(tmp_path, "vid123") is False

    def test_a_written_transcript_is_seen(self, tmp_path):
        directory = tmp_path / "streams" / "vid123"
        directory.mkdir(parents=True)
        (directory / "transcript.json").write_text("{}", encoding="utf-8")
        assert detect.has_cached_transcript(tmp_path, "vid123") is True
        assert detect.has_analysis(tmp_path, "vid123") is False

    def test_a_written_analysis_is_seen(self, tmp_path):
        directory = tmp_path / "streams" / "vid123"
        directory.mkdir(parents=True)
        (directory / detect.ANALYSIS_FILENAME).write_text("{}", encoding="utf-8")
        assert detect.has_analysis(tmp_path, "vid123") is True

    def test_a_directory_in_place_of_the_file_is_not_a_transcript(self, tmp_path):
        """`is_file`, not `exists`: a directory named transcript.json is not
        a transcript, and reporting it as one would offer to detect on it."""
        (tmp_path / "streams" / "vid123" / "transcript.json").mkdir(parents=True)
        assert detect.has_cached_transcript(tmp_path, "vid123") is False

    def test_an_unsafe_video_id_answers_false_rather_than_raising(self, tmp_path):
        """These two are asked once per video over a whole catalogue, and
        the ids come from yt-dlp. An id that is not a safe segment names no
        file this workspace could hold - answering False is right, and one
        odd id must not 500 a list of 99 videos. `stream_dir` itself still
        raises, which is what `require_cached_transcript` needs."""
        assert detect.has_cached_transcript(tmp_path, "../etc") is False
        assert detect.has_analysis(tmp_path, "../etc") is False
        with pytest.raises(ValueError):
            detect.stream_dir(tmp_path, "../etc")

    def test_a_video_id_that_is_not_even_a_string_answers_false_too(self, tmp_path):
        """The same rule, for the one shape that used to walk past it. `_has`
        catches ValueError; a truthy non-string (an int from a hand-edited
        jobs.json, a malformed yt-dlp line) used to reach len()/match() inside
        `validate_segment` and leave as a TypeError instead - so the one case
        this tolerance exists for was the one it did not cover, and a single
        odd id sank the catalogue after all. See `pathnames.validate_segment`."""
        assert detect.has_cached_transcript(tmp_path, 12345) is False
        assert detect.has_analysis(tmp_path, ["vid123"]) is False
        with pytest.raises(ValueError):
            detect.stream_dir(tmp_path, 12345)

    def test_stream_dir_is_where_the_stream_lives(self, tmp_path):
        assert detect.stream_dir(tmp_path, "vid123") == \
            tmp_path / "streams" / "vid123"


class TestTheStreamPathHelpersAgree:
    """One path build, reached three ways.

    `analysis_path` and `windows_dir` predate `stream_dir` and each carried
    its own `validate_segment` + join. Three copies of one rule is three
    chances for one of them to drift; this pins that they cannot.
    """

    def test_every_helper_builds_under_the_same_stream_directory(self, tmp_path):
        base = detect.stream_dir(tmp_path, "vid123")
        assert detect.analysis_path(tmp_path, "vid123") == \
            base / detect.ANALYSIS_FILENAME
        assert detect.windows_dir(tmp_path, "vid123") == \
            base / detect.WINDOWS_DIRNAME

    @pytest.mark.parametrize("call", [
        lambda ws: detect.stream_dir(ws, "../../auth"),
        lambda ws: detect.analysis_path(ws, "../../auth"),
        lambda ws: detect.windows_dir(ws, "../../auth"),
    ])
    def test_each_one_still_validates_its_own_segment(self, tmp_path, call):
        """Routing through `stream_dir` must not become BORROWING its guard.

        CLAUDE.md's rule is that a write op validates its own path segment
        rather than relying on a caller having done it. Building on a helper
        that validates keeps that true - the check still runs inside the
        call, before any filesystem touch - and this is what pins it, so the
        refactor cannot be read as having dropped the rule.
        """
        with pytest.raises(ValueError):
            call(tmp_path)
