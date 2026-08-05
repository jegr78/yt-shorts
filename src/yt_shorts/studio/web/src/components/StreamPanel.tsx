import { useEffect, useRef, useState } from 'react'
import {
  ActionIcon,
  Alert,
  Anchor,
  Badge,
  Button,
  Center,
  Checkbox,
  Group,
  Loader,
  ScrollArea,
  Select,
  Stack,
  Text,
  Tooltip,
} from '@mantine/core'
import { ApiError, listStreams, type StreamCatalogue, type StreamVideo } from '../api'
import { formatStreamDuration } from '../format'
import type { QueuedEntries, TrackedWork } from '../hooks/useQueuedEntries'
import { jobLogFile, progressLabel, stateColor } from '../jobs'
import { routePath } from '../scopedApi'
import {
  ALL_STREAMS,
  type BulkAction,
  bulkPlan,
  legKey,
  playlistOptions,
  selectionNote,
  type StreamEntryIds,
  visibleVideos,
} from '../streams'
import { navigate } from '../useRoute'

/** The tower's own condensed face - same rationale as ClipList.tsx's
 * TOWER_FONT: it lets more of a stream's own title stay legible in a
 * narrow column before truncating. */
const TOWER_FONT = '"IBM Plex Sans Condensed", "IBM Plex Sans", sans-serif'

const VIEW_FORMATTER = new Intl.NumberFormat('en-US')

function formatViews(count: number | null): string {
  if (count === null || !Number.isFinite(count)) return '—'
  return VIEW_FORMATTER.format(count)
}

/** The row buttons' own force, ALWAYS on - unlike the bar below, which uses
 * the panel's own `force` state (off by default). A single row's click is an
 * explicit request for that ONE stream: the operator is looking at that
 * exact row, not sweeping a batch of thirteen where "skip what already
 * exists" is the safe default. That is precisely the argument that makes the
 * bar's default skip right and this skip wrong - the bar has to guess an
 * operator's intent across many rows at once, and a row button carries no
 * such ambiguity. Before this, a row click for a stream that already had a
 * transcript or analysis silently queued NOTHING at all (see the CRITICAL
 * review finding this constant closes) - `bulkPlan` skipped it, and the
 * click vanished with no notification and no state change. */
const ROW_FORCE = { transcribe: true, detect: true }

function transcribeTooltip(hasTranscript: boolean): string {
  return hasTranscript
    ? 'This stream already has a transcript. Clicking here re-transcribes it ' +
      'anyway - ordinarily cheap (its audio and cached chunks are reused), ' +
      'but useful after a glossary edit since it rewrites the transcript.'
    : 'Transcribe this stream.'
}

function detectTooltip(hasAnalysis: boolean): string {
  return hasAnalysis
    ? 'This stream already has an analysis. Clicking here re-detects it ' +
      // The fingerprint is over the transcript's WORDS, not over
      // transcript.json's identity - so a re-transcription that yields the
      // same words (an unchanged glossary, every chunk from the cache)
      // leaves every window a hit and this stays free. Hence "changed the
      // words" rather than "re-transcribed": the broader warning would be
      // wrong on the ordinary case the row's own Transcribe button
      // produces.
      'anyway - free only when a cached, model-scored window can be ' +
      'reused: not after something changed the transcript’s words (a ' +
      'glossary edit, then a re-transcription), not after a ' +
      'provider/model/marker change, and not if the last run had no model ' +
      'to score with and so cached no windows at all.'
    : 'Detect moments in this stream.'
}

/**
 * One tracked queue entry's live state, as this panel shows it: the state
 * badge, a link to the job's own log once there is one, why it has not
 * started, and why this panel stopped following it.
 *
 * Written once and used for BOTH actions rather than twice, because the
 * second copy is exactly what did not get written when the Transcribe button
 * was added: it threw its entry away and showed none of this, for the kind
 * whose job runs longest. A local component, not exported, so this file's
 * fast-refresh boundary stays component-only.
 */
