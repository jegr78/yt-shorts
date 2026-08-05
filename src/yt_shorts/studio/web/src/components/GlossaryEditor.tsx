import { useEffect, useState } from 'react'
import {
  Alert,
  Badge,
  Box,
  Button,
  Center,
  Group,
  Loader,
  Select,
  Stack,
  Text,
  TextInput,
  Tooltip,
} from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { ApiError, getGlossary, getTracks, putGlossary, type LayerSource, type TrackRow } from '../api'
import {
  addOwnReplacementRow,
  addOwnTermRow,
  disableRow,
  enableRow,
  incompleteReplacements,
  normaliseKey,
  pendingRemovals,
  removeOwnRow,
  rowsToOwn,
  setReplacementText,
  toReplacementRows,
  toTermRows,
  overrideRow,
  type ReplacementRow,
  type TermRow,
} from '../glossaryLayers'
import { sourceLabel } from '../momentsLexicon'

/**
 * The glossary editor, mounted at all three writable scopes exactly the way
 * MomentsEditor is - workspace (a Card in SettingsScreen), channel (a
 * Tabs.Tab in ChannelScreen) and event (a Drawer in App.tsx) - and built to
 * the same load/dirty/save shape: load once, edit local row arrays, Save PUTs
 * the whole own layer and replaces state from the response.
 *
 * What differs from the moments editor: TWO lists, not one.
 *
 * - Terms bias the decoder BEFORE it errs (glossary.hotwords). A term is
 *   either on or off; there is nothing to type but the spelling, and the
 *   spelling matters because it is literally what the decoder is biased
 *   toward.
 * - Corrections fix what it already got wrong, AFTER it errs
 *   (glossary.apply). A correction is a pair: what was heard, what it should
 *   say.
 *
 * Ownership is per ENTRY in both lists, not per section: an inherited row is
 * read-only until Override or Disable creates an own entry at this scope. A
 * disabled row stays visible, struck through, with a badge naming the layer
 * that disabled it - never dropped, the way the transcription merge drops it.
 * Every row mutation goes through glossaryLayers.ts's pure helpers, so the
 * sort order and the own/disabled invariants stay unit-tested in one place.
 *
 * At EVENT scope only, a Circuit selector picks one venue from the shipped
 * registry (tracks.py's ~41 packs); that pack's corner names then apply to
 * this event, and only to it - see GlossaryLayers.track's docstring in
 * api.ts for why a correction like "carousel" -> "Karussell" is safe scoped
 * to one event and would not be as a global rule. The pack is REFERENCED by
 * id, not copied into this event's own layer: an operator who wants to
 * change one of its entries overrides or disables that row here like any
 * other inherited row (it arrives with `source: 'track'`), rather than
 * editing the shipped pack itself. Because `putGlossary`'s `track` is not
 * sticky - a PUT overwrites the whole own layer - `handleSave` always sends
 * the current selector value back, or an unrelated term edit would silently
 * clear the event's venue.
 *
 * SCROLLING: a selected track pack alone (e.g. the Nürburgring Nordschleife's
 * ~32 terms, see tracks.py) runs to dozens of rows across the two lists
 * (deliberately not stated as an exact count here - the argument
 * holds at any size, and a number in a comment goes stale silently), and
 * this mounts inside three different scroll contexts (NavScreen's page scroll
 * for Settings/Channel, a Drawer's ScrollArea for the event case) - see
 * CLAUDE.md's standing scrolling requirement. Rather than depend on whichever
 * host gives it room, this owns a fixed-height flex column: the selector (at
 * event scope) and the intro paragraph are both `flex: 0 0 auto` above the
 * scrolling region. Both add-control groups live INSIDE the one
 * `flex: 1 1 auto; minHeight: 0; overflowY: auto` Box, each nested directly
 * above the list it belongs to, and Save stays pinned at the bottom, below
 * that Box. This deliberately diverges from MomentsEditor, which pins its
 * single add row above its single list, outside the scrolling region: with
 * two lists here, an add row belongs grouped with the list it adds to, not
 * stacked with a second, unrelated add row at the top - and every element
 * (the selector, both add rows, both lists, Save) still stays reachable by
 * scrolling, which is what the standing requirement actually asks for, not
 * any particular placement of the add controls.
 */
