import { describe, expect, it } from 'vitest'
import {
  appendLines,
  downloadFilename,
  downloadLogText,
  formatSize,
  initialLogFileFromSearch,
  jobKindFromLogName,
  logBaseName,
  MAX_LINES,
  parseLine,
} from './logs'

describe('parseLine', () => {
  it('splits a standard log line into its parts', () => {
    const parsed = parseLine('2026-07-24 10:00:00 WARNING chunk 3 failed: RuntimeError: boom')
    expect(parsed.timestamp).toBe('2026-07-24 10:00:00')
    expect(parsed.level).toBe('WARNING')
    expect(parsed.message).toBe('chunk 3 failed: RuntimeError: boom')
  })

  it('classifies ERROR and INFO', () => {
    expect(parseLine('2026-07-24 10:00:00 ERROR nope').level).toBe('ERROR')
    expect(parseLine('2026-07-24 10:00:00 INFO fine').level).toBe('INFO')
  })

  it('keeps an unparseable line whole as OTHER', () => {
    const parsed = parseLine('  Traceback (most recent call last):')
    expect(parsed.level).toBe('OTHER')
    expect(parsed.message).toBe('  Traceback (most recent call last):')
    expect(parsed.timestamp).toBe('')
  })
})

describe('appendLines', () => {
  it('appends incoming lines', () => {
    expect(appendLines(['a'], ['b', 'c'])).toEqual(['a', 'b', 'c'])
  })

  it('drops from the front past the cap so a long tail cannot grow forever', () => {
    const existing = Array.from({ length: MAX_LINES }, (_unused, index) => `line ${index}`)
    const result = appendLines(existing, ['newest'])
    expect(result).toHaveLength(MAX_LINES)
    expect(result[result.length - 1]).toBe('newest')
    expect(result[0]).toBe('line 1')
  })

  it('returns the existing array unchanged when nothing arrived', () => {
    const existing = ['a']
    expect(appendLines(existing, [])).toBe(existing)
  })
})

describe('formatSize', () => {
  it('formats bytes, kB and MB', () => {
    expect(formatSize(512)).toBe('512 B')
    expect(formatSize(2048)).toBe('2.0 kB')
    expect(formatSize(5 * 1024 * 1024)).toBe('5.0 MB')
  })
})

describe('jobKindFromLogName', () => {
  it('reads the kind from a job log name', () => {
    expect(jobKindFromLogName('detect-abc123.log')).toBe('detect')
    expect(jobKindFromLogName('render-xyz.log.gz')).toBe('render')
  })

  it('returns null for the central log', () => {
    expect(jobKindFromLogName('yt-shorts.log')).toBeNull()
  })
})

describe('logBaseName', () => {
  it('strips a trailing .gz', () => {
    expect(logBaseName('render-abc123.log.gz')).toBe('render-abc123.log')
  })

  it('leaves a plain name untouched', () => {
    expect(logBaseName('render-abc123.log')).toBe('render-abc123.log')
    expect(logBaseName('yt-shorts.log')).toBe('yt-shorts.log')
  })
})

describe('initialLogFileFromSearch', () => {
  it('reads the file query parameter', () => {
    expect(initialLogFileFromSearch('?file=render-abc123.log')).toBe('render-abc123.log')
  })

  it('returns null when absent or blank', () => {
    expect(initialLogFileFromSearch('')).toBeNull()
    expect(initialLogFileFromSearch('?other=1')).toBeNull()
    expect(initialLogFileFromSearch('?file=')).toBeNull()
  })
})

describe('downloadFilename', () => {
  it('strips a trailing .gz and appends .txt', () => {
    expect(downloadFilename('render-abc123.log.gz', null)).toBe('render-abc123.log.txt')
  })

  it('leaves a plain name and appends .txt', () => {
    expect(downloadFilename('yt-shorts.log', null)).toBe('yt-shorts.log.txt')
  })

  it('inserts an archive date before the extension', () => {
    expect(downloadFilename('yt-shorts.log', '2026-07-23')).toBe('yt-shorts.log.2026-07-23.txt')
  })
})

describe('downloadLogText', () => {
  it('joins lines with a trailing newline', () => {
    expect(downloadLogText(['a', 'b'])).toBe('a\nb\n')
  })

  it('is empty for no lines', () => {
    expect(downloadLogText([])).toBe('')
  })
})