function TrackedEntry({ label, work }: { label: string; work: TrackedWork }) {
  const entry = work.entry
  const logFile = entry === null ? null : jobLogFile(entry)
  // Detect (window) and transcribe (chunk) are the two longest-running
  // kinds in the tool - about an hour of decode and up to eight hours,
  // respectively - and this panel is where the operator pressed the button,
  // so it is where they are watching. progressLabel already returns null
  // for a kind with no unit to report (KINDS[kind].progress_unit is None -
  // see jobs.ts), so nothing here invents a reading for one that has none;
  // this is otherwise the same reading JobsScreen already shows for the
  // identical entry.
  const reading = entry === null ? null : progressLabel(entry)
  return (
    <>
      {entry && (
        <Stack gap={4}>
          <Group gap="xs" wrap="nowrap">
            <Badge color={stateColor(entry.state)} variant="dot" size="xs">
              {label}: {entry.state}
            </Badge>
            {/* Built from the ENTRY's job_id (via jobLogFile), which is null
                until the worker claims it - so this link simply is not drawn
                while the entry waits, rather than pointing at a file that
                does not exist. `/api/jobs/{id}` is two disjoint id spaces;
                a link built from the entry's own id 404s. */}
            {logFile && (
              <Anchor
                size="xs"
                c="steel.3"
                onClick={() =>
                  navigate(
                    `${routePath({ screen: 'logs' })}?file=${encodeURIComponent(logFile)}`)
                }
              >
                View log
              </Anchor>
            )}
            <Anchor size="xs" c="steel.3" onClick={() => navigate(routePath({ screen: 'jobs' }))}>
              Jobs
            </Anchor>
          </Group>
          {reading && (
            <Text size="xs" c="dimmed">
              {reading}
            </Text>
          )}
          {work.waiting && (
            // The queued state, said out loud. Without this the row's button
            // would sit at "Queued…" with no explanation, which for a stopped
            // worker means forever.
            <Alert color="yellow" title="Queued - not started yet" p="xs">
              <Text size="xs">{work.waiting}</Text>
            </Alert>
          )}
        </Stack>
      )}
      {work.error && (
        // Why this panel stopped following the entry - the row left the plan,
        // or the plan could not be read. Deliberately OUTSIDE the `entry`
        // block above: a removed entry clears `entry` (a badge still reading
        // "queued" would be a lie), so a message rendered in there would
        // vanish with the very row it is explaining. The row's button is back
        // to its idle label by the same flag - see TrackedWork.error.
        <Text size="xs" c="red.4">
          {work.error}
        </Text>
      )}
    </>
  )
}

interface StreamPanelProps {
  /** The event this panel belongs to - threaded through so each row's title
   * can link to its own stream screen (see routePath({ screen: 'stream', ... })).
   * StreamPanel has no other need of either segment; the queue actions still
   * go through the module-scoped API (see api.ts's eventScope()). */
  channel: string
  event: string
  /** Every queue entry created here, by video - owned by App so a row's
   * live state survives switching tabs. */
  entries: Record<string, StreamEntryIds>
  /** Those entries' live state, from ONE poll of the plan. */
  work: QueuedEntries
  /** Which (video, kind) LEGS have an enqueue POST still in flight right
   * now - the brief window before the plan knows about them at all.
   * Distinct from "queued", which is a state the plan can report. Keyed as
   * `${videoId}:${kind}`, per leg rather than per video: a single-row
   * Transcribe click must not make the Detect button on the same row read
   * "Queued…" too. */
  busyLegs: Set<string>
  onQueue: (
    action: BulkAction,
    videoIds: string[],
    force: { transcribe: boolean; detect: boolean },
    videos: StreamVideo[],
  ) => void
  /** Bumped by App whenever a tracked stream entry (this tab's own
   * transcribe/detect) reaches a terminal state. This panel reacts by
   * re-reading the catalogue with NO `refresh` - see this component's own
   * `load()` and its effect below for why that is cheap and why it must
   * never pass `refresh: true`. A plain counter, not the catalogue itself:
   * App owns the tracked entries, this panel owns `load()` and the
   * catalogue state, so the two stay decoupled through a signal rather than
   * App reaching into this panel's state. */
  catalogueStaleAt: number
}

