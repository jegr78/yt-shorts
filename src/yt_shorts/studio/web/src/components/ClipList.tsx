import {
  Badge,
  Group,
  ScrollArea,
  Stack,
  Switch,
  Text,
  UnstyledButton,
  VisuallyHidden,
} from '@mantine/core'
import type { ClipSummary, ClipStatus } from '../api'
import { formatDuration } from '../format'

/** The condensed face, scoped to the tower's own labels - its column
 * headers and each row's clip title - per theme.ts's own note on why
 * this is not the general heading font. Condensed earns its place here
 * specifically: it lets more of a clip's title stay legible before
 * truncating in a narrow, dense list. */
const TOWER_FONT = '"IBM Plex Sans Condensed", "IBM Plex Sans", sans-serif'

/** The status column's fixed width, shared by the header cell and every
 * row's badge so they line up. Sized for STATUS_CODE's four-character
 * codes in the monospace face plus the coloured dot - well under the old
 * spelled-out column's 8.5em, and that freed width is exactly what goes
 * to the title column below (see the module docstring on why the title
 * is the one column allowed to grow). */
const STATUS_COLUMN_WIDTH = '5.6em'

const STATUS_COLOR: Record<ClipStatus, string> = {
  candidate: 'amber',
  kept: 'green',
  discarded: 'dark.3',
}

const STATUS_LABEL: Record<ClipStatus, string> = {
  candidate: 'Candidate',
  kept: 'Kept',
  discarded: 'Discarded',
}

/** The tower's visible status text. All six rows of the real event this
 * column was built against read "Candidate" - a spelled-out word that is
 * identical on every row earns its keep on a live timing screen no better
 * than it does here, and it was costing the title column the width an
 * operator actually needs to read a clip by (see the module docstring).
 * Motorsport timing towers abbreviate status as a matter of course, so a
 * short uppercase code is idiomatic here, not a compromise - fixed-width
 * so the coloured dot beside it lines up down the column the same way the
 * duration figures do. This is a supplementary SHORT label, never a
 * replacement for text: STATUS_LABEL above still carries the full word,
 * and statusAccessibleLabel below is what assistive technology reads. */
const STATUS_CODE: Record<ClipStatus, string> = {
  candidate: 'CAND',
  kept: 'KEPT',
  discarded: 'DISC',
}

/** "83.4" -> "1:23.4" - tabular so a changing duration never reflows the
 * column next to it (see theme.ts's fontFamilyMonospace and index.css's
 * .tnum). Never negative and never NaN in practice (clips.json always
 * carries a >=0 duration), but a defensive fallback keeps a malformed one
 * from rendering "NaN:NaN". */

interface ClipListProps {
  clips: ClipSummary[]
  selectedName: string | null
  onSelect: (name: string) => void
  showDiscarded: boolean
  onShowDiscardedChange: (value: boolean) => void
}

/** The full accessible description of a clip's state, read by assistive
 * technology instead of the visible STATUS_CODE badge (see the row JSX:
 * the badge is `aria-hidden` and this string lives in a VisuallyHidden
 * span right beside it) - abbreviating the badge to a short code must
 * never mean a screen reader hears less than a sighted operator reads
 * elsewhere on the row, and this is what keeps "the accessible name
 * still says what the state is" true. Folds the "no rendered short"
 * marker into the SAME string rather than a second line, for the same
 * reason the old spelled-out badge did - see the module docstring below
 * on why a timing tower cannot afford two lines per row. The "has hand
 * corrections" marker is NOT folded in here - see the small "E" marker
 * in the row itself, which already carries its own accessible text via
 * its `title` attribute. */
function statusAccessibleLabel(clip: ClipSummary): string {
  return [STATUS_LABEL[clip.status], !clip.has_short ? 'Not rendered' : null]
    .filter((part): part is string => Boolean(part))
    .join(' · ')
}

/**
 * The clip list as a timing tower: dense, ONE ROW PER CLIP - position,
 * title, status and duration all on the same line, the way a live timing
 * screen reads. A two-line row (title on one line, status wrapped to a
 * second) halves how many clips fit in the column and defeats the whole
 * point of a scannable tower, so every row here is a single `nowrap`
 * Group: the title is the only element allowed to shrink/truncate freely,
 * the status badge truncates its own (already-compact) text past a fixed
 * width, and the position and duration columns stay fixed so the tabular
 * duration figures line up down the column. Colour is never the only
 * signal (see theme.ts's own docstring and statusAccessibleLabel above):
 * every status and marker also carries its name in text, inside the
 * row's accessible name, not just a hover-only tooltip.
 */
