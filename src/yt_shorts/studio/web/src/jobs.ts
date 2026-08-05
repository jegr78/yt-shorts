/**
 * The Jobs screen's pure rules, over the plan `GET /api/jobs` returns (see
 * api.py's `list_queue`, whose docstring is the authority on this payload,
 * and yt_shorts/job_queue.py for the state machine underneath it).
 *
 * Its own module, exporting no React, for the reason words.ts/format.ts/
 * streamTimeline.ts are: Vite's fast-refresh boundary stays component-only,
 * and every rule about what a button may promise is unit-testable without
 * rendering anything. That matters more here than usual - a button offered
 * on the wrong state is not a cosmetic slip, it is a control that answers
 * 409 (or, worse, appears to work) for an operator who is trying to stop
 * something expensive.
 *
 * Five things about the payload this module encodes so no component has to
 * rediscover them (all five are recorded in `list_queue`'s own docstring):
 *
 * - **`progress` is written by three kinds and by no others.** A running
 *   `transcribe` reports a chunk, a `detect` a window and a `render` a clip;
 *   `trim` and `upload` report nothing, because a trim is one cut and an
 *   upload's bytes go to a layer the tool does not watch (the server's
 *   authority on this is `jobs.KINDS[kind].progress_unit`, and a kind whose
 *   unit is null is handed no callback at all). So `progressLabel`/
 *   `progressPercent` still return null for whole kinds, and for every row
 *   that is not running - the reading is cleared the moment an entry leaves
 *   `running` - and the component must go on rendering NOTHING for those.
 *   Inventing "1 of 1" for a trim is worse than silence.
 * - **`worker_running: false` is not "idle".** `create_app` builds the
 *   worker and never starts it - only `bin/yt-shorts studio` does - so a
 *   full plan can sit motionless with nothing wrong. `stallNote` says that
 *   plainly; it must never be softened into "nothing to do".
 * - **`finished` arrives in QUEUE order, oldest first.** `recentFirst`
 *   reverses it for display; the heading stays "Recently finished".
 * - **`params` may carry the literal `"[redacted]"`.** `paramSummary`
 *   renders it. Nothing in this module or its screen ever SENDS a listed
 *   row's params back to the server: there is deliberately no control that
 *   re-enqueues a row from what this screen read, because one built that way
 *   would write that string into the plan as if it were the real parameter.
 *   The Retry button is not a counter-example - `POST …/{id}/retry` sends no
 *   params at all and re-queues the entry from the ones the SERVER already
 *   holds, which is exactly why it is safe and a client-side re-enqueue is
 *   not.
 * - **`/api/jobs/{id}` is two disjoint id spaces.** `jobLogFile` builds its
 *   name from `job_id` (the `studio.jobs.Job`), never from the entry's own
 *   `id`; a log link built from the latter 404s.
 *
 * The last two are why this module is no longer only the Jobs SCREEN's. Every
 * panel that starts work now enqueues it (`POST /api/jobs`), so a Render, a
 * Detect and a Trim all begin their life as a row in this same plan - queued,
 * with no `job_id` yet and nothing running. `findEntry`/`waitNote`/
 * `isTerminal` below are what those panels read, so the answer to "why has
 * this not started?" is written once here rather than three times in three
 * components.
 */

/** Every state `job_queue.STATES` holds. Grouped by api.py into `running`
 * (running + stopping), `queued` (queued + paused) and `finished` (the four
 * terminal ones) - the three sections this screen renders. */
export type JobState =
  | 'queued'
  | 'paused'
  | 'running'
  | 'stopping'
  | 'done'
  | 'failed'
  | 'stopped'
  | 'interrupted'

/** One row of the plan, exactly as `api.py`'s `_entry_json` sends it.
 * `state` is widened to `string` on purpose: `jobs.json` is a plain file an
 * older version may have written, so a state this client does not know must
 * render as an unknown row rather than crash a screen. */
