import { useEffect, useRef, useState } from 'react'
import {
  ActionIcon,
  Alert,
  Badge,
  Box,
  Button,
  Divider,
  Grid,
  Group,
  NumberInput,
  SegmentedControl,
  Stack,
  Text,
  TextInput,
  Tooltip,
} from '@mantine/core'
import { notifications } from '@mantine/notifications'
import {
  ApiError,
  currentScope,
  enqueueJob,
  getClip,
  patchClip,
  shortUrl,
  type ClipDetail,
  type ClipStatus,
  type PatchClipBody,
  type Word,
} from '../api'
import { formatStreamDuration } from '../format'
import { useQueuedJob } from '../hooks/useQueuedJob'
import { endedNotice } from '../jobs'
import {
  fromPair,
  isPending,
  masterDuration,
  previewOffsets,
  remainingSeconds,
  trimActionLabel,
  trimNeedsAction,
  trimProblems,
} from '../trim'
import { withWindow } from '../window'
import { wordsEqual } from '../words'
import { PreviewPane } from './PreviewPane'
import { WordsEditor } from './WordsEditor'

interface ClipEditorProps {
  clip: ClipDetail
  onUpdated: (updated: ClipDetail) => void
  /** Freezes every edit control while a render is in flight. The render
   * reads edit.json LIVE per clip (jobs._render_one), so an edit staged or
   * saved mid-render could silently leak into a not-yet-rendered clip of a
   * batch run - locking the editor for the render's duration keeps the
   * process's inputs fixed and the behaviour predictable. */
  disabled?: boolean
}

const STATUS_DATA: { label: string; value: ClipStatus }[] = [
  { label: 'Candidate', value: 'candidate' },
  { label: 'Kept', value: 'kept' },
  { label: 'Discarded', value: 'discarded' },
]

/**
 * The editor for one selected clip: title, status, transcript correction
 * and the rendered-look preview. Title and transcript edits are staged
 * locally and only reach the server (edit.json, via PATCH) on an explicit
 * Save - see PreviewPane's own docstring for why that also gates when the
 * preview updates. Status changes are a distinct, immediate action: an
 * operator marking a clip kept or discarded is making one decision, not
 * mid-editing text that a background write could race.
 */
