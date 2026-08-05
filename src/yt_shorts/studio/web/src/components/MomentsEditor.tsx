import { useEffect, useState } from 'react'
import {
  Alert,
  Badge,
  Box,
  Button,
  Center,
  Group,
  Loader,
  Modal,
  NumberInput,
  Stack,
  Text,
  TextInput,
  Tooltip,
} from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { ApiError, adoptDefaultMoments, getMoments, putMoments } from '../api'
import {
  addOwnRow,
  disableRow,
  MAX_WEIGHT,
  overrideRow,
  parseWeight,
  pendingRemovals,
  removeOwnRow,
  rowsToMarkers,
  setOwnWeight,
  sourceLabel,
  toRows,
  type MarkerRow,
} from '../momentsLexicon'

/**
 * The moments-lexicon editor (stage D2c's own UI), mounted at all three
 * scopes it applies to - workspace (no props, a Card in SettingsScreen),
 * channel (a Tabs.Tab in ChannelScreen) and event (a Drawer in App.tsx) -
 * exactly the way BrandEditor/EventBrandEditor are mounted, and built to the
 * same load/dirty/save shape: load once, edit a local `rows` array, Save
 * PUTs the whole own layer and replaces state from the response.
 *
 * Two things make this different from a brand editor:
 *
 * - Ownership is per MARKER, not per section. `MarkerRow.own` (see
 *   momentsLexicon.ts) says whether the row belongs to THIS scope's own
 *   layer; an inherited row is read-only until "Override" or "Disable"
 *   creates an own entry for it. Every row mutation goes through
 *   momentsLexicon.ts's pure helpers - this component never edits a row's
 *   fields by hand - so the sort order and the own/disabled invariants stay
 *   centralised and unit-tested there, not duplicated here.
 * - Weight 0 is a real, visible state ("disabled here"), not an absence -
 *   rendered struck through with a badge naming which layer disabled it,
 *   never dropped from the list the way the scoring merge drops it.
 *
 * SCROLLING: the built-in default alone is ~39 entries, and this mounts
 * inside three different scroll contexts (NavScreen's own page scroll for
 * Settings/Channel, a Drawer's ScrollArea for the event case) - see
 * CLAUDE.md/MEMORY's standing scrolling requirement. Rather than depend on
 * whichever host happens to give it enough room, this component owns a
 * fixed-height flex column (mirrors LogsScreen's own two-pane Box): the
 * intro/add-marker controls are `flex: 0 0 auto` at the top, the row list is
 * the one `flex: 1 1 auto; minHeight: 0; overflowY: auto` region in the
 * middle, and Save/Adopt stay pinned at the bottom - reachable regardless of
 * how tall the host's own scroll area turns out to be.
 */