export interface JobEntry {
  id: string
  kind: string
  state: JobState | string
  params: Record<string, unknown>
  reason: string | null
  /** `{unit, done, total}` while this entry is running and its kind counts
   * something, null otherwise - see this module's docstring. `unknown`, not
   * a declared shape, so nothing can read it without going through
   * progressLabel/progressPercent's own guards: it is typed `Any`
   * server-side and round-trips through a hand-editable jobs.json. */
  progress: unknown
  created_at: number
  after: string | null
  /** The id of the `studio.jobs.Job` doing this entry's work, set once the
   * worker starts it. The ONLY link between the two id spaces. */
  job_id: string | null
  position: number
  pool: string | null
  /** `jobs.KINDS[kind].stop_point` - a real boundary in the work, shown
   * before the operator clicks. A LABEL, never the signal for whether a
   * stop is possible: that is `stoppable` below. Null for a kind KINDS no
   * longer knows. */
  stop_point: string | null
  hard_stop_allowed: boolean
  /** Whether `POST …/stop` can reach the work at all - `worker.STARTERS
   * [kind].stoppable`, i.e. whether the starter takes a cancel token, which
   * is exactly what `Worker.request_stop` refuses on. False for a kind this
   * build has no starter for. */
  stoppable: boolean
}

/** The whole plan plus the pool limits, as `GET /api/jobs` returns it. */
export interface JobPlan {
  running: JobEntry[]
  queued: JobEntry[]
  finished: JobEntry[]
  limits: Record<string, number>
  worker_running: boolean
  load_error: string | null
}

/** What a kind allows, as this screen needs it. Taken off the ROW (api.py
 * copies `stopPoint`/`hardStopAllowed` there from `jobs.KINDS` and
 * `stoppable` from `worker.STARTERS`) rather than from a second table
 * hardcoded here, which would be free to drift from the server's. */
export interface KindSpec {
  stopPoint: string | null
  hardStopAllowed: boolean
  stoppable: boolean
}

export function kindSpec(entry: JobEntry): KindSpec {
  return {
    stopPoint: entry.stop_point,
    hardStopAllowed: entry.hard_stop_allowed,
    stoppable: entry.stoppable,
  }
}

/**
 * True when a stop would actually reach the work.
 *
 * This reads the server's own `stoppable` boolean and nothing else. It used
 * to compare `stop_point` against the literal `'cannot be stopped'`, which
 * was a bet on server PROSE - and the bet lost under measurement: changing
 * `jobs.KINDS["upload"].stop_point` to `"Cannot be stopped"` left the whole
 * Python suite green while this screen would then have offered a Stop
 * button on a running upload, the one thing that spec exists to forbid.
 * `stop_point` is a label for the operator now, and only that.
 *
 * False for a kind this build has no starter for, which is the safe answer
 * in both directions: no button rather than a lying one.
 */
export function isStoppable(spec: KindSpec): boolean {
  return spec.stoppable
}

/** Every control the Jobs screen can draw for one row.
 * `stop` is the graceful one (`POST …/stop`), `kill` the hard one
 * (`?force=true`), `up`/`down` are `POST …/move`. */
export type Action = 'stop' | 'kill' | 'pause' | 'resume' | 'up' | 'down' | 'retry' | 'remove'

/**
 * Which buttons a given entry may show. Pure, so the rules are testable
 * without rendering - and so a button that would lie cannot be built by
 * accident in a component.
 *
 * Every rule here is the client side of a transition `job_queue.py` will
 * actually accept; anything else is a control whose only possible outcome
 * is a 409:
 * - `queued` may be paused, reordered and dropped.
 * - `paused` may be resumed and dropped, but NOT reordered - `move` refuses
 *   any state but `queued`.
 * - `running` may be asked to stop (and hard-stopped where the kind allows
 *   one), and may NOT be dropped: `remove` refuses an active entry and says
 *   to stop it first.
 * - `stopping` may only be ESCALATED to a hard stop, where the kind has one.
 *   A second graceful stop 409s (it asks for nothing that was not already
 *   asked for), a resume is not a transition the queue has, and a remove is
 *   refused like any active entry. The escalation is a real control and it
 *   matters: a graceful stop waits for the work's own safe point, which for
 *   a stream chunk or a rendered clip can be minutes away. It used to be
 *   withheld because `Worker.request_stop(force=True)` requested the kill
 *   and only THEN called `mark_stopping`, which raises on a non-running
 *   entry - so the click half-applied and reported an error for a kill it
 *   had actually performed. That was a server bug and it is fixed there,
 *   not worked around here.
 * - a terminal entry may be dropped, and a `failed`/`interrupted`/`stopped`
 *   one retried. `stopped` belongs there and used to be missing, which made
 *   this module's own `stopWarning` promise a control the screen withheld:
 *   the dialog says "a retry resumes at the first window nobody reached"
 *   before the click, and afterwards the only button offered was Remove.
 *   The queue accepts it now (`JobQueue.retry`) - the fix was to make the
 *   promise true, not to soften the wording, because the per-window and
 *   per-chunk caches that make a stop cheap are real. `done` is still the
 *   one terminal state with no retry: re-running work that succeeded is a
 *   new request, and for a paid kind it spends the money again.
 */
