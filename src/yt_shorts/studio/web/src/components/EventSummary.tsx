import { Group, Stack, Text } from '@mantine/core'
import type { ClipSummary } from '../api'

interface EventSummaryProps {
  clips: ClipSummary[]
}

interface RowProps {
  label: string
  value: number
}

function Row({ label, value }: RowProps) {
  return (
    <Group justify="space-between" wrap="nowrap" gap="xl">
      <Text size="sm" c="dimmed">
        {label}
      </Text>
      <Text size="sm" fw={600} ff="monospace" className="tnum">
        {value}
      </Text>
    </Group>
  )
}

/**
 * The right pane's resting state, with no clip selected. Previously one
 * line of text ("Select a clip...") in an otherwise empty pane - useful
 * information an operator would otherwise have to read off the tower
 * column by eye, clip by clip. This is quiet and typographic on purpose
 * (no hero, no illustration - see the redesign report): a session-summary
 * readout in the same label/tabular-value shape as the tower's own rows
 * (see ClipList.tsx), not a second visual system.
 *
 * Top-aligned rather than vertically centred: a `12vh` top margin plus
 * `mx="auto"` read as a floating centred card with a large dead area
 * beneath it (flagged in design review), which fights the "typographic
 * page, not a hero" intent above more than it serves it. Left-aligned at
 * the pane's own top, like the rest of the app's content, it reads as
 * the page it is.
 *
 * The "no raw video" count is the one genuinely actionable number here:
 * it is exactly the set of clips the preview routes would 409 on (see
 * PreviewPane's own docstring on that 409) and that a render would still
 * have to fetch before it could do anything else with them - not just a
 * curiosity, but what an operator would need to go do next.
 */
export function EventSummary({ clips }: EventSummaryProps) {
  const total = clips.length
  const kept = clips.filter((c) => c.status === 'kept').length
  const discarded = clips.filter((c) => c.status === 'discarded').length
  const candidate = clips.filter((c) => c.status === 'candidate').length
  const noRaw = clips.filter((c) => !c.has_raw).length

  return (
    <Stack gap="xl" maw={420}>
      <Stack gap={2}>
        <Text size="xs" c="dimmed" tt="uppercase" fw={600} ff="monospace">
          This event
        </Text>
        <Text size="xl" fw={700}>
          {total} clip{total === 1 ? '' : 's'} harvested
        </Text>
      </Stack>

      {total > 0 && (
        <Stack gap={6}>
          <Row label="Kept" value={kept} />
          <Row label="Candidates" value={candidate} />
          <Row label="Discarded" value={discarded} />
          <Row label="No raw video - cannot preview yet" value={noRaw} />
        </Stack>
      )}

      <Text size="xs" c="dimmed">
        Select a clip from the list on the left to review it.
      </Text>
    </Stack>
  )
}
