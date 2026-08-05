import { describe, expect, it } from 'vitest'
import {
  cheapestPriced,
  findProvider,
  priceFor,
  priceSentence,
  providerBlocker,
  providerLabel,
  providerOptions,
  unrecognizedProviderNote,
  unverifiedCaveat,
  type ProviderState,
} from './providers'

function state(overrides: Partial<ProviderState> = {}): ProviderState {
  return {
    id: 'anthropic',
    default_model: 'claude-opus-5',
    key_present: true,
    sdk_installed: true,
    install: '.venv/bin/pip install anthropic',
    verified: true,
    prices: {
      'claude-haiku-4-5': [1.0, 5.0],
      'claude-sonnet-5': [3.0, 15.0],
      'claude-opus-5': [5.0, 25.0],
    },
    ...overrides,
  }
}

const OPENAI = state({
  id: 'openai',
  default_model: 'gpt-5.6-terra',
  install: '.venv/bin/pip install openai',
  verified: false,
  prices: {
    'gpt-5.6-sol': [5.0, 30.0],
    'gpt-5.6-terra': [2.0, 12.0],
    'gpt-5.6-luna': [0.2, 1.2],
  },
})

describe('providerLabel', () => {
  it('names each provider this build knows', () => {
    expect(providerLabel('anthropic')).toBe('Anthropic')
    expect(providerLabel('openai')).toBe('OpenAI')
    expect(providerLabel('gemini')).toBe('Google Gemini')
  })

  it('falls back to the id for an unknown provider', () => {
    // A server ahead of this client must show something honest rather than
    // a blank cell.
    expect(providerLabel('mistral')).toBe('mistral')
  })
})

describe('providerBlocker', () => {
  it('names the missing SDK before the missing key', () => {
    const blocker = providerBlocker(state({ sdk_installed: false, key_present: false }))
    expect(blocker).not.toBeNull()
    expect(blocker).toMatch(/SDK/i)
    expect(blocker).not.toMatch(/key/i)
  })

  it('still names the SDK when a key is already stored', () => {
    // An absent SDK cannot be fixed by pasting a key, so it stays first
    // even when the key half is already done.
    expect(providerBlocker(state({ sdk_installed: false }))).toMatch(/SDK/i)
  })

  it('names the missing key when the SDK is installed', () => {
    const blocker = providerBlocker(state({ key_present: false }))
    expect(blocker).toMatch(/key/i)
    expect(blocker).toMatch(/lexicon/i)
  })

  it('returns null when both are in place', () => {
    expect(providerBlocker(state())).toBeNull()
  })
})

describe('providerOptions', () => {
  const all = [state(), OPENAI]

  it('lists every known provider, unverified ones marked in their own label', () => {
    const options = providerOptions(all, '')
    expect(options).toEqual([
      { value: 'anthropic', label: 'Anthropic' },
      { value: 'openai', label: 'OpenAI — unverified' },
    ])
  })

  it('does not add a synthetic entry for a known current value', () => {
    expect(providerOptions(all, 'openai')).toHaveLength(2)
  })

  it('does not add a synthetic entry when current is empty', () => {
    // The unset state - nothing stored, nothing to preserve a slot for.
    expect(providerOptions(all, '')).toHaveLength(2)
  })

  it('appends a synthetic entry for a stored provider this build does not know', () => {
    // This is the M1 regression: without it, Mantine's Select resolves an
    // unmatched value to undefined and silently falls back to the
    // placeholder, showing "Anthropic (the built-in default)" for a channel
    // that actually chose something else.
    const options = providerOptions(all, 'mistral')
    expect(options).toHaveLength(3)
    expect(options[2]).toEqual({ value: 'mistral', label: 'mistral' })
  })

  it('the synthetic entry carries no verified/unverified tag', () => {
    // There is nothing behind it to check - see unrecognizedProviderNote for
    // what the card shows instead.
    const options = providerOptions(all, 'mistral')
    expect(options[2].label).not.toMatch(/unverified/i)
  })

  it('also covers the failed-settings-load shape: an empty states list', () => {
    // GET /api/settings failing leaves BrandEditor's providerStates at [],
    // not null - the stored provider must still resolve to an option.
    expect(providerOptions([], 'gemini')).toEqual([{ value: 'gemini', label: 'Google Gemini' }])
  })
})