export function allowedActions(entry: JobEntry, spec: KindSpec): Action[] {
  switch (entry.state) {
    case 'queued':
      return ['pause', 'up', 'down', 'remove']
    case 'paused':
      return ['resume', 'remove']
    case 'running': {
      if (!isStoppable(spec)) return []
      return spec.hardStopAllowed ? ['stop', 'kill'] : ['stop']
    }
    case 'stopping':
      // `upload` reaches neither branch as anything but []: it is not
      // stoppable at any level, so it can never be in `stopping` to begin
      // with, and `hardStopAllowed` is false besides.
      return isStoppable(spec) && spec.hardStopAllowed ? ['kill'] : []
    case 'failed':
    case 'interrupted':
    case 'stopped':
      return ['retry', 'remove']
    case 'done':
      return ['remove']
    default:
      // A state written by a version this client does not know. Dropping it
      // is the one action that is safe whatever it meant.
      return ['remove']
  }
}

/**
 * The index `POST …/move` should be given to swap the queued entry at
 * `index` with its queued NEIGHBOUR, or null when there is no neighbour in
 * that direction (the row is already first or last among the queued).
 *
 * `position` indexes the WHOLE plan - running and finished entries sit in
 * it too - so `position - 1` is the wrong target: it can land on a finished
 * entry and reorder nothing an operator can see, which is a button that
 * appears to do nothing. The neighbour's own position is the target that
 * actually swaps the two, because only the relative order AMONG queued
 * entries matters to `claim_next` (it skips everything else).
 */
export function moveTarget(
  queued: JobEntry[], index: number, direction: 'up' | 'down',
): number | null {
  const neighbour = queued[direction === 'up' ? index - 1 : index + 1]
  return neighbour === undefined ? null : neighbour.position
}

/** The unit a kind counts its work in. A FALLBACK only: the server names
 * the unit on every reading it writes (`jobs.KINDS[kind].progress_unit`),
 * so this is consulted for a reading that names none - a hand-edited
 * jobs.json, or one written by a version that did not carry the field.
 * `trim` and `upload` are here for that case alone; neither reports
 * anything, so neither ever reaches this table in normal operation. */
const UNIT_BY_KIND: Record<string, string> = {
  transcribe: 'chunk',
  detect: 'window',
  render: 'clip',
  trim: 'cut',
  upload: 'step',
}

interface Reading {
  unit: string
  done: number
  total: number
}

/** The progress reading of an entry, or null when there is none to read -
 * a kind that counts nothing, an entry that is not running, or a running one
 * that has not finished its first unit yet (see this module's docstring).
 * Defensive about the shape on purpose: `Entry.progress` is typed `Any`
 * server-side and round-trips through a hand-editable jobs.json, so anything
 * at all can arrive here and none of it may throw. */