/**
 * The operator's entry point into moment detection: a channel's own
 * back catalogue of streams (see GET /api/streams, yt_shorts.youtube.
 * channel_catalogue - the UNION of the flat Streams tab and every playlist's
 * membership, which is the whole point: it surfaces broadcasts a playlist
 * holds but the Streams tab does not), each with a "Transcribe" action and a
 * "Detect moments" one, plus a checkbox so several rows can be queued
 * together through the bar at the bottom. Every action - a single row's
 * button or a batch from the bar - goes through the same `onQueue`, which
 * enqueues an entry (`POST /api/jobs`) rather than starting anything:
 * detection scores an already-cached transcript (see yt_shorts.detect) and
 * is slow and paid for, which is precisely the kind of work an operator
 * needs to be able to schedule, reorder and stop.
 *
 * So a click here does NOT start anything, and this panel must not pretend it
 * did: every tracked entry reports its own state and, while it is still
 * waiting, WHY (see jobs.ts's `waitNote` - the worker not running at all, or
 * another job holding this event's lock). "Detecting…"/"Transcribing…" appear
 * only once the work is genuinely in flight.
 *
 * No button is gated on anything else running. Enqueuing takes no
 * `EventLock` - it writes one line into the plan and returns; the worker
 * takes the lock when it actually starts the entry, and waits rather than
 * failing if a CLI render holds it. Disabling a button while some other job
 * runs would refuse a request the queue exists to accept. The bar's three
 * buttons ARE deliberately gated on one thing: `busyLegs` non-empty, while
 * their own batch's sequential POSTs are still in flight - see the bar's own
 * comment below for why (MUST-FIX 1 in the review this closes).
 *
 * The catalogue is fetched once (this component's own `load()`) and held
 * locally - `getStreams`/`StreamScreen` fetch their OWN copy for the stream
 * view's title lookup, so this is not the only place in the studio that
 * needs the raw list, only the only place this component's own state lives.
 * A `502` from yt-dlp being missing or failing gets its own explanatory
 * Alert with a retry action rather than an empty or broken panel.
 */