describe('unrecognizedProviderNote', () => {
  it('names the unrecognised provider and says the choice is kept as-is', () => {
    const note = unrecognizedProviderNote('mistral')
    expect(note).toContain('mistral')
    expect(note).toMatch(/not.*recognise/i)
    expect(note).toMatch(/kept/i)
    expect(note).toMatch(/saved/i)
  })
})

describe('findProvider', () => {
  it('finds by id and answers undefined otherwise', () => {
    expect(findProvider([state(), OPENAI], 'openai')?.default_model).toBe('gpt-5.6-terra')
    expect(findProvider([state(), OPENAI], 'nope')).toBeUndefined()
  })
})

describe('unverifiedCaveat', () => {
  it('is null for a measured provider', () => {
    expect(unverifiedCaveat(state())).toBeNull()
  })

  it('says plainly that an unverified provider was never measured', () => {
    const caveat = unverifiedCaveat(OPENAI)
    expect(caveat).toMatch(/never been measured/i)
    expect(caveat).toContain('OpenAI')
  })
})

describe('priceFor', () => {
  it('reads the selected model row', () => {
    expect(priceFor(OPENAI, 'gpt-5.6-terra')).toEqual({
      model: 'gpt-5.6-terra',
      input: 2.0,
      output: 12.0,
    })
  })

  it('is null for a model with no published rate', () => {
    expect(priceFor(OPENAI, 'gpt-9-imaginary')).toBeNull()
  })
})

describe('cheapestPriced', () => {
  it('picks the lowest combined rate', () => {
    expect(cheapestPriced(OPENAI)).toEqual({ model: 'gpt-5.6-luna', input: 0.2, output: 1.2 })
    expect(cheapestPriced(state())?.model).toBe('claude-haiku-4-5')
  })

  it('breaks a tie by model name, so the answer is stable', () => {
    const tied = state({ prices: { zebra: [1, 1], alpha: [1, 1] } })
    expect(cheapestPriced(tied)?.model).toBe('alpha')
  })

  it('is null when the provider publishes no rates at all', () => {
    expect(cheapestPriced(state({ prices: {} }))).toBeNull()
  })
})

describe('priceSentence', () => {
  it('quotes the selected rate and the cheaper one beside it', () => {
    const sentence = priceSentence(OPENAI, 'gpt-5.6-terra')
    expect(sentence).toContain('$2.00')
    expect(sentence).toContain('$12.00')
    expect(sentence).toContain('gpt-5.6-luna')
    expect(sentence).toContain('$0.20')
    expect(sentence).toContain('$1.20')
  })

  it('never presents a rate as a bill', () => {
    // Both gemini_api and openai_api document their tables as a FLOOR for
    // the long windows this project's endurance streams reach, so the
    // wording says "from" and names the floor - never a total.
    const sentence = priceSentence(OPENAI, 'gpt-5.6-terra') ?? ''
    expect(sentence).toMatch(/from/i)
    expect(sentence).toMatch(/floor/i)
    expect(sentence).toMatch(/not a total/i)
    // And it never quotes a figure for a run/stream, which is the shape a
    // reader would take as a bill.
    expect(sentence).not.toMatch(/per run|per stream|per detection/i)
  })

  it('says so when the selected model is itself the cheapest', () => {
    const sentence = priceSentence(OPENAI, 'gpt-5.6-luna') ?? ''
    expect(sentence).toContain('$0.20')
    expect(sentence).toMatch(/cheapest/i)
    // Not "the cheapest here is gpt-5.6-luna" twice over.
    expect(sentence.match(/gpt-5\.6-luna/g)?.length ?? 0).toBeLessThan(2)
  })

  it('says there is no published rate for an unknown model, and still compares', () => {
    const sentence = priceSentence(OPENAI, 'gpt-9-imaginary') ?? ''
    expect(sentence).toMatch(/no published rate/i)
    expect(sentence).toContain('gpt-5.6-luna')
  })

  it('is null when the provider publishes no rates at all', () => {
    expect(priceSentence(state({ prices: {} }), 'anything')).toBeNull()
  })
})