export function ClipEditor({ clip, onUpdated, disabled = false }: ClipEditorProps) {
  const [localTitle, setLocalTitle] = useState(clip.effective_title)
  const [localWords, setLocalWords] = useState<Word[]>(clip.words)
  const [localWindow, setLocalWindow] = useState<[number, number]>(clip.effective_window)
  const [at, setAt] = useState(0)
  const [refreshToken, setRefreshToken] = useState(0)
  const [saving, setSaving] = useState(false)
  const [statusSaving, setStatusSaving] = useState(false)
  const [windowResetting, setWindowResetting] = useState(false)

  // The operator's staged head/tail cut, in seconds - NOT part of `dirty`/
  // handleSave the way title/words/window are: it is saved and applied in
  // one action (handleApplyTrim below), through the existing PatchClipBody
  // rather than a save path of its own (see CLAUDE.md's trim section).
  // shortDuration starts from the moment's own span MINUS whatever trim is
  // already applied - short.mp4, once a trim has landed, is already that
  // much shorter than the moment - and is corrected to the rendered
  // short's REAL duration once the video element reports it on
  // loadedmetadata - the two are usually close but not identical (the
  // render pads/crops slightly), and trimProblems must check against the
  // real file, not the moment. Seeding from the raw span alone overstated
  // the effective duration (masterDuration adds appliedTrim back on top of
  // shortDuration - see its own docstring) by exactly the applied cut,
  // for the few hundred milliseconds between mount and loadedmetadata.
  const [trimHead, setTrimHead] = useState(clip.trim?.[0] ?? 0)
  const [trimTail, setTrimTail] = useState(clip.trim?.[1] ?? 0)
  const [shortDuration, setShortDuration] = useState(
    remainingSeconds(clip.detected_window[1] - clip.detected_window[0], fromPair(clip.trim_applied)),
  )
  const [trimSaving, setTrimSaving] = useState(false)
  // The cut is a QUEUE ENTRY now, not a job started on the click (see
  // handleApplyTrim). What is held here is the entry's id; `trimWork` carries
  // its state, why it has not started if it has not, and the job once the
  // worker claims it.
  const [trimEntryId, setTrimEntryId] = useState<string | null>(null)
  const trimWork = useQueuedJob(trimEntryId)
  const videoRef = useRef<HTMLVideoElement>(null)

  // Reset staged edits only when navigating to a DIFFERENT clip - not on
  // every prop update, or an in-progress correction would be wiped out by
  // an unrelated status change bumping the same clip object (see module
  // docstring above).
  //
  // Done here, during render, rather than in a useEffect: an effect runs
  // in its OWN commit, after the one that already delivered the new
  // `clip` prop - PreviewPane would render once with the new clip name
  // but the PREVIOUS clip's still-live `at`, fire a preview request for
  // that, and then render again once this effect's reset landed a moment
  // later and fire a second, correct request. Two requests for one
  // selection (see PreviewPane.tsx's own `key` for the other half of this
  // fix). Resetting synchronously here means React finishes this render
  // with the new clip's settled state before anything ever commits, so
  // there is only ever one commit, and PreviewPane only ever fetches
  // once - see https://react.dev/learn/you-might-not-need-an-effect
  // ("Adjusting some state when a prop changes").
  const [resetForClip, setResetForClip] = useState(clip.name)
  if (resetForClip !== clip.name) {
    setResetForClip(clip.name)
    setLocalTitle(clip.effective_title)
    setLocalWords(clip.words)
    setLocalWindow(clip.effective_window)
    setAt(0)
    setTrimHead(clip.trim?.[0] ?? 0)
    setTrimTail(clip.trim?.[1] ?? 0)
    setShortDuration(
      remainingSeconds(clip.detected_window[1] - clip.detected_window[0], fromPair(clip.trim_applied)),
    )
  }

  const wordsDirty = !wordsEqual(localWords, clip.words)
  const titleDirty = localTitle !== clip.effective_title
  const windowDirty =
    localWindow[0] !== clip.effective_window[0] || localWindow[1] !== clip.effective_window[1]
  const dirty = titleDirty || wordsDirty || windowDirty

  // Lead-in/lead-out are DERIVED from localWindow against the fixed
  // detected_window, not their own separate state - that would be a
  // second source of truth that could drift from localWindow (the value
  // everything else - the "unsaved changes" badge, the effective-window
  // readout, Save itself - actually reads). Positive means the window was
  // widened past the detected boundary on that side; negative means it
  // was trimmed inside it.
  const [detectedStart, detectedEnd] = clip.detected_window
  const leadIn = Number((detectedStart - localWindow[0]).toFixed(1))
  const leadOut = Number((localWindow[1] - detectedEnd).toFixed(1))

  function handleLeadInChange(value: number | string) {
    const num = typeof value === 'number' ? value : parseFloat(value)
    if (Number.isNaN(num)) return
    const newStart = Math.max(0, detectedStart - num)
    setLocalWindow(([, end]) => [Math.min(newStart, end - 0.1), end])
  }

  function handleLeadOutChange(value: number | string) {
    const num = typeof value === 'number' ? value : parseFloat(value)
    if (Number.isNaN(num)) return
    const newEnd = detectedEnd + num
    setLocalWindow(([start]) => [start, Math.max(newEnd, start + 0.1)])
  }

  async function handleSave() {
    setSaving(true)
    try {
      const body: PatchClipBody = { title: localTitle, words: localWords }
      // `window` is only ever included when it actually changed - sending
      // it unconditionally on every save (e.g. a title-only edit) would
      // silently turn an unset window into an explicit override equal to
      // its current value, which is not what the operator asked to save
      // (see api.ts's PatchClipBody docstring on this same rule for title).
      if (windowDirty) body.window = localWindow
      const patchResult = await patchClip(clip.name, body)
      const updated = withWindow(patchResult, clip, windowDirty ? localWindow : undefined)
      onUpdated(updated)
      setLocalTitle(updated.effective_title)
      setLocalWords(updated.words)
      setLocalWindow(updated.effective_window)
      setRefreshToken((token) => token + 1)
      notifications.show({ message: 'Saved.', color: 'green' })
    } catch (error) {
      notifications.show({
        title: 'Save failed',
        message: error instanceof ApiError ? error.message : String(error),
        color: 'red',
      })
    } finally {
      setSaving(false)
    }
  }

  async function handleResetTitle() {
    try {
      const patchResult = await patchClip(clip.name, { title: null })
      const updated = withWindow(patchResult, clip)
      onUpdated(updated)
      setLocalTitle(updated.effective_title)
      notifications.show({ message: 'Title reset to the harvested title.', color: 'green' })
    } catch (error) {
      notifications.show({
        title: 'Could not reset title',
        message: error instanceof ApiError ? error.message : String(error),
        color: 'red',
      })
    }
  }

  async function handleResetWindow() {
    setWindowResetting(true)
    try {
      const patchResult = await patchClip(clip.name, { window: null })
      const updated = withWindow(patchResult, clip, clip.detected_window)
      onUpdated(updated)
      setLocalWindow(updated.effective_window)
      notifications.show({ message: 'Window reset to the detected window.', color: 'green' })
    } catch (error) {
      notifications.show({
        title: 'Could not reset window',
        message: error instanceof ApiError ? error.message : String(error),
        color: 'red',
      })
    } finally {
      setWindowResetting(false)
    }
  }

  async function handleStatusChange(status: ClipStatus) {
    setStatusSaving(true)
    try {
      const patchResult = await patchClip(clip.name, { status })
      const updated = withWindow(patchResult, clip)
      onUpdated(updated)
      setRefreshToken((token) => token + 1)
    } catch (error) {
      notifications.show({
        title: 'Could not change status',
        message: error instanceof ApiError ? error.message : String(error),
        color: 'red',
      })
    } finally {
      setStatusSaving(false)
    }
  }

  // Compared against the LOCAL staged values, not the last-saved clip.trim -
  // so the button reacts the moment the operator types a new number, before
  // anything has been saved at all (saving and applying happen together,
  // below). Once a trim job lands, `clip.trim_applied` catches up and this
  // naturally flips back to false.
  const appliedTrim = fromPair(clip.trim_applied)
  const trimIsPending = isPending({ head: trimHead, tail: trimTail }, appliedTrim)
  // Server-reported: short.mp4's cut state cannot be trusted even though
  // the staged values agree with the last-known-applied ones (see
  // ClipSummary.trim_unknown / trim.py's is_unknown). trimIsPending alone
  // reads false in exactly this state - null desired against null applied
  // - which is what left the repair unreachable: the button said "Trim
  // applied" for a file that might be a stale cut.
  const trimUnknown = clip.trim_unknown
  // Busy covers the whole life of the request: saving the values, the entry
  // sitting in the plan, and the cut actually running. A queued cut has NOT
  // touched short.mp4 - which is why `trimWork.waiting` is shown beside the
  // button rather than left to a spinner that says nothing.
  const trimBusy = trimSaving || trimWork.pending || trimWork.running

  // trimHead/trimTail are always ABSOLUTE cuts from the untrimmed master
  // (see trim.py's own module docstring), but `shortDuration` comes from
  // `video.duration` - the ALREADY-CUT short.mp4's own length once any
  // trim is applied. Every consumer below must compare against the
  // reconstructed MASTER length, not shortDuration directly, or it
  // double-counts whatever `appliedTrim` already removed - see trim.ts's
  // own docstring on `masterDuration` for what that looked like in a real
  // browser before this fix.
  const effectiveDuration = masterDuration(shortDuration, appliedTrim)
  // The preview (seek/pause) plays against the file actually loaded in the
  // player, which IS already cut - so it needs the offset relative to
  // THAT file, not the master. See previewOffsets' own docstring on why
  // this can go negative and why only the head needs clamping.
  const preview = previewOffsets({ head: trimHead, tail: trimTail }, appliedTrim)

  async function handleApplyTrim() {
    setTrimSaving(true)
    try {
      // Saved through the existing PATCH (patchClip/PatchClipBody.trim) -
      // no dedicated save route - and only then does the job that actually
      // re-encodes the file start, so it always cuts from the value the
      // operator is looking at, never a stale one left over from a previous
      // visit.
      const patchResult = await patchClip(clip.name, { trim: [trimHead, trimTail] })
      // Hand the new clip.trim up to App.tsx BEFORE starting the encode job,
      // exactly as handleSave/handleResetTitle/handleStatusChange do - this
      // is what keeps ManualUploadPanel/UploadPanel's trim-pending guard
      // (isPending against selectedClip.trim/trim_applied) correct for the
      // whole encode window, not just once the job finishes. Measured
      // consequence of skipping this: with no saved trim, staging head=2/
      // tail=3 and clicking Apply left selectedClip.trim === null for the
      // ~15s encode, so both panels read isPending(null, null) as false and
      // offered Download/Upload while the server correctly 409'd them - and
      // on a FAILED job onUpdated was never called at all, so the panels
      // stayed wrong indefinitely rather than just for the encode's
      // duration.
      const updated = withWindow(patchResult, clip)
      onUpdated(updated)
      // Queued, not started (`POST /api/jobs`, kind "trim"). The direct route
      // still exists - see api.py's post_trim - but a cut that cannot be
      // scheduled or stopped and never appears on the Jobs screen is exactly
      // what this change removes. The two guards the direct route applied are
      // not lost: it 409'd a clip with no rendered short (the button is only
      // offered for one that has one, and the job fails with that reason
      // otherwise) and a held EventLock, which the worker now WAITS for
      // instead of refusing.
      const scope = currentScope()
      const { entry } = await enqueueJob('trim', {
        channel: scope.channel, event: scope.event, clip: clip.name,
      })
      setTrimEntryId(entry.id)
    } catch (error) {
      notifications.show({
        title: 'Could not queue the trim',
        message: error instanceof ApiError ? error.message : String(error),
        color: 'red',
      })
    } finally {
      setTrimSaving(false)
    }
  }

  // The ENTRY reaching "done" means short.mp4 now embodies the cut - refetch
  // the clip (trim_applied, short_version, has_short all change) and hand
  // it up, exactly as a render's own finish-then-refetch flow does elsewhere
  // in this app (see AuthStatusBar's connectJob for the same shape).
  //
  // Keyed on the entry's outcome, not the job's status: an entry that never
  // started at all (a param the worker refuses, a profile that will not load)
  // has no job to ask, and keying on the job would leave the button spinning
  // on a cut that had already ended.
  useEffect(() => {
    const outcome = trimWork.outcome
    if (outcome === null) return
    if (outcome === 'done') {
      setTrimEntryId(null)
      getClip(clip.name).then((updated) => {
        onUpdated(updated)
        setTrimHead(updated.trim?.[0] ?? 0)
        setTrimTail(updated.trim?.[1] ?? 0)
      })
    } else {
      setTrimEntryId(null)
      // Red for a failure, orange for a STOP - the operator asked for that
      // one, and reporting it in the failure colour is the relabelling this
      // branch of the studio forbids everywhere else. The decision is
      // `jobs.ts`'s `endedNotice`, once and unit-tested, rather than a third
      // copy here that could drift from the other two.
      notifications.show({ ...endedNotice(
        'Trim', 'trim', outcome,
        trimWork.job?.results[clip.name]?.reason ?? trimWork.entry?.reason ?? null) })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trimWork.outcome])

  return (
    <Stack gap="md">
      {disabled && (
        <Alert color="steel" title="A render is running">
          Editing is locked while the render is running - it reads each clip's
          saved state as it goes, so changes are held until it finishes.
        </Alert>
      )}
      <Group justify="space-between" align="flex-start" wrap="nowrap">
        <Stack gap={4} style={{ flex: 1, minWidth: 0 }}>
          <Text size="xs" c="dimmed" ff="monospace" truncate>
            {clip.name}
          </Text>
          <Group gap="xs" align="flex-end">
            <TextInput
              label="Title"
              value={localTitle}
              onChange={(event) => setLocalTitle(event.currentTarget.value)}
              disabled={disabled}
              style={{ flex: 1 }}
              data-autofocus
            />
            <Tooltip label="Reset to the harvested title">
              <ActionIcon variant="default" onClick={handleResetTitle} disabled={disabled} mb={4}>
                ⟲
              </ActionIcon>
            </Tooltip>
          </Group>
          <Text size="xs" c="dimmed" truncate>
            Harvested as: {clip.harvested_title}
          </Text>
        </Stack>
        <Stack gap={4} align="flex-end">
          <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
            Status
          </Text>
          <SegmentedControl
            size="xs"
            value={clip.status}
            disabled={statusSaving || disabled}
            onChange={(value) => handleStatusChange(value as ClipStatus)}
            data={STATUS_DATA}
          />
        </Stack>
      </Group>

      {clip.conflict && (
        <Alert color="steel" title="This correction may be out of date">
          The transcript underneath this correction has changed since the correction was made.
          The correction is still being used as-is - nothing was merged automatically. Review the
          words below and save again once you are satisfied.
        </Alert>
      )}

      {/* Nudges the clip's own window - the span of the source stream it
          is cut from (see api.py's detected_window/effective_window and
          CLAUDE.md's "A moment's window is editorial" note). Lead-in/out
          are offsets from the DETECTED boundary rather than raw start/end
          seconds: an operator thinking "give it two more seconds before
          the crash" should not have to do that subtraction against an
          absolute timestamp themselves. Folded into the SAME Save action
          as title/words rather than a second button - one explicit save
          per clip, one "Unsaved changes" badge covering everything staged
          (see the module docstring above on why status alone is the one
          exception, as an immediate decision rather than a correction). */}
      <Stack gap="xs">
        <Group justify="space-between" wrap="nowrap">
          <Text fw={600} size="xs" tt="uppercase" c="dimmed">
            Window
          </Text>
          <Group gap="xs">
            {windowDirty && (
              <Badge color="steel" variant="dot" size="xs">
                Unsaved changes
              </Badge>
            )}
            <Button
              size="xs"
              variant="subtle"
              color="steel"
              loading={windowResetting}
              disabled={disabled}
              onClick={handleResetWindow}
            >
              Reset to detected
            </Button>
          </Group>
        </Group>
        <Group grow>
          <NumberInput
            label="Lead-in (s)"
            description="Seconds before the detected start"
            value={leadIn}
            onChange={handleLeadInChange}
            step={0.5}
            decimalScale={1}
            size="xs"
            disabled={disabled}
          />
          <NumberInput
            label="Lead-out (s)"
            description="Seconds after the detected end"
            value={leadOut}
            onChange={handleLeadOutChange}
            step={0.5}
            decimalScale={1}
            size="xs"
            disabled={disabled}
          />
        </Group>
        <Text size="xs" c="dimmed" ff="monospace" className="tnum">
          Effective window: {localWindow[0].toFixed(1)}s - {localWindow[1].toFixed(1)}s (
          {(localWindow[1] - localWindow[0]).toFixed(1)}s)
        </Text>
      </Stack>

      <Divider />

      <Grid>
        <Grid.Col span={{ base: 12, sm: 7 }}>
          <Stack gap="sm">
            <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
              Rendered short
            </Text>
            {clip.has_short ? (
              <>
                {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
                <video
                  ref={videoRef}
                  controls
                  src={shortUrl(clip.name, clip.short_version, 'play')}
                  style={{ width: '100%', maxHeight: 360 }}
                  onLoadedMetadata={(event) => {
                    const video = event.currentTarget
                    if (Number.isFinite(video.duration)) setShortDuration(video.duration)
                    // previewOffsets is relative to THIS file (already cut
                    // by appliedTrim), not the master - and can go negative
                    // when the staged head is smaller than what is already
                    // applied, which is not a valid seek target.
                    video.currentTime = Math.max(0, preview.head)
                  }}
                  onTimeUpdate={(event) => {
                    // The preview costs nothing: no encoding, no request, no
                    // new file. That is what makes nudging the values until
                    // they look right free.
                    const video = event.currentTarget
                    if (video.duration && video.currentTime >= video.duration - preview.tail) {
                      video.pause()
                    }
                  }}
                />
                <Group gap="xs" mt="xs" align="end">
                  <NumberInput
                    label="Head (s)"
                    min={0}
                    step={0.5}
                    value={trimHead}
                    onChange={(v) => setTrimHead(Number(v) || 0)}
                    disabled={disabled || trimBusy}
                    w={110}
                  />
                  <NumberInput
                    label="Tail (s)"
                    min={0}
                    step={0.5}
                    value={trimTail}
                    onChange={(v) => setTrimTail(Number(v) || 0)}
                    disabled={disabled || trimBusy}
                    w={110}
                  />
                  <Text size="sm" c="dimmed">
                    {formatStreamDuration(
                      remainingSeconds(effectiveDuration, { head: trimHead, tail: trimTail }),
                    )}{' '}
                    after the cut
                  </Text>
                  <Button
                    size="xs"
                    color="steel"
                    loading={trimBusy}
                    onClick={handleApplyTrim}
                    disabled={
                      disabled
                      || trimBusy
                      || !trimNeedsAction(trimIsPending, trimUnknown)
                      || trimProblems(effectiveDuration, { head: trimHead, tail: trimTail }).length > 0
                    }
                  >
                    {trimActionLabel(trimIsPending, trimUnknown)}
                  </Button>
                </Group>
                {trimWork.waiting && (
                  // The cut is in the plan and has NOT run: short.mp4 is
                  // still whatever it was. Saying which of the two reasons it
                  // is waiting for is the whole difference between this and a
                  // button that spins forever.
                  <Alert color="yellow" title="Trim queued - not applied yet" p="xs">
                    <Text size="xs">{trimWork.waiting}</Text>
                  </Alert>
                )}
                {trimWork.error && (
                  // Why this panel stopped following its trim entry - the row
                  // left the plan (removed on the Jobs screen), or the plan
                  // could not be read. The Head/Tail inputs and Apply are
                  // usable again by the same flag: `trimBusy` reads
                  // `trimWork.pending`/`running`, which are false whenever
                  // this is set. See QueuedWork.error.
                  <Text size="xs" c="red.4">
                    {trimWork.error}
                  </Text>
                )}
                {trimUnknown && !trimIsPending && (
                  <Text size="xs" c="orange">
                    This clip&apos;s applied trim state is unknown - short.mp4 may not be the
                    file it appears to be. Click Repair trim to rebuild it from the untrimmed
                    master.
                  </Text>
                )}
                {trimProblems(effectiveDuration, { head: trimHead, tail: trimTail }).map((problem) => (
                  <Text key={problem} size="xs" c="red">
                    {problem}
                  </Text>
                ))}
              </>
            ) : (
              <Text size="sm" c="dimmed">
                No rendered short yet for this clip - use the render buttons to create one.
              </Text>
            )}
            <Group justify="space-between">
              <Text fw={600} size="xs" tt="uppercase" c="dimmed">
                Transcript
              </Text>
              {dirty && (
                <Badge color="steel" variant="dot">
                  Unsaved changes
                </Badge>
              )}
            </Group>
            <WordsEditor
              words={localWords}
              onChange={setLocalWords}
              onJumpTo={setAt}
              disabled={disabled}
            />
            <Group>
              <Button onClick={handleSave} disabled={!dirty || disabled} loading={saving} color="steel">
                Save changes
              </Button>
              {!dirty && (
                <Text size="xs" c="dimmed">
                  Nothing to save.
                </Text>
              )}
            </Group>
          </Stack>
        </Grid.Col>
        <Grid.Col span={{ base: 12, sm: 5 }}>
          <Box pos="sticky" top={0}>
            {/* Keyed by clip name: switching clips is a fresh remount, not
                a prop update - PreviewPane's own debounce/image state
                starts clean against the new clip's already-reset `at`
                (see the render-time reset above) instead of settling
                mid-flight from the previous clip's values. This is what
                actually guarantees a single request per selection; the
                render-time reset above only ensures `at` itself is right
                by the time this mounts. */}
            <PreviewPane
              key={clip.name}
              clipName={clip.name}
              duration={clip.duration}
              at={at}
              onAtChange={setAt}
              refreshToken={refreshToken}
              words={localWords}
              wordsDirty={wordsDirty}
              title={localTitle}
              titleDirty={titleDirty}
            />
          </Box>
        </Grid.Col>
      </Grid>
    </Stack>
  )
}
