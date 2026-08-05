import { useEffect, useRef, useState } from 'react'
import { ApiError, listJobs, type JobEntry, type JobPlan } from '../api'
import { activity, findEntry, waitNote } from '../jobs'

const POLL_INTERVAL_MS = 750
const MAX_CONSECUTIVE_ERRORS = 5

/** What this hook says when a row it was following has left the plan. Not
 * a read failure - the plan WAS read, and the entry is not in it. */
const GONE =
  'This entry is no longer in the plan - it was removed on the Jobs screen, ' +
  'or the plan was rewritten. Nothing has been started for it here; the Jobs ' +
  'screen is where to see what became of it.'

/** What it says when it gave up reading the plan. The entry is deliberately
 * NOT called gone: nothing was learned about it either way. */
const LOST =
  'Lost contact with the studio while following this entry - it may well ' +
  'still be in the plan; the Jobs screen is where to check.'

/** One tracked entry. Same fields `useQueuedJob` has always exposed, minus
 * the `job` and `plan` that only a single-entry follower needs. */
export interface TrackedWork {
  entry: JobEntry | null
  pending: boolean
  running: boolean
  outcome: string | null
  waiting: string | null
  /** Why this hook STOPPED following, or null. Whenever it is set,
   * `pending` and `running` are BOTH false - a control disabled for good
   * while the panel still claims work is queued is the same lie as a
   * button that claims to have started something. */
  error: string | null
}

export interface QueuedEntries {
  plan: JobPlan | null
  byId: Record<string, TrackedWork>
}

const IDLE: TrackedWork = {
  entry: null, pending: false, running: false, outcome: null,
  waiting: null, error: null,
}

/** Keeps only the entries of `state` whose key is still in `keep` - the
 * selective half of the reset below. A plain object filter rather than a Map
 * because `rows`/`errors`/`seen` are already plain `Record<string, T>`. */
function keepTracked<T>(state: Record<string, T>, keep: Set<string>): Record<string, T> {
  const next: Record<string, T> = {}
  for (const [id, value] of Object.entries(state)) {
    if (keep.has(id)) next[id] = value
  }
  return next
}

/**
 * Follows SEVERAL queue entries an operator just put in the plan.
 *
 * One `GET /api/jobs` answers for all of them - the plan is a single
 * document, and a bulk action over thirteen streams followed by thirteen
 * copies of `useQueuedJob` would fetch it thirteen times a second.
 * `useQueuedJob` is now this hook with one id, so the rules below exist
 * once: the `seen` race guard (a row absent BEFORE it was ever seen raced
 * the enqueue's own write and must be retried; one absent AFTER aged out or
 * was removed), the error budget, and the rule that a stop which is not a
 * terminal state must never leave a panel pending.
 *
 * Polling stops when EVERY tracked id has reached a terminal state or
 * stopped being followed. The caller clears the ids it passed in when it is
 * done with the results.
 *
 * The id SET can grow (a later batch tracking more legs) as well as shrink,
 * and a change to it used to reset ALL of `rows`/`errors`/`seen`, for every
 * id, not only the ones that changed - so `App.tsx`'s `streamEntryIds`
 * growing by one leg reset every already-tracked id's state too. Two real
 * consequences: every tracked entry read momentarily idle on each reset (a
 * running row's button re-enabled for a tick), and an id whose row had
 * already aged out of the plan's `finished` section could never settle
 * after a reset - absent, `seen` false, so it looked exactly like a poll
 * that raced its own enqueue and was retried forever, which pinned the
 * 750ms poll for the life of the screen. The fix below keeps the state of
 * any id still present in the new set and drops only the ids that left it -
 * which still serves the original reason for resetting (a caller switching
 * to a wholly different id must not read the previous one's state), because
 * an id that is genuinely gone from the set has its `rows`/`errors`/`seen`
 * entries dropped exactly the same as a full reset would have dropped them.
 *
 * That fix preserved the STATE but not the CONCLUSION drawn from it: the
 * poll loop's own `settled` set - which ids this run of the effect has
 * already decided not to re-check - was still rebuilt empty on every id-set
 * change, so an id whose row was already terminal (and had therefore never
 * needed re-checking) was put back up for re-interrogation anyway. For one
 * that had since aged out of `finished` (see `_trim_finished`'s 50-row cap),
 * that re-check reported it GONE - a real regression measured on this
 * branch, not a hypothetical: three 13-row "both" batches is 78 terminal
 * entries, well past the point where an early one has aged out while a
 * later batch is still changing the id set. `settled` is now seeded from
 * each id's last known row (via `rowsRef`, the same mirror-on-every-render
 * idiom `latest` uses for `ids`) whenever that row already reads terminal -
 * see the effect's own comment for why an ACTIVE row is deliberately not
 * seeded the same way.
 */
