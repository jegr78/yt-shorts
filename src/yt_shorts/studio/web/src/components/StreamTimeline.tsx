import { useRef } from 'react'
import type { MouseEvent, PointerEvent } from 'react'
import { Box, Group, Stack, Text } from '@mantine/core'

import { formatStreamDuration } from '../format'
import type { Moment } from '../momentList'
import {
  CURVE_STEP_SECONDS, dragSelection, fractionToSeconds, secondsToFraction, zoomAround,
} from '../streamTimeline'
import type { Zoom } from '../streamTimeline'

const ZOOM_SPAN_SECONDS = 180

const CATEGORY_COLOUR: Record<string, string> = {
  start_finish: 'var(--mantine-color-grape-6)',
  incident: 'var(--mantine-color-red-6)',
  highlight: 'var(--mantine-color-yellow-6)',
  race_control: 'var(--mantine-color-blue-6)',
  reaction: 'var(--mantine-color-teal-6)',
}

function colourFor(category: string): string {
  return CATEGORY_COLOUR[category] ?? 'var(--mantine-color-gray-5)'
}

/** Where in a lane's box a pointer event landed, as 0..1. */
function fractionOf(element: HTMLElement, clientX: number): number {
  const box = element.getBoundingClientRect()
  if (box.width <= 0) return 0
  return Math.min(Math.max((clientX - box.left) / box.width, 0), 1)
}

/**
 * The two-lane timeline: an overview strip that LOCATES over the whole
 * stream, and a zoom lane that EDITS a clip window. See this module's own
 * design note in streamTimeline.ts - one full-width slider cannot express a
 * boundary on an eight-hour stream, so the overview only ever moves the zoom
 * window and never sets a selection directly.
 */