export function StreamPanel({
  channel,
  event,
  entries,
  work,
  busyLegs,
  onQueue,
  catalogueStaleAt,
}: StreamPanelProps) {
  const [catalogue, setCatalogue] = useState<StreamCatalogue | null>(null)
  const [playlist, setPlaylist] = useState<string>(ALL_STREAMS)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [selected, setSelected] = useState<string[]>([])
  const [forceTranscribe, setForceTranscribe] = useState(false)
  const [forceDetect, setForceDetect] = useState(false)

  async function load(refresh = false) {
    setLoading(true)
    try {
      const next = await listStreams(refresh)
      setCatalogue(next)
      // A refresh can drop the playlist that was selected. Falling back to
      // "all" is the only option that cannot show an empty list with no
      // explanation.
      setPlaylist((current) =>
        current === ALL_STREAMS || next.playlists.some((p) => p.id === current)
          ? current
          : ALL_STREAMS)
      setError(null)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // MUST-FIX 3 from the whole-branch review: `has_transcript`/`has_analysis`
  // are stat'd fresh on every `/streams` response (see api.py's own comment
  // on why - a cached "no transcript" would survive a finished transcription
  // until someone pressed refresh), but this panel only ever called `load()`
  // from its mount effect above, the ⟲ icon and the error Retry - so a
  // tracked entry finishing left the row's badge stale until one of those
  // fired, and `bulkPlan` kept offering to transcribe/detect a stream that
  // just got its transcript or analysis. `catalogueStaleAt` is App's signal
  // that a tracked entry just settled; reacting to it with a PLAIN `load()`
  // (no `refresh`) is what makes this cheap - the server's per-channel
  // yt-dlp cache answers it from memory, redoing only the two `stat` calls
  // per video. `refresh: true` is never used here: that would re-run yt-dlp
  // for a fact that has nothing to do with YouTube.
  //
  // Skips the FIRST render deliberately - the mount effect above already
  // loads once, and `catalogueStaleAt` starts at 0 like any other counter,
  // so without the guard this would fire a redundant second load on every
  // mount.
  const catalogueStaleSeen = useRef(false)
  useEffect(() => {
    if (!catalogueStaleSeen.current) {
      catalogueStaleSeen.current = true
      return
    }
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [catalogueStaleAt])

  // Until the catalogue arrives there are no options, and a Select whose
  // value matches none of them renders the raw sentinel (`*all*`) at the
  // operator. One placeholder option carries the label through that window;
  // the count is left off deliberately rather than guessed, since nothing
  // has been counted yet.
  const options = catalogue
    ? playlistOptions(catalogue)
    : [{ value: ALL_STREAMS, label: 'All streams', count: null as number | null }]
  const streams = catalogue ? visibleVideos(catalogue, playlist) : null
  const force = { transcribe: forceTranscribe, detect: forceDetect }
  const visibleIds = (streams ?? []).map((video) => video.video_id)
  const allVideos = catalogue?.videos ?? []
  const hidden = selectionNote(selected, visibleIds)

  return (
    <Stack h="100%" gap="sm">
      <Group justify="space-between" wrap="nowrap" gap="xs">
        <Select
          size="xs"
          style={{ flex: 1 }}
          aria-label="Filter by playlist"
          value={playlist}
          onChange={(value) => setPlaylist(value ?? ALL_STREAMS)}
          data={options.map((option) => ({
            value: option.value,
            label: option.label,
          }))}
          allowDeselect={false}
          comboboxProps={{ withinPortal: true }}
        />
        <Tooltip label="Re-fetch the stream list and playlists from YouTube">
          <ActionIcon
            variant="default"
            size="sm"
            onClick={() => load(true)}
            disabled={loading}
            aria-label="Refresh streams"
          >
            {loading ? <Loader size={12} color="steel" /> : '⟲'}
          </ActionIcon>
        </Tooltip>
      </Group>

      {catalogue && catalogue.failed_playlists.length > 0 && (
        // Never swallowed: a catalogue missing a playlist looks exactly
        // like a complete one, and an operator would read a stream's
        // absence as "not published" rather than "not fetched".
        <Alert color="yellow" title="Some playlists could not be loaded" p="xs">
          {/* The reason, not just the title: it is the only thing that says
              whether to retry or look elsewhere. Safe to show verbatim - it
              is built in youtube.channel_catalogue from a yt-dlp failure
              against a PUBLIC playlist URL; that module has no API key, no
              OAuth and never touches the workspace's auth/ directory. */}
          <Stack gap={4}>
            {/* Bounded, not merely floored: one line per failed playlist
                (see the commit this comment documents) means the Alert
                itself grows without limit on a channel with many rate-
                limited playlists at once, which would push its own later
                entries off-screen even with the list below given a floor -
                reachability is a rule for every element, not just the list.
                mah is the same ScrollArea.Autosize idiom RenderPanel/
                UploadPanel use for a bounded, internally-scrolling region;
                160px keeps several rows visible at once while still
                yielding room to the stream list's own floor below. */}
            <ScrollArea.Autosize mah={160} offsetScrollbars>
              <Stack gap={4}>
                {catalogue.failed_playlists.map((failure) => (
                  <Text size="xs" key={failure.title}>
                    <strong>{failure.title}</strong> — {failure.reason}
                  </Text>
                ))}
              </Stack>
            </ScrollArea.Autosize>
            <Text size="xs" c="dimmed">
              Their streams may be missing from this list unless another
              playlist also holds them. Refresh to try again.
            </Text>
          </Stack>
        </Alert>
      )}

      {error && (
        <Alert color="red" title="Could not load streams">
          <Stack gap="xs">
            <Text size="sm">
              {error} - check that yt-dlp is installed on this machine and can reach YouTube,
              then retry.
            </Text>
            <Button size="xs" variant="light" color="red" onClick={() => load(true)}>
              Retry
            </Button>
          </Stack>
        </Alert>
      )}

      {!error && !streams && (
        <Center h="100%">
          <Stack align="center" gap="xs">
            <Loader color="steel" />
            <Text size="xs" c="dimmed">
              Loading streams…
            </Text>
          </Stack>
        </Center>
      )}

      {!error && streams && streams.length === 0 && (
        <Text size="sm" c="dimmed" p="xs">
          {playlist === ALL_STREAMS
            ? 'No finished streams found for this channel yet.'
            : 'This playlist holds no usable videos - its entries may be ' +
              'deleted or private. Pick another playlist, or "All streams".'}
        </Text>
      )}

      {!error && streams && streams.length > 0 && (
        // minHeight is deliberate, not decoration - same reasoning and the
        // same 120px as HitList.tsx's own ScrollArea: a flex item that is
        // itself a scroll container gets an automatic minimum size of 0
        // (the CSS scroll-container exemption), while the header above it
        // (the playlist select, and - worst case - a long failed-playlists
        // Alert) does not. With no floor, a short viewport and a fully
        // loaded header starve this area to 0px and the list becomes
        // entirely unreachable - measured at 1280x520 with 12 failed
        // playlists, where 40 real mouse-wheel steps moved the target row's
        // bounding box by zero. The Alert above is itself bounded (see its
        // own comment) so the two floors do not fight over the same space
        // without limit.
        <ScrollArea style={{ flex: 1, minHeight: 120 }} offsetScrollbars>
          <Stack gap={6}>
            {streams.map((stream) => {
              const ids = entries[stream.video_id] ?? {}
              // Per LEG, not per row: only the button whose own enqueue POST
              // is actually in flight relabels - the other stays live, so a
              // single-row Transcribe click never makes the Detect button
              // beside it read "Queued…" too (and vice versa).
              const transcribeBusy = busyLegs.has(legKey(stream.video_id, 'transcribe'))
              const detectBusy = busyLegs.has(legKey(stream.video_id, 'detect'))
              const transcribeWork = ids.transcribe ? work.byId[ids.transcribe] : undefined
              const detectWork = ids.detect ? work.byId[ids.detect] : undefined
              return (
                <Stack
                  key={stream.video_id}
                  gap={4}
                  p="xs"
                  style={{
                    border: '1px solid var(--mantine-color-dark-6)',
                    borderRadius: 'var(--mantine-radius-sm)',
                  }}
                >
                  <Group gap="xs" wrap="nowrap" align="flex-start">
                    <Checkbox
                      size="xs"
                      mt={4}
                      aria-label={`Select ${stream.title}`}
                      checked={selected.includes(stream.video_id)}
                      onChange={(changeEvent) => setSelected((held) =>
                        changeEvent.currentTarget.checked
                          ? [...held, stream.video_id]
                          : held.filter((id) => id !== stream.video_id))}
                    />
                    <Text
                      size="sm"
                      fw={500}
                      lineClamp={2}
                      style={{ fontFamily: TOWER_FONT, cursor: 'pointer' }}
                      onClick={() => navigate(routePath({
                        screen: 'stream', channel, event, videoId: stream.video_id,
                      }))}
                      role="button"
                      tabIndex={0}
                      // Named `keyEvent`, not `event`: this component already has an
                      // `event` prop (the event slug) in scope, and shadowing it with
                      // the keyboard event here would silently send the KeyboardEvent
                      // object itself as routePath's `event` field.
                      onKeyDown={(keyEvent) => {
                        if (keyEvent.key === 'Enter' || keyEvent.key === ' ') {
                          keyEvent.preventDefault()
                          navigate(routePath({
                            screen: 'stream', channel, event, videoId: stream.video_id,
                          }))
                        }
                      }}
                    >
                      {stream.title}
                    </Text>
                  </Group>
                  <Group justify="space-between" wrap="wrap" gap="xs">
                    <Group gap="md" wrap="nowrap">
                      <Text size="xs" c="dimmed" ff="monospace" className="tnum">
                        {formatStreamDuration(stream.duration_seconds)}
                      </Text>
                      <Text size="xs" c="dimmed" ff="monospace" className="tnum">
                        {formatViews(stream.view_count)} views
                      </Text>
                      {stream.has_transcript && (
                        <Badge size="xs" variant="dot" color="teal">
                          Transcript
                        </Badge>
                      )}
                      {stream.has_analysis && (
                        <Badge size="xs" variant="dot" color="grape">
                          Analysis
                        </Badge>
                      )}
                    </Group>
                    <Group gap={6} wrap="nowrap">
                      {/* Always force (ROW_FORCE), never the panel's own
                          `force` state the bar below uses - see ROW_FORCE's
                          own comment for why a row click is exempt from the
                          bar's skip-if-it-exists default. */}
                      <Tooltip label={transcribeTooltip(stream.has_transcript)}>
                        <Button
                          size="xs"
                          variant={transcribeWork?.running ? 'light' : 'default'}
                          color={transcribeWork?.running ? 'steel' : undefined}
                          disabled={transcribeBusy || transcribeWork?.pending}
                          loading={transcribeWork?.running}
                          onClick={() => onQueue(
                            'transcribe', [stream.video_id], ROW_FORCE, allVideos)}
                        >
                          {transcribeWork?.running ? 'Transcribing…'
                            : transcribeBusy || transcribeWork?.pending ? 'Queued…' : 'Transcribe'}
                        </Button>
                      </Tooltip>
                      <Tooltip label={detectTooltip(stream.has_analysis)}>
                        <Button
                          size="xs"
                          variant={detectWork?.running ? 'light' : 'default'}
                          color={detectWork?.running ? 'steel' : undefined}
                          disabled={detectBusy || detectWork?.pending}
                          loading={detectWork?.running}
                          onClick={() => onQueue(
                            'detect', [stream.video_id], ROW_FORCE, allVideos)}
                        >
                          {detectWork?.running ? 'Detecting…'
                            : detectBusy || detectWork?.pending ? 'Queued…' : 'Detect moments'}
                        </Button>
                      </Tooltip>
                    </Group>
                  </Group>
                  {/* Whichever of this row's two entries exist - the badge,
                      the log link, waitNote and a stop's own reason. */}
                  {transcribeWork && <TrackedEntry label="Transcribe" work={transcribeWork} />}
                  {detectWork && <TrackedEntry label="Detect" work={detectWork} />}
                </Stack>
              )
            })}
          </Stack>
        </ScrollArea>
      )}

      {selected.length > 0 && (
        <Stack gap={6} p="xs" style={{
          borderTop: '1px solid var(--mantine-color-dark-6)',
        }}>
          <Group justify="space-between" wrap="wrap" gap="xs">
            <Text size="xs" c="dimmed">
              {selected.length} selected{hidden ? ` (${hidden})` : ''}
            </Text>
            <Group gap={6} wrap="nowrap">
              <Button size="xs" variant="default"
                      onClick={() => setSelected((held) =>
                        // ADDS the visible ids rather than replacing the
                        // selection outright - a plain `setSelected(visibleIds)`
                        // contradicted the very reason selection survives a
                        // filter change (see `selectionNote`'s own docstring: a
                        // race weekend split across two playlists is the
                        // ordinary case this exists for), by silently dropping
                        // every off-view pick the moment this button was
                        // pressed. `Set` dedupes rather than growing the array
                        // with a re-tick of a row already selected.
                        Array.from(new Set([...held, ...visibleIds])))}>
                Select all shown
              </Button>
              <Button size="xs" variant="subtle" onClick={() => setSelected([])}>
                Clear
              </Button>
            </Group>
          </Group>

          {/* What WILL happen, before the click. A button that silently
              does nothing is the same lying control as a spinner that
              never moves - so when everything is skipped, the button is
              disabled and this line is the reason. */}
          {(['transcribe', 'detect', 'both'] as BulkAction[]).map((action) => {
            const planned = bulkPlan(selected, allVideos, action, force)
            // Both halves, always - not `planned.note ?? countSentence`. The
            // count of what WILL happen used to vanish exactly when
            // something was skipped, which is the one case an operator
            // cannot infer it (the design document's own example is "3
            // selected · 2 transcriptions skipped: already transcribed · 1
            // will be queued" - the bar was rendering only the middle
            // clause). `planned.note` already ends its own sentence with a
            // period.
            const countSentence = `${planned.steps.length} will be queued`
            const summary = planned.note ? `${planned.note} ${countSentence}.` : countSentence
            // Disabled and relabelled while ANY enqueue is in flight, from
            // this bar OR a row: MUST-FIX 1 in the review this closes. A
            // second click during `handleQueueStreams`'s async loop of
            // sequential POSTs recomputed the identical plan and queued the
            // whole batch AGAIN - N duplicate multi-hour transcriptions or N
            // duplicate paid detections, invisibly, because `setStreamEntries`'s
            // per-video merge let the second batch's entry id silently
            // overwrite the first's. `busyLegs` already carries exactly the
            // state needed; gating on the whole set rather than a per-action
            // refcount is deliberate and closes M11 from the same review at
            // the same time - a refcount exists to let unrelated concurrent
            // uses of a shared resource coexist safely, and here there are
            // none LEFT to coexist: every path that can add to `busyLegs`
            // (a row click, this bar) is itself blocked while the set is
            // non-empty, of THIS component's own row buttons via `disabled=
            // {transcribeBusy || ...}` above and of this bar via the same
            // check right here - so two concurrent batches touching the same
            // leg are no longer reachable, and the refcount that would guard
            // against them un-busying each other's slot early has nothing
            // left to protect. Do not "simplify" this guard away on the
            // reasoning that busyLegs is "just a Set" - removing it reopens
            // exactly that.
            const busy = busyLegs.size > 0
            return (
              <Group key={action} justify="space-between" wrap="nowrap" gap="xs">
                <Text size="xs" c="dimmed">
                  {summary}
                </Text>
                <Button
                  size="xs"
                  variant="default"
                  disabled={planned.steps.length === 0 || busy}
                  loading={busy}
                  onClick={() => onQueue(action, selected, force, allVideos)}
                >
                  {/* `get_by_role` matches a name as a substring by default, so a
                      bar button whose name CONTAINS a row button's name
                      makes every non-exact lookup in the suite ambiguous
                      the moment a row is ticked. These three share no
                      substring with the row buttons ("Transcribe"/"Detect
                      moments"). "Queuing…" shares no substring with any of
                      the three idle labels either, so it stays unambiguous
                      the same way. */}
                  {busy ? 'Queuing…' : action === 'both' ? 'Queue transcription and detection for selected'
                    : action === 'transcribe'
                      ? 'Queue transcription for selected' : 'Queue detection for selected'}
                </Button>
              </Group>
            )
          })}

          <Group gap="md" wrap="wrap">
            <Checkbox size="xs" label="Re-transcribe anyway"
                      checked={forceTranscribe}
                      onChange={(e) => setForceTranscribe(e.currentTarget.checked)} />
            <Checkbox size="xs" label="Re-detect anyway"
                      checked={forceDetect}
                      onChange={(e) => setForceDetect(e.currentTarget.checked)} />
          </Group>
        </Stack>
      )}
    </Stack>
  )
}