function reading(entry: JobEntry): Reading | null {
  const progress = entry.progress
  if (progress === null || typeof progress !== 'object' || Array.isArray(progress)) {
    return null
  }
  const { unit, done, total } = progress as Record<string, unknown>
  if (typeof done !== 'number' || typeof total !== 'number') return null
  if (!Number.isFinite(done) || !Number.isFinite(total)) return null
  // A total of 0 is not "nothing done yet", it is "no reading" - rendering
  // it as "0 of 0" is exactly the broken-looking column this returns null
  // to avoid.
  if (total <= 0) return null
  return {
    unit: typeof unit === 'string' && unit.trim() !== ''
      ? unit.trim()
      : (UNIT_BY_KIND[entry.kind] ?? 'step'),
    done,
    total,
  }
}

/** "chunk 20 of 50", "window 3 of 9". Returns null when there is no reading
 * to show - a kind that counts nothing (`trim`, `upload`), a row that is not
 * running, or a garbled payload - so the component renders nothing rather
 * than "0 of 0". That null is not a leftover from before there was a
 * producer: it is the honest answer for a job whose work has one unit, and
 * the component must keep rendering nothing for it. */
export function progressLabel(entry: JobEntry): string | null {
  const read = reading(entry)
  return read === null ? null : `${read.unit} ${read.done} of ${read.total}`
}

/** The same reading as a 0-100 percentage for a progress bar, clamped, or
 * null on the same terms as progressLabel - so a bar and its label always
 * appear and disappear together. */
export function progressPercent(entry: JobEntry): number | null {
  const read = reading(entry)
  if (read === null) return null
  return Math.round(Math.max(0, Math.min(1, read.done / read.total)) * 100)
}

/** What a stop costs, per kind - the half of the warning that is about the
 * work rather than about the timing.
 *
 * The `detect` sentence used to end at "stopping refunds nothing, and the
 * rest of the stream is never scanned", which was true and read like a
 * threat: it was written before the per-window cache existed and still had
 * the shape of the warning that cache was built to make unnecessary. A
 * stopped run really does keep what it paid for - the scores are cached, and
 * a retry resumes at the first window nobody reached - so the honest sentence
 * says both halves. What it must NOT do is drop "paid for"/"refunds nothing"
 * and imply a stop is free: the windows already scored are spent, and only
 * the unscanned rest is what a retry pays for.
 */
function costSentence(entry: JobEntry): string {
  const read = reading(entry)
  switch (entry.kind) {
    case 'detect':
      return read === null
        ? 'Every window this run has already scored is paid for and stopping ' +
          'refunds nothing - but those scores are cached, so a retry resumes ' +
          'at the first window nobody reached and pays only for the rest.'
        : `The ${read.done} of ${read.total} windows already scored are paid ` +
          'for and stopping refunds nothing - but those scores are cached, so ' +
          'a retry resumes at the first window nobody reached and pays only ' +
          'for the rest.'
    case 'transcribe':
      return 'Chunks already decoded stay cached, so a retry resumes from the ' +
        'first missing chunk instead of starting the whole stream over.'
    case 'render':
      return 'Clips already rendered keep their shorts; the rest are left as ' +
        'they were.'
    case 'trim':
      return 'A cut already applied stays applied; the untrimmed master is ' +
        'kept either way.'
    default:
      return 'Work already finished stays finished; nothing already done is ' +
        'undone.'
  }
}

/**
 * The sentence shown before a stop: what it will cost and when it takes
 * effect. Must name the cost for a detect ("3 of 9 windows already paid
 * for") and must never promise "immediately".
 *
 * A stop is ACCEPTED, not completed - `POST …/stop` answers 202 and the
 * entry moves to `stopping`, not `stopped`. The work reaches its own next
 * safe point (`jobs.KINDS[kind].stop_point`, carried on the row) and ends
 * there, which for a stream chunk or a rendered clip can be minutes away.
 * Any rewording has to keep both halves: when it lands, and what has
 * already been spent by the time it does.
 */
export function stopWarning(entry: JobEntry, spec: KindSpec): string {
  if (!isStoppable(spec)) {
    return `A ${entry.kind} job cannot be stopped ` +
      `(${spec.stopPoint ?? 'this studio does not know this kind'}).`
  }
  return (
    `Asking a ${entry.kind} job to stop is accepted, not done: the work ` +
    `carries on until ${spec.stopPoint}, and the entry sits in "stopping" ` +
    `until it gets there. ${costSentence(entry)}`
  )
}

