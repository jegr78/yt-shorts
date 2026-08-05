import { useState } from 'react'
import { ActionIcon, Box, Group, Modal, Text } from '@mantine/core'

/**
 * The stream, small by default and expandable to an overlay.
 *
 * A YouTube embed, not a local file: the source is a public stream and this
 * project already downloads only its AUDIO for transcription. Re-fetching hours
 * of video to scrub through it locally would cost the operator's disk and buy
 * nothing the embed does not give.
 *
 * `startAt` is applied through the src, so changing it remounts the iframe and
 * seeks. That is deliberate rather than lazy: the alternative is the YouTube
 * iframe API, a third-party script this project's studio does not load and a
 * network dependency on a screen that is otherwise entirely local.
 */
export function StreamPlayer({ videoId, startAt }: { videoId: string; startAt: number }) {
  const [expanded, setExpanded] = useState(false)
  const src = `https://www.youtube.com/embed/${encodeURIComponent(videoId)}`
    + `?start=${Math.max(Math.floor(startAt), 0)}`

  const frame = (height: number) => (
    <Box
      component="iframe"
      title="Stream"
      src={src}
      allow="accelerometer; encrypted-media; picture-in-picture"
      style={{ width: '100%', height, border: 0, borderRadius: 4, background: '#000' }}
    />
  )

  return (
    <>
      <Group justify="space-between" mb={4}>
        <Text size="xs" c="dimmed">Player</Text>
        <ActionIcon
          size="sm" variant="subtle" aria-label="Expand the player"
          onClick={() => setExpanded(true)}
        >
          ⤢
        </ActionIcon>
      </Group>
      {frame(180)}
      <Modal
        opened={expanded}
        onClose={() => setExpanded(false)}
        size="80%"
        title="Stream"
        centered
      >
        {frame(520)}
      </Modal>
    </>
  )
}
