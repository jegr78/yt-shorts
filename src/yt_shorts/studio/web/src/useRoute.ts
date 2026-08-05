/** The studio's hand-rolled client-side router - no dependency added. It
 * reads window.location.pathname, drives navigation with history.pushState,
 * and re-renders on both a pushState (via a private subscriber set) and the
 * browser's own popstate (Back/Forward, and a reload lands on the same
 * path). parseRoute (see scopedApi.ts) turns the path into one of the seven
 * screens. Exports only functions/hooks, so Vite's fast-refresh boundary
 * stays component-only (same as hooks/useJobPolling.ts). */

import { useSyncExternalStore } from 'react'
import { parseRoute, type Route } from './scopedApi'

const listeners = new Set<() => void>()

function notify(): void {
  for (const listener of listeners) listener()
}

/** Navigate to an in-app path, pushing a history entry so Back/Forward
 * work, then re-rendering every subscriber. A no-op push (same path) still
 * re-renders, which is harmless. */
export function navigate(path: string): void {
  if (path !== window.location.pathname) {
    window.history.pushState({}, '', path)
  }
  notify()
}

function subscribe(callback: () => void): () => void {
  listeners.add(callback)
  window.addEventListener('popstate', callback)
  return () => {
    listeners.delete(callback)
    window.removeEventListener('popstate', callback)
  }
}

function getSnapshot(): string {
  return window.location.pathname
}

/** The current route, recomputed whenever the path changes. */
export function useRoute(): Route {
  const pathname = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
  return parseRoute(pathname)
}