/** The `finished` section in the order its heading claims. `GET /api/jobs`
 * returns it in QUEUE order - oldest first, like the other two sections,
 * because it is the plan sliced by state and not an activity feed. A copy,
 * never a reverse in place: the caller's array is React state. */
export function recentFirst(entries: JobEntry[]): JobEntry[] {
  return [...entries].reverse()
}

/** What to say when the worker is not running, or null when it is.
 *
 * `worker_running: false` is the single most misleading thing this payload
 * can carry: a plan full of queued entries that will never move looks
 * exactly like a busy one. So it is stated as a fact about the WORKER, never
 * as a fact about the queue - "idle" and "nothing to do" are precisely the
 * wrong words, and a plan with entries waiting says how many. */
export function stallNote(plan: JobPlan): string | null {
  if (plan.worker_running) return null
  const waiting = plan.queued.length
  const inFlight = plan.running.length
  const base =
    'The worker is not running, so nothing in this plan will start. Only ' +
    '`bin/yt-shorts studio` starts it; a studio app built any other way ' +
    'leaves it stopped.'
  if (waiting === 0 && inFlight === 0) return base
  const parts: string[] = []
  if (waiting > 0) {
    parts.push(`${waiting} queued ${waiting === 1 ? 'entry' : 'entries'} will not start`)
  }
  if (inFlight > 0) {
    parts.push(
      `${inFlight} ${inFlight === 1 ? 'entry is' : 'entries are'} recorded as ` +
      'running with no worker behind them (restart the studio to have them ' +
      'recovered as interrupted)',
    )
  }
  return `${base} ${parts.join(', and ')}.`
}

/** What each state means for something WATCHING one entry: is it still
 * waiting to start, is the work in flight, or is it over.
 *
 * A `Record<JobState, …>` rather than a hand-written list, deliberately - the
 * same gate `scopedApi.test.ts` puts on `Screen`. Adding a state to `JobState`
 * without classifying it here fails the build instead of silently landing in
 * whatever the fallback happens to be, which is how a panel would come to spin
 * forever on a state nobody thought about.
 */
const ACTIVITY: Record<JobState, 'pending' | 'active' | 'terminal'> = {
  queued: 'pending',
  paused: 'pending',
  running: 'active',
  stopping: 'active',
  done: 'terminal',
  failed: 'terminal',
  stopped: 'terminal',
  interrupted: 'terminal',
}

/** The colour every surface that shows an entry's state uses for it.
 *
 * A `Record<JobState, …>` for exactly the reason ACTIVITY above is one, and
 * it was learned the same way: this map used to be three byte-identical
 * `Record<string, string>` copies - JobsScreen.tsx, RenderPanel.tsx,
 * StreamPanel.tsx - each with its own `?? 'gray'`. Adding a state to
 * `JobState` failed the build here at ACTIVITY and rendered gray in all
 * three panels, silently. Written once, gated once.
 *
 * `stopping` is deliberately NOT the same colour as `stopped`: one is work
 * still in progress that has been asked to end, the other is work that has
 * ended.
 */
const STATE_COLOR: Record<JobState, string> = {
  queued: 'gray',
  paused: 'gray',
  running: 'steel',
  stopping: 'yellow',
  done: 'green',
  failed: 'red',
  stopped: 'orange',
  interrupted: 'orange',
}

/** The colour for one entry's state. Gray for a state this build does not
 * know - `jobs.json` is a plain file an older or newer version may have
 * written, and an unknown row must still render (see `activity`). */
export function stateColor(state: JobEntry['state']): string {
  return STATE_COLOR[state as JobState] ?? 'gray'
}

/** What a panel shows when the entry it was following has ENDED in
 * something other than `done`. */
export interface EndedNotice {
  title: string
  message: string
  color: string
}

/** What a STOP left behind, per kind - the reason it is safe to say a stop
 * costs nothing, and the sentence to show when the work itself recorded no
 * reason of its own. The same facts `costSentence` promises BEFORE the click,
 * stated afterwards; if one of these two ever stops matching the other, the
 * warning was a lie. */
