# Model providers: Anthropic, Gemini and OpenAI behind one seam

**Date:** 2026-07-31
**Scope:** making the model that scores moments a choice rather than a
dependency — three provider implementations behind the one `caller` seam
`moment_scan.scan` already takes, selectable per channel in the studio, with
their API keys managed there too. Transcription (faster-whisper), rendering,
uploading and every other part of the pipeline are untouched.

## Problem

Moment detection is the only feature in this project that talks to a
commercial model API, and it talks to exactly one: Anthropic. That is a
sound engineering choice — the bake-off that picked `claude-opus-5` was
measured on this workspace's own qualifying, not read from a datasheet —
but it is a single point of dependence in a repository that is about to be
published.

The seam itself is already provider-neutral. `moment_scan.scan` takes an
**unconstrained** `caller(system, user, schema) -> dict` and its own
docstring says so; the model never sees a timestamp, only line numbers it
must return; `validate_moment` never raises, so a provider that answers
sloppily costs one candidate rather than a run. Everything vendor-specific
sits in a small, countable area:

| Location | Lines | What is vendor-specific |
|---|---|---|
| `claude_client.py` | 252 | the SDK call, key file, default model, error wrapping |
| `_anthropic.py` | 33 | the optional-dependency guard |
| `detect._caller_from_config` | ~40 | key load + caller construction |
| `estimate.PRICES` | 5 | the price table |
| `studio/api.py` | 1 | `claude_client.DEFAULT_MODEL` |

So this is not a rescue from lock-in. It is turning a property the code
already has into one a reader of the repository can see and a contributor
can extend.

## Decisions taken

Recorded here because each closes a question that has a defensible opposite
answer:

1. **Two providers are measured against the real API, the third ships
   labelled.** Anthropic (already measured) and Gemini (to be measured).
   OpenAI ships, is tested against fakes, and is stated everywhere as not
   verified against the real service.
2. **Anthropic stays the default.** Nothing about an existing workspace
   changes unless its operator changes it.
3. **Gemini is the second provider**, chosen over OpenAI because the Gemini
   API has a free tier on its Flash models (verified 2026-07-31 at
   `ai.google.dev/gemini-api/docs/pricing`).

   **This argument is weaker than it first looked, and the correction is
   recorded rather than buried.** Google's additional terms
   (`ai.google.dev/gemini-api/terms`, read 2026-07-31) say only Paid
   Services may be used when making API clients available to users in the
   EEA, Switzerland or the UK, and practitioners report that billing must be
   enabled there even for free-tier-eligible models. So for this project's
   own operator — in Germany — and for European readers of the repository,
   "try it without a credit card" does not hold.

   Gemini stays the choice anyway: the repository is public and global, the
   free tier is real elsewhere, and Flash pricing is a fraction of Opus's, so
   a detection run is cents either way. But README must carry the regional
   caveat rather than the unqualified claim, and the claim must not be the
   headline reason a reader picks it.

   One genuine upside falls out of the same terms: in the EEA, Switzerland
   and the UK the **Paid Services** data terms apply to all services,
   including unpaid quota — so Google does not train on submitted content
   there, unlike the free tier elsewhere, where the terms say plainly that it
   does.
4. **The provider is an explicit config key, not inferred from the model
   name.** A typo in a model name must not silently select the wrong
   provider — this project has paid for silent degradation more than once.
5. **The choice is made in the studio, not only in a file**, and the studio
   also accepts and removes API keys.

## What does not change

Stated first because it is the actual proof:

- **`moment_scan.py` — not one line.** It already takes an unconstrained
  caller. If supporting three providers required editing it, the seam would
  have been a fiction.
- `validate_moment`, the line-number contract, `MAX_PER_WINDOW`, the
  one-engine-per-run rule, `missing_windows`, and the lexicon fallback.
- The lexicon engine remains what happens when no provider is usable, and
  it still announces itself.
- Every test rule: no test reaches the network, reads a real key, imports a
  vendor SDK, or spends money.

## Architecture

### The package

`src/yt_shorts/providers/`:

| File | Contents |
|---|---|
| `__init__.py` | `Usage`, `MissingKey`, `ModelError`, `SdkUnavailable`, `load_api_key`, `PROVIDERS`, `get`, `DEFAULT_PROVIDER` |
| `anthropic_api.py` | today's `claude_client.py`, moved with `git mv` |
| `gemini_api.py` | new |
| `openai_api.py` | new |

The `_api` suffix is not decoration: `openai_api.py` must not be able to
shadow the real `openai` package. Absolute imports make that safe in
principle; the suffix makes it safe without needing the argument.

`_anthropic.py` is deleted. Its job — an actionable message when an optional
SDK is missing — becomes one parameterised helper in `__init__.py`, because
three near-identical copies of a 33-line module is how they drift.
`_google.py` (the YouTube upload libraries) is unrelated and stays.
`detect._caller_from_config` catches `AnthropicUnavailable` by name today and
must be moved onto `SdkUnavailable` with it — the degrade-to-lexicon path
depends on that except clause matching.

