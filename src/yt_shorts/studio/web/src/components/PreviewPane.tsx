import { useEffect, useRef, useState } from 'react'
import { Alert, Badge, Box, Group, Slider, Stack, Text } from '@mantine/core'
import { useDebouncedValue } from '@mantine/hooks'
import { fetchPreview, fetchPreviewForWords, PreviewUnavailableError, type Word } from '../api'

interface PreviewPaneProps {
  clipName: string
  duration: number
  at: number
  onAtChange: (value: number) => void
  /** Bumped by the parent whenever saved state changes (title, words,
   * status) so the raster preview refreshes to match once a save lands. */
  refreshToken: number
  /** The transcript words currently staged in the editor - ALWAYS the
   * current local state, whether or not it differs from what is saved
   * (see `wordsDirty`). Sending the current words on every request, dirty
   * or not, means the live-preview (POST) path never needs a second,
   * separately-tracked "what to send" value. */
  words: Word[]
  wordsDirty: boolean
  /** Same idea as `words`/`wordsDirty`, for the title (see api.py's
   * PreviewBody: `title` overrides the hook the same way `words`
   * overrides the caption). */
  title: string
  titleDirty: boolean
}

/**
 * The rendered-look preview (see GET and POST /api/clips/{name}/preview in
 * api.py): a PNG built server-side from raw.mp4, either against the clip's
 * SAVED editorial state (GET, nothing staged) or against words and/or a
 * title the operator is still typing (POST) - this is what lets a
 * correction "look right" before Save, rather than only after it. Saving
 * is still the only thing that persists anything; this pane updating
 * never implies a save happened (see the "Unsaved" marker below, and
 * ClipEditor's own "Unsaved changes" badge, which tracks the same staged
 * state independently).
 *
 * The slider and the staged title/words are all debounced so dragging the
 * slider, or typing a correction, does not fire one ffmpeg
 * frame-extraction per pixel of travel or per keystroke.
 */
export function PreviewPane({
  clipName,
  duration,
  at,
  onAtChange,
  refreshToken,
  words,
  wordsDirty,
  title,
  titleDirty,
}: PreviewPaneProps) {
  const dirty = wordsDirty || titleDirty
  const [debouncedAt] = useDebouncedValue(at, 250)
  const [debouncedWords] = useDebouncedValue(wordsDirty ? words : undefined, 300)
  const [debouncedTitle] = useDebouncedValue(titleDirty ? title : undefined, 300)
  const [imageUrl, setImageUrl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const currentUrl = useRef<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    const request =
      debouncedWords !== undefined || debouncedTitle !== undefined
        ? fetchPreviewForWords(clipName, debouncedAt, debouncedWords ?? words, debouncedTitle)
        : fetchPreview(clipName, debouncedAt)
    request
      .then((url) => {
        if (cancelled) {
          URL.revokeObjectURL(url)
          return
        }
        if (currentUrl.current) URL.revokeObjectURL(currentUrl.current)
        currentUrl.current = url
        setImageUrl(url)
        setError(null)
      })
      .catch((err) => {
        if (cancelled) return
        setImageUrl(null)
        if (err instanceof PreviewUnavailableError) {
          setError(err.message)
        } else {
          setError(`Could not load preview: ${err.message ?? err}`)
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clipName, debouncedAt, debouncedWords, debouncedTitle, refreshToken])

  useEffect(
    () => () => {
      if (currentUrl.current) URL.revokeObjectURL(currentUrl.current)
    },
    [],
  )

  const marks = buildTickMarks(duration)

  return (
    <Stack gap="xs">
      <Group justify="space-between" wrap="nowrap">
        <Text fw={600} size="xs" tt="uppercase" c="dimmed">
          Preview
        </Text>
        {dirty && (
          <Badge color="steel" variant="dot" size="xs">
            Unsaved
          </Badge>
        )}
      </Group>

      {/* The signature element: a restrained broadcast-monitor bezel
          around the 9:16 picture - this is where the boldness lives (see
          theme.ts's own note on why everything else stays quiet). */}
      <Box className="monitorBezel">
        <Box className="monitorScreen">
          <Text className="monitorTimecode tnum" ff="monospace">
            {debouncedAt.toFixed(1)}s
          </Text>
          {!error && imageUrl && (
            <img
              src={imageUrl}
              alt={`Preview at ${debouncedAt.toFixed(1)}s`}
              style={{
                width: '100%',
                height: '100%',
                objectFit: 'contain',
                opacity: loading ? 0.55 : 1,
              }}
            />
          )}
          {!error && !imageUrl && (
            <Text size="xs" c="dimmed">
              {loading ? 'Loading…' : 'No preview yet'}
            </Text>
          )}
          {error && (
            <Text size="xs" c="dimmed" tt="uppercase" ff="monospace" className="tnum">
              No signal
            </Text>
          )}
        </Box>
      </Box>

      {error && (
        <Alert color="steel" title="No preview available">
          This clip has no downloaded raw video yet, so there is nothing to draw a preview on.
          Render this clip at least once (see the render buttons below) - that also produces the
          finished short.
        </Alert>
      )}

      {duration > 0 ? (
        <Box className="timingStrip">
          <Slider
            value={at}
            onChange={onAtChange}
            min={0}
            max={duration}
            step={0.1}
            color="steel"
            size="sm"
            marks={marks}
            label={(value) => `${value.toFixed(1)}s`}
            styles={{
              markLabel: { display: 'none' },
              label: { fontFamily: 'var(--mantine-font-family-monospace)' },
            }}
          />
          <Group justify="space-between" mt={2}>
            <Text size="xs" c="dimmed" ff="monospace" className="tnum">
              0:00.0
            </Text>
            <Text size="xs" c="dimmed" ff="monospace" className="tnum">
              {duration.toFixed(1)}s
            </Text>
          </Group>
        </Box>
      ) : (
        <Text size="xs" c="dimmed">
          No duration recorded for this clip.
        </Text>
      )}

      <Text size="xs" c="dimmed">
        {dirty
          ? 'Reflects your unsaved edits - Save still persists nothing until you click it.'
          : 'Reflects the last saved correction.'}
      </Text>
    </Stack>
  )
}

/** Tick marks for the timing-strip scrubber, spaced so a long clip does
 * not end up with hundreds of unreadable ticks. Unlabelled (markLabel is
 * hidden via styles above) - they are a rhythm cue, like the second marks
 * on a timing screen's own scrubber, not a second set of numbers to read
 * next to the slider's own drag label. */
function buildTickMarks(duration: number): { value: number }[] {
  if (duration <= 0) return []
  const interval = duration <= 20 ? 1 : duration <= 60 ? 5 : duration <= 300 ? 15 : 30
  const marks: { value: number }[] = []
  for (let t = 0; t <= duration; t += interval) {
    marks.push({ value: t })
  }
  return marks
}
