import { Alert, Anchor, Badge, Box, Button, Group, Progress, ScrollArea, Stack, Text } from '@mantine/core'
import type { QueuedWork } from '../hooks/useQueuedJob'
import { jobLogFile, stateColor } from '../jobs'
import { routePath } from '../scopedApi'
import { navigate } from '../useRoute'

interface RenderPanelProps {
  selectedClipName: string | null
  keptCount: number
  /** The queue entry this panel put in the plan, followed from "queued" to
   * whatever it ends as (see useQueuedJob). NOT a Job: a render begins as a
   * row in the plan with no job behind it at all, and `work.job` stays null
   * until the worker claims it. */
  work: QueuedWork
  onRenderThisClip: () => void
  onRenderAllKept: () => void
  renderStarting: boolean
}

const RESULT_COLOR: Record<string, string> = {
  done: 'green',
  failed: 'red',
  skipped: 'dark.3',
}

/**
 * Queues and follows a render (see `POST /api/jobs` with `kind: "render"`,
 * `yt_shorts.job_queue` and `studio/worker.py`). One job at a time per event
 * is still true - the EventLock has not moved - but it is no longer this
 * panel's problem: enqueuing takes no lock, and the worker waits for a busy
 * event rather than refusing, so both buttons stay available.
 *
 * **What this panel must never do is report "running" for something merely
 * queued.** A queue entry does not start on the click: the worker may not be
 * running at all (`create_app` never starts it - only `bin/yt-shorts studio`
 * does), or another job may hold this event's lock. Both are stated, in those
 * words, by `jobs.ts`'s `waitNote`, and shown here for as long as the entry
 * waits. A spinner in that place would be the lying button this whole queue
 * exists to remove.
 *
 * The per-clip result list underneath still comes from the JOB, because that
 * is where per-clip results live - and it therefore appears only once the
 * worker has claimed the entry and there is a job to read. Same for the log
 * link, built from `job_id` and not from the entry's own id (two disjoint id
 * spaces - see jobs.ts).
 *
 * Deliberately NOT a bordered Card: a two-button control does not earn
 * the visual weight of its own section, and this sits BELOW the selected
 * clip's own identity (see App.tsx) so an operator meets "which clip am I
 * looking at" before "render" - a thin top rule is enough to separate it
 * from whatever is above, the same quiet chrome a toolbar gets rather
 * than a panel.
 */
export function RenderPanel({
  selectedClipName,
  keptCount,
  work,
  onRenderThisClip,
  onRenderAllKept,
  renderStarting,
}: RenderPanelProps) {
  const { entry, job, pending, running, waiting } = work
  // A render already in the plan is not queued twice by accident. This is a
  // guard against a double click, not the old "the server would 409 this" -
  // it would not; it would simply plan a second render.
  const inFlight = pending || running || renderStarting
  const resultEntries = job ? Object.entries(job.results) : []
  const total = job ? Math.max(resultEntries.length, 1) : 0
  const finished = resultEntries.length
  const logFile = entry === null ? null : jobLogFile(entry)

  return (
    <Box pt="sm" style={{ borderTop: '1px solid var(--mantine-color-dark-6)' }}>
      <Stack gap="xs">
        <Group justify="space-between" wrap="wrap" gap="xs">
          <Group gap="xs" wrap="nowrap">
            <Text fw={600} size="xs" tt="uppercase" c="dimmed">
              Render
            </Text>
            <Button
              size="xs"
              variant="default"
              disabled={!selectedClipName || inFlight}
              onClick={onRenderThisClip}
            >
              This clip
            </Button>
            <Button
              size="xs"
              color="steel"
              variant="light"
              disabled={keptCount === 0 || inFlight}
              onClick={onRenderAllKept}
            >
              All kept ({keptCount})
            </Button>
          </Group>
          {entry && (
            <Group gap="xs" wrap="nowrap">
              <Badge color={stateColor(entry.state)} variant="dot" size="xs">
                {entry.state}
              </Badge>
              {logFile && (
                <Anchor
                  size="xs"
                  c="steel.3"
                  onClick={() =>
                    navigate(`${routePath({ screen: 'logs' })}?file=${encodeURIComponent(logFile)}`)
                  }
                >
                  View log
                </Anchor>
              )}
              <Anchor size="xs" c="steel.3" onClick={() => navigate(routePath({ screen: 'jobs' }))}>
                Jobs
              </Anchor>
            </Group>
          )}
        </Group>
        {waiting && (
          // The honest queued state: it has NOT started, and this says which
          // of the two reasons it is. Never a spinner in this place.
          <Alert color="yellow" title="Queued - not started yet" p="xs">
            <Text size="xs">{waiting}</Text>
          </Alert>
        )}
        {work.error && (
          // Why this panel stopped following its entry. Shown as the sentence
          // the hook composed, not decorated here: "the entry is gone" and
          // "the plan could not be read" call for different words, and only
          // the hook knows which happened. The buttons above are enabled
          // again by the same flag (see QueuedWork.error).
          <Text size="xs" c="red.4">
            {work.error}
          </Text>
        )}
        {entry?.reason && !pending && (
          <Text size="xs" c={entry.state === 'failed' ? 'red.4' : 'dimmed'}>
            {entry.reason}
          </Text>
        )}
        {job && (
          <>
            <Progress
              value={running ? (finished / total) * 100 : 100}
              animated={running}
              color="steel"
              size="sm"
            />
            <ScrollArea.Autosize mah={120} offsetScrollbars>
              <Stack gap={2}>
                {resultEntries.map(([name, result]) => (
                  <Group key={name} gap={6} wrap="nowrap">
                    <Badge size="xs" variant="dot" color={RESULT_COLOR[result.status] ?? 'dark.3'}>
                      {result.status}
                    </Badge>
                    <Text size="xs" truncate ff="monospace">
                      {name}
                    </Text>
                    {result.reason && (
                      <Text size="xs" c="dimmed" truncate>
                        {result.reason}
                      </Text>
                    )}
                  </Group>
                ))}
              </Stack>
            </ScrollArea.Autosize>
          </>
        )}
      </Stack>
    </Box>
  )
}
