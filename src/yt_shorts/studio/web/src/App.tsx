import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Anchor,
  AppShell,
  Alert,
  Badge,
  Button,
  Center,
  Drawer,
  Group,
  Loader,
  ScrollArea,
  Stack,
  Tabs,
  Text,
  Title,
} from '@mantine/core'
import { notifications } from '@mantine/notifications'
import {
  ApiError,
  enqueueJob,
  extractUploadUrl,
  getAuth,
  getClip,
  listClips,
  setScope,
  startUpload,
  type AuthStatus,
  type ClipDetail,
  type ClipSummary,
  type StreamVideo,
} from './api'
import { batchNotice } from './jobs'
import { bulkPlan, legKey, type BulkAction, type StreamEntryIds } from './streams'
import { navigate } from './useRoute'
import { routePath } from './scopedApi'
import { AuthStatusBar } from './components/AuthStatusBar'
import { ClipList } from './components/ClipList'
import { ClipEditor } from './components/ClipEditor'
import { EventBrandEditor } from './components/EventBrandEditor'
import { EventSummary } from './components/EventSummary'
import { GlossaryEditor } from './components/GlossaryEditor'
import { ManualUploadPanel } from './components/ManualUploadPanel'
import { MomentsEditor } from './components/MomentsEditor'
import { RenderPanel } from './components/RenderPanel'
import { StreamPanel } from './components/StreamPanel'
import { UploadPanel, type StartUploadOptions, type UploadRecord } from './components/UploadPanel'
import { useJobPolling } from './hooks/useJobPolling'
import { useQueuedEntries } from './hooks/useQueuedEntries'
import { useQueuedJob } from './hooks/useQueuedJob'

/** What an operator calls each queue KIND, for the notification once a
 * tracked entry settles - "Transcription finished", "Moment detection
 * stopped". Kept here rather than inlined at the one call site so the
 * finish effect below reads as a sentence rather than a lookup. The ENQUEUE
 * notification does not use this: it is built from the ACTION the operator
 * clicked (see `ACTION_LABEL`), not from a settled entry's own kind, because
 * at enqueue time there may be several kinds queued at once (a 'both'
 * batch) and no single entry yet to read one off of. */
const KIND_LABEL: Record<string, string> = {
  transcribe: 'Transcription',
  detect: 'Moment detection',
}

/** The same labelling, but for a BULK ACTION rather than a settled entry's
 * own kind - `handleQueueStreams` knows the action before any entry exists
 * to read a kind off of. */
const ACTION_LABEL: Record<BulkAction, string> = {
  transcribe: 'Transcription',
  detect: 'Moment detection',
  both: 'Transcription and detection',
}

/** The editor screen, scoped to one {channel, event} the router resolved
 * from the URL. Setting the API scope here - synchronously during render,
 * before any child effect fires a fetch - is what threads {channel, event}
 * through every event-scoped call in api.ts (and {channel} through the auth
 * calls) without changing a single inner component's signature. */