export function ClipList({
  clips,
  selectedName,
  onSelect,
  showDiscarded,
  onShowDiscardedChange,
}: ClipListProps) {
  const visible = showDiscarded ? clips : clips.filter((c) => c.status !== 'discarded')

  return (
    <Stack h="100%" gap="sm">
      <Switch
        label="Show discarded clips"
        checked={showDiscarded}
        onChange={(event) => onShowDiscardedChange(event.currentTarget.checked)}
        size="sm"
      />
      <Group justify="space-between" wrap="nowrap">
        <Text size="xs" c="dimmed">
          {visible.length} of {clips.length} clip{clips.length === 1 ? '' : 's'}
        </Text>
      </Group>

      {visible.length > 0 && (
        <Group
          gap="xs"
          px="xs"
          wrap="nowrap"
          style={{ borderBottom: '1px solid var(--mantine-color-dark-6)', paddingBottom: 4 }}
        >
          <Text
            size="xs"
            c="dimmed"
            fw={600}
            tt="uppercase"
            style={{ width: '2.4em', fontFamily: TOWER_FONT }}
          >
            #
          </Text>
          <Text size="xs" c="dimmed" fw={600} tt="uppercase" style={{ flex: 1, fontFamily: TOWER_FONT }}>
            Clip
          </Text>
          <Text
            size="xs"
            c="dimmed"
            fw={600}
            tt="uppercase"
            style={{ width: STATUS_COLUMN_WIDTH, fontFamily: TOWER_FONT }}
          >
            Status
          </Text>
          <Text size="xs" c="dimmed" fw={600} tt="uppercase" ff="monospace">
            Dur
          </Text>
        </Group>
      )}

      <ScrollArea style={{ flex: 1 }} offsetScrollbars>
        <Stack gap={2}>
          {visible.map((clip, index) => {
            const selected = clip.name === selectedName
            return (
              <UnstyledButton
                key={clip.name}
                onClick={() => onSelect(clip.name)}
                px="xs"
                py={6}
                style={{
                  borderRadius: 'var(--mantine-radius-sm)',
                  borderLeft: `3px solid ${
                    selected ? 'var(--mantine-color-steel-4)' : 'transparent'
                  }`,
                  backgroundColor: selected
                    ? 'var(--mantine-color-dark-7)'
                    : 'transparent',
                  opacity: clip.status === 'discarded' ? 0.6 : 1,
                }}
              >
                <Group gap="xs" wrap="nowrap" align="center">
                  <Text
                    size="xs"
                    c="dimmed"
                    ff="monospace"
                    className="tnum"
                    style={{ width: '2.2em', flexShrink: 0 }}
                  >
                    {String(index + 1).padStart(2, '0')}
                  </Text>
                  {clip.has_edit && (
                    <Text
                      size="xs"
                      c="violet.2"
                      fw={700}
                      title="Hand-edited"
                      style={{ flexShrink: 0, width: '1em' }}
                    >
                      E
                    </Text>
                  )}
                  {clip.has_upload && (
                    <Text
                      size="xs"
                      c="green.4"
                      fw={700}
                      title="Uploaded to YouTube"
                      style={{ flexShrink: 0, width: '1em' }}
                    >
                      ↑
                    </Text>
                  )}
                  <Text
                    size="sm"
                    fw={selected ? 600 : 400}
                    truncate
                    style={{ flex: 1, minWidth: 0, fontFamily: TOWER_FONT }}
                  >
                    {clip.effective_title || clip.name}
                  </Text>
                  {/* The coloured dot plus a short uppercase code, in the
                      same monospace face as the duration column - the
                      idiomatic timing-tower way to show a status that
                      reads identically on most rows without spending the
                      title column's width on a spelled-out word. Colour
                      still never carries this alone: the code is real
                      text, `aria-hidden` only because the VisuallyHidden
                      span right after it already gives assistive
                      technology the full word (and the "not rendered"
                      state), so nothing is lost, just not doubled. */}
                  <Badge
                    color={STATUS_COLOR[clip.status]}
                    variant="dot"
                    size="xs"
                    tt="none"
                    ff="monospace"
                    aria-hidden="true"
                    style={{ width: STATUS_COLUMN_WIDTH, flexShrink: 0 }}
                  >
                    {STATUS_CODE[clip.status]}
                  </Badge>
                  <VisuallyHidden>{statusAccessibleLabel(clip)}</VisuallyHidden>
                  <Text
                    size="xs"
                    ff="monospace"
                    className="tnum"
                    c="dimmed"
                    style={{ flexShrink: 0, width: '3.6em', textAlign: 'right' }}
                  >
                    {formatDuration(clip.duration)}
                  </Text>
                </Group>
              </UnstyledButton>
            )
          })}
          {visible.length === 0 && clips.length === 0 && (
            <Text size="sm" c="dimmed" p="xs">
              No clips harvested yet for this event. Run <code>bin/yt-shorts harvest</code> for it,
              then reload this page.
            </Text>
          )}
          {visible.length === 0 && clips.length > 0 && (
            <Text size="sm" c="dimmed" p="xs">
              Every clip in this event is discarded. Turn on &ldquo;Show discarded clips&rdquo;
              above to see them.
            </Text>
          )}
        </Stack>
      </ScrollArea>
    </Stack>
  )
}
