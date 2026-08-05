import { describe, expect, it } from 'vitest'
import { formatDuration, formatStreamDuration } from './format'

describe('formatDuration', () => {
  it('formats minutes:seconds.tenths with zero-padded seconds', () => {
    expect(formatDuration(72.3)).toBe('1:12.3')
  })
  it('pads seconds below ten', () => {
    expect(formatDuration(65.0)).toBe('1:05.0')
  })
  it('handles zero', () => {
    expect(formatDuration(0)).toBe('0:00.0')
  })
  it('returns the sentinel for a negative or non-finite duration', () => {
    expect(formatDuration(-1)).toBe('--:--')
    expect(formatDuration(NaN)).toBe('--:--')
  })
})

describe('formatStreamDuration', () => {
  it('shows hours:minutes:seconds for a long stream', () => {
    expect(formatStreamDuration(3661)).toBe('1:01:01')
  })
  it('drops the hours field when under an hour', () => {
    expect(formatStreamDuration(61)).toBe('1:01')
  })
  it('returns the sentinel for null', () => {
    expect(formatStreamDuration(null)).toBe('--:--')
  })
})
