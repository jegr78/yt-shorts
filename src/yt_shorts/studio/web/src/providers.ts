/**
 * The moment-detection model providers, as the Settings screen and the
 * channel Brand editor need them.
 *
 * Pure and React-free, in its own module rather than inside either component,
 * for the reason words.ts/trim.ts/settings.ts already are: Vite's fast-refresh
 * boundary stays component-only, and every rule below is unit-tested without
 * rendering anything.
 *
 * Everything here is fed by GET /api/settings's `providers` array (see
 * api.py's get_settings), which carries shipped constants and booleans only -
 * never a key, never a path. `prices` is each provider module's own PRICES
 * table verbatim; read the notes on `priceSentence` before wording anything
 * with it, because those numbers are a FLOOR and must never be presented as a
 * bill.
 */

/** One row of GET /api/settings's `providers` array. */
export interface ProviderState {
  id: string
  default_model: string
  key_present: boolean
  sdk_installed: boolean
  /** The exact command that installs this provider's SDK, shipped by the
   * provider module (its INSTALL constant) rather than composed here. */
  install: string
  /** False for a provider that has never been measured against the real
   * service (see each provider module's own VERIFIED). Disclosed in BOTH
   * surfaces - an operator choosing one in the brand editor must not have to
   * have visited Settings to learn it. */
  verified: boolean
  /** model -> [USD per 1M input tokens, USD per 1M output tokens]. */
  prices: Record<string, [number, number]>
}

/** The display name for a provider id. Falls back to the id itself for one
 * this build does not know, so a server ahead of this client shows something
 * honest rather than blank. */
export function providerLabel(id: string): string {
  switch (id) {
    case 'anthropic':
      return 'Anthropic'
    case 'openai':
      return 'OpenAI'
    case 'gemini':
      return 'Google Gemini'
    default:
      return id
  }
}

/** Why this provider cannot be used right now, or null when it can.
 * Ordered by what the operator must do FIRST: an absent SDK cannot be fixed
 * by pasting a key, so it is named even when the key is already stored. The
 * caller shows `state.install` beside this one - the sentence names the
 * problem, the command is a separate, copyable line. */
export function providerBlocker(state: ProviderState): string | null {
  if (!state.sdk_installed) {
    return `The ${providerLabel(state.id)} SDK is not installed, so this provider cannot run yet. Install it, then restart the studio.`
  }
  if (!state.key_present) {
    return 'No API key stored - detection falls back to the offline lexicon engine, which is weaker.'
  }
  return null
}

/** The provider row with this id, or undefined. */
export function findProvider(
  states: ProviderState[],
  id: string,
): ProviderState | undefined {
  return states.find((state) => state.id === id)
}

/** The Select's own `data`: every known provider, plus - when `current` is
 * non-empty and not among them - one synthetic entry for it.
 *
 * Without that entry, Mantine's `Select` resolves an unmatched `value` to
 * `undefined` and renders the PLACEHOLDER instead - which for this field
 * reads as "Anthropic (the built-in default)" while a different provider is
 * actually stored, and the caveat/price block below reads it as unchosen too
 * (see `findProvider` at the call site). This is not hypothetical: it is
 * exactly what a `brand.json` naming a provider this build has since dropped,
 * or a `GET /api/settings` that failed (`states` is `[]`, not `null`, in that
 * case - see BrandEditor's own load-error handling), both produce. The
 * synthetic entry has no verified/unverified tag, because there is nothing
 * behind it to check - see `unrecognizedProviderNote` for what the card shows
 * in that case instead of a caveat or a price. */
export function providerOptions(
  states: ProviderState[],
  current: string,
): { value: string; label: string }[] {
  const options = states.map((state) => ({
    value: state.id,
    label: state.verified ? providerLabel(state.id) : `${providerLabel(state.id)} — unverified`,
  }))
  if (current && !findProvider(states, current)) {
    options.push({ value: current, label: providerLabel(current) })
  }
  return options
}

/** The note shown in place of the caveat/price block when the stored
 * provider is not one this build recognises at all (see `providerOptions`).
 * Deliberately weaker than `unverifiedCaveat`: there is no `ProviderState` to
 * build a label, a verified flag or a price from, so this names what is
 * UNKNOWN rather than asserting anything about it, and says plainly that the
 * stored choice is kept and would be saved as-is - the disclosure this task
 * exists to guarantee must never be silently absent, even when there is
 * nothing to disclose it WITH. */
