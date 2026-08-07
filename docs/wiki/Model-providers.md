Moment detection is the only part of this tool that talks to a commercial model
API, and which vendor answers is a choice. Three providers ship —
**Anthropic**, **Google Gemini** and **OpenAI** — behind one seam, so switching
is a config key rather than a rebuild. Nothing else in the project changes:
harvesting, rendering, transcription, subtitles and upload never touch a model
API at all.

**Choosing one.** In the studio, open a channel's **Brand editor** and use its
*Moment detection* section. That writes a `detect` block into the channel's
`brand.json`, which you can equally well type by hand:

```json
{
  "detect": {
    "provider": "gemini",
    "model": "gemini-3.6-flash"
  }
}
```

Both keys are optional, and so is the whole block: an absent (or explicitly
`null`) `provider` means Anthropic, and an absent (or `null`) `model` means
that provider's own default model — which is what every profile written before
this existed gets. An **unknown provider name is a reported profile defect**,
not a silent fall back to the default: a typo that quietly ran a different
vendor than you asked for is exactly the kind of silent substitution this
project refuses. The model name, by contrast, is deliberately **not** checked
against the vendor's catalogue, and what a wrong one does is worth knowing
before you type one: it does **not** fall back to the lexicon. Nothing reads
the name until the first request, so the run has already committed to
`"engine": "model:<whatever-you-typed>"`; every window then fails, each with
its cause logged and its index recorded in `missing_windows`, and you get a
finished analysis with **zero moments**. That is loud rather than silent — the
stream view's hit list flags missing windows — but it is an empty result, not a
weaker one. The lexicon engine takes over only when no caller can be built at
all: no key file, that provider's SDK not installed, or the service unreachable
when the client is constructed.

`detect` is **channel-level only**. An event's `brand.json` override may not
set it (the studio refuses it by name rather than dropping it silently),
because it decides whose bill a run spends — a property of the channel, not of
one event's look.

**Where the keys live.** One file per provider, in the same workspace
directory as the YouTube credentials, mode 600, gitignored and never logged:

```
<workspace>/auth/anthropic.json
<workspace>/auth/gemini.json
<workspace>/auth/openai.json
```

Each accepts two shapes — a bare key string, or `{"api_key": "..."}` — because
the file as you first create it usually holds the raw key despite its
extension. Each provider also needs its own optional SDK, lazily imported and
required by nothing else:

```bash
.venv/bin/pip install anthropic      # Anthropic
.venv/bin/pip install google-genai   # Google Gemini
.venv/bin/pip install openai         # OpenAI
```

**Setting one from the studio.** The **Settings** page has a *Model providers*
block with one row per provider. It shows whether that provider's SDK is
installed (with the exact command if it is not), whether a key is stored, and
lets you paste a new key or forget the stored one. A stored key is never shown
again and never returned by the server — a row carries only booleans, shipped
constants (the default model, the install command, the price table) and the
provider's public id. Keys
are workspace-wide; the *channel* rows further down show which provider each
channel is currently set to use, read-only, since that is the Brand editor's
job. Removing a key is reversible by pasting it again, and makes detection for
every channel using that provider fall back to the offline lexicon engine.

## How far each provider has actually been measured

All three have now been measured against their real service, on the **same
stream**, so what follows is a comparison rather than a gradation of
confidence. That stream is this workspace's own 98-minute qualifying (5574
transcript words, 2 scan windows); Anthropic's run was on **2026-07-29**,
Gemini's and OpenAI's on **2026-07-31**.

| provider / model | moments | agreement | measured cost |
| --- | --- | --- | --- |
| `claude-opus-5` *(the default)* | 10–11 | reference | **$0.1362** |
| `gemini-3.6-flash` | 6 | 6/6, both key moments | $0.0590 |
| `gpt-5.6-terra` | 5 | 5/5, both key moments | $0.0326 |
| `gpt-5.6-luna` | 7 | 6/7, splits the pole lap | $0.0041 |

