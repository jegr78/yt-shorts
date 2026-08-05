import { useState } from 'react'
import {
  Alert, Badge, Box, Chip, Group, ScrollArea, SegmentedControl, Stack, Text,
} from '@mantine/core'

import { formatStreamDuration } from '../format'
import { CATEGORY_ORDER, categoryLabel, filterMoments, sortMoments } from '../momentList'
import type { Moment, SortKey } from '../momentList'

/**
 * The detected moments, and the two things about them the operator must not
 * have to dig for.
 *
 * `engine` is shown because a lexicon-fallback list looks identical to a model
 * list on screen, and the two are not comparable in quality - this project has
 * already paid once for a degradation that was technically logged and
 * practically invisible. `missingWindows` is shown for the same reason: a
 * failed window is an hour of the stream nobody looked at, and an absent
 * moment is indistinguishable from an uneventful hour unless it is said.
 */
export function HitList({
  moments, engine, missingWindows, selectedStart, onPick,
}: {
  moments: Moment[]
  engine: string | null
  missingWindows: number[]
  selectedStart: number | null
  onPick: (moment: Moment) => void
}) {
  const [sort, setSort] = useState<SortKey>('score')
  const [categories, setCategories] = useState<string[]>([])

  const shown = sortMoments(filterMoments(moments, new Set(categories)), sort)

  return (
    <Stack gap="xs" style={{ flex: 1, minHeight: 0 }}>
      <Group justify="space-between" wrap="nowrap">
        <SegmentedControl
          size="xs"
          value={sort}
          onChange={(value) => setSort(value as SortKey)}
          data={[{ label: 'Strongest', value: 'score' }, { label: 'In order', value: 'time' }]}
        />
        <Badge variant="light" color="steel">{shown.length} / {moments.length}</Badge>
      </Group>

      <Chip.Group multiple value={categories} onChange={setCategories}>
        <Group gap={4}>
          {CATEGORY_ORDER.map((category) => (
            <Chip key={category} value={category} size="xs" variant="outline">
              {categoryLabel(category)}
            </Chip>
          ))}
        </Group>
      </Chip.Group>

      {engine === 'lexicon' && (
        <Alert color="yellow" title="Found without a model">
          These came from the offline lexicon engine, which is markedly weaker.
          Configure an API key to use the model.
        </Alert>
      )}
      {engine === null && moments.length === 0 && (
        <Text size="sm" c="dimmed">
          This stream has not been analysed yet. You can still pick a window by
          hand in the lane below.
        </Text>
      )}
      {missingWindows.length > 0 && (
        <Alert color="orange" title={`${missingWindows.length} window(s) failed`}>
          Those parts of the stream were not analysed at all — an absent moment
          there means nobody looked, not that nothing happened.
        </Alert>
      )}

      {/* minHeight is deliberate, not decoration: a flex item that is itself
          a scroll container gets an automatic minimum size of 0 (the CSS
          scroll-container exemption), while the header above it (the sort
          control, the category chips, and - worst case - BOTH alerts at
          once) does not. With no floor, a short viewport and a fully loaded
          header starve this area to 0px and the list becomes entirely
          unreachable - measured in a real 1280x600 browser with both alerts
          showing, not assumed. 120px keeps a handful of rows reachable even
          in that worst case, at the cost of the header being what shrinks
          first instead. */}
      <ScrollArea style={{ flex: 1, minHeight: 120 }} offsetScrollbars>
        <Stack gap={4}>
          {shown.map((moment) => (
            <Box
              key={`${moment.start}-${moment.end}`}
              onClick={() => onPick(moment)}
              // A clickable Box renders as a <div> with no built-in keyboard
              // affordance - role+tabIndex+the Enter/Space handler is what
              // makes it reachable and activatable from a keyboard, same
              // pattern as TranscriptPane's lines and StreamPanel's stream
              // title below.
              role="button"
              tabIndex={0}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault()
                  onPick(moment)
                }
              }}
              style={{
                cursor: 'pointer', padding: 6, borderRadius: 4,
                background: selectedStart === moment.start
                  ? 'var(--mantine-color-steel-light)' : 'var(--mantine-color-dark-6)',
              }}
            >
              <Group justify="space-between" wrap="nowrap" gap="xs">
                <Text size="xs" c="dimmed">{formatStreamDuration(moment.start)}</Text>
                <Badge size="xs" variant="light">{categoryLabel(moment.category)}</Badge>
                <Text size="xs" fw={600}>{moment.score.toFixed(1)}</Text>
              </Group>
              <Text size="sm" mt={2}>{moment.reason}</Text>
              {moment.hook_suggestion && (
                <Text size="xs" c="dimmed" mt={2}>“{moment.hook_suggestion}”</Text>
              )}
            </Box>
          ))}
        </Stack>
      </ScrollArea>
    </Stack>
  )
}
