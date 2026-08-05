import { describe, expect, it } from 'vitest'
import { extractUploadUrl, safeExternalUrl, type Job } from './api'

function job(log: string[]): Job {
  return { id: 'j', status: 'done', results: {}, log }
}

describe('extractUploadUrl', () => {
  it('returns the url from the matching "uploaded" log line', () => {
    const j = job(['uploaded: my-clip -> https://youtu.be/VID1'])
    expect(extractUploadUrl(j, 'my-clip')).toBe('https://youtu.be/VID1')
  })

  it('returns null when no line matches the clip name', () => {
    expect(extractUploadUrl(job([]), 'my-clip')).toBeNull()
    expect(extractUploadUrl(job(['done: my-clip']), 'my-clip')).toBeNull()
  })

  it('does not match a line for a different clip', () => {
    const j = job(['uploaded: other-clip -> https://youtu.be/OTHER'])
    expect(extractUploadUrl(j, 'my-clip')).toBeNull()
  })
})

describe('safeExternalUrl', () => {
  it('passes an http(s) URL through unchanged', () => {
    expect(safeExternalUrl('https://youtu.be/VID1')).toBe('https://youtu.be/VID1')
    expect(safeExternalUrl('http://127.0.0.1:8765/x')).toBe('http://127.0.0.1:8765/x')
  })

  it('rejects a non-http(s) scheme (returns null so it renders as text, not an href)', () => {
    expect(safeExternalUrl('javascript:alert(1)')).toBeNull()
    expect(safeExternalUrl('data:text/html,<script>1</script>')).toBeNull()
    expect(safeExternalUrl('not a url')).toBeNull()
    expect(safeExternalUrl(null)).toBeNull()
    expect(safeExternalUrl(undefined)).toBeNull()
  })
})
