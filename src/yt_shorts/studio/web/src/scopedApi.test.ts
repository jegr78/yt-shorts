import { describe, expect, it } from 'vitest'
import {
  channelBase,
  encodeSegment,
  eventBase,
  parseRoute,
  routePath,
  type Route,
  type Screen,
} from './scopedApi'

/** Every member of the `Screen` union, as a runtime list. The Record type is
 * the gate rather than a comment: `npm run build` type-checks `src/**`,
 * tests included, so a screen added to the union without a row here fails
 * the build instead of silently going untested (this project's recurring
 * defect - a new wiring point added beside an enumeration that was not
 * extended to it). */
const ALL_SCREENS: Record<Screen, true> = {
  channels: true,
  events: true,
  editor: true,
  settings: true,
  logs: true,
  jobs: true,
  stream: true,
}
const SCREENS = Object.keys(ALL_SCREENS) as Screen[]

describe('channelBase', () => {
  it('builds an absolute channel-scoped base', () => {
    expect(channelBase('erf')).toBe('/api/channels/erf')
  })
  it('encodes a reserved character in the channel segment', () => {
    expect(channelBase('a/b')).toBe('/api/channels/a%2Fb')
  })
})

describe('eventBase', () => {
  it('builds an absolute event-scoped base', () => {
    expect(eventBase('erf', 'studio-test')).toBe('/api/channels/erf/events/studio-test')
  })
  it('encodes both segments independently', () => {
    expect(eventBase('a b', 'c/d')).toBe('/api/channels/a%20b/events/c%2Fd')
  })
})

describe('encodeSegment', () => {
  it('percent-encodes a single path segment', () => {
    expect(encodeSegment('a/b c')).toBe('a%2Fb%20c')
  })
})

describe('parseRoute', () => {
  it('maps / to the channel list', () => {
    expect(parseRoute('/')).toEqual({ screen: 'channels' })
    expect(parseRoute('')).toEqual({ screen: 'channels' })
  })
  it('maps /{channel} to that channel event list', () => {
    expect(parseRoute('/erf')).toEqual({ screen: 'events', channel: 'erf' })
    // A trailing slash is not a second, empty segment.
    expect(parseRoute('/erf/')).toEqual({ screen: 'events', channel: 'erf' })
  })
  it('maps /{channel}/{event} to the editor', () => {
    expect(parseRoute('/erf/studio-test')).toEqual({
      screen: 'editor',
      channel: 'erf',
      event: 'studio-test',
    })
  })
  it('maps /settings to the settings screen, not a channel named "settings"', () => {
    expect(parseRoute('/settings')).toEqual({ screen: 'settings' })
    expect(parseRoute('/settings/')).toEqual({ screen: 'settings' })
  })
  it('decodes encoded segments', () => {
    expect(parseRoute('/a%2Fb/c%20d')).toEqual({
      screen: 'editor',
      channel: 'a/b',
      event: 'c d',
    })
  })
  it('does not throw on a malformed percent-escape (falls back to raw segment)', () => {
    // A hand-typed or external URL like /%  or /erf/%E0 must not crash the SPA:
    // decodeURIComponent would throw URIError; parseRoute has to survive it.
    expect(() => parseRoute('/%')).not.toThrow()
    expect(parseRoute('/%')).toEqual({ screen: 'events', channel: '%' })
    expect(parseRoute('/erf/%E0%A4')).toEqual({
      screen: 'editor',
      channel: 'erf',
      event: '%E0%A4',
    })
  })
})

describe('routePath', () => {
  it('round-trips each screen back to a path', () => {
    // ENUMERATING: one row per member of the Screen union (see scopedApi.ts).
    // A seventh screen added without a row here is the exact defect this
    // shape exists to catch - the router would gain a screen no test names.
    const cases: Array<[Route, string]> = [
      [{ screen: 'channels' }, '/'],
      [{ screen: 'settings' }, '/settings'],
      [{ screen: 'logs' }, '/logs'],
      [{ screen: 'jobs' }, '/jobs'],
      [{ screen: 'events', channel: 'erf' }, '/erf'],
      [{ screen: 'editor', channel: 'erf', event: 'studio-test' }, '/erf/studio-test'],
      [
        { screen: 'stream', channel: 'erf', event: 'studio-test', videoId: 'vid1' },
        '/erf/studio-test/streams/vid1',
      ],
    ]
    const covered = new Set(cases.map(([route]) => route.screen))
    expect(covered.size).toBe(SCREENS.length)
    for (const [route, path] of cases) {
      expect(routePath(route)).toBe(path)
      expect(parseRoute(path).screen).toBe(route.screen)
    }
  })
  it('encodes segments so a reserved name still round-trips through parseRoute', () => {
    const path = routePath({ screen: 'editor', channel: 'a b', event: 'c/d' })
    expect(path).toBe('/a%20b/c%2Fd')
    expect(parseRoute(path)).toEqual({ screen: 'editor', channel: 'a b', event: 'c/d' })
  })

  it('routes /logs to the logs screen', () => {
    expect(parseRoute('/logs')).toEqual({ screen: 'logs' })
    expect(routePath({ screen: 'logs' })).toBe('/logs')
  })

  it('routes /jobs to the jobs screen, not a channel named "jobs"', () => {
    // Same precedence rule /settings and /logs already have: the literal
    // one-segment path wins over the generic {channel} rule below it.
    expect(parseRoute('/jobs')).toEqual({ screen: 'jobs' })
    expect(parseRoute('/jobs/')).toEqual({ screen: 'jobs' })
    expect(routePath({ screen: 'jobs' })).toBe('/jobs')
  })
})

describe('parseRoute: the stream screen', () => {
  it('reads channel, event and video id from a four-segment path', () => {
    expect(parseRoute('/erf/n24-2026/streams/V9nVNEQNdR4')).toEqual({
      screen: 'stream', channel: 'erf', event: 'n24-2026', videoId: 'V9nVNEQNdR4',
    })
  })

  it('is not confused by a third segment that is not "streams"', () => {
    // The editor catch-all owns everything else, and must keep owning it.
    expect(parseRoute('/erf/n24-2026/something/else')).toEqual({
      screen: 'editor', channel: 'erf', event: 'n24-2026',
    })
  })

  it('falls back to the editor when the video id is missing', () => {
    expect(parseRoute('/erf/n24-2026/streams')).toEqual({
      screen: 'editor', channel: 'erf', event: 'n24-2026',
    })
  })

  it('decodes each segment', () => {
    expect(parseRoute('/erf/n24%202026/streams/vid%2D1')).toEqual({
      screen: 'stream', channel: 'erf', event: 'n24 2026', videoId: 'vid-1',
    })
  })

  it('round-trips through routePath', () => {
    const route = {
      screen: 'stream' as const, channel: 'erf', event: 'n24 2026', videoId: 'V9n',
    }
    expect(parseRoute(routePath(route))).toEqual(route)
  })
})