export function unrecognizedProviderNote(provider: string): string {
  return `"${providerLabel(provider)}" is not a provider this build recognises - it cannot show whether it is verified, or what it costs. The stored choice is kept and will be saved as-is.`
}

/** The plainly worded warning for a provider that has never been measured
 * against the real service, or null for one that has. */
export function unverifiedCaveat(state: ProviderState): string | null {
  if (state.verified) return null
  return `${providerLabel(state.id)} has never been measured against the real service on this project's own transcripts - it is shipped unverified, and how well it detects moments here is unknown.`
}

/** One priced model: the two published per-million rates, with the model
 * they belong to. */
export interface PricedModel {
  model: string
  input: number
  output: number
}

/** A USD-per-million rate, formatted for display. Two decimals throughout,
 * so `$0.20` and `$12.00` line up rather than reading as `$0.2`. */
export function formatRate(usd: number): string {
  return `$${usd.toFixed(2)}`
}

/** The published rate for one model, or null when this provider lists none
 * for it. Absent is NOT free: an unpriced model shows as unpriced. */
export function priceFor(state: ProviderState, model: string): PricedModel | null {
  const rate = state.prices[model]
  if (!rate) return null
  return { model, input: rate[0], output: rate[1] }
}

/** The cheapest model this provider publishes a rate for, by the two rates
 * summed - the comparison the disclosure quotes. Ties break alphabetically so
 * the answer never depends on object key order. */
export function cheapestPriced(state: ProviderState): PricedModel | null {
  let best: PricedModel | null = null
  for (const [model, rate] of Object.entries(state.prices)) {
    const candidate: PricedModel = { model, input: rate[0], output: rate[1] }
    if (best === null) {
      best = candidate
      continue
    }
    const a = candidate.input + candidate.output
    const b = best.input + best.output
    if (a < b || (a === b && candidate.model < best.model)) best = candidate
  }
  return best
}

/**
 * What choosing this provider and model costs, disclosed at the moment of
 * choosing: the selected model's published rate, plus this provider's own
 * cheapest priced model beside it for comparison.
 *
 * It exists because a default model is not always the cheapest one, and an
 * operator paying that difference should see it while choosing rather than on
 * an invoice - `openai_api.DEFAULT_MODEL` is deliberately the middle of its
 * family rather than its floor (picking cheapest is the argument the Anthropic
 * bake-off refuted on this project's own transcripts). Whether a given gap is
 * EARNED is each provider module's own question, answered beside its
 * `DEFAULT_MODEL` and liable to move as measurements land; this function asks
 * nobody to trust an answer, it just puts both numbers on screen. Turning a
 * hidden exposure into a disclosed one is the same move `verified` already
 * makes for correctness.
 *
 * NEVER a bill. Every number here is a per-million rate from a dated
 * snapshot, and both gemini_api and openai_api document their tables as a
 * FLOOR: long context is priced higher, and this project's endurance streams
 * are exactly the workload that reaches it (measured - gpt-5.6-terra's
 * long-context output rate is 18.00 against the 12.00 stored). So the wording
 * says "from", names the floor, and never multiplies anything out into a
 * total. Anyone rewording this must keep both.
 */
export function priceSentence(state: ProviderState, model: string): string | null {
  const cheapest = cheapestPriced(state)
  if (cheapest === null) return null
  const floor =
    'Long windows are billed at a higher tier, so read these as a floor, not a total.'
  const rate = (priced: PricedModel) =>
    `${formatRate(priced.input)} / ${formatRate(priced.output)} per 1M input / output tokens`
  const selected = priceFor(state, model)
  if (selected === null) {
    return `No published rate here for "${model}". The cheapest model this provider lists is ${cheapest.model}, from ${rate(cheapest)}. ${floor}`
  }
  if (selected.model === cheapest.model) {
    return `From ${rate(selected)} - the cheapest model this provider lists. ${floor}`
  }
  return `From ${rate(selected)}. The cheapest model this provider lists is ${cheapest.model}, from ${formatRate(cheapest.input)} / ${formatRate(cheapest.output)}. ${floor}`
}