const KEPT_AFTER_A_STOP: Record<string, string> = {
  detect: 'Every window it had already scored stays cached, so a retry ' +
    'resumes at the first one nobody reached.',
  transcribe: 'Chunks already decoded stay cached, so a retry resumes from ' +
    'the first missing one.',
  render: 'Clips already rendered keep their shorts; the rest are left as ' +
    'they were.',
  trim: 'The short was left as it was; the untrimmed master is kept either ' +
    'way, so applying the cut again costs nothing extra.',
}

/**
 * The notification for an entry that ended in anything but `done`.
 *
 * It exists as a pure function, rather than three copies inside three
 * components, because of the one thing all three got wrong: they were RED
 * for every outcome that was not `done`, so an operator's own "stop this"
 * came back looking exactly like a crash - the rule this queue restates most
 * often, broken at the last mile, in the place no type-check and no component
 * test in this project looks.
 *
 * The colour comes from `stateColor` and nowhere else, so a notification and
 * the Jobs screen's badge for the same entry can never disagree. `reason` -
 * the job's own recorded reason, or the entry's when the worker could never
 * start it - always wins over the canned sentence; the canned one is only for
 * a stop that recorded nothing, where "See the job log for details" would
 * send an operator looking for a cause that does not exist.
 */
export function endedNotice(
  what: string, kind: string, outcome: string, reason: string | null,
): EndedNotice {
  return {
    title: `${what} ${outcome}`,
    message: reason
      ?? (outcome === 'stopped'
        ? (KEPT_AFTER_A_STOP[kind]
           ?? 'Nothing already finished is undone.')
        : 'See the job log for details.'),
    color: stateColor(outcome),
  }
}

/**
 * One notification for a whole batch, rather than one per entry.
 *
 * A bulk action over thirteen streams would otherwise raise thirteen
 * toasts. A SINGLE-row action is a batch of one and goes through here too,
 * so there is one code path and not two - and for that case this DELEGATES
 * to `endedNotice` for every outcome except `done`, which `jobs.test.ts`
 * pins. `done` is the deliberate exception, not a gap: `endedNotice` is
 * built for the not-done cases - `KEPT_AFTER_A_STOP`'s per-kind sentences,
 * and "See the job log for details." as its fallback - and pointing a
 * successful job at a log for "details" of nothing would be exactly the
 * kind of small lie this module exists to avoid. So a batch of one that
 * succeeded gets its own shape here instead.
 *
 * The colour comes from `stateColor` via the worst outcome present, in the
 * order failed > interrupted > stopped > done. `stopped` sits BELOW
 * `interrupted` and nowhere near `failed` on purpose: a stop is what the
 * operator asked for, and reporting it in the failure colour sends them
 * looking for a cause that does not exist.
 */
export function batchNotice(
  what: string, kind: string,
  outcomes: { outcome: string; reason: string | null }[],
): EndedNotice {
  if (outcomes.length === 0) {
    // No caller reaches this today - every call site builds `outcomes` from
    // the batch it just acted on, which is never empty. Handled anyway so a
    // future caller cannot get back ". The Jobs screen has each one." with an
    // empty count line in front of it.
    return { title: `${what} finished`, message: 'Nothing was left to report.',
      color: stateColor('done') }
  }
  if (outcomes.length === 1) {
    const only = outcomes[0]
    if (only.outcome !== 'done') {
      return endedNotice(what, kind, only.outcome, only.reason)
    }
    return {
      title: `${what} finished`,
      message: only.reason ?? 'It is done.',
      color: stateColor('done'),
    }
  }
  const counts: Record<string, number> = {}
  for (const { outcome } of outcomes) counts[outcome] = (counts[outcome] ?? 0) + 1
  const worst = ['failed', 'interrupted', 'stopped', 'done']
    .find((state) => (counts[state] ?? 0) > 0) ?? 'done'
  const parts: string[] = []
  for (const state of ['done', 'failed', 'stopped', 'interrupted']) {
    const count = counts[state] ?? 0
    if (count > 0) parts.push(`${count} ${state === 'done' ? 'finished' : state}`)
  }
  return {
    title: `${what}: ${outcomes.length} ended`,
    message: `${parts.join(', ')}. The Jobs screen has each one.`,
    color: stateColor(worst),
  }
}

