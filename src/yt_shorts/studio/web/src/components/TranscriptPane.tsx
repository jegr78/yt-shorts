import {
  forwardRef, useEffect, useImperativeHandle, useMemo, useRef, useState,
} from 'react'
import { Badge, Box, Group, ScrollArea, Stack, Text, TextInput } from '@mantine/core'

import { formatStreamDuration } from '../format'
import type { Word } from '../api'

const GROUP_SECONDS = 12

interface Line {
  start: number
  text: string
}

/** What a caller can ask the transcript to do, beyond what its props already
 * drive - see the module docstring below on why this is a ref rather than an
 * effect on `currentTime`. */
export interface TranscriptPaneHandle {
  /** Bring the line covering `seconds` into view. Called on a PICK, never
   * wired to a prop that changes on every tick. */
  scrollToTime: (seconds: number) => void
}

/**
 * The stream transcript, grouped into readable lines and searchable in place.
 *
 * Grouped at the same ~12 s the detector uses for its own line numbering, so a
 * moment's span and a transcript line mean the same unit of text on screen as
 * they do in the model's answer.
 *
 * The whole transcript is in memory - ~2 MB for eight hours, served whole by
 * design - so search is a filter over an array and needs no request, no paging
 * and no debounce beyond what typing already gives.
 *
 * Following a pick is an imperative ref method (`scrollToTime`), not an
 * effect watching `currentTime`. `currentTime` already changes on more than a
 * pick - a plain click on a transcript line calls `onSeek`, which is also how
 * a future playback-position sync would drive it - and an effect cannot tell
 * those apart from a hit-list pick without inventing a second piece of state
 * to carry that distinction anyway. A ref method IS that distinction: only
 * `StreamScreen`'s pick handler calls it, so scrolling only ever happens on a
 * pick, never on a tick, with no risk of a later caller of `setCurrentTime`
 * silently starting to yank the operator's view.
 */
