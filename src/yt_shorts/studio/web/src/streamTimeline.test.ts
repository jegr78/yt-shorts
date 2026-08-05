import { describe, expect, it } from 'vitest'
import {
  clampZoom, curveBucket, dragSelection, fractionToSeconds, secondsToFraction, zoomAround,
} from './streamTimeline'

describe('secondsToFraction / fractionToSeconds', () => {
  it('maps the zoom window onto 0..1', () => {
    expect(secondsToFraction(150, { start: 100, end: 200 })).toBeCloseTo(0.5)
    expect(fractionToSeconds(0.5, { start: 100, end: 200 })).toBeCloseTo(150)
  })

  it('round-trips', () => {
    const zoom = { start: 3600, end: 3720 }
    expect(fractionToSeconds(secondsToFraction(3660, zoom), zoom)).toBeCloseTo(3660)
  })

  it('returns 0 rather than dividing by zero on a collapsed window', () => {
    expect(secondsToFraction(10, { start: 10, end: 10 })).toBe(0)
  })
})

describe('clampZoom', () => {
  it('keeps the window inside the stream', () => {
    expect(clampZoom({ start: -50, end: 70 }, 600)).toEqual({ start: 0, end: 120 })
    expect(clampZoom({ start: 580, end: 700 }, 600)).toEqual({ start: 480, end: 600 })
  })

  it('refuses to collapse below the minimum span', () => {
    // A window narrower than this cannot be dragged accurately, and a zero-width
    // one divides by zero in secondsToFraction.
    const zoom = clampZoom({ start: 100, end: 100.5 }, 600, 10)
    expect(zoom.end - zoom.start).toBeCloseTo(10)
  })

  it('gives the whole stream when it is shorter than the requested span', () => {
    expect(clampZoom({ start: 0, end: 120 }, 45)).toEqual({ start: 0, end: 45 })
  })
})

describe('zoomAround', () => {
  it('centres the span on the given second', () => {
    expect(zoomAround(3660, 120, 28800)).toEqual({ start: 3600, end: 3720 })
  })

  it('shifts rather than overhanging at the start of the stream', () => {
    expect(zoomAround(10, 120, 28800)).toEqual({ start: 0, end: 120 })
  })

  it('shifts rather than overhanging at the end of the stream', () => {
    expect(zoomAround(28795, 120, 28800)).toEqual({ start: 28680, end: 28800 })
  })
})

describe('curveBucket', () => {
  it('indexes the activity array by the stream position', () => {
    // The curve is one value per 60 s (moments.activity_curve's step).
    const activity = [0.1, 0.9, 0.4]
    expect(curveBucket(0, activity)).toBe(0)
    expect(curveBucket(90, activity)).toBe(1)
    expect(curveBucket(179, activity)).toBe(2)
  })

  it('clamps past the end instead of reading off the array', () => {
    // The curve can be one bucket longer or shorter than duration/60 implies -
    // activity_curve derives its length from the last WORD, not the stream, and
    // appends a trailing bucket when that lands on an exact multiple.
    expect(curveBucket(10_000, [0.1, 0.2])).toBe(1)
  })

  it('returns -1 for an empty curve so a caller can skip drawing', () => {
    expect(curveBucket(10, [])).toBe(-1)
  })

  it('clamps a negative second to the first bucket, never onto the sentinel', () => {
    // -1 means "no curve at all, draw nothing". An unclamped negative index
    // would return -1 for a curve that DOES have data, so a caller would skip
    // drawing real activity - and a playhead a fraction before zero is an
    // ordinary thing for a scrubber to report, not a contrived input.
    // Removing the lower clamp leaves every other test in this file green.
    expect(curveBucket(-30, [0.1, 0.2])).toBe(0)
    expect(curveBucket(-10_000, [0.1, 0.2])).toBe(0)
  })
})

describe('dragSelection', () => {
  it('orders the two points regardless of drag direction', () => {
    expect(dragSelection(100, 140)).toEqual({ start: 100, end: 140 })
    expect(dragSelection(140, 100)).toEqual({ start: 100, end: 140 })
  })

  it('returns null when the pointer has not moved off the drag start', () => {
    // A plain click's pointerdown and pointerup land on the same pixel, so
    // this is what tells "nothing dragged yet" apart from a real, zero-width
    // range - there is no selection to report, not an empty one.
    expect(dragSelection(100, 100)).toBeNull()
  })
})
