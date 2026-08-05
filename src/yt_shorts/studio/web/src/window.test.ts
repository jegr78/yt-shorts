import { describe, expect, it } from 'vitest'
import type { ClipDetail } from './api'
import { withWindow } from './window'

// Only the window fields matter to withWindow; the rest is cast in as a
// minimal ClipDetail. `base` is a full GET result (has the window fields);
// `patchResult` is a PATCH response (which does NOT echo them back).
const base = {
  detected_window: [92, 104],
  effective_window: [92, 104],
} as unknown as ClipDetail

const patchResult = {
  // a PATCH response carries no window fields, or stale ones - either way
  // withWindow must not trust them
  detected_window: undefined,
  effective_window: undefined,
} as unknown as ClipDetail

describe('withWindow', () => {
  it('restores the detected window from the base, not the patch response', () => {
    expect(withWindow(patchResult, base).detected_window).toEqual([92, 104])
  })

  it('applies an explicit effective-window override', () => {
    expect(withWindow(patchResult, base, [95, 108]).effective_window).toEqual([95, 108])
  })

  it('falls back to the base effective window when no override is given', () => {
    expect(withWindow(patchResult, base).effective_window).toEqual([92, 104])
  })
})
