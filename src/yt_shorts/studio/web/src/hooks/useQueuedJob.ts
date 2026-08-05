import { type Job, type JobEntry, type JobPlan } from '../api'
import { useJobPolling } from './useJobPolling'
import { useQueuedEntries } from './useQueuedEntries'

/** One tracked queue entry, as a panel needs to read it. */
export interface QueuedWork {
  /** The plan's row for this entry - the LAST one seen, so a finished entry
   * that has since aged out of the plan does not blank the panel. Null until
   * the first poll answers. */
  entry: JobEntry | null
  /** The plan the row was read from. Needed for one thing this row cannot
   * carry: whether the worker is running at all. */
  plan: JobPlan | null
  /** The `studio.jobs.Job` doing this entry's work, once the worker has
   * claimed it and set `job_id` - null before that, which is most of the life
   * of a queued entry. Anything that follows a job by id (a log link, a
   * per-clip result list) must handle that null rather than build a link out
   * of the entry's own id: the two id spaces are disjoint (see jobs.ts). */
  job: Job | null
  /** True while the entry is queued or paused: enqueued, not started. */
  pending: boolean
  /** True while the work is actually in flight (running or stopping). */
  running: boolean
  /** The terminal state the entry reached, or null while it is still active.
   * Read this rather than `job.status` for "has it finished": an entry that
   * never started at all - a malformed param, an unresolvable profile - has
   * no Job to ask, and would otherwise finish invisibly. */
  outcome: string | null
  /** Why it has not started yet, or null when there is nothing to explain.
   * See `waitNote` - a panel MUST show this; it is the whole difference
   * between an honest queued state and a spinner that never moves. */
  waiting: string | null
  /** Why this hook STOPPED following the entry before it ever reached a
   * terminal state, or null while it is still following one. Two causes, and
   * a panel shows the sentence rather than deciding between them:
   * - the plan could not be read at all (the queue unavailable, contact
   *   lost). Never rendered as "not queued": the entry may well be sitting
   *   there, which is why `entry` is KEPT in this case.
   * - the entry is no longer in the plan - removed on the Jobs screen, or the
   *   plan rewritten. `entry` is cleared: the row genuinely does not exist,
   *   and a badge still reading "queued" would be a lie.
   *
   * Whenever this is set, `pending` and `running` are BOTH false. That is the
   * whole point of the field: a panel keyed on them hands the operator their
   * controls back instead of sitting disabled forever on an entry nothing
   * will ever resolve. A control that is disabled for good while the panel
   * still claims work is queued is the same lie as a button that claims to
   * have started something, pointed the other way. */
  error: string | null
}

const IDLE: QueuedWork = {
  entry: null, plan: null, job: null, pending: false, running: false,
  outcome: null, waiting: null, error: null,
}

/**
 * Follows one entry an operator just put in the plan (`POST /api/jobs`),
 * from "queued" through to whatever it ends as.
 *
 * This is the second half of routing a panel's button through the queue. The
 * first half is easy - call `enqueueJob` instead of the panel's own start
 * route - and the second is where the honesty lives: the work does NOT begin
 * on the click. Until the worker claims the entry there is no `job_id`, no
 * job log and no per-clip results, and it may never be claimed at all (see
 * `waitNote`'s two reasons). So this polls the PLAN, not the job, and only
 * reaches for the job once the entry names one.
 *
 * Polling stops when the entry reaches a terminal state, when it vanishes
 * from the plan (a long-finished row genuinely ages out - `_trim_finished`
 * keeps 50), or after the error budget above. The caller clears the id it
 * passed in when it is done with the result.
 *
 * **Neither of the two stops that are not a terminal state may leave the
 * panel pending.** This is the rule `useJobPolling` states in its own
 * docstring and implements by synthesising a `failed` job on a 404 and on
 * budget exhaustion, "so the consumer's status effects fire and the UI leaves
 * its 'running' state instead of being wedged on a job that will never
 * resolve". The same hazard is worse here, because this hook is what DISABLES
 * a panel's controls: both stops set `error`, and `pending`/`running` are
 * false for as long as it is set. Reached by an ordinary supported flow, not
 * a corner case - `allowedActions` offers `remove` for a `queued` entry, so
 * "queue a trim, change your mind on the Jobs screen, remove it" used to
 * leave the clip editor's Head/Tail/Apply controls dead until a reload.
 *
 * This is now `useQueuedEntries` with a single id: the `seen` race guard, the
 * error budget and the "error set means neither pending nor running" rule all
 * live there once instead of twice, and `useQueuedJob.test.tsx` is unchanged
 * from before that move - it is what proves nothing was lost re-expressing
 * this hook on top of the other one.
 */
export function useQueuedJob(entryId: string | null): QueuedWork {
  // One id through the multi-entry follower, so the `seen` race guard, the
  // error budget and the "error set means neither pending nor running" rule
  // exist in ONE place. A second copy of those three is exactly the
  // duplication this project has already paid for once, with three copies
  // of a colour ternary in three components.
  const ids = entryId ? [entryId] : EMPTY_IDS
  const tracked = useQueuedEntries(ids)
  const work = entryId ? tracked.byId[entryId] : undefined
  // Null until the worker claims the entry - which is exactly the state
  // this hook exists to make visible, so it is passed straight through
  // rather than faked with the entry's own id.
  const job = useJobPolling(work?.entry?.job_id ?? null)
  if (!entryId || work === undefined) return IDLE
  return { ...work, plan: tracked.plan, job }
}

/** A stable empty array, so `useQueuedEntries`'s effect key does not churn
 * on every render while no entry is being followed. */
const EMPTY_IDS: string[] = []