export function StreamTimeline({
  duration, activity, moments, zoom, selection, onZoomChange, onSelectionChange,
}: {
  duration: number
  activity: number[]
  moments: Moment[]
  zoom: Zoom
  selection: { start: number; end: number } | null
  onZoomChange: (zoom: Zoom) => void
  onSelectionChange: (selection: { start: number; end: number }) => void
}) {
  const dragStart = useRef<number | null>(null)

  function handleOverviewClick(event: MouseEvent<HTMLDivElement>) {
    const seconds = fractionOf(event.currentTarget, event.clientX) * duration
    onZoomChange(zoomAround(seconds, ZOOM_SPAN_SECONDS, duration))
  }

  function handleLanePointerDown(event: PointerEvent<HTMLDivElement>) {
    event.currentTarget.setPointerCapture(event.pointerId)
    dragStart.current = fractionToSeconds(fractionOf(event.currentTarget, event.clientX), zoom)
  }

  function handleLanePointerMove(event: PointerEvent<HTMLDivElement>) {
    if (dragStart.current === null) return
    const now = fractionToSeconds(fractionOf(event.currentTarget, event.clientX), zoom)
    // dragSelection returns null while the pointer sits on its start pixel -
    // a plain click never gets this far past pointerdown, so there is no
    // zero-width selection to fire and nothing for pointerup to undo. See
    // its own docstring in streamTimeline.ts for why the guard belongs HERE
    // and not on release.
    const next = dragSelection(dragStart.current, now)
    if (next) onSelectionChange(next)
  }

  function handleLanePointerUp(event: PointerEvent<HTMLDivElement>) {
    event.currentTarget.releasePointerCapture(event.pointerId)
    // Only clears the drag's own bookkeeping. This used to also null the
    // selection when the release point matched the drag's start, to catch a
    // plain click that never fired pointermove - but that could not tell a
    // zero-width drag apart from a selection made elsewhere (e.g. picking a
    // hit-list row) and cleared both alike. The zero-width case is now
    // handled where it actually originates, in handleLanePointerMove, so
    // release has nothing left to undo.
    dragStart.current = null
  }

  function handleLanePointerCancel() {
    // A cancelled drag (e.g. the browser takes the pointer for something
    // else mid-drag) fires neither pointerup nor another pointermove -
    // without this, dragStart stays set and the NEXT pointermove (from an
    // unrelated later gesture) would resume as if the old drag were still
    // live, extending a selection from a stale start point.
    dragStart.current = null
  }

  return (
    <Stack gap={4}>
      <Group justify="space-between">
        <Text size="xs" c="dimmed">Whole stream — click to zoom</Text>
        <Text size="xs" c="dimmed">{formatStreamDuration(duration)}</Text>
      </Group>

      {/* Overview: activity as bars, moments as ticks. Locates, never edits. */}
      <Box
        onClick={handleOverviewClick}
        aria-label="Stream overview"
        style={{
          position: 'relative', height: 44, cursor: 'pointer',
          background: 'var(--mantine-color-dark-6)', borderRadius: 4, overflow: 'hidden',
        }}
      >
        {activity.map((value, index) => (
          <Box
            key={index}
            style={{
              position: 'absolute', bottom: 0,
              left: `${(index * CURVE_STEP_SECONDS / Math.max(duration, 1)) * 100}%`,
              width: `${(CURVE_STEP_SECONDS / Math.max(duration, 1)) * 100}%`,
              height: `${Math.max(value, 0) * 100}%`,
              background: 'var(--mantine-color-steel-5)', opacity: 0.55,
            }}
          />
        ))}
        {moments.map((moment) => (
          <Box
            key={`${moment.start}-${moment.end}`}
            title={moment.reason}
            style={{
              position: 'absolute', top: 0, bottom: 0,
              left: `${(moment.start / Math.max(duration, 1)) * 100}%`,
              width: 2, background: colourFor(moment.category),
            }}
          />
        ))}
        {/* Where the zoom lane currently is. */}
        <Box
          style={{
            position: 'absolute', top: 0, bottom: 0,
            left: `${(zoom.start / Math.max(duration, 1)) * 100}%`,
            width: `${((zoom.end - zoom.start) / Math.max(duration, 1)) * 100}%`,
            border: '1px solid var(--mantine-color-yellow-4)',
            background: 'rgba(255,255,255,0.06)',
          }}
        />
      </Box>

      <Group justify="space-between">
        <Text size="xs" c="dimmed">{formatStreamDuration(zoom.start)}</Text>
        <Text size="xs" c="dimmed">Drag to set a clip window</Text>
        <Text size="xs" c="dimmed">{formatStreamDuration(zoom.end)}</Text>
      </Group>

      {/* Zoom lane: edits. Wide enough that a second is several pixels. */}
      <Box
        onPointerDown={handleLanePointerDown}
        onPointerMove={handleLanePointerMove}
        onPointerUp={handleLanePointerUp}
        onPointerCancel={handleLanePointerCancel}
        aria-label="Zoom lane"
        style={{
          position: 'relative', height: 56, cursor: 'ew-resize',
          background: 'var(--mantine-color-dark-7)', borderRadius: 4,
          overflow: 'hidden', touchAction: 'none',
        }}
      >
        {moments
          .filter((moment) => moment.end > zoom.start && moment.start < zoom.end)
          .map((moment) => (
            <Box
              key={`${moment.start}-${moment.end}`}
              title={moment.reason}
              style={{
                position: 'absolute', top: 4, bottom: 4,
                left: `${secondsToFraction(moment.start, zoom) * 100}%`,
                width: `${Math.max(
                  (secondsToFraction(moment.end, zoom)
                    - secondsToFraction(moment.start, zoom)) * 100, 0.4,
                )}%`,
                background: colourFor(moment.category), opacity: 0.5, borderRadius: 2,
              }}
            />
          ))}
        {selection && (
          <Box
            style={{
              position: 'absolute', top: 0, bottom: 0,
              left: `${secondsToFraction(selection.start, zoom) * 100}%`,
              width: `${Math.max(
                (secondsToFraction(selection.end, zoom)
                  - secondsToFraction(selection.start, zoom)) * 100, 0.3,
              )}%`,
              border: '2px solid var(--mantine-color-yellow-4)',
              background: 'rgba(255, 214, 0, 0.15)',
            }}
          />
        )}
      </Box>
    </Stack>
  )
}
