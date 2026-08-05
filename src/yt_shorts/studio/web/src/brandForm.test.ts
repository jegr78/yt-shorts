import { describe, expect, it } from 'vitest'
import { detectSection, formFromBrand, type BrandEditorForm } from './brandForm'

describe('formFromBrand bands', () => {
  it('defaults a missing section to full strength', () => {
    expect(formFromBrand({}).bands).toEqual({ top: 1, bottom: 1 })
  })

  it('defaults a missing key to full strength', () => {
    expect(formFromBrand({ bands: { top: 0.25 } }).bands).toEqual({ top: 0.25, bottom: 1 })
  })

  it('ignores a malformed value rather than rendering NaN', () => {
    expect(formFromBrand({ bands: { top: 'x' } }).bands).toEqual({ top: 1, bottom: 1 })
  })

  it('reads both values', () => {
    expect(formFromBrand({ bands: { top: 0, bottom: 0.5 } }).bands).toEqual({
      top: 0,
      bottom: 0.5,
    })
  })

  // A malformed SECTION, not just a malformed value inside one. These pass
  // today only because JS never throws on property access against a
  // non-nullish primitive or an array - `('nope').top` is simply undefined,
  // which the number guard then defaults. That is behaviour falling out of
  // the language rather than behaviour anyone wrote, so an innocent-looking
  // refactor (say, replacing `?? {}` with a typeof check that forgets arrays)
  // would regress it silently. Pinned here so it cannot.
  it.each([
    ['a string', 'nope'],
    ['null', null],
    ['a bool', true],
    ['an array', [1, 2]],
  ])('degrades %s section to full strength', (_label, bands) => {
    expect(formFromBrand({ bands }).bands).toEqual({ top: 1, bottom: 1 })
  })
})

describe('formFromBrand detect', () => {
  it('reads an absent section as unset, never as the built-in default', () => {
    // "" means inherited. Seeding the default here would turn "inherited"
    // into "chosen" on the next save of an unrelated field.
    const form = formFromBrand({})
    expect(form.detectProvider).toBe('')
    expect(form.detectModel).toBe('')
  })

  it('reads both values', () => {
    const form = formFromBrand({ detect: { provider: 'gemini', model: 'gemini-2.5-flash' } })
    expect(form.detectProvider).toBe('gemini')
    expect(form.detectModel).toBe('gemini-2.5-flash')
  })

  it('reads a provider with no model as "use the provider default"', () => {
    expect(formFromBrand({ detect: { provider: 'openai' } }).detectModel).toBe('')
  })

  it.each([
    ['a string', 'gemini'],
    ['null', null],
    ['a bool', true],
    ['an array', ['gemini']],
  ])('degrades %s section to unset', (_label, detect) => {
    const form = formFromBrand({ detect })
    expect(form.detectProvider).toBe('')
    expect(form.detectModel).toBe('')
  })

  it('degrades a non-string provider/model to unset', () => {
    // brand.json is hand-editable and this read does not go through
    // profile.load, so `{"provider": ["gemini"]}` reaches here intact - it
    // must not render as [object Object] in a select.
    const form = formFromBrand({ detect: { provider: ['gemini'], model: 7 } })
    expect(form.detectProvider).toBe('')
    expect(form.detectModel).toBe('')
  })
})

describe('detectSection', () => {
  const form = (overrides: Partial<BrandEditorForm> = {}): BrandEditorForm => ({
    ...formFromBrand({}),
    ...overrides,
  })

  it('is undefined while no provider is chosen', () => {
    // A blank provider is not a registered id, so sending it would 400 every
    // save of an unrelated field.
    expect(detectSection(form())).toBeUndefined()
    expect(detectSection(form({ detectModel: 'claude-opus-5' }))).toBeUndefined()
  })

  it('omits a blank model rather than sending an empty string', () => {
    // profile._validate_detect refuses an empty model; absent IS how "use
    // the provider's own default" is written on disk.
    expect(detectSection(form({ detectProvider: 'openai' }))).toEqual({ provider: 'openai' })
    expect(detectSection(form({ detectProvider: 'openai', detectModel: '   ' }))).toEqual({
      provider: 'openai',
    })
  })

  it('sends both, trimmed, once a model is set', () => {
    expect(
      detectSection(form({ detectProvider: 'gemini', detectModel: ' gemini-2.5-flash ' })),
    ).toEqual({ provider: 'gemini', model: 'gemini-2.5-flash' })
  })
})