export const TranscriptPane = forwardRef<TranscriptPaneHandle, {
  words: Word[]
  currentTime: number
  onSeek: (seconds: number) => void
}>(function TranscriptPane({ words, currentTime, onSeek }, ref) {
  const [query, setQuery] = useState('')
  // DOM nodes for each line, keyed by its start second, so scrollToTime can
  // find the one to scroll to without re-deriving it from currentTime (which
  // a pick and a future playback tick would otherwise look identical to).
  const lineNodes = useRef(new Map<number, HTMLDivElement>())

  const lines = useMemo(() => {
    const out: Line[] = []
    let current: Line | null = null
    for (const word of words) {
      if (!current || word.end - current.start > GROUP_SECONDS) {
        current = { start: word.start, text: '' }
        out.push(current)
      }
      // Joined with "" because a decoder token carries its own leading space -
      // the same rule captions.py relies on, and why "C.L.R." renders correctly.
      current.text += word.text
    }
    return out
  }, [words])

  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return lines
    return lines.filter((line) => line.text.toLowerCase().includes(needle))
  }, [lines, query])

  // A line waiting to be scrolled to once it exists in the DOM - set when
  // scrollToTime has to clear the search query first (see below), since that
  // clear is asynchronous and the target's node does not exist yet on the
  // same tick scrollToTime runs on.
  const pendingScrollTarget = useRef<number | null>(null)

  useImperativeHandle(ref, () => ({
    scrollToTime(seconds: number) {
      if (lines.length === 0) return
      // The line covering `seconds`, or - for a moment that sits past the
      // transcript's own last word, which happens - the last line at or
      // before it, so a late pick still lands somewhere instead of doing
      // nothing.
      const target = lines.find(
        (line) => seconds >= line.start && seconds < line.start + GROUP_SECONDS,
      ) ?? [...lines].reverse().find((line) => line.start <= seconds) ?? lines[0]

      const visible = shown.some((line) => line.start === target.start)
      if (!visible) {
        // The picked line is hidden by an active search filter. Doing
        // nothing here would look identical to a bug - the operator clicked
        // a hit and the screen did not move - so the filter is cleared
        // rather than left in place with a notice the operator would still
        // have to read and act on: clearing is the one response that both
        // explains itself (the list changes back to everything) and gives
        // the operator what they actually asked for, the picked moment.
        setQuery('')
        pendingScrollTarget.current = target.start
        return
      }
      lineNodes.current.get(target.start)?.scrollIntoView({ block: 'center' })
    },
  }), [lines, shown])

  useEffect(() => {
    // Runs after a query clear triggered by scrollToTime above brings the
    // pending target back into `shown` and its ref callback (below) has
    // attached the node.
    if (pendingScrollTarget.current === null) return
    const node = lineNodes.current.get(pendingScrollTarget.current)
    if (node) {
      node.scrollIntoView({ block: 'center' })
      pendingScrollTarget.current = null
    }
  }, [shown])

  return (
    // minHeight here is NOT the same "0" HitList.tsx's own root Stack uses -
    // that 0 is safe there only because HitList is the SOLE occupant of its
    // Grid.Col, so its own "flex: 1" already receives the column's WHOLE
    // budget and only needs the scroll-container exemption removed one level
    // down (its ScrollArea's own minHeight, see below). This pane instead
    // SHARES its column with StreamPlayer and both StreamTimeline lanes
    // above it, both sized by their own content rather than "flex: 1" - so
    // with a root minHeight of 0 the outer flex algorithm sees no size
    // request from this component AT ALL and hands it whatever those two
    // fixed-ish siblings leave over, which on a real 1280x600 browser was
    // observed to be under 30px total for the search row AND the list
    // combined. Because that outer Stack has the CSS default
    // `overflow: visible`, the shortfall does not get clipped locally where
    // it would at least stay partly reachable - it bleeds all the way up to
    // NavScreen's fillHeight box (the one ancestor in this tree that DOES
    // clip, `overflow: hidden`), past the bottom of the viewport, with no
    // scrollbar there either. 180 is a genuine size request (header row +
    // search input + the ScrollArea's own floor below), not just a token
    // non-zero value: it is what makes the outer flex algorithm take space
    // FROM the player and the lanes instead of silently starving this pane
    // to a fraction of a usable row.
    <Stack gap="xs" style={{ flex: 1, minHeight: 180 }}>
      <Group justify="space-between" wrap="nowrap">
        <TextInput
          placeholder="Search the transcript"
          value={query}
          onChange={(event) => setQuery(event.currentTarget.value)}
          style={{ flex: 1 }}
        />
        <Badge variant="light" color="steel">
          {shown.length} / {lines.length}
        </Badge>
      </Group>
      {lines.length === 0 && (
        <Text c="dimmed" size="sm">This stream has no transcript words.</Text>
      )}
      {lines.length > 0 && shown.length === 0 && (
        <Text c="dimmed" size="sm">Nothing in the transcript matches “{query}”.</Text>
      )}
      {/* This second floor is the same fix HitList.tsx applies to its own
          ScrollArea, for the same reason: a flex item that is itself a
          scroll container gets an automatic minimum size of 0 regardless of
          what its own parent requests. The root Stack's minHeight above
          gets this component a fair SHARE of the column; this is what stops
          that share being spent entirely on the header row and leaving the
          list itself at 0. */}
      <ScrollArea style={{ flex: 1, minHeight: 120 }} offsetScrollbars>
        <Stack gap={2}>
          {shown.map((line) => {
            const active = currentTime >= line.start && currentTime < line.start + GROUP_SECONDS
            return (
              <Box
                key={line.start}
                ref={(el: HTMLDivElement | null) => {
                  if (el) lineNodes.current.set(line.start, el)
                  else lineNodes.current.delete(line.start)
                }}
                onClick={() => onSeek(line.start)}
                role="button"
                tabIndex={0}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    onSeek(line.start)
                  }
                }}
                style={{
                  cursor: 'pointer',
                  padding: '2px 6px',
                  borderRadius: 4,
                  background: active ? 'var(--mantine-color-steel-light)' : undefined,
                }}
              >
                <Text component="span" size="xs" c="dimmed" mr={8}>
                  {formatStreamDuration(line.start)}
                </Text>
                <Text component="span" size="sm">{line.text.trim()}</Text>
              </Box>
            )
          })}
        </Stack>
      </ScrollArea>
    </Stack>
  )
})
