import { useRoute } from './useRoute'
import { ChannelsScreen } from './components/ChannelsScreen'
import { ChannelScreen } from './components/ChannelScreen'
import { JobsScreen } from './components/JobsScreen'
import { LogsScreen } from './components/LogsScreen'
import { SettingsScreen } from './components/SettingsScreen'
import { StreamScreen } from './components/StreamScreen'
import App from './App'

/** The seven-screen client-side router. Reads the current URL (see
 * useRoute) and renders the matching screen:
 *   /                              -> the channel list
 *   /settings                     -> the workspace-level Settings screen (G4)
 *   /logs                         -> the workspace-level Logs screen
 *   /jobs                         -> the workspace-level Jobs screen (the job queue)
 *   /{channel}                    -> that channel's screen (Events + Brand tabs -
 *                                    see ChannelScreen, stage G3b)
 *   /{channel}/{event}            -> the existing editor (App), scoped to that event
 *   /{channel}/{event}/streams/{video_id} -> the stream view (StreamScreen, below)
 * A deep link or reload on any of the seven lands on the right screen: the
 * SPA fallback in api.py serves index.html for a non-/api path, and this
 * reads window.location.pathname to pick the screen. */
export function Root() {
  const route = useRoute()

  if (route.screen === 'editor' && route.channel && route.event) {
    // `key` remounts the editor cleanly when the operator navigates from one
    // event to another, so no per-event state leaks across the switch.
    return <App key={`${route.channel}/${route.event}`} channel={route.channel} event={route.event} />
  }
  if (route.screen === 'settings') {
    return <SettingsScreen />
  }
  if (route.screen === 'logs') {
    return <LogsScreen />
  }
  if (route.screen === 'jobs') {
    return <JobsScreen />
  }
  if (route.screen === 'events' && route.channel) {
    return <ChannelScreen key={route.channel} channel={route.channel} />
  }
  if (route.screen === 'stream' && route.channel && route.event && route.videoId) {
    return (
      <StreamScreen
        key={`${route.channel}/${route.event}/${route.videoId}`}
        channel={route.channel}
        event={route.event}
        videoId={route.videoId}
      />
    )
  }
  return <ChannelsScreen />
}