*Agreement* counts a moment as agreeing when it overlaps one of Anthropic's by
more than half of the shorter window. Anthropic's run is the **reference, not
an answer key** — it is the production analysis this workspace already had, not
a ground truth anyone checked the others against. The costs are computed from
each API's own reported token counts at the rates in that provider's price
table; **none of them was read off an invoice.** The input token counts differ
per vendor for the same text (15369 / 13052 / 11047), because the tokenizers
differ — so the table compares **cost**, not tokens.

**Every figure here is a single sample, and the reference itself moves.** The
same `claude-opus-5` over the same stream has been observed to return **7, 10
and 11** moments on three separate runs (2026-07-29 twice, 2026-07-31 once).
What varies is the **borderline tail**, not the result: the two logged runs
agree on 9 of 10, differing by two weak moments in one (scored 5.0 and 4.5) and
one in the other (scored 7.0). The strong moments — including both the pole lap
and the Speed Hunter lap every provider is scored on — are stable across all of
them. So read the moment counts as approximate, and read the *agreement* column
as approximate too: "6/6" is agreement against **one** run of a reference that
would have offered a slightly different list on a different day, not a
precision claim. Only the Opus row has more than one run behind it; the other
three are one run each, so their spread is unknown rather than zero.

The Opus cost here (`$0.1362`, from the API's own reported counts on
2026-07-31: 2 calls, 15369 input + 2374 output tokens) replaces a `~$0.062`
that this table used to carry and that was never a measurement — it came from
the character-counting estimate `estimate.py` documents as circular. The
correction changes what the table *says*, not just a decimal: Opus is **more
than twice** Gemini Flash rather than about the same as it, and over forty
times `gpt-5.6-luna`. The Anthropic bake-off's own `~$0.0xx` figures below are
that same estimate and are labelled as such.

Read the whole table as **one stream, one qualifying session, two windows, one
run per model — except Opus, which has three.** That caveat used to end with
"re-measure before treating any of it as settled", and the re-measurement has
since happened. It changed the conclusion.

## The same three providers over an eight-hour race

The qualifying is not this project's workload. On **2026-07-31** all three ran
over `Esm9vv5-PdU`, *"ERF 24h Nürburgring 2026 | The Race | Part 1"* — **8 h 19
min**, 41 925 transcript words, 50 decoded chunks with none missing, **9 scan
windows** against the qualifying's two.

| provider / model | moments | ≥ 7.0 | ≥ 8.0 | failed windows | measured cost | runtime |
| --- | --- | --- | --- | --- | --- | --- |
| `claude-opus-5` | 39 | 13 | 2 | 0 | $0.7603 | 130 s |
| `gemini-3.6-flash` | 30 | 21 | 5 | **1** | $0.3347 | 146 s |
| `gpt-5.6-terra` | 33 | 24 | 8 | 0 | **$0.2140** | 78 s |

**Agreement collapsed.** Over the qualifying, Gemini and OpenAI proposed only
moments Anthropic had also found (6/6 and 5/5). Over the race, pairwise
agreement is **38 % to 67 %**:

| | vs `opus` | vs `gemini` | vs `terra` |
| --- | --- | --- | --- |
| `claude-opus-5` (39) | — | 15 (38 %) | 22 (56 %) |
| `gemini-3.6-flash` (30) | 15 (50 %) | — | 16 (53 %) |
| `gpt-5.6-terra` (33) | 22 (67 %) | 16 (48 %) | — |

**No model's list is a subset of another's.** Each proposes strong moments the
others miss entirely — two of Gemini's five ≥ 8.0 moments appear in neither
other list, and one of OpenAI's eight. Their stated reasons read plausibly in
every case (a three-wide fight for the lead, a spin on cold tyres, a crash on
the restart), so this is divergence, not one model hallucinating.