`__init__.py` imports all three provider modules at module scope. That costs
nothing, because each imports its own SDK lazily, and it means a syntax
error in `openai_api.py` fails in the suite rather than for whoever first
pastes an OpenAI key.

### The contract

Each provider module exposes exactly five names:

| Name | Type | Meaning |
|---|---|---|
| `PROVIDER_ID` | `str` | `"anthropic"`, `"gemini"`, `"openai"` |
| `KEY_FILENAME` | `str` | `"anthropic.json"`, `"gemini.json"`, `"openai.json"` |
| `DEFAULT_MODEL` | `str` | used when no model is configured |
| `PRICES` | `dict[str, tuple[float, float]]` | model → (input, output) USD per 1M tokens |
| `make_caller` | `(api_key, *, model, max_tokens=4096, sdk=None, usage=None) -> Callable[[str, str, dict], dict]` | returns `call(system, user, schema) -> dict` |

`sdk` stays injected in all three, for the reason it exists today: the whole
path tests with no package, no key, no network and no cent.

### Known SDK shapes

Anthropic is unchanged. Gemini, verified 2026-07-31 against
`ai.google.dev/gemini-api/docs/text-generation` and `.../structured-output`:

```python
from google import genai                    # package: google-genai
client = genai.Client(api_key=api_key)
interaction = client.interactions.create(
    model=model,
    system_instruction=system,
    input=user,
    response_format={"type": "text", "mime_type": "application/json",
                     "schema": schema},     # raw JSON Schema dicts accepted
)
text = interaction.output_text
```

The usage attribute names are **not** documented on those pages and must be
pinned during implementation against the SDK itself — as must OpenAI's call
shape, where two APIs (chat completions and responses) are both plausible.
Neither is guessed here: an implementer reads the current documentation and
records what they found in the module docstring. This is a deliberate
instruction, not a gap in the spec.

### Schema dialects differ, and the provider owns the adaptation

`moment_scan.SCHEMA` is written for Anthropic's dialect. It already avoids
`minimum`/`maximum` (silently stripped there) and already sets
`additionalProperties: false` throughout. But `hook_suggestion` is optional,
and OpenAI's strict mode requires **every** property to appear in
`required`, expressing optionality as a nullable union instead.

`moment_scan.SCHEMA` does not change. Each provider adapts the schema it is
handed, inside its own module, as part of its job. The conformance suite
pins the property that matters: **every provider's caller accepts
`moment_scan.SCHEMA` exactly as `scan` passes it.** A provider that needs a
different shape produces it itself.

## Configuration

The `detect` section of `brand.json` gains `provider`; absent means
`anthropic`, so every existing profile keeps working untouched.

`profile.py` gains `_validate_detect`, collecting its defects with all the
others as the module already does. An unknown provider id is a reported
defect — never a silent fall back to the default.

`model` keeps its meaning but takes its default from the selected provider
rather than from `claude_client`. A model name the chosen provider does not
recognise is **not** caught at load time: that would require `profile.py` to
carry three vendors' model catalogues and re-check them monthly. It fails at
call time, is wrapped as `ModelError`, and falls back to the lexicon with
the loud log a missing key already produces.

## Keys

`auth/anthropic.json`, `auth/gemini.json`, `auth/openai.json`. `auth/` is
already gitignored wholesale, so new key files are covered with no change.
Both existing shapes stay accepted: a raw key, or `{"api_key": ...}`.

`load_api_key` takes the filename as a parameter instead of hardcoding it.
That is its only change; every branch of its error handling — including the
`UnicodeDecodeError` case and the `"api_key": null` case — stays as it is.

## The studio

### Settings — managing keys

A "Model providers" block with one row per provider, showing: whether a key
is present, whether the SDK is installed (with the `pip install` line when
it is not), the default model, and — for OpenAI — that it has not been
measured against the real service. Each row offers a paste-and-save field
and a remove button with a confirmation, symmetric with the existing
disconnect for YouTube.

Two routes, `PUT` and `DELETE` on `/api/providers/{id}/key`, under four
rules:

- **`{id}` is checked against the registry**, not against a name pattern.
  The set of valid providers is closed, which is stronger than
  `pathnames.validate_segment` and cannot reach the filesystem at all for an
  unknown id.
- **The write is atomic and 0600.** Written as `{"api_key": ...}` to a
  sibling scratch file whose mode is set *before* `os.replace` — setting it
  afterwards leaves a window with a world-readable key. Same mechanic as
  `render.compose`.
- **The key never appears in a response, an exception message or a log
  line.** `GET /api/settings` keeps returning booleans only, as it already
  does for YouTube connections. Error messages name the path, never the
  content.
- **Refused with 400:** empty, non-string, containing any newline or control
  character, or longer than 4096 characters. The number is a sanity bound,
  not a vendor fact — every current key is far shorter, and the point is to
  refuse a pasted file rather than to police a format that changes.

Each channel row in Settings additionally shows, read-only, which provider
and model that channel currently uses.

### Brand editor — choosing a provider

A "Moment detection" section: a provider select and a model text input,
seeded with the selected provider's default. Switching provider resets the
model field to the new provider's default, so a model name cannot be
carried across to a vendor that has never heard of it.