export function GlossaryEditor({ channel, event }: { channel?: string; event?: string }) {
  const scope = { channel, event }

  const [termRows, setTermRows] = useState<TermRow[] | null>(null)
  const [savedTermRows, setSavedTermRows] = useState<TermRow[] | null>(null)
  const [repRows, setRepRows] = useState<ReplacementRow[] | null>(null)
  const [savedRepRows, setSavedRepRows] = useState<ReplacementRow[] | null>(null)
  const [tracks, setTracks] = useState<TrackRow[]>([])
  const [track, setTrack] = useState<string | null>(null)
  const [savedTrack, setSavedTrack] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [problems, setProblems] = useState<string[]>([])

  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  const [newTerm, setNewTerm] = useState('')
  const [termError, setTermError] = useState<string | null>(null)
  const [newFrom, setNewFrom] = useState('')
  const [newTo, setNewTo] = useState('')
  const [repError, setRepError] = useState<string | null>(null)

  function applyState(layers: Parameters<typeof toTermRows>[0]) {
    const terms = toTermRows(layers)
    const reps = toReplacementRows(layers)
    setTermRows(terms)
    setSavedTermRows(terms)
    setRepRows(reps)
    setSavedRepRows(reps)
    setTrack(layers.track)
    setSavedTrack(layers.track)
    setProblems(layers.problems)
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    getGlossary(scope)
      .then((layers) => {
        if (cancelled) return
        applyState(layers)
        setLoadError(null)
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

  useEffect(() => {
    // Only an event picks a venue, so only an event scope needs the list.
    if (!channel || !event) return
    let cancelled = false
    getTracks()
      .then((rows) => {
        if (!cancelled) setTracks(rows)
      })
      .catch(() => {
        // A failed registry load leaves the selector empty rather than
        // breaking the editor: the rows below still load and save, and the
        // operator can still edit everything except the venue. Surfacing it
        // as a second error banner would bury the one that matters.
        if (!cancelled) setTracks([])
      })
    return () => {
      cancelled = true
    }
  }, [channel, event])

  const dirty =
    termRows !== null &&
    savedTermRows !== null &&
    repRows !== null &&
    savedRepRows !== null &&
    (JSON.stringify(termRows) !== JSON.stringify(savedTermRows) ||
      JSON.stringify(repRows) !== JSON.stringify(savedRepRows) ||
      track !== savedTrack)

  const removedCount =
    termRows && savedTermRows && repRows && savedRepRows
      ? pendingRemovals(termRows, savedTermRows).length +
        pendingRemovals(repRows, savedRepRows).length
      : 0

  // Own, enabled corrections with a blank target - the sequence Enable can
  // produce (see glossaryLayers.incompleteReplacements). Sending one of these
  // as-is gets a 400 the operator did not cause and cannot interpret, so Save
  // is gated on this being empty and the offending rows are named below.
  const incompleteKeys = repRows ? incompleteReplacements(repRows) : []
  // What the caption below actually SHOWS: the offending rows' raw `from`
  // text, not their normalised `key` - the list renders `from`, so naming a
  // row by its normalised key can print a string (stripped of case and
  // punctuation) that appears nowhere on screen for a key an operator typed
  // with any of that in it.
  const incompleteLabels = repRows
    ? repRows.filter((row) => incompleteKeys.includes(row.key)).map((row) => row.from)
    : []

  function handleAddTerm() {
    if (!termRows) return
    setTermError(null)
    const next = addOwnTermRow(termRows, newTerm)
    if (next === null) {
      setTermError(
        newTerm.trim() === ''
          ? 'Enter a term.'
          : 'This term is already one of your own entries - edit it in the list below instead.',
      )
      return
    }
    setTermRows(next)
    setNewTerm('')
  }

  function handleAddReplacement() {
    if (!repRows) return
    setRepError(null)
    const next = addOwnReplacementRow(repRows, newFrom, newTo)
    if (next === null) {
      if (newTo.trim() === '') setRepError('Enter what it should say.')
      else if (newFrom.trim() === '') setRepError('Enter what the decoder heard.')
      else if (normaliseKey(newFrom) === '')
        setRepError('Enter a heard phrase with at least one letter or number - punctuation alone is not enough.')
      else
        setRepError(
          'This heard phrase is already one of your own entries - edit it in the list below instead.',
        )
      return
    }
    setRepRows(next)
    setNewFrom('')
    setNewTo('')
  }

  function noteRemoval() {
    // The row vanishes immediately (see removeOwnRow on why it cannot show a
    // placeholder), so this toast is a second, non-exclusive cue - the
    // caption near Save is the one that survives after it fades.
    notifications.show({ message: 'Entry removed - Save to apply.', color: 'yellow' })
  }

  async function handleSave() {
    if (!termRows || !repRows || !dirty || saving || incompleteKeys.length > 0) return
    setSaving(true)
    setSaveError(null)
    try {
      const payload = rowsToOwn(termRows, repRows)
      const layers = await putGlossary(scope, payload.terms, payload.replacements, track)
      applyState(layers)
      notifications.show({ message: 'Saved.', color: 'green' })
    } catch (err) {
      setSaveError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  if (loading) {
    return (
      <Center py="xl">
        <Stack align="center" gap="xs">
          <Loader color="steel" />
          <Text size="xs" c="dimmed">
            Loading glossary…
          </Text>
        </Stack>
      </Center>
    )
  }

  if (loadError || !termRows || !repRows) {
    return (
      <Alert color="red" title="Could not load the glossary">
        {loadError} - check that the studio server is still running, then reload this page.
      </Alert>
    )
  }

  return (
    <Box style={{ display: 'flex', flexDirection: 'column', height: '70vh', minHeight: 460 }}>
      {channel && event ? (
        <Stack gap={4} style={{ flex: '0 0 auto', marginBottom: 8 }}>
          <Select
            label="Circuit"
            placeholder="No circuit selected"
            data={tracks.map((row) => ({ value: row.id, label: row.name }))}
            value={track}
            onChange={setTrack}
            clearable
            searchable
            nothingFoundMessage="No circuit of that name"
          />
          <Text size="xs" c="dimmed">
            The circuit's own corner names apply to this event, and only to it - which
            is why a correction like "carousel" is safe here and would not be as a
            global rule. Rows it contributes appear below marked "track"; override or
            disable any of them like any other inherited row.
          </Text>
        </Stack>
      ) : null}

      {problems.length > 0 ? (
        <Alert
          color="yellow"
          variant="light"
          title="Some layers could not be read"
          style={{ flex: '0 0 auto', marginBottom: 8 }}
        >
          <Stack gap={2}>
            <Text size="xs">
              The file(s) below could not be read, so they contribute nothing right now - neither
              to the rows shown here nor to transcription - until the file is fixed by hand, or
              Save at that layer overwrites it with a clean one.
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
          Proper nouns a transcript keeps getting wrong. <b>Terms</b> bias the decoder before it
          errs; <b>corrections</b> fix what it already got wrong. Every row below is what applies
          here; only the highlighted rows belong to this scope's own layer and are editable
          directly. Disabling an entry never deletes it from the layer that set it.
        </Text>
      </Stack>

      <Box style={{ flex: '1 1 auto', minHeight: 0, overflowY: 'auto', marginTop: 12 }}>
        <Stack gap="lg">
          <Stack gap="xs">
            <Text fw={600} size="sm" tt="uppercase" c="dimmed">
              Terms ({termRows.length})
            </Text>
            <Group align="flex-end" gap="xs" wrap="wrap">
              <TextInput
                label="New term"
                placeholder="e.g. Schwalbenschwanz"
                value={newTerm}
                onChange={(e) => setNewTerm(e.currentTarget.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') handleAddTerm()
                }}
                style={{ flex: '1 1 240px' }}
              />
              <Button variant="light" color="steel" onClick={handleAddTerm}>
                Add term
              </Button>
            </Group>
            {termError ? (
              <Text size="xs" c="red">
                {termError}
              </Text>
            ) : null}
            <Stack gap={4}>
              {termRows.map((row) => (
                <EntryRow
                  key={row.key}
                  label={row.term}
                  detail={null}
                  enabled={row.enabled}
                  own={row.own}
                  source={row.source}
                  onOverride={() => setTermRows((prev) => (prev ? overrideRow(prev, row.key) : prev))}
                  onDisable={() => setTermRows((prev) => (prev ? disableRow(prev, row.key) : prev))}
                  onEnable={() => setTermRows((prev) => (prev ? enableRow(prev, row.key) : prev))}
                  onRemove={() => {
                    setTermRows((prev) => (prev ? removeOwnRow(prev, row.key) : prev))
                    noteRemoval()
                  }}
                />
              ))}
            </Stack>
          </Stack>

          <Stack gap="xs">
            <Text fw={600} size="sm" tt="uppercase" c="dimmed">
              Corrections ({repRows.length})
            </Text>
            <Group align="flex-end" gap="xs" wrap="wrap">
              <TextInput
                label="Heard as"
                placeholder="e.g. kessichen"
                value={newFrom}
                onChange={(e) => setNewFrom(e.currentTarget.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') handleAddReplacement()
                }}
                style={{ flex: '1 1 180px' }}
              />
              <TextInput
                label="Should say"
                placeholder="e.g. Kesselchen"
                value={newTo}
                onChange={(e) => setNewTo(e.currentTarget.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') handleAddReplacement()
                }}
                style={{ flex: '1 1 180px' }}
              />
              <Button variant="light" color="steel" onClick={handleAddReplacement}>
                Add correction
              </Button>
            </Group>
            {repError ? (
              <Text size="xs" c="red">
                {repError}
              </Text>
            ) : null}
            <Stack gap={4}>
              {repRows.map((row) => (
                <EntryRow
                  key={row.key}
                  label={row.from}
                  detail={
                    row.own && row.enabled ? (
                      <TextInput
                        size="xs"
                        w={200}
                        value={row.to}
                        error={row.to.trim() === ''}
                        onChange={(e) =>
                          setRepRows((prev) =>
                            prev ? setReplacementText(prev, row.key, e.currentTarget.value) : prev,
                          )
                        }
                      />
                    ) : (
                      <Text size="sm" c="dimmed" ff="monospace" truncate w={200}>
                        {row.enabled ? row.to : '—'}
                      </Text>
                    )
                  }
                  enabled={row.enabled}
                  own={row.own}
                  source={row.source}
                  onOverride={() => setRepRows((prev) => (prev ? overrideRow(prev, row.key) : prev))}
                  onDisable={() => setRepRows((prev) => (prev ? disableRow(prev, row.key) : prev))}
                  onEnable={() => setRepRows((prev) => (prev ? enableRow(prev, row.key) : prev))}
                  onRemove={() => {
                    setRepRows((prev) => (prev ? removeOwnRow(prev, row.key) : prev))
                    noteRemoval()
                  }}
                />
              ))}
            </Stack>
          </Stack>
        </Stack>
      </Box>

      {saveError ? (
        <Alert
          color="red"
          title="Could not save the glossary"
          mt="sm"
          // The one sibling in this fixed-height column that carried no
          // explicit flex, so it inherited the shrinkable default while every
          // other sibling is `0 0 auto`. A long save error is exactly the text
          // an operator must be able to read in full.
          style={{ flex: '0 0 auto' }}
        >
          {saveError}
        </Alert>
      ) : null}

      {removedCount > 0 ? (
        <Text size="xs" c="yellow" mt="sm" style={{ flex: '0 0 auto' }}>
          {removedCount} removed - Save to apply; each removed entry falls back to its inherited or
          built-in value.
        </Text>
      ) : null}

      {incompleteKeys.length > 0 ? (
        <Text size="xs" c="red" mt="sm" style={{ flex: '0 0 auto' }}>
          {incompleteKeys.length === 1 ? 'This correction needs' : 'These corrections need'} a
          replacement before you can Save: {incompleteLabels.join(', ')}. Fill in what it should
          say, or use Disable if the intent was to switch it off.
        </Text>
      ) : null}

      <Group justify="flex-end" mt="sm" style={{ flex: '0 0 auto' }}>
        <Button
          color="steel"
          onClick={handleSave}
          loading={saving}
          disabled={!dirty || incompleteKeys.length > 0}
        >
          Save
        </Button>
      </Group>
    </Box>
  )
}

/** One row of either list: its name (struck through with a badge when
 * disabled), an optional detail slot (the correction's target text, editable
 * for an own enabled row), and the row's actions, which form a matrix over
 * (own, enabled) rather than two branches:
 *
 *   inherited, enabled   Override  Disable
 *   inherited, disabled  Override  Enable
 *   own,       enabled   Disable   Remove
 *   own,       disabled  Enable    Remove
 *
 * Enable and Disable are therefore ALWAYS both reachable - whichever one the
 * row is not already in. Two holes are why: Override alone was a dead end for
 * a row a less-specific layer had disabled (it copies the CURRENT, disabled
 * state, and neither a term row nor a disabled correction's target then offers
 * anything editable), and an own ENABLED row had no way to become an explicit
 * disable, which also made the incomplete-target caption name a Disable button
 * that was not rendered for the very row it was talking about. Both
 * glossaryLayers.enableRow and disableRow create the own entry themselves, so
 * a disabled inherited row switches on in one click rather than
 * Override-then-Enable. Kept local/unexported; the shared logic these call
 * into all lives in glossaryLayers.ts. */
function EntryRow({
  label,
  detail,
  enabled,
  own,
  source,
  onOverride,
  onDisable,
  onEnable,
  onRemove,
}: {
  label: string
  detail: React.ReactNode
  enabled: boolean
  own: boolean
  source: LayerSource
  onOverride: () => void
  onDisable: () => void
  onEnable: () => void
  onRemove: () => void
}) {
  return (
    <Group
      justify="space-between"
      wrap="nowrap"
      gap="xs"
      p={6}
      style={{
        borderRadius: 'var(--mantine-radius-sm)',
        background: own ? 'var(--mantine-color-dark-7)' : 'transparent',
      }}
    >
      <Group gap={8} wrap="nowrap" style={{ minWidth: 0, flex: '1 1 auto' }}>
        <Text
          size="sm"
          ff="monospace"
          truncate
          td={!enabled ? 'line-through' : undefined}
          c={!enabled ? 'dimmed' : undefined}
        >
          {label}
        </Text>
        {!enabled ? (
          <Badge size="xs" color="dark.3" variant="light">
            {own ? 'disabled' : `disabled (${sourceLabel(source)})`}
          </Badge>
        ) : !own ? (
          <Badge size="xs" color="dark.3" variant="light">
            {sourceLabel(source)}
          </Badge>
        ) : null}
      </Group>

      <Group gap={6} wrap="nowrap" style={{ flexShrink: 0 }}>
        {detail}
        {/* The action set is a matrix over (own, enabled), not two branches
            with an extra button bolted on, because the earlier version left
            two holes an operator could fall into. Enable/Disable are always
            BOTH reachable - whichever one the row is not already in - which
            is what makes the incomplete-target caption below able to name
            Disable as a real way out, and what lets a disabled inherited row
            be switched on in ONE click rather than Override-then-Enable
            (enableRow creates the own entry itself, exactly as disableRow
            always has). */}
        {own ? (
          <>
            {enabled ? (
              <Tooltip label="Write an explicit 'off' at this scope - the entry stops applying here without being removed from the layer that set it">
                <Button size="xs" variant="default" onClick={onDisable}>
                  Disable
                </Button>
              </Tooltip>
            ) : (
              <Button size="xs" variant="default" onClick={onEnable}>
                Enable
              </Button>
            )}
            <Tooltip label="Drop this scope's own entry - the inherited value or the built-in default takes its place after Save">
              <Button size="xs" variant="subtle" color="red" onClick={onRemove}>
                Remove
              </Button>
            </Tooltip>
          </>
        ) : (
          <>
            <Button size="xs" variant="default" onClick={onOverride}>
              Override
            </Button>
            {enabled ? (
              <Button size="xs" variant="subtle" color="red" onClick={onDisable}>
                Disable
              </Button>
            ) : (
              <Button size="xs" variant="default" onClick={onEnable}>
                Enable
              </Button>
            )}
          </>
        )}
      </Group>
    </Group>
  )
}