/** Whether this entry is still waiting to start, working, or over.
 *
 * A state this build does not know - `jobs.json` is a plain file an older or
 * newer version may have written - reads as `terminal`, which is the safe
 * answer for a panel: it stops tracking and hands the operator back their
 * controls, rather than showing a spinner for a state it can never see end.
 * The Jobs screen is where an unknown row can still be read and dropped. */
export function activity(entry: JobEntry): 'pending' | 'active' | 'terminal' {
  return ACTIVITY[entry.state as JobState] ?? 'terminal'
}

/** This entry as the plan currently holds it, or null when the plan has no
 * such row - which happens for real: `_trim_finished` keeps only the most
 * recent 50 finished entries, so a long-finished one genuinely ages out.
 * Searches all three sections, because a row moves between them as it runs. */
export function findEntry(plan: JobPlan, entryId: string): JobEntry | null {
  for (const section of [plan.running, plan.queued, plan.finished]) {
    const found = section.find((row) => row.id === entryId)
    if (found !== undefined) return found
  }
  return null
}

/**
 * Why the entry an operator just queued has not started yet - or null when
 * there is nothing to explain, because it is running or already over.
 *
 * This is the sentence every panel that used to flip straight to "running" on
 * click now needs. A queued job does NOT start when the operator clicks it,
 * and a panel that shows a spinner instead of saying so is the lying button
 * this whole queue exists to avoid. There are two reasons that are not merely
 * "wait a moment", and both come off this same payload:
 *
 * - **The worker is not running** (`worker_running: false`). Nothing in the
 *   plan will EVER start - `create_app` builds the worker and leaves it
 *   stopped; only `bin/yt-shorts studio` starts it. This one is not a delay,
 *   it is a dead end, so it is checked first and stated as a fact about the
 *   worker (never "idle", never "nothing to do" - see `stallNote`).
 * - **Another job holds this event's lock**, which the worker records on the
 *   entry itself as its `reason` while leaving it `queued`. That IS normal
 *   and temporary - a CLI render, or another job of this studio's own - and
 *   the entry is never failed for it, so the server's own wording is passed
 *   through rather than reworded into something more alarming.
 *
 * Everything else is ordinary queueing: paused, waiting behind others, or
 * waiting for a free slot in its pool. `queued_ahead` (what `POST /api/jobs`
 * answers with) is not usable here because it is a number from the moment of
 * the click; this counts the rows that are actually still in front of it now,
 * and counts only `queued` ones - a `paused` row keeps its place in line but
 * `claim_next` skips it, so counting it would overstate the wait.
 */