`brand_admin.update_brand` adds `"detect"` to its allowed sections and
validates it with the same logic `profile` uses — the module's existing
rule that a brand it accepts is one `profile.load` accepts.

The editor writes the **channel's** `brand.json`. An event-level override
stays possible and stays a file edit, exactly as for every other section.

## Error handling and key secrecy

The hardest part of a new provider is the part that took two review rounds
on the first one: **three** entry points can leak the key — building the
client, sending the request, and **reading the response**. The third was
missed twice; a fake SDK whose `response.content` raised
`AttributeError("... sk-ant-XXXX ...")` reached the caller with the key
intact.

Every provider wraps all three, raising `ModelError` built from the
exception's **type name only**. The consequence for consumers is unchanged
and now applies to all three: `ModelError` and `MissingKey` messages may be
logged in full; anything else is logged by type name only, as
`detect._caller_from_config` and `moment_scan.scan` both already do.

The `__cause__` hazard documented in `claude_client.py` carries over
verbatim and must be repeated in each module: chaining keeps the original
message alive for tracebacks, so the day anyone adds `exc_info=True` or
`logger.exception` on this path, the chain is walked and the original text
printed.

## Usage and prices

`Usage.record` currently reads the field names `input_tokens` and
`output_tokens` directly off the response. Those names differ per vendor.
Rather than teaching one function three vocabularies, each provider derives
its own `(input, output)` pair and calls `usage.record_counts(input,
output)`. The guarantee is copied word for word: tolerates anything, raises
nothing — losing the bookkeeping is acceptable, losing the window over the
bookkeeping is not. Recording still happens **before** the response is read,
because the tokens were spent the moment the response came back.

`estimate.PRICES` moves into the provider modules; `estimate` and
`detect._report_usage` look prices up through the registry. A model with no
price entry still reports its tokens and says the cost is unknown, not zero.

For Gemini's free-tier Flash models the **paid** rates are recorded. Zero
would be wrong for anyone paying, and zero is not "unknown" — so the number
is an honest upper bound.

## Testing

### One conformance suite over the registry

The centrepiece. One parameterised test file asserting the same properties
for **every** registered provider:

1. the five contract names exist with the right types
2. a failure building the client → `ModelError`, type name only, no key
3. a failure sending the request → same
4. a failure **reading the response** → same
5. a non-JSON answer → `ModelError`
6. usage is recorded before the response is read
7. an unreadable/empty/malformed key file → `MissingKey`, never anything else
8. importing the module pulls in no vendor SDK
9. the caller accepts `moment_scan.SCHEMA` unchanged

A fourth provider inherits all nine the moment it enters the registry. That
is what makes "swappable" a checkable claim rather than a sentence in a
README.

### Everything else

`profile` validation of `detect`, `brand_admin`'s new section, the two key
routes (including that the key never appears in any response), the Settings
and brand-editor screens, and `detect`'s provider selection. Existing
Anthropic tests move with the module and keep passing unchanged — if they
need edits, the move was not a move.

## The measurement

Gemini runs once, for real, over the same 98-minute qualifying the Anthropic
bake-off used, scored against the same four known-good moments. A free-tier
Flash model first, since the free tier is why Gemini was chosen; a paid
model only if Flash falls down badly.

The result goes into README as a dated table and beside `DEFAULT_MODEL` in
`gemini_api.py`, in the same form `claude_client.py`'s existing bake-off
comment takes.

## What README may claim

In exactly this gradation:

- **Anthropic** — the default, measured, with the existing table.
- **Gemini** — measured, with its own table and date.
- **OpenAI** — ships, tested against fakes, **not verified against the real
  service**.

Plus the contract itself, so a fourth provider is a file rather than a
rebuild.

## Out of scope

- Any provider for transcription. faster-whisper is local, free and works.
- Per-event provider selection in the studio UI (the config layering already
  supports it as a file edit).
- Automatic failover between providers. One engine per run stays the rule;
  two scoring scales in one list is exactly the invisible mess that rule
  exists to prevent.
- Re-running the Anthropic bake-off. Its numbers stand.

## Documentation to update

- README: the provider section, the three-way claim gradation, key setup.
- CLAUDE.md: the moment-detection section (five modules become a package),
  the key-secrecy paragraph (now a shared, tested rule), the optional-
  dependency list.
- CLAUDE.md correction found while writing this spec: it still states that
  `brand_admin` edits "`colors`, `fonts` and `subtitles` only" and never
  takes `output` from the patch. The code allows seven sections including
  `output`. Corrected as part of this work.

## Risks

- **The unverified provider.** OpenAI's shipped implementation may be wrong
  in a way fakes cannot reveal. Mitigated by labelling, not by hope.
- **Vendor drift.** Three SDKs change independently. The conformance suite
  catches shape breaks in our own code, not a vendor changing its API. The
  price tables are dated snapshots and say so.
- **The move touches a lot of prose.** `claude_client.py` is referenced
  throughout CLAUDE.md and several docstrings. Mechanical, but real work,
  and easy to leave half-done.
