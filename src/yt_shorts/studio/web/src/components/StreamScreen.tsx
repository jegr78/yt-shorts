import { useEffect, useRef, useState } from 'react'
import {
  Alert, Anchor, Badge, Button, Center, Grid, Group, Loader, Stack, Text, TextInput,
} from '@mantine/core'
import { useMediaQuery } from '@mantine/hooks'
import { notifications } from '@mantine/notifications'

import {
  ApiError, createClipFromWindow, enqueueJob, estimateRun, getStreamAnalysis, getStreams,
  getStreamTranscript,
} from '../api'
import type { RunEstimate, StreamAnalysis, StreamTranscript } from '../api'
import { formatStreamDuration } from '../format'
import { useQueuedJob } from '../hooks/useQueuedJob'
import { endedNotice, jobLogFile, stateColor } from '../jobs'
import { HitList } from './HitList'
import { NavScreen } from './NavScreen'
import { StreamPlayer } from './StreamPlayer'
import { StreamTimeline } from './StreamTimeline'
import { TranscriptPane } from './TranscriptPane'
import type { TranscriptPaneHandle } from './TranscriptPane'
import type { Moment } from '../momentList'
import { routePath } from '../scopedApi'
import { navigate } from '../useRoute'
import { clampZoom, zoomAround } from '../streamTimeline'
import type { Zoom } from '../streamTimeline'

/**
 * One stream: its transcript, its activity curve and its detected moments.
 *
 * Reachable at /{channel}/{event}/streams/{video_id}, and the ORDER of what it
 * loads is the design. The transcript is what the screen is for; the analysis
 * is an overlay on it. A stream that has never been analysed still opens, still
 * searches and still lets a window be picked by hand - so the analysis failing
 * to exist is not an error state here, it is the ordinary starting state.
 */
