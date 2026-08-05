import { describe, expect, it } from 'vitest'
import { brandReadyToSave, fontFilename, isValidHexColor, outputReadyToSave } from './brand'

describe('isValidHexColor', () => {
  it.each(['#FFFFFF', '#000000', '#B8F5CA', '#fff', '#000', '#Ab1', '#a1B2c3'])(
    'accepts a valid hex color: %s',
    (good) => {
      expect(isValidHexColor(good)).toBe(true)
    },
  )

  it.each([
    '',
    'FFFFFF',
    '#FFFFF',
    '#FFFFFFF',
    '#GGGGGG',
    '#12',
    'red',
    '#fff ',
    ' #fff',
    'rgb(0,0,0)',
  ])('rejects an invalid hex color: %s', (bad) => {
    expect(isValidHexColor(bad)).toBe(false)
  })
})

describe('brandReadyToSave', () => {
  const completeColors = { text: '#FFFFFF', base: '#004625', accent: '#144E53', edge: '#B8F5CA' }
  const completeFonts = { hook: 'fonts/Hook.ttf', small: 'fonts/Small.ttf' }

  it('is true only when all four colors are valid hex AND both fonts are assigned', () => {
    expect(brandReadyToSave({ colors: completeColors, fonts: completeFonts })).toBe(true)
  })

  it('is false when a color is missing', () => {
    const { text: _text, ...rest } = completeColors
    expect(brandReadyToSave({ colors: rest, fonts: completeFonts })).toBe(false)
  })

  it('is false when a color is invalid', () => {
    expect(
      brandReadyToSave({
        colors: { ...completeColors, accent: 'not-a-color' },
        fonts: completeFonts,
      }),
    ).toBe(false)
  })

  it('is false when hook is unassigned', () => {
    expect(brandReadyToSave({ colors: completeColors, fonts: { small: 'fonts/Small.ttf' } })).toBe(
      false,
    )
  })

  it('is false when small is unassigned', () => {
    expect(brandReadyToSave({ colors: completeColors, fonts: { hook: 'fonts/Hook.ttf' } })).toBe(
      false,
    )
  })

  it('is false when both fonts are unassigned', () => {
    expect(brandReadyToSave({ colors: completeColors, fonts: {} })).toBe(false)
  })

  it('is false when a font is assigned but empty', () => {
    expect(
      brandReadyToSave({ colors: completeColors, fonts: { hook: '', small: 'fonts/Small.ttf' } }),
    ).toBe(false)
  })
})

describe('fontFilename', () => {
  it('replaces spaces in the stem with a hyphen and keeps the extension', () => {
    expect(fontFilename('My Font.ttf')).toBe('My-Font.ttf')
  })

  it('lowercases the extension', () => {
    expect(fontFilename('MyFont.TTF')).toBe('MyFont.ttf')
    expect(fontFilename('MyFont.OTF')).toBe('MyFont.otf')
  })

  it('accepts .ttf and .otf', () => {
    expect(fontFilename('Regular.otf')).toBe('Regular.otf')
  })

  it('collapses a run of unsafe characters into a single hyphen', () => {
    expect(fontFilename('My  Cool!! Font.ttf')).toBe('My-Cool-Font.ttf')
  })

  it('strips a leading dot from the stem', () => {
    expect(fontFilename('.hidden.ttf')).toBe('hidden.ttf')
  })

  it('returns "" for a name with no font extension', () => {
    expect(fontFilename('photo.png')).toBe('')
  })

  it('returns "" for a name with no extension at all', () => {
    expect(fontFilename('noextension')).toBe('')
  })

  it('returns "" when the stem is empty after stripping', () => {
    expect(fontFilename('...ttf')).toBe('')
  })
})

describe('outputReadyToSave', () => {
  const ok = { width: 1080, height: 1920, video_width: 1080, video_height: 608, video_y: 600 }

  it('accepts a valid portrait window', () => {
    expect(outputReadyToSave(ok)).toBe(true)
    expect(outputReadyToSave({ ...ok, accent_offset: 48 })).toBe(true)
  })

  it('rejects a non-integer dimension', () => {
    expect(outputReadyToSave({ ...ok, video_y: 600.5 })).toBe(false)
    // a NumberInput can hand back NaN for an emptied field
    expect(outputReadyToSave({ ...ok, video_y: Number.NaN })).toBe(false)
  })

  it('rejects a non-positive frame or video size', () => {
    expect(outputReadyToSave({ ...ok, width: 0 })).toBe(false)
    expect(outputReadyToSave({ ...ok, video_height: 0 })).toBe(false)
  })

  it('rejects a video wider than the frame', () => {
    expect(outputReadyToSave({ ...ok, video_width: 1200 })).toBe(false)
  })

  it('rejects a window that does not fit inside the frame height', () => {
    expect(outputReadyToSave({ ...ok, video_y: 1500 })).toBe(false) // 1500 + 608 > 1920
    expect(outputReadyToSave({ ...ok, video_y: -1 })).toBe(false)
  })
})
