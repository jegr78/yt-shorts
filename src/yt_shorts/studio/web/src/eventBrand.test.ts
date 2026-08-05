import { describe, expect, it } from 'vitest'
import { buildOverridePayload, overriddenSections, SECTIONS } from './eventBrand'

describe('overriddenSections', () => {
  it('returns the section keys present in the override', () => {
    expect(overriddenSections({ colors: {}, logo: {} })).toEqual(new Set(['colors', 'logo']))
    expect(overriddenSections({})).toEqual(new Set())
  })
  it('ignores unknown keys', () => {
    expect(overriddenSections({ colors: {}, upload: {} })).toEqual(new Set(['colors']))
  })
})

describe('buildOverridePayload', () => {
  const effective = {
    colors: { text: '#fff', base: '#000', accent: '#f00', edge: '#0f0' },
    fonts: { hook: 'fonts/H.ttf', small: 'fonts/S.ttf' },
    output: { width: 1080, height: 1920 },
  }
  it('includes only overridden sections, whole', () => {
    expect(buildOverridePayload(effective, new Set(['colors']))).toEqual({ colors: effective.colors })
  })
  it('is empty when nothing is overridden (fully inherited)', () => {
    expect(buildOverridePayload(effective, new Set())).toEqual({})
  })
})

describe('bands as an overridable section', () => {
  it('is one of the sections', () => {
    expect(SECTIONS).toContain('bands')
  })

  it('reports bands as overridden when the event stores it', () => {
    expect(overriddenSections({ bands: { top: 0, bottom: 1 } })).toContain('bands')
  })

  it('carries the whole bands section in the payload', () => {
    const payload = buildOverridePayload(
      { colors: {}, bands: { top: 0.5, bottom: 1 } },
      new Set(['bands']),
    )
    expect(payload).toEqual({ bands: { top: 0.5, bottom: 1 } })
  })

  it('omits bands entirely when inherited', () => {
    const payload = buildOverridePayload(
      { bands: { top: 0.5, bottom: 1 } },
      new Set(['colors']),
    )
    expect(payload).not.toHaveProperty('bands')
  })
})
