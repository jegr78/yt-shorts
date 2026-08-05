import { describe, expect, it } from 'vitest'
import { setScope, shortUrl } from './api'

describe('shortUrl', () => {
  it('carries the version as a query parameter', () => {
    // Without this the URL is a constant, so a re-render leaves the mounted
    // <video> pointing at the same src and the browser answers from cache.
    setScope('erf', 'e1')
    expect(shortUrl('clip-a', '17-42', 'play')).toBe(
      '/api/channels/erf/events/e1/clips/clip-a/short?v=17-42',
    )
  })

  it('omits the query entirely for a deliberate null version and play', () => {
    setScope('erf', 'e1')
    expect(shortUrl('clip-a', null, 'play')).toBe(
      '/api/channels/erf/events/e1/clips/clip-a/short',
    )
  })

  it('encodes a token that would otherwise break the URL', () => {
    // Defensive: today's token is digits and a hyphen. The encoding is here
    // so the shape of the token stays an implementation detail of the server
    // (see _short_version) rather than a constraint on this function.
    // Built with URLSearchParams now (needed for the `as=download` param
    // below), which encodes a space as `+` rather than `%20` - both decode
    // to the same value server-side, so this is a change in spelling, not
    // in meaning.
    setScope('erf', 'e1')
    expect(shortUrl('clip-a', 'a b&c', 'play')).toBe(
      '/api/channels/erf/events/e1/clips/clip-a/short?v=a+b%26c',
    )
  })

  it('adds as=download for the download form, alongside the version', () => {
    // The download link (ManualUploadPanel) needs this - the server refuses
    // it (409) while a trim is pending, exactly to stop an operator hand-
    // uploading a stale, untrimmed video (see api.py's get_short).
    setScope('erf', 'e1')
    expect(shortUrl('clip-a', '17-42', 'download')).toBe(
      '/api/channels/erf/events/e1/clips/clip-a/short?v=17-42&as=download',
    )
  })

  it('adds as=download even with a deliberate null version', () => {
    setScope('erf', 'e1')
    expect(shortUrl('clip-a', null, 'download')).toBe(
      '/api/channels/erf/events/e1/clips/clip-a/short?as=download',
    )
  })
})
