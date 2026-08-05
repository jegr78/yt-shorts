import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Alert,
  Badge,
  Button,
  Card,
  Center,
  Group,
  Loader,
  Modal,
  Progress,
  Stack,
  Text,
  Tooltip,
} from '@mantine/core'
import { notifications } from '@mantine/notifications'
import {
  ApiError,
  listJobs,
  moveJob,
  pauseJob,
  removeJob,
  resumeJob,
  retryJob,
  stopJob,
} from '../api'
import {
  allowedActions,
  jobLogFile,
  kindSpec,
  moveTarget,
  paramSummary,
  progressLabel,
  progressPercent,
  recentFirst,
  stallNote,
  stateColor,
  stopWarning,
  type Action,
  type JobEntry,
  type JobPlan,
} from '../jobs'
import { routePath } from '../scopedApi'
import { navigate } from '../useRoute'
import { NavScreen } from './NavScreen'

const POLL_INTERVAL_MS = 2000

/**
 * The workspace-level Jobs screen: the plan `yt_shorts.job_queue` owns, in
 * the three sections `GET /api/jobs` returns it in (see api.py's
 * `list_queue`), with the controls each row's state actually permits.
 *
 * Every rule about what a control may promise lives in `../jobs.ts`, not
 * here - `allowedActions`, `stopWarning`, `progressLabel`, `moveTarget`,
 * `recentFirst`, `stallNote`, `jobLogFile` - so they are unit-tested without
 * rendering and Vite's fast-refresh boundary stays component-only. Read that
 * module's docstring before changing anything on this screen; four of the
 * traps it records are invisible from the DOM.
 *
 * Three of them show up directly in what this component draws:
 *
 * - **The progress line appears per ROW, not as a column, and a row without
 *   a reading shows nothing.** A running `transcribe`, `detect` or `render`
 *   reports its own chunk/window/clip count into `Entry.progress` and gets
 *   the label and the bar; a `trim` or an `upload` reports nothing at all
 *   (`jobs.KINDS[kind].progress_unit` is null for them), and so does any row
 *   that is not running - the reading is cleared the moment an entry leaves
 *   `running`. Both are rendered only when `progressLabel`/`progressPercent`
 *   return a reading, which is what keeps a one-cut trim from being given an
 *   invented "1 of 1" and a finished row from keeping a stale one.
 * - **A stopped worker is stated, loudly.** `worker_running: false` is not
 *   "idle" - `create_app` never starts the worker, only `bin/yt-shorts
 *   studio` does - so a plan that will never move looks exactly like a busy
 *   one. `stallNote` is shown as a warning above the sections rather than
 *   as a quiet footnote.
 * - **"Recently finished" really is most-recent-first.** The payload's
 *   `finished` arrives in QUEUE order, oldest first, like the other two
 *   sections; `recentFirst` reverses it here rather than the heading being
 *   reworded to match the wrong order.
 *
 * And one that shows up in what it does NOT draw: nothing here builds an
 * enqueue out of a row it read. A listed row's `params` may carry the
 * literal "[redacted]" in place of a key-shaped value (api.py's
 * `_safe_params`), so a control that re-submitted a row's params would write
 * that string into the plan as a real parameter. New work is queued where
 * the operator has the real context - the stream list's Transcribe button, a
 * render, an upload. The Retry button below is not that control: it sends no
 * params, and `POST …/{id}/retry` re-queues the entry from the ones the
 * server itself stored.
 */