export function waitNote(plan: JobPlan, entry: JobEntry): string | null {
  if (activity(entry) !== 'pending') return null
  if (entry.state === 'paused') {
    return 'It is paused, so it will not start until it is resumed on the Jobs screen.'
  }
  if (!plan.worker_running) {
    return 'The worker is not running, so this will not start at all. Only ' +
      '`bin/yt-shorts studio` starts it; a studio app built any other way ' +
      'leaves it stopped.'
  }
  if (entry.reason) return entry.reason
  // A dependency the queue is holding this entry back for (`Entry.after`,
  // checked by job_queue._dependency_status). Below `entry.reason`, because
  // a recorded reason is the more specific fact, and below the
  // worker_running check, because a stopped worker is a dead end while this
  // is temporary.
  //
  // Without this branch the answer was actively FALSE: a dependent whose
  // dependency is RUNNING has nothing queued in front of it, so `ahead` is
  // 0 and the last line below promised it starts "as soon as the worker has
  // a free slot" - and a free slot is precisely what will not start it.
  // Unreachable from the browser until the Streams tab began chaining a
  // detect behind its transcription; JobsScreen only ever DISPLAYED `after`.
  //
  // A dependency the plan no longer holds is SATISFIED, not missing -
  // `_trim_finished` ages a long-since-done one out, and the queue treats
  // absence as met - so this stays quiet for it (`dependency === null` below
  // reaches neither of the two branches that follow).
  if (entry.after) {
    const dependency = findEntry(plan, entry.after)
    if (dependency !== null && activity(dependency) !== 'terminal') {
      return `It waits for the ${dependency.kind} job it depends on to finish ` +
        `first, so a free worker slot will not start it yet.`
    }
    // A dependency that HAS ended, but not in `done`, is not a wait - it is a
    // dead end for this entry too. `job_queue.claim_next` fails this entry on
    // its very next pass ("dependency … ended without succeeding; this entry
    // can never run"), so promising "next in line" here would be false for
    // roughly the second it takes the worker to get there.
    //
    // Gated on a state this build KNOWS to be terminal-and-not-done
    // (`failed`/`stopped`/`interrupted`), not merely `!== 'done'`. A `jobs.json`
    // another build wrote can hold a dependency state this build has never
    // heard of - `activity()` answers `'terminal'` for it (the safe default
    // for a panel FOLLOWING that row), but `job_queue._dependency_status`
    // answers `"waiting"` for the identical state, because the Python side
    // only recognises `done`/`failed`/`stopped`/`interrupted` as settled and
    // treats anything else as "not yet". The naive `!== 'done'` check here
    // used to agree with `activity()`'s optimistic terminal-by-default and
    // disagree with the queue itself: it told the operator this entry "will
    // be failed on the worker's next pass" for a dependency `claim_next`
    // will in fact hold in `_dependency_status`'s `"waiting"` branch
    // indefinitely. Only a state this file can actually name gets to make
    // that claim.
    const KNOWN_DEAD_END: JobState[] = ['failed', 'stopped', 'interrupted']
    if (dependency !== null &&
        KNOWN_DEAD_END.includes(dependency.state as JobState)) {
      return `The ${dependency.kind} job it depends on ended without ` +
        `succeeding, so this entry cannot run - it will be failed on the ` +
        `worker's next pass.`
    }
  }
  const ahead = plan.queued.filter(
    (row) => row.state === 'queued' && row.position < entry.position).length
  if (ahead > 0) {
    return `It is waiting behind ${ahead} other queued ` +
      `${ahead === 1 ? 'entry' : 'entries'}.`
  }
  return 'It is next in line, and starts as soon as the worker has a free slot.'
}

/** The name of the log file this entry's work is writing, under
 * `<workspace>/logs/jobs/` - or null until the worker has actually started
 * it and set `job_id`.
 *
 * Built from `job_id`, NEVER from `entry.id`: `/api/jobs/{id}` addresses two
 * disjoint id spaces (a `studio.jobs.Job` for `GET` and `…/log`, a queue
 * `Entry` for `DELETE` and every `POST …/{id}/…`), minted by different code
 * and never equal. The name matches `studio/jobs.py`'s `_open_job_log`
 * (`<kind>-<job id>.log`), which is what the Logs screen lists. */
export function jobLogFile(entry: JobEntry): string | null {
  return entry.job_id === null ? null : `${entry.kind}-${entry.job_id}.log`
}

/** One row's params as a short line: the channel/event scope first, then
 * everything else `key=value`, alphabetically so the order does not depend
 * on how the JSON happened to be written.
 *
 * A value may be the literal `"[redacted]"` where the NAME looked like a
 * secret (see api.py's `_safe_params`). It is rendered as it arrived and
 * never sent anywhere - see this module's docstring. */
export function paramSummary(entry: JobEntry): string {
  const params = entry.params ?? {}
  const parts: string[] = []
  const channel = params.channel
  const event = params.event
  if (typeof channel === 'string' && typeof event === 'string') {
    parts.push(`${channel}/${event}`)
  }
  for (const key of Object.keys(params).sort()) {
    if (parts.length > 0 && (key === 'channel' || key === 'event')) continue
    parts.push(`${key}=${String(params[key])}`)
  }
  return parts.join(' · ')
}