function App({ channel, event }: { channel: string; event: string }) {
  setScope(channel, event)

  const [clips, setClips] = useState<ClipSummary[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [selectedName, setSelectedName] = useState<string | null>(null)
  const [selectedClip, setSelectedClip] = useState<ClipDetail | null>(null)
  const [showDiscarded, setShowDiscarded] = useState(false)
  const [navTab, setNavTab] = useState<'clips' | 'streams'>('clips')
  const [brandOpen, setBrandOpen] = useState(false)
  const [momentsOpen, setMomentsOpen] = useState(false)
  const [glossaryOpen, setGlossaryOpen] = useState(false)
  // A render and a moment detection are QUEUE ENTRIES now, not jobs started
  // on the click (see api.ts's note where startRender/startDetect used to
  // live). What is tracked here is therefore the entry's id, and what each
  // panel shows comes from `useQueuedJob`: the entry's own state, why it has
  // not started if it has not, and - once the worker claims it - the job doing
  // the work. `*Starting` is still just the round trip of the enqueue itself.
  const [renderEntryId, setRenderEntryId] = useState<string | null>(null)
  const [renderStarting, setRenderStarting] = useState(false)
  const renderWork = useQueuedJob(renderEntryId)
  // Every queue entry the Streams tab has created, by video. One map
  // instead of the four variables this used to be (a detect entry id, a
  // transcribe entry id and an "active video" for each): a bulk action
  // creates several at once, and four variables can hold one. It lives
  // HERE, not in StreamPanel, so a row's live state survives switching the
  // navbar's tabs - the same reason the single detect entry was hoisted.
  const [streamEntries, setStreamEntries] =
    useState<Record<string, StreamEntryIds>>({})
  // Which (video, kind) LEGS have an enqueue POST still in flight right now.
  // Distinct from "queued": this is the brief window before the plan knows
  // about them at all. Per LEG rather than per video - a single-row
  // Transcribe click must not make the Detect button on the same row read
  // "Queued…" too; only a 'both' action legitimately marks both at once,
  // because it really is queuing both. Keyed as `${videoId}:${kind}`.
  //
  // Also what `StreamPanel`'s bulk bar gates ALL THREE of its own buttons on
  // (`busyLegs.size > 0`, disabled and relabelled "Queuing…") - closing the
  // finding that a double-click during the bar's own async batch of
  // sequential POSTs queued the whole batch again, invisibly. This is a
  // plain `Set` with no refcount, and that is deliberate rather than an
  // oversight: a refcount exists to let unrelated concurrent writers share
  // one resource safely, and gating the bar on this set is what removes the
  // only way two concurrent batches could touch the same leg at once - see
  // `StreamPanel.tsx`'s own comment on its bar buttons for the full
  // argument. Do not add a refcount "to be safe"; it would have nothing left
  // to protect against, silently.
  const [queueingLegs, setQueueingLegs] = useState<Set<string>>(new Set())
  const streamEntryIds = useMemo(
    () => Object.values(streamEntries)
      .flatMap((ids) => [ids.transcribe, ids.detect])
      .filter((id): id is string => id !== undefined),
    [streamEntries])
  const streamWork = useQueuedEntries(streamEntryIds)

  // Auth/quota (see GET /api/auth) - true across the whole event, not one
  // clip, so it lives here and is shown in the header (AuthStatusBar) and
  // handed down to UploadPanel for its own "remaining today" line and
  // confirmation copy.
  const [auth, setAuth] = useState<AuthStatus | null>(null)
  const [authError, setAuthError] = useState<string | null>(null)
  const [authLoading, setAuthLoading] = useState(true)

  // Upload state - tracked the same way render/detect are (a single job id
  // polled globally), plus WHICH clip that job belongs to, so a job for
  // one clip is never mistaken for another clip's state after the
  // operator switches selection mid-upload (see UploadPanel's own
  // docstring). `uploadRecords` is the session-only "uploaded" memory the
  // clip summary itself does not carry (see api.ts's extractUploadUrl).
  const [uploadJobId, setUploadJobId] = useState<string | null>(null)
  const [uploadStarting, setUploadStarting] = useState(false)
  const [uploadingClipName, setUploadingClipName] = useState<string | null>(null)
  const [uploadRecords, setUploadRecords] = useState<Record<string, UploadRecord>>({})
  const uploadJob = useJobPolling(uploadJobId)

  async function refreshAuth() {
    try {
      const status = await getAuth()
      setAuth(status)
      setAuthError(null)
    } catch (error) {
      setAuthError(error instanceof ApiError ? error.message : String(error))
    } finally {
      setAuthLoading(false)
    }
  }

  useEffect(() => {
    refreshAuth()
  }, [])

  async function refreshClips() {
    try {
      const list = await listClips()
      setClips(list)
      setLoadError(null)
    } catch (error) {
      setLoadError(error instanceof ApiError ? error.message : String(error))
    }
  }

  useEffect(() => {
    refreshClips()
  }, [])

  useEffect(() => {
    if (!selectedName) {
      setSelectedClip(null)
      return
    }
    let cancelled = false
    getClip(selectedName)
      .then((detail) => {
        if (!cancelled) setSelectedClip(detail)
      })
      .catch((error) => {
        if (!cancelled) {
          notifications.show({
            title: 'Could not load clip',
            message: error instanceof ApiError ? error.message : String(error),
            color: 'red',
          })
        }
      })
    return () => {
      cancelled = true
    }
  }, [selectedName])

  // Once a render ENTRY reaches a terminal state, the clips it touched may
  // have new "has_short" state - refresh the list (and the open clip, if any)
  // so that shows up without the operator reloading the page.
  //
  // Keyed on the entry's outcome rather than on the job's status, and that is
  // not interchangeable: an entry that never started at all (a malformed
  // param, a profile that will not load) has no Job to ask, so a job-keyed
  // effect would never fire for it and the panel would sit on a render that
  // silently ended.
  useEffect(() => {
    if (renderWork.outcome !== null) {
      refreshClips()
      if (selectedName) {
        getClip(selectedName).then(setSelectedClip).catch(() => undefined)
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [renderWork.outcome])

  // A render started OUTSIDE this tab - by the CLI, or in another tab - has
  // nothing to notify us with: the studio's job runner and the CLI
  // deliberately do not know about each other (see CLAUDE.md), so the only
  // evidence a render happened is the files on disk. Refetching when the
  // operator returns to the window is what turns "alt-tab back from the
  // terminal" into a fresh player: the clip's short_version changes, and
  // with it the <video> src.
  //
  // BOTH events, because neither covers the case alone. Switching browser
  // TABS reliably fires visibilitychange; alt-tabbing to another
  // APPLICATION does not do so dependably across platforms and browser
  // versions, and `focus` is what carries that case. When both fire we do
  // two small idempotent GETs, which is a better trade than picking one
  // event and missing half the situations.
  //
  // Safe for unsaved edits, and not by luck: ClipEditor resets its staged
  // title/words/window only when clip.name CHANGES (see its own comment on
  // exactly this), so replacing the clip prop for the same clip leaves an
  // in-progress correction untouched.
  useEffect(() => {
    function refreshIfVisible() {
      if (document.visibilityState === 'hidden') return
      refreshClips()
      if (selectedName) {
        getClip(selectedName).then(setSelectedClip).catch(() => undefined)
      }
    }
    window.addEventListener('focus', refreshIfVisible)
    document.addEventListener('visibilitychange', refreshIfVisible)
    return () => {
      window.removeEventListener('focus', refreshIfVisible)
      document.removeEventListener('visibilitychange', refreshIfVisible)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedName])

  // Once an upload job finishes, record the result against the clip it
  // was FOR (uploadingClipName, captured when the job started - see
  // handleStartUpload) rather than whatever clip happens to be selected
  // now, and re-check quota (see quota.QuotaTracker.book_insert - a
  // successful upload just spent some of today's estimate). `uploadRecords`
  // is only the immediate, session-local echo (see UploadPanel's own
  // docstring on why) - refreshClips()/getClip() below are what make
  // `has_upload`/`upload_url` (the server-authoritative state a reload
  // reads) show up without the operator reloading the page themselves,
  // the same way a finished render already refreshes `has_short`.
  useEffect(() => {
    if (uploadJob && uploadJob.status !== 'running' && uploadingClipName) {
      const name = uploadingClipName
      if (uploadJob.status === 'done') {
        const url = extractUploadUrl(uploadJob, name)
        if (url) {
          setUploadRecords((current) => ({ ...current, [name]: { url } }))
        }
        notifications.show({
          message: url ? `Uploaded - ${url}` : 'Upload finished.',
          color: 'green',
        })
      } else {
        notifications.show({
          title: 'Upload failed',
          message: uploadJob.results[name]?.reason ?? 'See the job log for details.',
          color: 'red',
        })
      }
      refreshAuth()
      refreshClips()
      if (selectedName === name) {
        getClip(name).then(setSelectedClip).catch(() => undefined)
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [uploadJob?.status])

  function handleClipUpdated(updated: ClipDetail) {
    setSelectedClip(updated)
    setClips((current) =>
      current
        ? current.map((c) => (c.name === updated.name ? { ...c, ...summaryOf(updated) } : c))
        : current,
    )
  }

  /** How many entries are already in front of this one, as a sentence - the
   * only half of "queued" that the click itself knows. Everything else about
   * WHY it has not started comes off the plan afterwards (see `waitNote`).
   *
   * Which is why the empty-queue case says "nothing is in front of it" and
   * NOT "it starts as soon as the worker is free", which is what it used to
   * say: that is a claim about the WORKER, and the click has not read
   * `worker_running` - with the worker stopped it is simply false, and it is
   * the exact sentence `waitNote` exists to keep off the screen. */
  function queuedMessage(ahead: number): string {
    return ahead === 0
      ? 'Nothing is queued in front of it. Follow it in the panel, or on the Jobs screen.'
      : `It waits behind ${ahead} other entr${ahead === 1 ? 'y' : 'ies'}. ` +
        'Follow it in the panel, or on the Jobs screen.'
  }

  // Renders and detections are QUEUED, not started. The notification says so
  // in those words: reporting "Render started." for an entry the worker may
  // never claim is exactly the lying button the queue exists to remove - and
  // the panel that owns the action carries the fuller explanation (which of
  // the two reasons it is waiting for) for as long as it waits.
  async function handleRender(clipNames?: string[]) {
    setRenderStarting(true)
    try {
      const { entry, queued_ahead } = await enqueueJob('render', {
        channel, event, clips: clipNames ?? null,
      })
      setRenderEntryId(entry.id)
      notifications.show({
        title: 'Render queued', message: queuedMessage(queued_ahead), color: 'steel',
      })
    } catch (error) {
      notifications.show({
        title: 'Could not queue the render',
        message: error instanceof ApiError ? error.message : String(error),
        color: 'red',
      })
    } finally {
      setRenderStarting(false)
    }
  }

  /**
   * Queue work for one or many streams.
   *
   * A single row's button is a batch of ONE through this same path, so
   * there is one set of rules and not two.
   *
   * Enqueued SEQUENTIALLY, in the order `bulkPlan` returns (catalogue
   * order): parallel requests would scramble the queue's own order, and the
   * plan an operator reads should match the list they were looking at.
   *
   * The chain breaks PER VIDEO. If a video's transcribe POST fails, its
   * detect is not enqueued at all - a detect without its `after` would
   * quietly run on an untranscribed stream and fail with
   * TranscriptNotCached, which reads as a bug in detection rather than as
   * the refused request it is.
   *
   * `plan.steps.length === 0` is a real outcome, not just the bar's own
   * "everything is skipped" display - a row's own click routes through here
   * too, and used to hit this exact branch silently for a stream whose badge
   * already showed a transcript or analysis (see the CRITICAL review finding
   * this comment replaces): the click did nothing at all, no notification, no
   * state change. No current call site can reach it (the bar disables the
   * button on the same computation, the row buttons force), and it exists so
   * that a future call site cannot make a click vanish into silence.
   */
  async function handleQueueStreams(
    action: BulkAction,
    videoIds: string[],
    force: { transcribe: boolean; detect: boolean },
    videos: StreamVideo[],
  ) {
    const plan = bulkPlan(videoIds, videos, action, force)
    if (plan.steps.length === 0) {
      notifications.show({
        title: 'Nothing queued',
        message: plan.note ?? 'Nothing to queue for this selection.',
        color: 'steel',
      })
      return
    }
    const legs = new Set<string>()
    for (const step of plan.steps) {
      if (step.transcribe) legs.add(legKey(step.videoId, 'transcribe'))
      if (step.detect) legs.add(legKey(step.videoId, 'detect'))
    }
    // Functional updates, union on entry - two concurrent batches (a row
    // click while the bar's own batch is still enqueueing) must compose
    // rather than the second batch's Set replacing the first's outright,
    // which used to un-busy the first batch's legs mid-flight.
    setQueueingLegs((current) => new Set([...current, ...legs]))
    const titles = new Map(videos.map((video) => [video.video_id, video.title]))
    const created: Record<string, StreamEntryIds> = {}
    const refusals: string[] = []

    try {
      for (const step of plan.steps) {
        const ids: StreamEntryIds = {}
        try {
          if (step.transcribe) {
            const { entry } = await enqueueJob('transcribe', {
              channel, event, video_id: step.videoId,
            })
            ids.transcribe = entry.id
          }
        } catch (error) {
          refusals.push(error instanceof ApiError ? error.message : String(error))
          continue          // no transcript coming, so no detect for this one
        }
        try {
          if (step.detect) {
            const { entry } = await enqueueJob('detect', {
              channel, event, video_id: step.videoId,
              stream_title: titles.get(step.videoId) ?? '',
            }, ids.transcribe ?? null)
            ids.detect = entry.id
          }
        } catch (error) {
          refusals.push(error instanceof ApiError ? error.message : String(error))
        }
        created[step.videoId] = ids
      }
    } finally {
      // Difference, the other half of the union above - this batch's own
      // legs come off, whatever else has joined `queueingLegs` since.
      setQueueingLegs((current) => {
        const next = new Set(current)
        for (const leg of legs) next.delete(leg)
        return next
      })
    }

    // Merge per VIDEO rather than replace: a video already tracked for one
    // leg (say `{detect: e2}`, from an earlier "Transcribe + detect") must
    // keep that leg when this batch adds another (a lone re-Transcribe
    // click, `{transcribe: e3}`) - `{ ...held, ...created }` replaced the
    // whole StreamEntryIds and silently dropped e2, its badge, its waitNote
    // and its own finish notification.
    setStreamEntries((held) => {
      const next = { ...held }
      for (const [videoId, ids] of Object.entries(created)) {
        next[videoId] = { ...held[videoId], ...ids }
      }
      return next
    })
    // Count only videos that actually got an entry id - not
    // `Object.keys(created).length`, which also counted a video whose ONLY
    // requested leg was `detect` and whose POST threw: the loop above still
    // runs `created[step.videoId] = ids` with `ids` left `{}`, so a video
    // for which nothing was queued at all used to be counted as queued
    // ("1 queued" for zero actual entries).
    const queued = Object.values(created).filter(
      (ids) => ids.transcribe !== undefined || ids.detect !== undefined).length
    if (refusals.length === 0) {
      // A single row's action keeps the specific label an operator already
      // knows ("Transcription queued") rather than a bare count - the same
      // sentence a batch of one has always shown. A real batch (queued > 1)
      // has no single kind to name, so it falls back to the count.
      notifications.show({
        title: queued === 1 ? `${ACTION_LABEL[action]} queued` : `${queued} queued`,
        message: 'Queued, not started - the Streams tab shows each one\'s ' +
          'state, and the Jobs screen has the whole plan.',
        color: 'steel',
      })
    } else {
      // Never a bare "queued" when part of it was refused.
      notifications.show({
        title: `${queued} queued, ${refusals.length} refused`,
        message: refusals[0],
        color: 'red',
      })
    }
  }

  // Entries that turn terminal together, in the SAME 750ms poll, are
  // reported together in one notification rather than one per entry -
  // batchNotice delegates to endedNotice for a batch of one, so a
  // single-row action still says exactly what it always said, and a STOP is
  // never red. This does not mean a genuine thirteen-stream batch raises one
  // toast: with the `cpu` pool limit at 1 (see CLAUDE.md), thirteen
  // transcriptions finish hours apart, not together, and DO raise thirteen
  // toasts - one per poll that finds a newly-settled entry. What this groups
  // is entries that happen to land in the same poll, which for a fast kind
  // (or a `net`-pool batch of several detects finishing close together) can
  // genuinely be more than one.
  //
  // Detection creates no clips of its own (see detect.py's own module
  // docstring - a clip exists only once an operator picks a window via
  // clip_from_moment.create_clip), and neither does a finished transcription,
  // so unlike the render-finish effect above, nothing here calls
  // refreshClips(): there is nothing for it to find. But `streamCatalogueStaleAt`
  // (below) IS bumped here, because something else genuinely goes stale when
  // a tracked entry finishes: the Streams tab's own has_transcript/
  // has_analysis badges (see MUST-FIX 3 in the review this closes). Bumping
  // it once per settle, in the same place the notification fires, is what
  // keeps the two in lockstep without StreamPanel having to poll on its own.
  const settledRef = useRef<Set<string>>(new Set())
  // A signal, not a boolean or the catalogue itself: `App.tsx` owns the
  // tracked entries, `StreamPanel` owns `load()` and the catalogue state -
  // this is deliberately just a counter StreamPanel's own effect reacts to,
  // never the data. Bumped ONLY on a genuine settle (never on every poll),
  // so it triggers exactly one re-read per finished entry rather than one
  // every 750ms for as long as anything is tracked.
  const [streamCatalogueStaleAt, setStreamCatalogueStaleAt] = useState(0)
  useEffect(() => {
    const ended: { outcome: string; reason: string | null }[] = []
    // The KIND comes from the entries themselves, and only matters for a
    // batch of one: that is the case batchNotice delegates to endedNotice,
    // whose KEPT_AFTER_A_STOP sentence is per kind ("chunks already decoded
    // stay cached" for a transcribe, "every window it had already scored"
    // for a detect). Hard-coding one would tell a stopped detection what a
    // stopped transcription keeps.
    let kind = 'transcribe'
    for (const [id, work] of Object.entries(streamWork.byId)) {
      if (work.outcome === null || settledRef.current.has(id)) continue
      settledRef.current.add(id)
      if (work.entry) kind = work.entry.kind
      ended.push({ outcome: work.outcome, reason: work.entry?.reason ?? null })
    }
    if (ended.length === 0) return
    setStreamCatalogueStaleAt((token) => token + 1)
    // A batch of one keeps the specific label an operator already knows
    // ("Moment detection finished") rather than a generic "The stream job" -
    // the same sentence a single detect or transcribe has always shown.
    notifications.show({ ...batchNotice(
      ended.length === 1 ? (KIND_LABEL[kind] ?? 'The stream job') : 'Stream jobs',
      kind, ended) })
  }, [streamWork])

  async function handleStartUpload(name: string, opts: StartUploadOptions) {
    setUploadingClipName(name)
    setUploadStarting(true)
    try {
      const { job_id } = await startUpload(name, {
        visibility: opts.visibility,
        publish_at: opts.publishAt,
        confirm: opts.confirm,
        force: opts.force,
      })
      setUploadJobId(job_id)
      notifications.show({
        message: opts.force ? 'Re-upload started.' : 'Upload started.',
        color: 'steel',
      })
    } catch (error) {
      setUploadingClipName(null)
      notifications.show({
        title: 'Could not start upload',
        message: error instanceof ApiError ? error.message : String(error),
        color: 'red',
      })
    } finally {
      setUploadStarting(false)
    }
  }

  const keptClips = (clips ?? []).filter((c) => c.status === 'kept')
  // "running" here means the WORK is in flight, never merely that it was
  // queued: a queued render has not opened a single edit.json, so freezing
  // the editor for it (below) would lock the operator out for as long as the
  // entry waits - which, with the worker stopped, is forever.
  const rendering = renderWork.running
  // Any TRACKED detect entry running, across however many the Streams tab
  // has queued - a bulk detect can have several in flight for one event,
  // and any one of them holds the same EventLock a render or an upload
  // would need.
  const detecting = Object.values(streamWork.byId).some(
    (work) => work.running && work.entry?.kind === 'detect')
  // Only one job (render, detect OR upload) ever works on this event at once
  // - studio/jobs.py's EventLock, shared by start_render_job,
  // start_detect_job and start_upload_job. That is still true, but it no
  // longer has to be a DISABLED BUTTON for the two kinds that go through the
  // queue: enqueuing takes no lock at all (it writes one line into the plan),
  // and the worker waits for the lock rather than failing, so a render or a
  // detect may be queued at any time. Only the UPLOAD button still refuses,
  // because that one really does start its job on the click and really would
  // 409 - `blockedByForUpload` names which other job is the reason.
  const blockedByForUpload = rendering
    ? 'A render'
    : detecting
      ? 'Moment detection'
      : null

  return (
    <AppShell header={{ height: 52 }} navbar={{ width: 400, breakpoint: 'sm' }} padding="md">
      <AppShell.Header
        style={{ background: 'var(--mantine-color-dark-9)', borderColor: 'var(--mantine-color-dark-6)' }}
      >
        <Group h="100%" px="md" justify="space-between" wrap="nowrap">
          <Group gap="sm" wrap="nowrap">
            <Title order={4} tt="uppercase" style={{ letterSpacing: '0.06em' }}>
              YT-Shorts Studio
            </Title>
            {/* Breadcrumb back up the two navigation screens. The event
                itself is the current location, so it is plain text, not a
                link. */}
            <Group gap={6} wrap="nowrap" visibleFrom="sm">
              <Text c="dimmed" size="sm">/</Text>
              <Anchor size="sm" c="steel.3" onClick={() => navigate(routePath({ screen: 'channels' }))}>
                Channels
              </Anchor>
              <Text c="dimmed" size="sm">/</Text>
              <Anchor
                size="sm"
                c="steel.3"
                onClick={() => navigate(routePath({ screen: 'events', channel }))}
              >
                {channel}
              </Anchor>
              <Text c="dimmed" size="sm">/</Text>
              <Text size="sm" ff="monospace">{event}</Text>
            </Group>
          </Group>
          <Group gap="lg" wrap="nowrap">
            <Button variant="default" size="xs" onClick={() => setBrandOpen(true)}>
              Event branding
            </Button>
            <Button variant="default" size="xs" onClick={() => setMomentsOpen(true)}>
              Event moments
            </Button>
            <Button variant="default" size="xs" onClick={() => setGlossaryOpen(true)}>
              Event glossary
            </Button>
            <AuthStatusBar auth={auth} error={authError} loading={authLoading} onRefresh={refreshAuth} />
            <Text size="xs" c="dimmed" ff="monospace" visibleFrom="sm">
              LOCAL EDITOR
            </Text>
          </Group>
        </Group>
      </AppShell.Header>
      {/* The event-level Brand & fonts editor (stage G-event-brand) opens as a
          right-hand Drawer rather than a route of its own - it edits ONE
          event's brand override, not a navigation destination, and a Drawer
          keeps the clip list/editor underneath intact. scrollAreaComponent
          makes the Drawer body itself the scroll container (the same
          ScrollArea.Autosize idiom RenderPanel/WordsEditor use for a bounded
          list), which is what keeps every section plus the live preview
          reachable at a short viewport - the editor's own content does not
          scroll the page, there being no page scroll to fall back on here. */}
      <Drawer
        opened={brandOpen}
        onClose={() => setBrandOpen(false)}
        position="right"
        size="xl"
        title="Event branding"
        scrollAreaComponent={ScrollArea.Autosize}
      >
        <EventBrandEditor channel={channel} event={event} />
      </Drawer>
      {/* Same idiom as the branding Drawer above: one event's own moments-
          lexicon layer, not a navigation destination. MomentsEditor owns its
          own bounded, independently-scrolling list internally (see its own
          docstring on why) rather than relying on this Drawer's
          scrollAreaComponent the way EventBrandEditor does - the built-in
          default's ~39 rows are long enough that the header and Save button
          need to stay reachable without scrolling the whole drawer body. */}
      <Drawer
        opened={momentsOpen}
        onClose={() => setMomentsOpen(false)}
        position="right"
        size="xl"
        title="Event moments"
        scrollAreaComponent={ScrollArea.Autosize}
      >
        <MomentsEditor channel={channel} event={event} />
      </Drawer>
      {/* Same idiom as the two Drawers above: one event's own glossary layer,
          not a navigation destination. GlossaryEditor owns its own bounded,
          independently-scrolling lists internally (see its own docstring)
          rather than relying on this Drawer's scrollAreaComponent - the
          selected track pack alone fills both lists past a drawer's height, and
          the header and Save button must stay reachable without scrolling the
          whole drawer body. */}
      <Drawer
        opened={glossaryOpen}
        onClose={() => setGlossaryOpen(false)}
        position="right"
        size="xl"
        title="Event glossary"
        scrollAreaComponent={ScrollArea.Autosize}
      >
        <GlossaryEditor channel={channel} event={event} />
      </Drawer>
      <AppShell.Navbar
        p="md"
        style={{
          background: 'var(--mantine-color-dark-8)',
          borderColor: 'var(--mantine-color-dark-6)',
          display: 'flex',
          flexDirection: 'column',
        }}
      >
        {/* Two lists that answer different questions - "what has already
            been harvested" (Clips, the existing tower) vs. "what could I
            harvest next" (Streams, this stage's own addition) - kept as
            separate tabs rather than merged or stacked, so each keeps the
            full navbar height a dense list needs (see ClipList's and
            StreamPanel's own docstrings on why density matters here).
            Defaults to Clips: an operator returning to curate what is
            already there is the common case, and every existing E2E test
            expects the clip tower visible without any extra click. */}
        <Tabs
          value={navTab}
          onChange={(value) => setNavTab(value === 'streams' ? 'streams' : 'clips')}
          style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}
        >
          <Tabs.List>
            <Tabs.Tab
              value="clips"
              rightSection={
                clips ? (
                  <Badge size="xs" variant="light" color="dark.3">
                    {clips.length}
                  </Badge>
                ) : undefined
              }
            >
              Clips
            </Tabs.Tab>
            <Tabs.Tab value="streams">Streams</Tabs.Tab>
          </Tabs.List>

          <Tabs.Panel
            value="clips"
            pt="sm"
            style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}
          >
            {clips ? (
              <ClipList
                clips={clips}
                selectedName={selectedName}
                onSelect={setSelectedName}
                showDiscarded={showDiscarded}
                onShowDiscardedChange={setShowDiscarded}
              />
            ) : loadError ? (
              <Alert color="red" title="Could not load clips">
                {loadError} - check that the studio server is still running, then reload this
                page.
              </Alert>
            ) : (
              <Center h="100%">
                <Stack align="center" gap="xs">
                  <Loader color="steel" />
                  <Text size="xs" c="dimmed">
                    Loading clips…
                  </Text>
                </Stack>
              </Center>
            )}
          </Tabs.Panel>

          <Tabs.Panel
            value="streams"
            pt="sm"
            style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}
          >
            <StreamPanel
              channel={channel}
              event={event}
              entries={streamEntries}
              work={streamWork}
              busyLegs={queueingLegs}
              onQueue={handleQueueStreams}
              catalogueStaleAt={streamCatalogueStaleAt}
            />
          </Tabs.Panel>
        </Tabs>
      </AppShell.Navbar>
      {/* height + overflowY make Main its own scroll container: the app sets
          `body { overflow: hidden }` (index.css) so the two NavScreen screens
          can own a 100vh column, which means AppShell.Main can no longer fall
          back to the document scroll - without this, a tall editor (ClipEditor
          + upload + RenderPanel stacked) clips its lower panels with no way to
          reach them. The navbar's two lists scroll on their own (ClipList /
          StreamPanel each wrap a flex:1 ScrollArea). */}
      <AppShell.Main style={{ height: '100vh', overflowY: 'auto' }}>
        {/* The selected clip's own identity comes first - a render
            action never outweighs knowing which clip is on screen (see
            RenderPanel's own docstring). With nothing selected, the pane
            shows the event summary instead of one line of text in an
            empty area (see EventSummary.tsx). */}
        <Stack gap="md">
          {selectedClip ? (
            <>
              {/* Freeze the editor while a render is in flight: the render
                  reads each clip's edit.json live as it goes (jobs._render_one),
                  so an edit staged mid-render could leak into a not-yet-rendered
                  clip of an "All kept" batch. See ClipEditor's `disabled`. */}
              <ClipEditor
                clip={selectedClip}
                onUpdated={handleClipUpdated}
                disabled={rendering || renderStarting}
              />
              {/* Only ever rendered for a kept, already-rendered clip - it
                  returns null itself otherwise (see UploadPanel's own
                  docstring on why: the backend 409s every other case, so
                  this never offers an action the server would refuse). */}
              {auth?.upload_mode === 'manual' ? (
                <ManualUploadPanel clip={selectedClip} />
              ) : (
                <UploadPanel
                  clip={selectedClip}
                  job={uploadingClipName === selectedClip.name ? uploadJob : null}
                  jobStarting={uploadingClipName === selectedClip.name && uploadStarting}
                  uploadedRecord={uploadRecords[selectedClip.name] ?? null}
                  remainingUploads={auth?.remaining_uploads ?? null}
                  authConnected={auth?.connected ?? null}
                  blockedBy={blockedByForUpload}
                  onStartUpload={handleStartUpload}
                />
              )}
            </>
          ) : (
            <EventSummary clips={clips ?? []} />
          )}
          <RenderPanel
            selectedClipName={selectedName}
            keptCount={keptClips.length}
            work={renderWork}
            renderStarting={renderStarting}
            onRenderThisClip={() => selectedName && handleRender([selectedName])}
            onRenderAllKept={() => handleRender(keptClips.map((c) => c.name))}
          />
        </Stack>
      </AppShell.Main>
    </AppShell>
  )
}

function summaryOf(detail: ClipDetail): ClipSummary {
  const {
    name,
    harvested_title,
    effective_title,
    status,
    has_short,
    short_version,
    has_transcript,
    has_edit,
    has_raw,
    duration,
    has_upload,
    upload_url,
    trim,
    trim_applied,
    trim_unknown,
  } = detail
  return {
    name,
    harvested_title,
    effective_title,
    status,
    has_short,
    short_version,
    has_transcript,
    has_edit,
    has_raw,
    duration,
    has_upload,
    upload_url,
    trim,
    trim_applied,
    trim_unknown,
  }
}

export default App