export function JobsScreen() {
  const [plan, setPlan] = useState<JobPlan | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [stopTarget, setStopTarget] = useState<{ entry: JobEntry; force: boolean } | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  // Kept in a ref so the polling effect below never re-subscribes when the
  // plan changes - it runs once for the life of the screen.
  const pollingRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)

  const refresh = useCallback(async () => {
    try {
      setPlan(await listJobs())
      setError(null)
    } catch (err) {
      // A 503 here means this studio could not open a workspace at all; the
      // server's own message names the cause and what to check. It must
      // never be rendered as an empty plan - "no jobs" is a different and
      // much worse answer than "the queue is unavailable".
      setError(err instanceof ApiError ? err.message : String(err))
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    async function tick() {
      await refresh()
      if (cancelled) return
      pollingRef.current = setTimeout(tick, POLL_INTERVAL_MS)
    }
    tick()
    return () => {
      cancelled = true
      if (pollingRef.current) clearTimeout(pollingRef.current)
    }
  }, [refresh])

  /** Runs one queue call, reports the server's own message on failure, and
   * refreshes either way. The server's message is the useful half of every
   * refusal here: it names the state that made the call invalid. */
  async function run(entryId: string, call: () => Promise<unknown>, what: string) {
    setBusyId(entryId)
    try {
      await call()
    } catch (err) {
      notifications.show({
        title: `Could not ${what}`,
        color: 'red',
        message: err instanceof ApiError ? err.message : String(err),
      })
    } finally {
      setBusyId(null)
      await refresh()
    }
  }

  function handleAction(action: Action, entry: JobEntry, queued: JobEntry[], index: number) {
    switch (action) {
      case 'stop':
        setStopTarget({ entry, force: false })
        return
      case 'kill':
        setStopTarget({ entry, force: true })
        return
      case 'pause':
        run(entry.id, () => pauseJob(entry.id), 'pause this entry')
        return
      case 'resume':
        run(entry.id, () => resumeJob(entry.id), 'resume this entry')
        return
      case 'retry':
        // "retry", not "re-queue": the screen deliberately has no control
        // that re-enqueues a row from params it read (see the docstring),
        // and a failure message using that word read like one.
        run(entry.id, () => retryJob(entry.id), 'retry this entry')
        return
      case 'remove':
        run(entry.id, () => removeJob(entry.id), 'drop this entry')
        return
      case 'up':
      case 'down': {
        const target = moveTarget(queued, index, action)
        if (target === null) return
        run(entry.id, () => moveJob(entry.id, target), 'reorder this entry')
      }
    }
  }

  async function confirmStop() {
    if (!stopTarget) return
    const { entry, force } = stopTarget
    setStopTarget(null)
    await run(entry.id, () => stopJob(entry.id, force), 'stop this job')
  }

  const note = plan === null ? null : stallNote(plan)

  return (
    <NavScreen
      crumbs={[{ label: 'Channels', path: routePath({ screen: 'channels' }) }, { label: 'Jobs' }]}
      title="Jobs"
      subtitle="Everything this workspace has planned, is working on, and has recently finished."
    >
      {error ? (
        <Alert color="red" title="Could not load the job queue">
          {error}
        </Alert>
      ) : plan === null ? (
        <Center py="xl">
          <Stack align="center" gap="xs">
            <Loader color="steel" />
            <Text size="xs" c="dimmed">
              Loading the job queue…
            </Text>
          </Stack>
        </Center>
      ) : (
        <Stack gap="lg">
          {plan.load_error && (
            <Alert color="red" title="The saved plan could not be read">
              {plan.load_error}
            </Alert>
          )}
          {note && (
            <Alert color="yellow" title="The worker is not running">
              {note}
            </Alert>
          )}

          <Group justify="space-between" wrap="wrap" gap="xs">
            <Text size="xs" c="dimmed">
              {Object.entries(plan.limits)
                .map(([pool, limit]) => `${pool}: ${limit} at a time`)
                .join(' · ')}
            </Text>
            <Button
              size="xs"
              variant="default"
              onClick={() => navigate(routePath({ screen: 'logs' }))}
            >
              View logs
            </Button>
          </Group>

          <Section
            title="Running"
            entries={plan.running}
            empty="Nothing is running."
            busyId={busyId}
            onAction={handleAction}
          />
          <Section
            title="Queued"
            entries={plan.queued}
            empty="Nothing is queued."
            busyId={busyId}
            onAction={handleAction}
          />
          <Section
            // recentFirst, because the payload's own order is oldest-first
            // (see this component's docstring and jobs.ts).
            title="Recently finished"
            entries={recentFirst(plan.finished)}
            empty="Nothing has finished yet."
            busyId={busyId}
            onAction={handleAction}
          />
        </Stack>
      )}

      <Modal
        opened={stopTarget !== null}
        onClose={() => setStopTarget(null)}
        title={stopTarget?.force ? 'Hard-stop this job?' : 'Stop this job?'}
      >
        {stopTarget && (
          <Stack gap="md">
            <Text size="sm">{stopWarning(stopTarget.entry, kindSpec(stopTarget.entry))}</Text>
            {stopTarget.force && (
              <Text size="sm" c="dimmed">
                A hard stop terminates the subprocess the work is waiting on instead of
                letting it reach {stopTarget.entry.stop_point}. The job's own thread still
                runs its cleanup and reports “stopped”; nothing here kills a thread.
              </Text>
            )}
            <Group justify="flex-end">
              <Button variant="default" onClick={() => setStopTarget(null)}>
                Keep going
              </Button>
              <Button color={stopTarget.force ? 'red' : 'orange'} onClick={confirmStop}>
                {stopTarget.force ? 'Hard stop' : 'Ask it to stop'}
              </Button>
            </Group>
          </Stack>
        )}
      </Modal>
    </NavScreen>
  )
}