export function MomentsEditor({ channel, event }: { channel?: string; event?: string }) {
  const scope = { channel, event }
  // Only a workspace-scope instance (no channel, no event) offers "Adopt the
  // built-in default" - adoptDefaultMoments always writes the WORKSPACE
  // layer regardless of which scope's editor called it (see its own
  // docstring in api.ts), so offering the button anywhere else would adopt
  // into a layer this editor is not even looking at.
  const isWorkspaceScope = !channel && !event

  const [rows, setRows] = useState<MarkerRow[] | null>(null)
  const [savedRows, setSavedRows] = useState<MarkerRow[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  // A malformed moments.json at some OTHER layer than this scope's own (see
  // lexicon_admin.read's `problems`) - the scope still loads and is still
  // editable, so this is a warning banner, never a substitute for loadError.
  // Refreshed from every response that carries a fresh one (load, save,
  // adopt), since a save can change which layers are readable.
  const [problems, setProblems] = useState<string[]>([])

  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  const [newMarker, setNewMarker] = useState('')
  const [newWeightInput, setNewWeightInput] = useState('1')
  const [addError, setAddError] = useState<string | null>(null)

  const [adoptOpen, setAdoptOpen] = useState(false)
  const [adopting, setAdopting] = useState(false)
  const [adoptError, setAdoptError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    getMoments(scope)
      .then((lex) => {
        if (cancelled) return
        const initial = toRows(lex)
        setRows(initial)
        setSavedRows(initial)
        setLoadError(null)
        setProblems(lex.problems)
      })
      .catch((err) => {
        if (cancelled) return
        setLoadError(err instanceof ApiError ? err.message : String(err))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [channel, event])

  const dirty = rows !== null && savedRows !== null && JSON.stringify(rows) !== JSON.stringify(savedRows)
  const removedCount = rows !== null && savedRows !== null ? pendingRemovals(rows, savedRows).length : 0

  function handleOverride(marker: string) {
    setRows((prev) => (prev ? overrideRow(prev, marker) : prev))
  }

  function handleDisable(marker: string) {
    setRows((prev) => (prev ? disableRow(prev, marker) : prev))
  }

  function handleRemove(marker: string) {
    setRows((prev) => (prev ? removeOwnRow(prev, marker) : prev))
    // The row itself vanishes from the list immediately (see removeOwnRow's
    // own docstring on why it cannot show a placeholder), so this toast is a
    // SECOND, non-exclusive cue - the persistent caption near Save below is
    // the one that survives after the toast fades.
    notifications.show({ message: 'Marker removed - Save to apply.', color: 'yellow' })
  }

  function handleWeightChange(marker: string, value: number | string) {
    // Mirrors ClipEditor's handleLeadInChange, not BrandEditor's
    // setOutputField this was originally copied from: that field is
    // `allowDecimal={false}` (an operator is always retyping a whole
    // integer), but this one is `step={0.1}` - rejecting every non-number
    // intermediate value (in particular '', what the box holds for one
    // render while it is being cleared to retype) left the box stuck unable
    // to ever go empty. Parsing with a NaN guard instead lets '' pass
    // through as "no update yet" without touching state, so the box stays
    // clearable.
    const num = typeof value === 'number' ? value : parseFloat(value)
    if (Number.isNaN(num)) return
    setRows((prev) => (prev ? setOwnWeight(prev, marker, num) : prev))
  }

  function handleAdd() {
    if (!rows) return
    setAddError(null)
    const weight = parseWeight(newWeightInput)
    if (weight === null) {
      setAddError(`Enter a weight between 0 and ${MAX_WEIGHT}.`)
      return
    }
    const next = addOwnRow(rows, newMarker, weight)
    if (next === null) {
      setAddError(
        newMarker.trim() === ''
          ? 'Enter a marker.'
          : 'This marker is already one of your own entries - edit it in the list below instead.',
      )
      return
    }
    setRows(next)
    setNewMarker('')
    setNewWeightInput('1')
  }

  async function handleSave() {
    if (!rows || !dirty || saving) return
    setSaving(true)
    setSaveError(null)
    try {
      const lex = await putMoments(scope, rowsToMarkers(rows))
      const next = toRows(lex)
      setRows(next)
      setSavedRows(next)
      setProblems(lex.problems)
      notifications.show({ message: 'Saved.', color: 'green' })
    } catch (err) {
      setSaveError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  async function handleAdopt() {
    setAdopting(true)
    setAdoptError(null)
    try {
      const lex = await adoptDefaultMoments()
      const next = toRows(lex)
      setRows(next)
      setSavedRows(next)
      setProblems(lex.problems)
      setAdoptOpen(false)
      notifications.show({ message: 'Adopted the built-in default as your own.', color: 'green' })
    } catch (err) {
      setAdoptError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setAdopting(false)
    }
  }

  if (loading) {
    return (
      <Center py="xl">
        <Stack align="center" gap="xs">
          <Loader color="steel" />
          <Text size="xs" c="dimmed">
            Loading moments lexicon…
          </Text>
        </Stack>
      </Center>
    )
  }

  if (loadError || !rows) {
    return (
      <Alert color="red" title="Could not load the moments lexicon">
        {loadError} - check that the studio server is still running, then reload this page.
      </Alert>
    )
  }

  return (
    <Box style={{ display: 'flex', flexDirection: 'column', height: '65vh', minHeight: 440 }}>
      {problems.length > 0 ? (
        // A header/footer sibling like everything else outside the middle
        // scrolling Box below (see the component's own SCROLLING note) -
        // `flex: 0 0 auto` so it never grows to eat the list's space, and it
        // sits ABOVE the list so it cannot push Save/Adopt out of reach the
        // way a tall list-bottom banner could.
        <Alert
          color="yellow"
          variant="light"
          title="Some layers could not be read"
          style={{ flex: '0 0 auto', marginBottom: 8 }}
        >
          <Stack gap={2}>
            <Text size="xs">
              The file(s) below could not be read, so they contribute nothing right now - neither
              to the rows shown here nor to scoring - until the file is fixed by hand, or Save at
              that layer overwrites it with a clean one.
            </Text>
            {problems.map((problem) => (
              <Text key={problem} size="xs" ff="monospace">
                {problem}
              </Text>
            ))}
          </Stack>
        </Alert>
      ) : null}

      <Stack gap="xs" style={{ flex: '0 0 auto' }}>
        <Text size="xs" c="dimmed">
          Markers that move a transcript moment's score - weight 0 disables a marker at this
          scope without deleting it from whichever layer set it. Every row below is what
          currently applies here; only the highlighted rows belong to this scope's own layer and
          are editable directly.
        </Text>
        <Group align="flex-end" gap="xs" wrap="wrap">
          <TextInput
            label="New marker"
            placeholder="e.g. yellow flag"
            value={newMarker}
            onChange={(e) => setNewMarker(e.currentTarget.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') handleAdd()
            }}
            style={{ flex: '1 1 220px' }}
          />
          <TextInput
            label="Weight"
            placeholder="0 – 10"
            value={newWeightInput}
            onChange={(e) => setNewWeightInput(e.currentTarget.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') handleAdd()
            }}
            w={110}
          />
          <Button variant="light" color="steel" onClick={handleAdd}>
            Add
          </Button>
        </Group>
        {addError ? (
          <Text size="xs" c="red">
            {addError}
          </Text>
        ) : null}
      </Stack>

      <Box style={{ flex: '1 1 auto', minHeight: 0, overflowY: 'auto', marginTop: 12 }}>
        {rows.length === 0 ? (
          <Text size="sm" c="dimmed">
            No markers at all - this should not happen while the built-in default underlies every
            scope; reload if this persists.
          </Text>
        ) : (
          <Stack gap={4}>
            {rows.map((row) => (
              <MarkerRowView
                key={row.marker}
                row={row}
                onOverride={() => handleOverride(row.marker)}
                onDisable={() => handleDisable(row.marker)}
                onRemove={() => handleRemove(row.marker)}
                onWeightChange={(value) => handleWeightChange(row.marker, value)}
              />
            ))}
          </Stack>
        )}
      </Box>

      {saveError ? (
        <Alert color="red" title="Could not save the moments lexicon" mt="sm">
          {saveError}
        </Alert>
      ) : null}

      {removedCount > 0 ? (
        <Text size="xs" c="yellow" mt="sm" style={{ flex: '0 0 auto' }}>
          {removedCount} removed - Save to apply; each removed marker falls back to its inherited
          or built-in value.
        </Text>
      ) : null}

      <Group justify="space-between" mt="sm" style={{ flex: '0 0 auto' }}>
        {isWorkspaceScope ? (
          <Button variant="default" onClick={() => setAdoptOpen(true)}>
            Adopt the built-in default
          </Button>
        ) : (
          <span />
        )}
        <Button color="steel" onClick={handleSave} loading={saving} disabled={!dirty}>
          Save
        </Button>
      </Group>

      <Modal
        opened={adoptOpen}
        onClose={() => {
          if (!adopting) setAdoptOpen(false)
        }}
        title="Adopt the built-in default"
        closeOnEscape={!adopting}
        closeOnClickOutside={!adopting}
        centered
      >
        <Stack gap="md">
          <Text size="sm">
            Copies every one of the built-in default's ~39 markers into this workspace's own
            layer. They become your own entries here - editable and removable like any other own
            row - and stop tracking any future update to the built-in list; a marker the built-in
            default adds later will not appear here unless added by hand.
          </Text>
          <Text size="sm" c="dimmed">
            This does not change what currently scores a clip - the built-in default already
            underlies every scope. It only makes it editable.
          </Text>
          {dirty ? (
            <Alert color="yellow" variant="light" title="Unsaved changes in this editor">
              You have unsaved changes in this editor - adopting the default will discard them.
              Adopt replaces the rows shown here with the response from the server; any override,
              addition or removal you made in this session that you have not Saved will be lost.
            </Alert>
          ) : null}
          {adoptError ? (
            <Alert color="red" title="Could not adopt the default">
              {adoptError}
            </Alert>
          ) : null}
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setAdoptOpen(false)} disabled={adopting}>
              Cancel
            </Button>
            <Button color="steel" onClick={handleAdopt} loading={adopting}>
              Adopt as my own
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Box>
  )
}

/** One row: the marker (struck through + a "disabled" badge at weight 0),
 * its weight (an editable NumberInput for an own row, plain text plus a
 * source badge for an inherited one), and the row's action - "Override"/
 * "Disable" for an inherited row, an in-place edit plus "Remove" for an own
 * one. Kept local/unexported, same as BrandEditor's own field-level helpers
 * - the shared logic these call into (override/disable/remove/setWeight) all
 * lives in momentsLexicon.ts, this only wires clicks to it. */
function MarkerRowView({
  row,
  onOverride,
  onDisable,
  onRemove,
  onWeightChange,
}: {
  row: MarkerRow
  onOverride: () => void
  onDisable: () => void
  onRemove: () => void
  onWeightChange: (value: number | string) => void
}) {
  return (
    <Group
      justify="space-between"
      wrap="nowrap"
      gap="xs"
      p={6}
      style={{
        borderRadius: 'var(--mantine-radius-sm)',
        background: row.own ? 'var(--mantine-color-dark-7)' : 'transparent',
      }}
    >
      <Group gap={8} wrap="nowrap" style={{ minWidth: 0, flex: '1 1 auto' }}>
        <Text
          size="sm"
          ff="monospace"
          truncate
          td={row.disabled ? 'line-through' : undefined}
          c={row.disabled ? 'dimmed' : undefined}
        >
          {row.marker}
        </Text>
        {row.disabled ? (
          <Badge size="xs" color="dark.3" variant="light">
            {row.own ? 'disabled' : `disabled (${sourceLabel(row.source)})`}
          </Badge>
        ) : !row.own ? (
          <Badge size="xs" color="dark.3" variant="light">
            {sourceLabel(row.source)}
          </Badge>
        ) : null}
      </Group>

      <Group gap={6} wrap="nowrap" style={{ flexShrink: 0 }}>
        {row.own ? (
          <NumberInput
            size="xs"
            w={84}
            step={0.1}
            min={0}
            max={MAX_WEIGHT}
            value={row.weight}
            onChange={onWeightChange}
          />
        ) : (
          <Text size="sm" c="dimmed" ff="monospace" className="tnum" w={84} ta="right">
            {row.weight}
          </Text>
        )}

        {row.own ? (
          <Tooltip label="Drop this scope's own entry - the inherited value or the built-in default takes its place after Save">
            <Button size="xs" variant="subtle" color="red" onClick={onRemove}>
              Remove
            </Button>
          </Tooltip>
        ) : (
          <>
            <Button size="xs" variant="default" onClick={onOverride}>
              Override
            </Button>
            <Button size="xs" variant="subtle" color="red" onClick={onDisable}>
              Disable
            </Button>
          </>
        )}
      </Group>
    </Group>
  )
}
