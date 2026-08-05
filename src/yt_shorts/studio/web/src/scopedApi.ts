/** Pure builders for the studio's SCOPED HTTP surface (see ../../api.py's
 * route list) and for the client-side router's six screens. Kept in its
 * own module, exporting no React, so Vite's fast-refresh boundary stays
 * component-only (same convention as words.ts/format.ts) and every URL can
 * be unit-tested directly - a wrong scoped URL 404s silently at runtime,
 * which is exactly the kind of mistake a unit test should catch.
 *
 * Every base is ABSOLUTE ("/api/..."), not relative: the SPA router changes
 * window.location.pathname to /{channel}/{event}, so a relative "api/..."
 * would resolve against that path and hit the wrong URL. The built page is
 * always served from the workspace root by FastAPI (see api.py's SPA
 * fallback), so an absolute base is correct there too.
 */

/** Percent-encodes one path segment, so a channel or event whose name
 * carries a reserved character still lands on the right route. */
export function encodeSegment(segment: string): string {
  return encodeURIComponent(segment)
}

/** The channel-scoped base: /api/channels/{channel}. Auth is channel-scoped
 * (no event) and hangs off this. */
export function channelBase(channel: string): string {
  return `/api/channels/${encodeSegment(channel)}`
}

/** The event-scoped base: /api/channels/{channel}/events/{event}. Every
 * clip/stream/render/preview/upload route hangs off this. */
export function eventBase(channel: string, event: string): string {
  return `${channelBase(channel)}/events/${encodeSegment(event)}`
}

/** Which of the seven screens a URL path names, plus its channel/event.
 *  - /                                          -> the channel list
 *  - /settings                                  -> the workspace-level settings screen (G4)
 *  - /logs                                      -> the workspace-level logs screen
 *  - /jobs                                      -> the workspace-level job queue screen
 *  - /{channel}                                 -> that channel's event list
 *  - /{channel}/{event}                         -> the existing editor, scoped to that event
 *  - /{channel}/{event}/streams/{video_id}      -> the stream screen (D2b), one level
 *                                                   deeper than the editor
 * Anything else deeper is treated as the editor over the first two segments. */
export type Screen = 'channels' | 'events' | 'editor' | 'settings' | 'logs' | 'jobs' | 'stream'

export interface Route {
  screen: Screen
  channel?: string
  event?: string
  videoId?: string
}

/** decodeURIComponent throws a URIError on a malformed escape (e.g. a lone
 * '%'), and parseRoute runs synchronously during render - an externally-supplied
 * URL like /erf/%E0 would otherwise crash the whole SPA to a blank screen. Fall
 * back to the raw segment so a bad URL degrades to "no such channel", not a
 * white page. App-generated links are always well-formed (routePath encodes). */
function safeDecode(segment: string): string {
  try {
    return decodeURIComponent(segment)
  } catch {
    return segment
  }
}

export function parseRoute(pathname: string): Route {
  const segments = pathname.split('/').filter(Boolean).map(safeDecode)
  if (segments.length === 0) return { screen: 'channels' }
  // Matched BEFORE the generic 1-segment events rule below, so the literal
  // path /settings is always the settings screen, never a channel whose
  // slug happens to be "settings" (a channel slug this collides with would
  // simply be unreachable by direct URL - the same trade-off channel.json's
  // slug rule already accepts for any other reserved-looking name).
  if (segments.length === 1 && segments[0] === 'settings') return { screen: 'settings' }
  // Same precedence and the same reasoning as /settings above: /logs is
  // always the workspace-level logs screen, never a channel slug "logs".
  if (segments.length === 1 && segments[0] === 'logs') return { screen: 'logs' }
  // Same precedence and the same reasoning again: /jobs is always the
  // workspace-level job-queue screen, never a channel slug "jobs".
  if (segments.length === 1 && segments[0] === 'jobs') return { screen: 'jobs' }
  if (segments.length === 1) return { screen: 'events', channel: segments[0] }
  // Before the editor catch-all, which owns every other path of two or more
  // segments: a fourth level only exists when the third segment is literally
  // "streams" AND a video id follows. `/a/b/streams` with nothing after it is
  // not a stream screen - there is no stream to show - so it falls through to
  // the editor rather than rendering a screen with an undefined video id.
  if (segments.length >= 4 && segments[2] === 'streams') {
    return {
      screen: 'stream', channel: segments[0], event: segments[1], videoId: segments[3],
    }
  }
  return { screen: 'editor', channel: segments[0], event: segments[1] }
}

/** The inverse of parseRoute: the URL path for a route, each segment
 * encoded, so navigate() and breadcrumbs never hand-build a path. */
export function routePath(route: Route): string {
  if (route.screen === 'channels') return '/'
  if (route.screen === 'settings') return '/settings'
  if (route.screen === 'logs') return '/logs'
  if (route.screen === 'jobs') return '/jobs'
  if (route.screen === 'events') return `/${encodeSegment(route.channel ?? '')}`
  if (route.screen === 'stream') {
    return (
      `/${encodeSegment(route.channel ?? '')}/${encodeSegment(route.event ?? '')}` +
      `/streams/${encodeSegment(route.videoId ?? '')}`
    )
  }
  return `/${encodeSegment(route.channel ?? '')}/${encodeSegment(route.event ?? '')}`
}
