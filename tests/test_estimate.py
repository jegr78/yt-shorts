from __future__ import annotations

import pytest

from yt_shorts import estimate
from yt_shorts.lexicon import Lexicon

# The price table is the CALLER's now - `estimate.py` no longer carries one, so
# it can stay pure (see its own docstring: it must import nothing from
# `providers`). These are anthropic's published numbers at the time of writing,
# copied deliberately rather than imported: this module's arithmetic is what is
# under test, and it must not start passing or failing because a vendor changed
# a price.
PRICES = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-opus-5": (5.00, 25.00),
}


def words(seconds=7200):
    return [{"start": t, "end": t + 0.8, "text": " word"} for t in range(seconds)]


class TestEstimateRun:
    def test_counts_one_window_per_hour_with_the_overlap(self, tmp_path):
        result = estimate.estimate_run(words(7200), Lexicon(markers={}),
                                       model="claude-opus-5", prices=PRICES)
        assert result["windows"] >= 2

    def test_the_cost_scales_with_the_model(self):
        cheap = estimate.estimate_run(words(3600), Lexicon(markers={}),
                                      model="claude-haiku-4-5", prices=PRICES)
        dear = estimate.estimate_run(words(3600), Lexicon(markers={}),
                                     model="claude-opus-5", prices=PRICES)
        assert dear["usd"] > cheap["usd"]

    def test_an_unknown_model_does_not_crash_and_says_it_priced_nothing(self):
        result = estimate.estimate_run(words(600), Lexicon(markers={}),
                                       model="made-up", prices=PRICES)
        assert result["usd"] == 0.0 and result["model"] == "made-up"
        # `usd: 0.0` alone is ambiguous - an unpriced model and a genuinely
        # free run look identical without this flag, so a caller must be
        # able to tell "we don't know" from "this is free".
        assert result["priced"] is False

    def test_a_known_model_says_it_priced_something(self):
        result = estimate.estimate_run(words(600), Lexicon(markers={}),
                                       model="claude-opus-5", prices=PRICES)
        assert result["priced"] is True

    def test_an_empty_transcript_is_a_zero_estimate(self):
        result = estimate.estimate_run([], Lexicon(markers={}),
                                       model="claude-opus-5", prices=PRICES)
        assert result["windows"] == 0 and result["usd"] == 0.0

    def test_the_payload_says_it_is_an_estimate(self):
        # No caller may present this as what the run will actually be billed.
        assert estimate.estimate_run(words(600), Lexicon(markers={}),
                                     model="claude-opus-5",
                                     prices=PRICES)["estimated"] is True

    def test_it_makes_no_network_call(self, monkeypatch):
        # The screen must work with no key and no connectivity; a token count
        # fetched from the API would defeat the preview's whole purpose.
        import socket
        monkeypatch.setattr(socket, "socket",
                            lambda *a, **k: pytest.fail("estimate must not use the network"))
        estimate.estimate_run(words(600), Lexicon(markers={}),
                              model="claude-opus-5", prices=PRICES)


class TestPricesAreTheCallersTable:
    """The table is a PARAMETER, not a module constant.

    It used to live here, and `detect._report_usage` imported it - which meant
    one vendor's prices were hard-wired into a module three providers share.
    Task 1 copied the same table into `anthropic_api`; this task deletes the
    copy that lived here and takes the table from whichever provider the
    profile selected.
    """

    def test_the_module_carries_no_price_table_of_its_own(self):
        # A resurrected module-level table would silently outrank the
        # parameter for anyone who forgot to pass one.
        assert not hasattr(estimate, "PRICES")

    def test_the_module_imports_nothing_from_providers(self):
        # `estimate.py` must stay pure: the stream screen's cost preview is
        # specified to work with no key, no SDK and no network, and the
        # providers package is where every vendor boundary lives. Read off the
        # AST rather than a substring search, so a mention in a comment or a
        # docstring does not fail the test and a lazy in-function import does.
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(estimate))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(alias.name for alias in node.names)
        assert not any("providers" in name for name in imported), imported

    def test_the_passed_table_is_the_one_used(self):
        # The mutation this pins: reading a hard-coded table instead of the
        # argument. Deliberately NOT a real vendor's price.
        dear = estimate.estimate_run(words(600), Lexicon(markers={}),
                                     model="m", prices={"m": (1000.0, 1000.0)})
        cheap = estimate.estimate_run(words(600), Lexicon(markers={}),
                                      model="m", prices={"m": (1.0, 1.0)})
        assert dear["usd"] > cheap["usd"] > 0.0
        assert dear["priced"] is True

    def test_an_empty_table_prices_nothing_rather_than_crashing(self):
        result = estimate.estimate_run(words(600), Lexicon(markers={}),
                                       model="claude-opus-5", prices={})
        assert result["priced"] is False and result["usd"] == 0.0

    def test_prices_are_keyword_only_and_required(self):
        # No default table anywhere: a call site that forgets the prices must
        # fail loudly rather than quietly bill against another vendor's rates.
        with pytest.raises(TypeError):
            estimate.estimate_run(words(60), Lexicon(markers={}), model="claude-opus-5")