function Section({
  title,
  entries,
  empty,
  busyId,
  onAction,
}: {
  title: string
  entries: JobEntry[]
  empty: string
  busyId: string | null
  onAction: (action: Action, entry: JobEntry, siblings: JobEntry[], index: number) => void
}) {
  return (
    <Stack gap="xs">
      <Group gap="xs">
        <Text fw={600} size="sm" tt="uppercase" c="dimmed">
          {title}
        </Text>
        <Text size="xs" c="dimmed">
          {entries.length}
        </Text>
      </Group>
      {entries.length === 0 ? (
        <Text size="sm" c="dimmed">
          {empty}
        </Text>
      ) : (
        entries.map((entry, index) => (
          <JobRow
            key={entry.id}
            entry={entry}
            siblings={entries}
            index={index}
            busy={busyId === entry.id}
            onAction={onAction}
          />
        ))
      )}
    </Stack>
  )
}

/** Human labels for the actions, so the button text is written once. */
const ACTION_LABEL: Record<Action, string> = {
  stop: 'Stop',
  kill: 'Hard stop',
  pause: 'Pause',
  resume: 'Resume',
  up: 'Move up',
  down: 'Move down',
  retry: 'Retry',
  remove: 'Drop',
}

function JobRow({
  entry,
  siblings,
  index,
  busy,
  onAction,
}: {
  entry: JobEntry
  siblings: JobEntry[]
  index: number
  busy: boolean
  onAction: (action: Action, entry: JobEntry, siblings: JobEntry[], index: number) => void
}) {
  const spec = kindSpec(entry)
  const actions = allowedActions(entry, spec)
  const label = progressLabel(entry)
  const percent = progressPercent(entry)
  const logFile = jobLogFile(entry)
  const scope = paramSummary(entry)

  return (
    <Card padding="sm" withBorder>
      <Stack gap={6}>
        <Group justify="space-between" wrap="wrap" gap="xs">
          <Group gap="xs" wrap="wrap">
            <Badge color="steel" variant="light" tt="none">
              {entry.kind}
            </Badge>
            <Badge color={stateColor(entry.state)} variant="dot" tt="none">
              {entry.state}
            </Badge>
            {scope && (
              <Text size="xs" c="dimmed" ff="monospace">
                {scope}
              </Text>
            )}
          </Group>
          <Group gap={6} wrap="wrap">
            {actions.map((action) => {
              // A move at either end of the queued list has no target, so
              // the button is disabled rather than sending a call that
              // would reorder nothing (see moveTarget).
              const noTarget =
                (action === 'up' || action === 'down') &&
                moveTarget(siblings, index, action) === null
              return (
                <Button
                  key={action}
                  size="xs"
                  variant={action === 'kill' ? 'light' : 'default'}
                  color={action === 'kill' ? 'red' : undefined}
                  loading={busy}
                  disabled={noTarget}
                  onClick={() => onAction(action, entry, siblings, index)}
                >
                  {ACTION_LABEL[action]}
                </Button>
              )
            })}
          </Group>
        </Group>

        {/* Rendered only when the work actually reported something: a
            running transcribe/detect/render, once its first chunk/window/
            clip is done. Nothing for a trim, an upload, or any row that is
            not running - see the component docstring; an empty bar would
            read as a broken column and "1 of 1" would be a decoration. */}
        {label !== null && percent !== null && (
          <Stack gap={2}>
            <Text size="xs" c="dimmed">
              {label}
            </Text>
            <Progress value={percent} color="steel" size="sm" />
          </Stack>
        )}

        {entry.reason && (
          <Text size="xs" c={entry.state === 'failed' ? 'red.4' : 'dimmed'}>
            {entry.reason}
          </Text>
        )}

        <Group gap="md" wrap="wrap">
          {entry.after && (
            <Tooltip label="This entry waits for another one to finish first">
              <Text size="xs" c="dimmed" ff="monospace">
                after {entry.after}
              </Text>
            </Tooltip>
          )}
          {spec.stopPoint && (entry.state === 'running' || entry.state === 'stopping') && (
            <Text size="xs" c="dimmed">
              stops {spec.stopPoint}
            </Text>
          )}
          {logFile !== null && (
            <Button
              size="compact-xs"
              variant="subtle"
              color="steel"
              onClick={() =>
                navigate(`${routePath({ screen: 'logs' })}?file=${encodeURIComponent(logFile)}`)
              }
            >
              View log
            </Button>
          )}
        </Group>
      </Stack>
    </Card>
  )
}