export function StreamScreen({ channel, event, videoId }: {
  channel: string
  event: string
  videoId: string
}) {
  const [transcript, setTranscript] = useState<StreamTranscript | null>(null)
  const [analysis, setAnalysis] = useState<StreamAnalysis | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [currentTime, setCurrentTime] = useState(0)
  // The real stream title, looked up from the stream LIST rather than the
  // analysis - see the docstring below on why both are needed.
  const [streamListTitle, setStreamListTitle] = useState<string | null>(null)
  const [zoom, setZoom] = useState<Zoom>({ start: 0, end: 180 })
  const [selection, setSelection] = useState<{ start: number; end: number } | null>(null)
  // Imperative handle onto the transcript pane, used ONLY by handlePick below
  // - see TranscriptPane's own module docstring for why a ref rather than an
  // effect on currentTime.
  const transcriptRef = useRef<TranscriptPaneHandle>(null)
  const [hook, setHook] = useState('')
  const [creating, setCreating] = useState(false)
  // Not fetched on mount, deliberately - see handleEstimate below and
  // estimate.py's own docstring. Nulled out to nothing until the operator
  // asks, and never auto-refreshed: it is a snapshot of one click, not a
  // live number that should silently go stale.
  const [runEstimate, setRunEstimate] = useState<RunEstimate | null>(null)
  const [estimating, setEstimating] = useState(false)
  const [queueingTranscribe, setQueueingTranscribe] = useState(false)
  // The transcribe entry this screen is following, and its live state. Owned
  // here rather than by a parent because this screen has no parent that
  // outlives it - it IS the route (see App.tsx for the editor's equivalent
  // state, which is hoisted for a reason that does not apply here).
  const [transcribeEntryId, setTranscribeEntryId] = useState<string | null>(null)
  const transcribeWork = useQueuedJob(transcribeEntryId)
  // Mirrors Grid.Col's own span={{ base: 12, md: 4/8 }} breakpoint below
  // (Mantine's `md`, 992px / 62em) rather than inventing a second one - see
  // the layout comment on the Grid itself for what this drives and why.
  const isWide = useMediaQuery('(min-width: 992px)')

  function load() {
    setError(null)
    getStreamTranscript(channel, event, videoId)
      .then(setTranscript)
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
    // The analysis is loaded independently and its failure is NOT surfaced as a
    // screen error: the route answers 200 with an empty analysis when detection
    // has never run, and anything worse still leaves a usable transcript.
    getStreamAnalysis(channel, event, videoId).then(setAnalysis).catch(() => setAnalysis(null))
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [channel, event, videoId])

  useEffect(() => {
    // A THIRD, independent load, deliberately never awaited before rendering:
    // the analysis's own `stream_title` is cached at detection time and may
    // well be "", which is why the heading used to show the video id twice.
    // It is no longer routine: a detect QUEUED from this app sends the title
    // the browser already has (see StreamPanel's enqueueJob), so the empty
    // one now comes from the direct route only - `post_detect` reads the
    // studio's process-lifetime stream cache and stores "" when it is cold,
    // and an analysis written before the queued path existed keeps whatever
    // it recorded then. So this lookup stays. The stream LIST
    // route knows the real title, but it can call out to yt-dlp on a cold
    // per-channel cache and be slow - so this must never block the transcript
    // or the analysis, and its failure is as silent as the analysis's own (the
    // heading just falls back to the video id, same as before this lookup
    // existed).
    setStreamListTitle(null)
    getStreams(channel, event)
      .then((catalogue) => {
        const found = catalogue.videos.find((video) => video.video_id === videoId)
        if (found) setStreamListTitle(found.title)
      })
      .catch(() => {
        // silent - see the comment above
      })
  }, [channel, event, videoId])

  const duration = analysis?.duration_seconds || transcript?.duration_seconds || 0

  useEffect(() => {
    // Re-clamp when the duration arrives: the initial zoom is a guess made
    // before anything is loaded, and on a stream shorter than 180 s it would
    // otherwise show time that does not exist.
    //
    // Guarded on duration > 0, not seeded once via a ref: on mount duration
    // is 0 (neither load has resolved yet), and clampZoom(_, 0) treats that
    // as a zero-length stream and collapses the window to {0,0} - which is
    // then re-clamped to MIN_ZOOM_SECONDS on the NEXT run, i.e. the reported
    // permanent-10-second bug. Skipping while duration is still 0 avoids
    // that collapse entirely, and a plain guard is enough (no need for a
    // "have we seeded yet" ref) because clampZoom is idempotent on a window
    // that already fits: once duration is known and the effect seeds
    // {0,180} (or the whole stream, if shorter), a LATER duration update -
    // the transcript's and the analysis's durations can arrive in either
    // order - just re-clamps that same good window against the new number
    // and gets it back unchanged, rather than re-running the 0-duration
    // guess.
    if (duration <= 0) return
    setZoom((current) => clampZoom(current, duration))
  }, [duration])

  // Prefer, in order: the analysis's own cached title (non-empty), then the
  // stream list's title, then the video id itself. When the resolved title
  // IS the video id, the subtitle is dropped rather than repeating it on a
  // second line - see this component's own docstring above.
  const title = analysis?.stream_title || streamListTitle || videoId
  const subtitle = title === videoId ? undefined : videoId

  // Picking a moment does three things at once - it is the whole point of
  // the hit list: move the clip-window selection, re-centre the zoom lane
  // on it, and bring both the player and the transcript to it. The
  // transcript scroll goes through the ref rather than the currentTime
  // state change above it, precisely because this is the one place a
  // "jump" genuinely happens - see TranscriptPane's own docstring.
  function handlePick(moment: Moment) {
    setSelection({ start: moment.start, end: moment.end })
    setZoom(zoomAround((moment.start + moment.end) / 2, 180, duration))
    setCurrentTime(moment.start)
    transcriptRef.current?.scrollToTime(moment.start)
  }

  // The one place this screen writes anything (see api.py's
  // post_clip_from_window and CLAUDE.md's amended studio write boundary):
  // turns the chosen window into a real clip directory. Disabled rather than
  // hidden without a selection - a control that vanishes leaves the operator
  // wondering where it went, while a disabled one that names the window it
  // would cut says what is missing.
  async function handleCreate() {
    if (!selection) return
    setCreating(true)
    try {
      const { name } = await createClipFromWindow(channel, event, videoId, {
        start: selection.start, end: selection.end, hook,
        // The same resolution the heading uses (see `title` above), minus its
        // video-id fallback: a video id is not a title, and on the
        // never-analysed path `analysis` is null, so this used to always send
        // "" here regardless of whether the stream LIST already had the real
        // title (see this component's own docstring on why that lookup
        // exists at all). Falling back to "" rather than `title` keeps a bare
        // video id out of youtube_upload.DEFAULT_DESCRIPTION's "Clip from
        // {source_title}." on a hand-uploaded short.
        source_title: analysis?.stream_title || streamListTitle || '',
      })
      notifications.show({ message: `Created ${name}.`, color: 'green' })
      setHook('')
    } catch (err) {
      // The server's message is the useful half here: it names both windows on
      // a collision and the directory on an unreadable neighbour.
      notifications.show({
        title: 'Could not create the clip', color: 'red',
        message: err instanceof ApiError ? err.message : String(err),
      })
    } finally {
      setCreating(false)
    }
  }

  // Queues a whole-stream transcription (POST /api/jobs, kind "transcribe").
  // This screen is where an operator lands on the dead end: detection refuses
  // a stream with no cached transcript, and the transcript route 404s, so
  // without this the only way forward was to send the POST by hand - which is
  // what the alert below used to say. Enqueuing takes no EventLock, so it is
  // never blocked by whatever else is running for this event; the worker
  // takes the lock when it starts the entry.
  //
  // The entry it answers with is KEPT (`transcribeEntryId` -> `useQueuedJob`),
  // and that is the whole difference between an honest button and a lying
  // one. It used to be discarded on the spot, and the notification asserted
  // "It starts as soon as the worker is free" without ever consulting
  // `worker_running` - which is precisely the sentence `waitNote` exists to
  // prevent, on the LONGEST kind there is. Now the click says only what it
  // knows (how many are ahead), and the panel below says why it has not
  // started, for as long as that is true.
  async function handleTranscribe() {
    setQueueingTranscribe(true)
    try {
      const { entry, queued_ahead } = await enqueueJob('transcribe', {
        channel, event, video_id: videoId,
      })
      setTranscribeEntryId(entry.id)
      notifications.show({
        color: 'steel',
        title: 'Transcription queued',
        message:
          queued_ahead === 0
            ? 'Nothing is queued in front of it. Follow it here, or on the Jobs screen.'
            : `It waits behind ${queued_ahead} other entr${queued_ahead === 1 ? 'y' : 'ies'}. ` +
              'Follow it here, or on the Jobs screen.',
      })
    } catch (err) {
      notifications.show({
        title: 'Could not queue the transcription', color: 'red',
        message: err instanceof ApiError ? err.message : String(err),
      })
    } finally {
      setQueueingTranscribe(false)
    }
  }

  // A finished transcription is exactly what this screen is waiting for: it
  // is the thing that turns the error state above into a usable screen. So
  // reload on `done` rather than making the operator work out that they
  // should. Keyed on the ENTRY's outcome, like every other queued action in
  // this app - an entry the worker could never start has no Job to ask.
  useEffect(() => {
    const outcome = transcribeWork.outcome
    if (outcome === null) return
    if (outcome === 'done') {
      notifications.show({
        title: 'Transcription finished',
        message: 'Reloading this stream.', color: 'green',
      })
      load()
    } else {
      // A stop is never relabelled a failure - `endedNotice` is where that
      // decision lives, for this panel and the two in App.tsx alike.
      notifications.show({ ...endedNotice(
        'Transcription', 'transcribe', outcome,
        transcribeWork.job?.results.transcribe?.reason
          ?? transcribeWork.entry?.reason ?? null) })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [transcribeWork.outcome])

  // What a detection run over this stream would cost - asked for, never
  // fetched on mount (see estimate.py's own docstring: this is information
  // the operator requests before deciding whether to configure a key, not a
  // number that should appear unbidden). Local and approximate, so it works
  // with no key and no network at all.
  async function handleEstimate() {
    setEstimating(true)
    try {
      setRunEstimate(await estimateRun(channel, event, videoId, null))
    } catch (err) {
      notifications.show({
        title: 'Could not estimate the run', color: 'red',
        message: err instanceof ApiError ? err.message : String(err),
      })
    } finally {
      setEstimating(false)
    }
  }

  // Read once, so the badge, the log link and the button below all describe
  // the same entry - and so nothing needs a non-null assertion to reach it.
  const transcribeEntry = transcribeWork.entry
  const transcribeLogFile = transcribeEntry === null ? null : jobLogFile(transcribeEntry)

  return (
    <NavScreen
      crumbs={[
        { label: 'Channels', path: routePath({ screen: 'channels' }) },
        { label: channel, path: routePath({ screen: 'events', channel }) },
        { label: event, path: routePath({ screen: 'editor', channel, event }) },
      ]}
      title={title}
      subtitle={subtitle}
      fillHeight
    >
      {/* The tracked transcription, above everything else this screen shows.
          Outside the error alert on purpose: the alert is what disappears
          when the transcript finally loads, and this has to survive the
          entry's whole life - including a failure that leaves the alert
          exactly as it was. `waiting` is the sentence that replaces the old
          notification's unchecked "It starts as soon as the worker is free". */}
      {transcribeEntry && (
        <Stack gap={4} pb="xs">
          <Group gap="xs" wrap="nowrap">
            <Badge color={stateColor(transcribeEntry.state)} variant="dot" size="xs">
              Transcribe: {transcribeEntry.state}
            </Badge>
            {/* From the ENTRY's job_id, never its own id: the two id spaces
                are disjoint, and the link simply is not drawn until the
                worker has claimed the entry (see jobs.ts's jobLogFile). */}
            {transcribeLogFile && (
              <Anchor
                size="xs"
                c="steel.3"
                onClick={() => navigate(
                  `${routePath({ screen: 'logs' })}?file=` +
                  `${encodeURIComponent(transcribeLogFile)}`)}
              >
                View log
              </Anchor>
            )}
            <Anchor size="xs" c="steel.3" onClick={() => navigate(routePath({ screen: 'jobs' }))}>
              Jobs
            </Anchor>
          </Group>
          {transcribeWork.waiting && (
            <Alert color="yellow" title="Queued - not started yet" p="xs">
              <Text size="xs">{transcribeWork.waiting}</Text>
            </Alert>
          )}
        </Stack>
      )}
      {transcribeWork.error && (
        // Why this screen stopped following the entry (removed on the Jobs
        // screen, or the plan could not be read). Outside the block above,
        // which clears `entry` for a removal.
        <Text size="xs" c="red.4" pb="xs">{transcribeWork.error}</Text>
      )}

      {error && (
        <Alert color="red" title="Could not load this stream">
          <Stack gap="xs" align="flex-start">
            <Text size="sm">{error}</Text>
            <Text size="sm" c="dimmed">
              A stream has a transcript only after it has been transcribed, and
              detection no longer does that itself — it refuses a stream with no
              cached transcript rather than starting an hour of decoding nobody
              asked for. Queue a transcription here, follow it on the Jobs
              screen, and come back when it is done.
            </Text>
            <Group gap="xs">
              <Button
                size="xs"
                color="steel"
                // Three states, not one: the enqueue itself in flight, the
                // entry queued (not started - hence disabled, against a
                // second entry for the same stream), and the work actually
                // running. Only the last may say "Transcribing…".
                loading={queueingTranscribe || transcribeWork.running}
                disabled={transcribeWork.pending}
                onClick={handleTranscribe}
              >
                {transcribeWork.running
                  ? 'Transcribing…'
                  : transcribeWork.pending
                    ? 'Queued…'
                    : 'Transcribe this stream'}
              </Button>
              <Button
                size="xs"
                variant="default"
                onClick={() => navigate(routePath({ screen: 'jobs' }))}
              >
                Jobs
              </Button>
              <Button size="xs" variant="light" onClick={load}>Retry</Button>
            </Group>
          </Stack>
        </Alert>
      )}
      {!error && !transcript && (
        <Center h="100%">
          <Group gap="xs">
            <Loader color="steel" />
            <Text c="dimmed">Loading the transcript…</Text>
          </Group>
        </Center>
      )}
      {!error && transcript && (
        <Grid
          gap="md"
          style={{
            flex: 1,
            minHeight: 0,
            display: 'flex',
            flexDirection: 'column',
            // Below "md" the two Grid.Col spans (12+12, see below) stack
            // onto their own lines instead of sitting side by side, and
            // neither pane keeps a bounded height of its own at that point -
            // see the ".inner" comment for why. That means THIS box is the
            // one thing standing between the stacked content and the
            // operator: it must be the scroll container, or the screen
            // reproduces the exact bug this fix exists for (content past the
            // viewport, no scrollbar, nothing reachable - see
            // index.css's body { overflow: hidden }, which removes the
            // page-level fallback a normal page would have). `NavScreen`'s
            // `fillHeight` already gives this box a bounded height to scroll
            // within; a plain overflowY here is enough, with no need to
            // reach past it for a page-level scroll NavScreen doesn't offer
            // in fillHeight mode.
            ...(isWide ? {} : { overflowY: 'auto' }),
          }}
          // Mantine's Grid wraps its row of columns in an internal ".inner"
          // div that has no height of its own (see GridCol below for the
          // matching problem one level down) - without this, ".inner" sizes
          // to its CONTENT (all 28 hit-list rows stacked) rather than to the
          // space actually available, so the row silently grows past the
          // viewport instead of giving HitList's own ScrollArea something
          // bounded to scroll within. Measured in a real 1280x600 browser,
          // not assumed - the hit list's last row sat off-screen with no
          // amount of scrolling until this was added.
          //
          // flexWrap: 'nowrap' is part of the same fix, not an unrelated
          // tweak: with the default 'wrap', a single line whose content
          // (either column) is taller than the available space is NOT
          // shrunk by align-content - wrap only ever grows a line, never
          // shrinks one below its natural content height - so the two
          // columns still overflowed even once ".inner" itself had a
          // bounded height. That is exactly right ABOVE "md", where the two
          // panes sit side by side and each owns its own internal scroll
          // (HitList's and TranscriptPane's own ScrollAreas) - it is what
          // keeps both panes' content correctly bounded to the viewport
          // instead of pushing it taller.
          //
          // Below "md" it is the wrong fix for a different layout. A
          // reviewer measured, in a real 900x700 browser, that 'nowrap'
          // combined with the two Grid.Col spans both being asked to occupy
          // the full row (base: 12) does not make them wrap onto their own
          // line as Grid's default 'wrap' would - it instead holds them side
          // by side and lets the right-hand column's content push it
          // entirely outside the viewport, with `body { overflow: hidden }`
          // removing even the fallback horizontal scrollbar a plain page
          // would have. There is no reachability trade-off left to accept
          // once nowrap is confined to "md" and up: below it, 'wrap' lets
          // the columns stack into a single column and each pane's own
          // "flex: 1, minHeight: 0" sizing (below) is dropped in favour of
          // natural content height, so the OUTER box above becomes the one
          // scroll container that reaches everything.
          styles={{
            inner: isWide
              ? { flex: 1, minHeight: 0, flexWrap: 'nowrap' }
              : { flexWrap: 'wrap' },
          }}
        >
          <Grid.Col
            span={{ base: 12, md: 4 }}
            style={isWide ? { display: 'flex', minHeight: 0 } : undefined}
          >
            <HitList
              moments={analysis?.moments ?? []}
              engine={analysis?.engine ?? null}
              missingWindows={analysis?.missing_windows ?? []}
              selectedStart={selection?.start ?? null}
              onPick={handlePick}
            />
          </Grid.Col>
          <Grid.Col
            span={{ base: 12, md: 8 }}
            style={isWide ? { display: 'flex', minHeight: 0 } : undefined}
          >
            {/* overflowY here is a SEPARATE fallback from the isWide switch
                above it, for a viewport that is wide enough to keep the two
                panes side by side but too SHORT to fit all three of this
                column's children even after every floor below (StreamPlayer,
                StreamTimeline's two lanes and TranscriptPane's own minHeight)
                does its best to shrink-fit them - measured on a real
                1280x600 browser, where their combined minimum still exceeds
                this column's own bounded height. Without it, that shortfall
                bleeds upward as plain CSS overflow (this Stack's default)
                until it reaches NavScreen's fillHeight box, the one ancestor
                that clips rather than scrolls - past the bottom of the
                viewport, unreachable, the same failure mode as IMPORTANT 1
                just rotated ninety degrees. This does not fight
                TranscriptPane's own "search box stays put while the list
                scrolls" contract (see TranscriptPane.tsx): it only engages
                when content does not already fit, and even then the search
                box merely needs scrolling INTO view once, exactly like the
                player above it - once visible, the nested ScrollArea below
                it still owns its own list scroll independently. */}
            <Stack gap="sm" style={isWide ? { flex: 1, minHeight: 0, overflowY: 'auto' } : undefined}>
              <StreamPlayer videoId={videoId} startAt={currentTime} />
              <StreamTimeline
                duration={duration}
                activity={analysis?.activity ?? []}
                moments={analysis?.moments ?? []}
                zoom={zoom}
                selection={selection}
                onZoomChange={setZoom}
                onSelectionChange={setSelection}
              />
              <Group gap="xs" align="flex-end" wrap="nowrap">
                <TextInput
                  label="Hook"
                  placeholder="On-screen title for this clip"
                  value={hook}
                  onChange={(event) => setHook(event.currentTarget.value)}
                  style={{ flex: 1 }}
                />
                <Button
                  onClick={handleCreate}
                  loading={creating}
                  disabled={!selection}
                >
                  {selection
                    ? `Make a clip (${formatStreamDuration(selection.start)}–${formatStreamDuration(selection.end)})`
                    : 'Make a clip'}
                </Button>
              </Group>
              <Group gap="xs" align="center">
                <Button
                  size="xs"
                  variant="light"
                  onClick={handleEstimate}
                  loading={estimating}
                >
                  Estimate a detection run
                </Button>
                {runEstimate && runEstimate.priced && (
                  // "At least", not "~". The server counts characters and
                  // divides by four; checked against a real invoice that runs
                  // LOW by roughly a factor of two (see estimate.py's
                  // OUTPUT_TOKENS_PER_WINDOW). An operator deciding whether to
                  // spend needs the direction of the error, not a tilde that
                  // reads as "could go either way".
                  <Text size="xs" c="dimmed">
                    At least ${runEstimate.usd.toFixed(3)} with {runEstimate.model} over{' '}
                    {runEstimate.windows} window(s) — a floor, not a quote. Real
                    runs have come in around twice this.
                  </Text>
                )}
                {runEstimate && !runEstimate.priced && (
                  // usd is 0.0 here too, but that is "unpriced", not "free" -
                  // showing it as a dollar figure would read as a free run.
                  <Text size="xs" c="dimmed">
                    No price data for {runEstimate.model} ({runEstimate.windows} window(s)) —
                    cost unknown, not free.
                  </Text>
                )}
              </Group>
              <TranscriptPane
                ref={transcriptRef}
                words={transcript.words}
                currentTime={currentTime}
                onSeek={setCurrentTime}
              />
            </Stack>
          </Grid.Col>
        </Grid>
      )}
    </NavScreen>
  )
}
