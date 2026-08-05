"""What a detection run would cost, worked out locally.

Pure and dependency-free by design - no `anthropic` import at module scope or
anywhere else, and no network call. The stream screen is specified to be useful
with NO API key, so a preview that needed one (the API's own token-counting
endpoint, for instance) would fail exactly where it is most wanted: before the
operator has decided whether to configure a key at all.

The count is therefore APPROXIMATE - characters divided by four, the usual
rough ratio - and the payload carries `estimated: True` so no caller can render
it as billing truth. It is meant to answer "cents or euros?", not "what will
the invoice say".

Measured against a real invoice it runs LOW, by roughly a factor of two - see
OUTPUT_TOKENS_PER_WINDOW below for what was compared and why the gap is
structural rather than a mis-set constant. Read a number from here as a floor.

The price table BELONGS TO THE PROVIDER and arrives as the `prices` argument
(`<provider>.PRICES`) - this module used to carry one Anthropic table of its
own, which was a second copy to drift and made a shared module quietly
vendor-specific. Importing `providers` here to look it up would cost exactly
the purity described above, so the caller passes it instead. A `model` the
given table has no entry for still gets a full response - `usd: 0.0` and
`priced: False` - rather than a crash or a silently free-looking number: a
caller must check `priced` before showing `usd` as "this is what it costs",
since 0.0 is what an unpriced model and a genuinely free run both look like
otherwise.
"""

from __future__ import annotations

from .lexicon import Lexicon
from .moment_scan import build_system_prompt, group_lines, render_window, split_windows

CHARS_PER_TOKEN = 4

# A window's answer is a short JSON list of moments.
#
# THIS WHOLE ESTIMATE RUNS LOW, and the comment that used to stand here said
# the opposite. It claimed ~12% HIGH, "a margin on the safe (over-, not
# under-) side", calibrated against a bake-off's measured cost. That
# calibration was circular: the bake-off script measured itself the same way
# this module does - characters // 4 for the input, the length of the PARSED
# answer for the output - so agreeing with it proved only that two character
# counts agree, never that either matches an invoice.
#
# Checked against the account's actual daily cost for 2026-07-29 (two
# bake-offs of three models each, plus one opus detect run over the same
# 98-minute qualifying), the real spend came out at roughly TWICE what this
# module predicted, and by a similar factor for all three models. Two causes,
# both structural: this material (clock times, line numbers, tabs, German
# proper nouns) tokenises denser than four characters per token, and the
# output side counts only the JSON that survives parsing, not everything the
# model actually emitted.
#
# The honest reading of a number from here is therefore "at least this much",
# not "about this much" - which is why `estimated: True` travels with it and
# why the studio must never present it as a bill.
#
# Do NOT simply double this constant to "fix" it: the gap is on both sides of
# the calculation, and one more character count calibrated against another
# character count would repeat the exact mistake this paragraph exists to
# record. `detect._report_usage` now logs the API's OWN reported tokens for
# every run; recalibrate from a handful of THOSE lines, against real prices,
# and say here which runs the new number came from.
OUTPUT_TOKENS_PER_WINDOW = 500


def estimate_run(words, lexicon: Lexicon, *, model: str,
                 prices: dict[str, tuple[float, float]]) -> dict:
    """What scanning `words` with `model` would cost, priced against `prices`.

    `prices` is REQUIRED and keyword-only, deliberately with no default: the
    table is the selected provider's (`<provider>.PRICES`), and a default here
    would let a call site that forgot it bill silently against some other
    vendor's rates.
    """
    lines = group_lines(words)
    windows = split_windows(lines)
    system = build_system_prompt(lexicon)
    characters = sum(len(system) + len(render_window(window)) for window in windows)
    input_tokens = characters // CHARS_PER_TOKEN
    output_tokens = len(windows) * OUTPUT_TOKENS_PER_WINDOW
    rates = prices.get(model)
    priced = rates is not None
    price_in, price_out = rates if rates is not None else (0.0, 0.0)
    usd = input_tokens / 1e6 * price_in + output_tokens / 1e6 * price_out
    return {
        "model": model,
        "windows": len(windows),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "usd": round(usd, 4),
        "estimated": True,
        "priced": priced,
    }