export function useQueuedEntries(ids: string[]): QueuedEntries {
  const [plan, setPlan] = useState<JobPlan | null>(null)
  const [rows, setRows] = useState<Record<string, JobEntry | null>>({})
  const [errors, setErrors] = useState<Record<string, string>>({})
  const seen = useRef<Record<string, boolean>>({})
  // The effect keys on the id SET, and on nothing else about the array.
  //
  // Sorted, because a caller can hand over the same ids in another order -
  // App derives `streamEntryIds` from `Object.values` of a map it mutates -
  // and restarting the poll for that would drop every id's known state for
  // a tick and re-fetch the whole plan for no new information.
  //
  // JSON, not `join(',')`, because a join is ambiguous by construction:
  // `['a,b']` and `['a','b']` produce the same string. Ids are server-minted
  // uuid4 hex today and cannot contain a comma - which is exactly the kind
  // of "cannot happen" that a later id format change turns into a silent
  // collision, where two different tracked sets would share one effect and
  // one set's state would be served for the other's.
  const key = JSON.stringify([...ids].sort())
  const latest = useRef(ids)
  latest.current = ids
  // Mirrors `rows` on every render, the same idiom `latest` uses for `ids` -
  // so the effect below can read the LAST KNOWN row for an id without
  // depending on `rows` itself (which would restart the poll loop on every
  // update it makes). See the effect's own `settled` seeding for why this
  // exists.
  const rowsRef = useRef<Record<string, JobEntry | null>>({})
  rowsRef.current = rows

  useEffect(() => {
    const keep = new Set(latest.current)
    seen.current = keepTracked(seen.current, keep)
    setRows((held) => keepTracked(held, keep))
    setErrors((held) => keepTracked(held, keep))
    if (latest.current.length === 0) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let failures = 0
    // Seeded from each id's LAST KNOWN row (preserved across this reset by
    // `keepTracked`, unlike a bare `new Set()` would be) whenever that row was
    // already terminal. Without this, every id-set change - a later batch
    // tracking one more leg, say - restarted `settled` from empty and put
    // every already-finished id back up for re-interrogation on the very next
    // poll. For one that had since aged out of the plan's `finished` section
    // (`_trim_finished` keeps only the most recent 50), that poll took the
    // "absent AND seen" branch below and reported it GONE - false for an
    // entry that genuinely ran for hours and finished; see this hook's own
    // module docstring and MUST-FIX 2 in the review this closes.
    //
    // An id last known ACTIVE (queued/running) is deliberately NOT seeded
    // here - if such an id is absent on the next poll, that is a genuine
    // removal (the Jobs screen's own `remove`, or a rewritten plan) and must
    // still be reported as one; only a row already `activity() === 'terminal'`
    // is exempted from that check.
    const settled = new Set<string>(
      latest.current.filter((id) => {
        const row = rowsRef.current[id]
        return row !== undefined && row !== null && activity(row) === 'terminal'
      }))

    async function poll() {
      try {
        const next = await listJobs()
        if (cancelled) return
        failures = 0
        setPlan(next)
        for (const id of latest.current) {
          if (settled.has(id)) continue
          const row = findEntry(next, id)
          if (row === null) {
            if (seen.current[id]) {
              settled.add(id)
              setRows((held) => ({ ...held, [id]: null }))
              setErrors((held) => ({ ...held, [id]: GONE }))
            }
            continue
          }
          seen.current[id] = true
          setRows((held) => ({ ...held, [id]: row }))
          if (activity(row) === 'terminal') settled.add(id)
        }
        if (settled.size >= latest.current.length) return
      } catch (err) {
        if (cancelled) return
        failures += 1
        if (failures >= MAX_CONSECUTIVE_ERRORS) {
          const detail = err instanceof ApiError ? err.message : String(err)
          const message = `${LOST} (${detail})`
          setErrors((held) => {
            const next = { ...held }
            for (const id of latest.current) {
              if (!settled.has(id)) next[id] = message
            }
            return next
          })
          return
        }
      }
      timer = setTimeout(poll, POLL_INTERVAL_MS)
    }
    poll()

    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [key])

  const byId: Record<string, TrackedWork> = {}
  for (const id of ids) {
    const entry = rows[id] ?? null
    const error = errors[id] ?? null
    const following = error === null
    const state = entry === null ? null : activity(entry)
    byId[id] = {
      ...IDLE,
      entry,
      pending: following && state === 'pending',
      running: following && state === 'active',
      outcome: state === 'terminal' && entry !== null ? entry.state : null,
      waiting: following && entry !== null && plan !== null ? waitNote(plan, entry) : null,
      error,
    }
  }
  return { plan, byId }
}