**Do not read the ≥ 7.0 and ≥ 8.0 columns across rows.** Each model scores on
its own scale, and this run shows how far apart those scales are: Anthropic
returned the *most* moments (39) and the *fewest* strong ones (2 at ≥ 8.0),
OpenAI the reverse. An 8.0 from one is not a claim about the same thing as an
8.0 from another. This is the first measured support for the "one engine per
run" rule, whose reasoning is in the
[detection-and-providers skill](https://github.com/jegr78/yt-shorts/blob/main/.claude/skills/detection-and-providers/SKILL.md).

**Gemini lost a window** — one of nine, an hour of the race that nothing
looked at. Recorded in `missing_windows` and flagged in the stream view's hit
list, which is exactly what those exist for; noted here because it is the first
time it has happened on a real run rather than in a test.

**What this changes.** The qualifying supported an argument that Anthropic's
extra moments were noise — procedural announcements and unreproducible
low-scored items. **That does not generalise.** Over eight hours the three
models genuinely disagree, and picking one means accepting the moments the
others would have found. `gpt-5.6-terra` remains the recommended choice on this
material — cheapest by a factor of 3.6, fastest, no lost windows, and the
highest confirmation rate of any pair (67 % of its moments also appear in
Anthropic's) — but on those grounds, not on "the others only add noise".

Everything above is still **one race, one run per model.** No provider's spread
over an eight-hour stream has been measured even once.

**Anthropic — the default, and measured.** A bake-off over this workspace's own
98-minute qualifying stream on **2026-07-29**, all three models, the same
corrected prompt, scored against the operator's four known-good moments:

| model | moments found | of the 4 known-good | cost *(estimate, not measured)* |
| --- | --- | --- | --- |
| `claude-haiku-4-5` | 2 | 0 | ~$0.012 |
| `claude-sonnet-5` | 7 | 1 | ~$0.037 |
| `claude-opus-5` | 7 | 3 | ~$0.062 |

The moment counts and the known-good hits in that table are observations; the
**cost column is not**. All three came from the character-counting estimate,
and it runs low — the one row since measured against the API's own token counts
is `claude-opus-5`, at **$0.1362**, more than double what its `~$0.062` says.
Nobody has re-measured Haiku or Sonnet, so those two rows still hold only an
estimate.

`claude-opus-5` is the default because it alone found the purple flying lap and
"what a lap", and gave the most specific reasons. The price gap is a few cents
per stream, which does not buy back a detector that misses the moments the
channel exists to publish. This measurement overturned the plan's own starting
assumption that the cheapest model would do; treat "the detector only ranks a
transcript it has already been handed, so it can be cheap" as an argument that
has already been refuted once.

The `7` in the Opus row is one run of a count that moves — see the variance
note above the comparison table, which also records how the 7-versus-11
discrepancy between this bake-off and the workspace's stored analysis was
settled.

**Gemini — measured.** One paid detection run on **2026-07-31**, one model.
Gemini found a **strict subset**: all 6 of its moments overlap an Anthropic
moment by more than half of the shorter window, 3 of the 6 exactly, and the top
three scores are identical on identical windows. It disagreed with Opus about
nothing — it found less, including both of the moments the Anthropic bake-off
credits to Opus alone. It is **not as cheap as a Flash model sounds**, because
it spends most of its answer thinking (5262 output tokens where the local
preview predicts 1000) — $0.0590 for this stream. Against Opus's *measured*
$0.1362 that is still well under half, so it is the cheaper option; it is not
the near-tie this paragraph claimed while the Opus figure was the ~$0.062
estimate. What separates the two here is recall as well as price, which is why
Anthropic remains the default and this is what a channel gets when it asks for
Gemini.

`gemini-3.5-flash` was not measured at all; it costs more per output token
($9.00 against $7.50), which on a model that thinks this much is the expensive
direction, and whether it buys the missing recall back is unknown. Two ids that
used to be priced here — `gemini-2.5-flash` and `gemini-2.5-pro` — were
**removed** from `gemini_api.PRICES` on 2026-07-31: both answer `NotFoundError`
on the Interactions API this project uses, existing only on the older
`generate_content` surface. Naming one in the `model` field is not a cheap run
and is not a lexicon one either: every window fails into `missing_windows` and
the analysis comes out empty (see "Choosing one" above).

On Gemini's **free tier**: it exists on the Flash models, but Google's terms
restrict free-tier use for the **EEA, Switzerland and the UK**, where billing
must be enabled on the project even for models that are otherwise free-tier
eligible (verified 2026-07-31 at `ai.google.dev/gemini-api/terms`). If you are
in one of those regions, expect to enable billing before the first call
succeeds — a quota or permission error on your first run is that, not a bug in
this tool. Concretely, as observed on 2026-07-31 before credits were added to
the project: every call comes back `429 "Your prepayment credits are
depleted"`, and this project handles that the way it handles any other failing
window — the window is recorded in `missing_windows`, the engine is named, the
cause is logged loudly — so the symptom is a **finished run with an empty
analysis**, not a crash or an error page. There is one genuine upside to the
same rule: in those regions the
**paid-tier data terms** apply across the services, so content you submit is
not used to improve Google's models.

**OpenAI — measured, and the one result that changed a decision.** Two paid
runs on **2026-07-31**, `gpt-5.6-terra` and `gpt-5.6-luna`, 0 failed windows
each. `gpt-5.6-terra` stays the default, and the reason is subtler than
"cheaper is worse": `gpt-5.6-luna` finds *more* moments and costs an eighth as
much, but it **cuts the pole lap in half** — proposing two windows that overlap
the reference moment by 49% and 33%, where `gpt-5.6-terra` and
`gemini-3.6-flash` each return one window at 86%. For a Shorts pipeline the
deliverable *is* the window: a split moment is two clips that each begin or end
in the wrong place, not one good one. Until this run, `gpt-5.6-terra` was
picked by **analogy** with the Anthropic bake-off — a reasonable argument from
another vendor's measurement, and now replaced by this vendor's own.

All five ids in `openai_api.PRICES` answered a 16-token ping on the same date,
so unlike Gemini's table this one has no entries the API declines to serve. The
other three (`gpt-5.6-sol`, `gpt-5.4`, `gpt-5.4-mini`) were **not run** and
have no numbers at all.

**An open question nobody has answered.** On identical input, Anthropic found
11 moments (10 on the re-run — see the variance note above), Gemini 6 and
OpenAI 5 — with perfect agreement wherever they overlap: every non-Anthropic
moment lands on an Anthropic one. Nobody has checked whether the other two
engines **scored** the moments only Opus found below threshold, or never
**proposed** them at all. It is not a cap being hit
(`moment_scan.MAX_PER_WINDOW` is 12 across 2 windows, nowhere near reached), so
"lower recall" currently describes the counts rather than explaining them. Part
of the gap is a tail that is not stable even within one model, which narrows
the question without answering it: the moments Opus alone found include the
weak ones its own runs disagree about. It is the most interesting thing the
measurement did not settle.

Prices shown anywhere in the studio are a **dated per-million-token rate and a
floor**, not a bill: each provider's table is a flat two-number snapshot that
cannot express batch, cached, long-context or service-tier pricing, and this
project's endurance-stream windows are long enough to reach the tiers that cost
more. The studio's cost preview on the stream screen is likewise local and
approximate (characters divided by four, no network, no key) — it answers
"cents or euros?", not "what will the invoice say".

## Adding a fourth provider

A provider is one module in `src/yt_shorts/providers/`, and registering it is
**two edits in `providers/__init__.py`, not one**: the module-scope `from
. import …` at the top, and the `_MODULES` tuple below it. `PROVIDERS` is a
comprehension over `_MODULES`, so inserting into that dict directly leaves you a
provider `ordered()` never returns — and `ordered()` is what both the Settings
payload and every parameterised contract test are built from, so the provider
would be registered and untested at the same time. Measured, not assumed: a
fourth provider added to `PROVIDERS` alone fails
`test_the_registry_is_the_three_modules_default_first` and is silently absent
from all thirty of the per-provider contract cases.

It must expose exactly the **eight names** in `providers.CONTRACT` and nothing
else:

| name | what it is |
| --- | --- |
| `PROVIDER_ID` | the id used in `brand.json`, in URLs and as the registry key |
| `KEY_FILENAME` | its key file under `<workspace>/auth/` — must be `<PROVIDER_ID>.json` |
| `DEFAULT_MODEL` | the model used when a channel names none |
| `PRICES` | `model -> (USD per 1M input tokens, USD per 1M output tokens)`, dated |
| `PACKAGE` | the importable SDK package name, for the "is it installed?" check |
| `INSTALL` | the exact command that installs it |
| `VERIFIED` | whether it has been measured against the real service |
| `make_caller` | `make_caller(api_key, *, model, max_tokens, sdk, usage) -> call(system, user, schema) -> dict` |

Two shapes the contract enforces that the table above cannot show, both easy to
get wrong once and then puzzling:

- **every rate in `PRICES` must be a `float`.** `(1, 5)` is rejected and
  `(1.00, 5.00)` accepted — an int is the shape a typo takes, and the numbers
  are dollars.
- **all four of `make_caller`'s keyword arguments need defaults**
  (`model=DEFAULT_MODEL`, `max_tokens=4096`, `sdk=None`, `usage=None`).
  Production calls it as `make_caller(key, model=…, usage=…)` and the suite as
  `make_caller(KEY, sdk=…)`; one required keyword anywhere fails all nine
  behavioural cases with a `TypeError` that names the argument rather than the
  rule.

`tests/test_provider_contract.py` is the bar, and it is worth reading before
writing the module rather than after. It holds **every registered provider** to
the same nine behavioural properties, parameterised over the registry, so a
fourth provider inherits all of them the moment it enters `_MODULES`: the
three key-secrecy wraps (building the client, sending the request, **reading
the response**), a non-JSON answer becoming a `ModelError`, accepting
`moment_scan`'s own schema and returning its answer, recording usage *before*
the response is read, accumulating the API's own token counts, surviving usage
that cannot be read, and working with no `usage` argument at all. Plus the key
file's own rules (0600, atomic write, every unusable key reported as
`MissingKey` and never quoted back) and a check that importing the package
pulls in no vendor SDK. Add your provider's fake SDK to that file's `FAKES`
table and nothing else *there* changes.

**But the test work is not only that file.** Two assertions elsewhere pin the
registry as it stands today, and a fourth provider trips them:

- `TestProviderKeys.test_settings_lists_every_provider_with_its_state` in
  `tests/test_studio_api.py` asserts the settings payload's provider ids are
  exactly `{"anthropic", "gemini", "openai"}` — add yours;
- `TestProviderKeys.test_the_verified_flag_is_each_modules_own`, in the same
  class, asserts every row's `verified` is `True`, which a new provider trips
  for as long as it is honestly `VERIFIED = False` — exempt yours until you
  have measured it.

Both are deliberate pins, not oversights: the registry is small enough to state
exactly, and stating it exactly is what catches a provider that quietly stops
being served. Expect to widen them, rather than to be surprised by them. With
those two edits and the `FAKES` entry, nothing else in the suite needs touching
— verified by writing a throwaway fourth provider, following this recipe
literally, and getting a clean full run (and `python3 tools/lint.py` green) with
it registered.

Two rules that are easy to get wrong and that the suite enforces — the lazy SDK
import and the key-secrecy wrap around every exception a vendor SDK raises —
are stated, with what they cost when they were got wrong, in the
[detection-and-providers skill](https://github.com/jegr78/yt-shorts/blob/main/.claude/skills/detection-and-providers/SKILL.md).
