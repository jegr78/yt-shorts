import { describe, expect, it } from 'vitest'
import { originLabel, parsePoolLimit } from './settings'

describe('originLabel', () => {
  it('describes YT_SHORTS_DATA as an env override', () => {
    expect(originLabel('YT_SHORTS_DATA')).toBe('from $YT_SHORTS_DATA')
  })
  it('describes default as the home-directory workspace', () => {
    expect(originLabel('default')).toBe('default (~/YT-Shorts-Data)')
  })
  it('describes repository as the legacy fallback', () => {
    expect(originLabel('repository')).toBe('repository fallback')
  })
  it('passes an unrecognised origin through unchanged', () => {
    expect(originLabel('something-new')).toBe('something-new')
  })
})

describe('parsePoolLimit', () => {
  it('accepts a plain positive whole number', () => {
    expect(parsePoolLimit('1')).toBe(1)
    expect(parsePoolLimit('12')).toBe(12)
  })
  it('trims surrounding whitespace', () => {
    expect(parsePoolLimit(' 3 ')).toBe(3)
  })
  it('refuses zero - a stalled pool with nothing saying why', () => {
    expect(parsePoolLimit('0')).toBeNull()
  })
  it('refuses a negative number', () => {
    expect(parsePoolLimit('-1')).toBeNull()
  })
  it('refuses a fractional number', () => {
    expect(parsePoolLimit('1.5')).toBeNull()
  })
  it('refuses a leading zero, a leading +, and non-numeric text', () => {
    expect(parsePoolLimit('01')).toBeNull()
    expect(parsePoolLimit('+1')).toBeNull()
    expect(parsePoolLimit('lots')).toBeNull()
  })
  it('refuses an empty or blank field', () => {
    expect(parsePoolLimit('')).toBeNull()
    expect(parsePoolLimit('   ')).toBeNull()
  })
})
