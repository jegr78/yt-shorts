import { describe, expect, it } from 'vitest'
import {
  activity,
  allowedActions,
  batchNotice,
  endedNotice,
  findEntry,
  isStoppable,
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
  waitNote,
  type JobEntry,
  type JobPlan,
} from './jobs'

function entry(over: Partial<JobEntry> = {}): JobEntry {
  return {
    id: 'e1',
    kind: 'render',
    state: 'queued',
    params: { channel: 'erf', event: 'n24-2026' },
    reason: null,
    progress: null,
    created_at: 1000,
    after: null,
    job_id: null,
    position: 0,
    pool: 'cpu',
    stop_point: 'after the current clip',
    hard_stop_allowed: true,
    stoppable: true,
    ...over,
  }
}

function plan(over: Partial<JobPlan> = {}): JobPlan {
  return {
    running: [],
    queued: [],
    finished: [],
    limits: { cpu: 1, net: 3 },
    worker_running: true,
    load_error: null,
    ...over,
  }
}

describe('kindSpec', () => {
  it('reads the stop point, the hard-stop flag and stoppable off the row itself', () => {
    expect(kindSpec(entry())).toEqual({
      stopPoint: 'after the current clip',
      hardStopAllowed: true,
      stoppable: true,
    })
  })
})

describe('isStoppable', () => {
  // This is the fix for a measured hole: `isStoppable` used to compare
  // `stop_point` against the literal 'cannot be stopped', and a reviewer
  // changed KINDS["upload"].stop_point to "Cannot be stopped" - 389 Python
  // tests stayed green while the screen would have offered a Stop button on
  // a running upload. The server sends a boolean now, and nothing here
  // reads the phrase.
  it('reads the server boolean, not the wording of stop_point', () => {
    expect(isStoppable(kindSpec(entry({ stoppable: true })))).toBe(true)
    expect(isStoppable(kindSpec(entry({ stoppable: false })))).toBe(false)
  })

  it('is unmoved by a stop_point reworded in any case or phrasing', () => {
    for (const phrase of ['cannot be stopped', 'Cannot be stopped', 'no stop', null]) {
      expect(isStoppable(kindSpec(entry({ stop_point: phrase, stoppable: true }))))
        .toBe(true)
      expect(isStoppable(kindSpec(entry({ stop_point: phrase, stoppable: false }))))
        .toBe(false)
    }
  })
})

describe('allowedActions', () => {
  it('offers pause, both moves and remove on a queued entry', () => {
    const row = entry({ state: 'queued' })
    expect(allowedActions(row, kindSpec(row))).toEqual(['pause', 'up', 'down', 'remove'])
  })

  it('offers resume and remove on a paused entry, but never a move', () => {
    // job_queue.move refuses anything that is not `queued` (409), so a move
    // button on a paused row would be a button that only ever errors.
    const row = entry({ state: 'paused' })
    expect(allowedActions(row, kindSpec(row))).toEqual(['resume', 'remove'])
  })

  it('offers a stop and a hard stop on a running entry, and never a remove', () => {
    const row = entry({ state: 'running' })
    expect(allowedActions(row, kindSpec(row))).toEqual(['stop', 'kill'])
  })

  it('offers only the graceful stop where the kind allows no hard stop', () => {
    const row = entry({ state: 'running', kind: 'copy', hard_stop_allowed: false,
                        stop_point: 'after the current file' })
    expect(allowedActions(row, kindSpec(row))).toEqual(['stop'])
  })

  it('offers NOTHING on a running upload - it cannot be stopped at any level', () => {
    // KINDS["upload"] is `KindSpec("net", "cannot be stopped", False)` and
    // `STARTERS["upload"].stoppable` is False: the starter takes no cancel
    // token at all, so `POST …/stop` answers 409 `not_stoppable`. A stop
    // button here would be a button that lies.
    const row = entry({
      kind: 'upload', state: 'running', pool: 'net',
      stop_point: 'cannot be stopped', hard_stop_allowed: false, stoppable: false,
    })
    expect(allowedActions(row, kindSpec(row))).toEqual([])
  })

  it('offers the hard-stop escalation on a stopping entry, and nothing else', () => {
    // The graceful stop was already accepted, and it waits for the work's
    // own safe point - minutes away for a stream chunk or a rendered clip.
    // Escalating to a kill is a real thing an operator needs, and the
    // server performs it (Worker.request_stop tolerates an entry already in
    // `stopping` when force is set). A SECOND graceful stop is not offered:
    // it asks for nothing that was not already asked for. A resume is not a
    // transition the queue has at all, and a remove is refused outright.
    const row = entry({ state: 'stopping' })
    const actions = allowedActions(row, kindSpec(row))
    expect(actions).toEqual(['kill'])
    expect(actions).not.toContain('stop')
    expect(actions).not.toContain('resume')
    expect(actions).not.toContain('remove')
  })

  it('offers nothing on a stopping entry whose kind has no hard stop', () => {
    // `copy`: its graceful stop is real (checked before every file) but
    // there is no subprocess to terminate, so a hard button could do nothing
    // the graceful one has not already done.
    const row = entry({ state: 'stopping', kind: 'copy', hard_stop_allowed: false,
                        stop_point: 'after the current file' })
    expect(allowedActions(row, kindSpec(row))).toEqual([])
  })

  it('offers nothing on a stopping entry of a kind it cannot stop at all', () => {
    const row = entry({ state: 'stopping', kind: 'upload', stoppable: false,
                        hard_stop_allowed: false, stop_point: 'cannot be stopped' })
    expect(allowedActions(row, kindSpec(row))).toEqual([])
  })

  it('offers retry and remove on a failed, interrupted or stopped entry', () => {
    // `stopped` is here on the operator's own decision, and it closes a
    // promise this very module makes: `stopWarning` tells them before the
    // click that "a retry resumes at the first window nobody reached", and
    // the screen used to answer that by offering Remove and nothing else.
    for (const state of ['failed', 'interrupted', 'stopped'] as const) {
      const row = entry({ state })
      expect(allowedActions(row, kindSpec(row))).toEqual(['retry', 'remove'])
    }
  })

  it('offers only remove on a done entry - a success is not retried', () => {
    // The one terminal state with no retry: re-running work that succeeded
    // is a new request, and for a paid kind it spends the money again.
    const row = entry({ state: 'done' })
    expect(allowedActions(row, kindSpec(row))).toEqual(['remove'])
  })

  it('promises a resumable retry only where one is actually offered', () => {
    // The two halves that fell out of step: `stopWarning`'s cost sentence
    // names a retry, and `allowedActions` is what has to provide it. Pinned
    // together so neither can be changed alone again.
    for (const kind of ['detect', 'transcribe'] as const) {
      const running = entry({ kind, state: 'running' })
      expect(stopWarning(running, kindSpec(running))).toContain('a retry resumes')
      const stopped = entry({ kind, state: 'stopped' })
      expect(allowedActions(stopped, kindSpec(stopped))).toContain('retry')
    }
  })

  it('offers no stop for a kind this studio no longer knows', () => {
    // An entry written by an older version: api.py sends stop_point null,
    // hard_stop_allowed false and stoppable false for a kind missing from
    // jobs.KINDS/worker.STARTERS. Refusing to offer a stop is the safe
    // default; the row can still be removed.
    const row = entry({ kind: 'ancient', state: 'running', stop_point: null,
                        hard_stop_allowed: false, stoppable: false })
    expect(allowedActions(row, kindSpec(row))).toEqual([])
  })
})

describe('endedNotice', () => {
  it('never reports a stop in the failure colour', () => {
    // The rule this branch restates most often, broken at the last mile:
    // App.tsx and ClipEditor.tsx were RED for every outcome that was not
    // `done`, so an operator's own "stop this" came back looking exactly
    // like a crash. Asserted against `stateColor` rather than the literal
    // 'orange', so the two can never be changed apart.
    const stopped = endedNotice('Moment detection', 'detect', 'stopped', null)
    expect(stopped.color).toBe(stateColor('stopped'))
    expect(stopped.color).not.toBe('red')
    expect(stopped.title).toBe('Moment detection stopped')
  })

  it('still reports a failure in the failure colour', () => {
    // The other half: making a stop orange must not have made a genuine
    // failure quiet.
    const failed = endedNotice('Trim', 'trim', 'failed', null)
    expect(failed.color).toBe(stateColor('failed'))
    expect(failed.color).toBe('red')
  })

  it('says what a stop KEPT, per kind, instead of pointing at a log', () => {
    // "See the job log for details" sends an operator looking for a cause
    // that does not exist: they asked for this. What they need instead is
    // what survived - the same facts `stopWarning` promised before the click.
    expect(endedNotice('x', 'detect', 'stopped', null).message).toContain('cached')
    expect(endedNotice('x', 'transcribe', 'stopped', null).message).toContain('cached')
    expect(endedNotice('x', 'trim', 'stopped', null).message).toContain('master')
    // A kind with no sentence of its own still says something true.
    expect(endedNotice('x', 'teleport', 'stopped', null).message)
      .toContain('Nothing already finished is undone')
    // A FAILURE has no such promise to make, so it points at the log.
    expect(endedNotice('x', 'detect', 'failed', null).message)
      .toContain('job log')
  })

  it('prefers the recorded reason over any canned sentence', () => {
    // The job's own reason - or the entry's, when the worker could never
    // start it - is the only thing that says what actually happened.
    for (const outcome of ['stopped', 'failed', 'interrupted']) {
      expect(endedNotice('x', 'detect', outcome, 'ffmpeg exploded').message)
        .toBe('ffmpeg exploded')
    }
  })
})

describe('stopWarning', () => {
  it('never promises that the stop is immediate', () => {
    for (const kind of ['render', 'detect', 'transcribe', 'trim']) {
      const row = entry({ kind, state: 'running' })
      const text = stopWarning(row, kindSpec(row))
      expect(text.toLowerCase()).not.toContain('immediat')
      expect(text.toLowerCase()).not.toContain('at once')
      expect(text.toLowerCase()).not.toContain('right away')
    }
  })

  it('names the kind own stop point, so the operator knows when it lands', () => {
    const row = entry({ kind: 'transcribe', state: 'running',
                        stop_point: 'after the current chunk' })
    expect(stopWarning(row, kindSpec(row))).toContain('after the current chunk')
  })

  it('names what a detect has already paid for, and that a retry pays only for the rest', () => {
    // Both halves, and the second one is the correction: the sentence used to
    // end at "the rest of the stream is never scanned", which was true and
    // read as though stopping threw the money away. The per-window cache
    // means a retry resumes at the first unscored window - the warning has to
    // say so, without dropping the part that IS a real cost.
    const row = entry({ kind: 'detect', state: 'running', pool: 'net',
                        stop_point: 'after the current window' })
    const text = stopWarning(row, kindSpec(row))
    expect(text).toMatch(/paid for/i)
    expect(text).toMatch(/refunds nothing/i)
    expect(text).toMatch(/cached/i)
    expect(text).toMatch(/resumes at the first window nobody reached/i)
    expect(text).not.toMatch(/never scanned/i)
  })

  it('names the paid windows by count when a progress reading exists', () => {
    // What a running detect actually carries once its first window is
    // scored (worker._progress_reporter writes it); the countless wording
    // above is what the sentence falls back to before that.
    const row = entry({
      kind: 'detect', state: 'running', stop_point: 'after the current window',
      progress: { unit: 'window', done: 3, total: 9 },
    })
    expect(stopWarning(row, kindSpec(row))).toContain('3 of 9')
  })

  it('says a transcribe resumes from its cached chunks rather than starting over', () => {
    const row = entry({ kind: 'transcribe', state: 'running',
                        stop_point: 'after the current chunk' })
    expect(stopWarning(row, kindSpec(row))).toMatch(/cached|resume/i)
  })

  it('says plainly that an unstoppable kind cannot be stopped', () => {
    const row = entry({ kind: 'upload', state: 'running', stoppable: false,
                        stop_point: 'cannot be stopped', hard_stop_allowed: false })
    expect(stopWarning(row, kindSpec(row))).toMatch(/cannot be stopped/i)
  })
})

describe('progressLabel', () => {
  it('returns null for an entry carrying no reading at all', () => {
    // Which is most rows: a kind that counts nothing, a row that is not
    // running (the reading is cleared on the way out of `running`), and a
    // running one that has not finished its first unit yet. Returning
    // "0 of 0" for any of them would put a broken-looking line on the row.
    expect(progressLabel(entry())).toBeNull()
    expect(progressPercent(entry())).toBeNull()
  })

  it('invents nothing for a kind whose work has one unit', () => {
    // `jobs.KINDS["trim"].progress_unit` is null, so the server hands a
    // trim no callback and its rows carry no reading. "1 of 1" for a single
    // ffmpeg cut is a decoration, and worse than silence - so this must
    // stay null even though UNIT_BY_KIND has a word for a trim, which is
    // there only for a hand-edited jobs.json.
    expect(progressLabel(entry({ kind: 'trim', state: 'running' }))).toBeNull()
    expect(progressPercent(entry({ kind: 'trim', state: 'running' }))).toBeNull()
    expect(progressLabel(entry({ kind: 'upload', state: 'running' }))).toBeNull()
  })

  it('returns null rather than a zero for a total of zero', () => {
    expect(progressLabel(entry({ progress: { unit: 'chunk', done: 0, total: 0 } }))).toBeNull()
    expect(progressPercent(entry({ progress: { unit: 'chunk', done: 0, total: 0 } }))).toBeNull()
  })

  it('returns null for a shape it does not recognise', () => {
    for (const progress of ['half', 42, [], { done: 1 }, { done: '1', total: '2' },
                            { done: 1, total: Number.NaN }]) {
      expect(progressLabel(entry({ progress }))).toBeNull()
    }
  })

  it('reads the unit, the count and the total when a producer writes one', () => {
    expect(progressLabel(entry({
      kind: 'transcribe', progress: { unit: 'chunk', done: 20, total: 50 },
    }))).toBe('chunk 20 of 50')
    expect(progressPercent(entry({
      kind: 'transcribe', progress: { unit: 'chunk', done: 20, total: 50 },
    }))).toBe(40)
  })

  it('falls back to the kind own unit when the producer names none', () => {
    expect(progressLabel(entry({ kind: 'detect', progress: { done: 3, total: 9 } })))
      .toBe('window 3 of 9')
    expect(progressLabel(entry({ kind: 'wat', progress: { done: 3, total: 9 } })))
      .toBe('step 3 of 9')
  })

  it('clamps a percentage that overshoots its own total', () => {
    expect(progressPercent(entry({ progress: { done: 12, total: 9 } }))).toBe(100)
    expect(progressPercent(entry({ progress: { done: -2, total: 9 } }))).toBe(0)
  })
})

describe('moveTarget', () => {
  // Positions index the WHOLE plan, and the queued rows are NOT contiguous
  // in it - that is the whole point of this function, and the fixture has to
  // show it. Here: queued at 1, a finished/running entry at 2, queued at 3,
  // two more non-queued rows, queued at 6. A contiguous fixture (2,3,4) is
  // what an earlier version of this test used, and it cannot tell
  // `neighbour.position` from `position ± 1` at all - the two coincide, so
  // the claim in moveTarget's own docstring ("position - 1 can land on a
  // finished entry") went unpinned and only the end-clamp cases killed the
  // mutation.
  const queued = [
    entry({ id: 'a', position: 1 }),
    entry({ id: 'b', position: 3 }),
    entry({ id: 'c', position: 6 }),
  ]

  it('targets the queued neighbour own position, not position plus or minus one', () => {
    // b sits at 3 with non-queued entries either side of it: `position - 1`
    // would be 2 (the row between a and b, which no operator can reorder)
    // and `position + 1` would be 4 - neither swaps anything visible.
    expect(moveTarget(queued, 1, 'up')).toBe(1)
    expect(moveTarget(queued, 1, 'down')).toBe(6)
  })

  it('is null at either end, so the button can be disabled rather than lie', () => {
    expect(moveTarget(queued, 0, 'up')).toBeNull()
    expect(moveTarget(queued, 2, 'down')).toBeNull()
  })

  it('is null for a single queued entry in either direction', () => {
    expect(moveTarget([entry({ position: 7 })], 0, 'up')).toBeNull()
    expect(moveTarget([entry({ position: 7 })], 0, 'down')).toBeNull()
  })
})

describe('recentFirst', () => {
  it('reverses the queue-ordered finished section for display', () => {
    // GET /api/jobs returns `finished` in QUEUE order, oldest first, like the
    // other two sections - it is the plan sliced by state, not an activity
    // feed. A "recently finished" heading over that order would be a lie.
    const rows = [entry({ id: 'a' }), entry({ id: 'b' }), entry({ id: 'c' })]
    expect(recentFirst(rows).map((row) => row.id)).toEqual(['c', 'b', 'a'])
  })

  it('does not mutate the array it was given', () => {
    const rows = [entry({ id: 'a' }), entry({ id: 'b' })]
    recentFirst(rows)
    expect(rows.map((row) => row.id)).toEqual(['a', 'b'])
  })
})

describe('stallNote', () => {
  it('says nothing while the worker is running', () => {
    expect(stallNote(plan({ worker_running: true, queued: [entry()] }))).toBeNull()
  })

  it('says the worker is not running, never that the queue is idle', () => {
    const note = stallNote(plan({ worker_running: false }))
    expect(note).not.toBeNull()
    expect(note as string).toMatch(/worker is not running/i)
    expect((note as string).toLowerCase()).not.toContain('idle')
    expect((note as string).toLowerCase()).not.toContain('nothing to do')
  })

  it('names how many entries are waiting on a worker that will never start them', () => {
    const note = stallNote(plan({
      worker_running: false,
      queued: [entry({ id: 'a' }), entry({ id: 'b' })],
      running: [entry({ id: 'c', state: 'running' })],
    }))
    expect(note as string).toContain('2')
    expect(note as string).toMatch(/will not start|never start/i)
  })
})

describe('activity', () => {
  it('classifies every state a JobState can be', () => {
    expect(activity(entry({ state: 'queued' }))).toBe('pending')
    expect(activity(entry({ state: 'paused' }))).toBe('pending')
    expect(activity(entry({ state: 'running' }))).toBe('active')
    expect(activity(entry({ state: 'stopping' }))).toBe('active')
    for (const state of ['done', 'failed', 'stopped', 'interrupted']) {
      expect(activity(entry({ state }))).toBe('terminal')
    }
  })

  it('reads an unknown state as terminal, so a panel hands the controls back', () => {
    // jobs.json is a plain file an older or newer version may have written.
    // "pending" or "active" here would leave a panel spinning on a state it
    // can never see end; terminal frees it, and the Jobs screen is where such
    // a row can still be read and dropped.
    expect(activity(entry({ state: 'transmogrifying' }))).toBe('terminal')
  })
})

describe('stateColor', () => {
  it('gives every state a JobState can be its own colour', () => {
    // Written once here rather than three times in three components: it used
    // to be a byte-identical Record<string, string> in JobsScreen,
    // RenderPanel and StreamPanel, each with its own `?? 'gray'` fallback, so
    // a new state failed the build at ACTIVITY and rendered gray in all
    // three. `stopping` is deliberately not `stopped`'s colour: one is work
    // asked to end, the other is work that has ended.
    expect(stateColor('queued')).toBe('gray')
    expect(stateColor('paused')).toBe('gray')
    expect(stateColor('running')).toBe('steel')
    expect(stateColor('done')).toBe('green')
    expect(stateColor('failed')).toBe('red')
    expect(stateColor('stopping')).not.toBe(stateColor('stopped'))
  })

  it('falls back to gray for a state this build does not know', () => {
    expect(stateColor('transmogrifying')).toBe('gray')
  })
})

describe('findEntry', () => {
  it('finds the row in whichever section it currently sits in', () => {
    const p = plan({
      running: [entry({ id: 'r', state: 'running' })],
      queued: [entry({ id: 'q' })],
      finished: [entry({ id: 'f', state: 'done' })],
    })
    expect(findEntry(p, 'r')?.id).toBe('r')
    expect(findEntry(p, 'q')?.id).toBe('q')
    expect(findEntry(p, 'f')?.id).toBe('f')
  })

  it('is null for a row that is no longer in the plan', () => {
    // Not hypothetical: _trim_finished keeps only the most recent 50 finished
    // entries, so a long-finished one genuinely ages out.
    expect(findEntry(plan(), 'gone')).toBeNull()
  })
})

describe('waitNote', () => {
  it('says nothing about an entry that is running or already over', () => {
    for (const state of ['running', 'stopping', 'done', 'failed', 'stopped', 'interrupted']) {
      expect(waitNote(plan({ worker_running: false }), entry({ state }))).toBeNull()
    }
  })

  it('names the stopped worker first, because that one is a dead end', () => {
    // A queued entry with a lock reason AND no worker: the worker is the
    // reason that matters, since nothing in the plan will ever start.
    const row = entry({ state: 'queued', reason: 'waiting for the event lock on erf/x' })
    const note = waitNote(plan({ worker_running: false, queued: [row] }), row)
    expect(note as string).toMatch(/worker is not running/i)
    expect((note as string).toLowerCase()).not.toContain('idle')
    expect((note as string).toLowerCase()).not.toContain('nothing to do')
  })

  it('passes the server own lock reason through while the worker is running', () => {
    const row = entry({
      state: 'queued',
      reason: 'waiting for the event lock on erf/n24-2026: another render or job is using this event',
    })
    expect(waitNote(plan({ worker_running: true, queued: [row] }), row))
      .toBe(row.reason)
  })

  it('says a paused entry is paused rather than inventing a wait', () => {
    const row = entry({ state: 'paused' })
    expect(waitNote(plan({ queued: [row] }), row) as string).toMatch(/paused/i)
  })

  it('counts the queued rows actually in front of it, skipping paused ones', () => {
    // A paused row keeps its place in line but claim_next skips it, so
    // counting it would overstate the wait.
    const row = entry({ id: 'mine', state: 'queued', position: 5 })
    const note = waitNote(plan({
      queued: [
        entry({ id: 'a', state: 'queued', position: 1 }),
        entry({ id: 'b', state: 'paused', position: 2 }),
        entry({ id: 'c', state: 'queued', position: 3 }),
        row,
        entry({ id: 'd', state: 'queued', position: 9 }),
      ],
    }), row)
    expect(note as string).toContain('2')
    expect(note as string).toMatch(/waiting behind/i)
  })

  it('says it is next in line when nothing queued is in front of it', () => {
    // Still not "starting now": a pool limit can hold it back with nothing
    // ahead of it at all, so the sentence names the free slot it waits for.
    const row = entry({ state: 'queued', position: 0 })
    expect(waitNote(plan({ queued: [row] }), row) as string).toMatch(/next in line|free slot/i)
  })
})

describe('jobLogFile', () => {
  it('is null until the worker has actually started the entry', () => {
    expect(jobLogFile(entry({ job_id: null }))).toBeNull()
  })

  it('builds the name from job_id, never from the entry own id', () => {
    // /api/jobs/{id} addresses TWO disjoint id spaces: a studio.jobs.Job for
    // GET and …/log, a queue Entry for DELETE and every POST …/{id}/…. A log
    // link built from the entry id 404s.
    const row = entry({ id: 'entry-1', kind: 'detect', job_id: 'job-9' })
    const name = jobLogFile(row) as string
    expect(name).toBe('detect-job-9.log')
    expect(name).not.toContain('entry-1')
  })
})

describe('paramSummary', () => {
  it('reads a channel/event pair as one scope', () => {
    expect(paramSummary(entry({ params: { channel: 'erf', event: 'n24-2026' } })))
      .toBe('erf/n24-2026')
  })

  it('appends the remaining params in a stable order', () => {
    expect(paramSummary(entry({
      params: { video_id: 'vid1', channel: 'erf', event: 'n24', force: true },
    }))).toBe('erf/n24 · force=true · video_id=vid1')
  })

  it('renders a redacted value as it arrived', () => {
    // api.py replaces the VALUE of a key-shaped param name with the literal
    // "[redacted]". It is rendered, and never sent back as a value.
    expect(paramSummary(entry({ params: { channel: 'erf', event: 'n24', api_key: '[redacted]' } })))
      .toBe('erf/n24 · api_key=[redacted]')
  })

  it('survives an entry with no params at all', () => {
    expect(paramSummary(entry({ params: {} }))).toBe('')
  })
})

describe('waitNote, for an entry that depends on another', () => {
  it('names the dependency instead of promising a free slot', () => {
    // The gap this closes was UNREACHABLE until the Streams tab began
    // chaining a detect behind its transcription: no browser call site
    // sent `after` at all (JobsScreen only displayed it). For a detect
    // waiting on a RUNNING transcription, `ahead` is 0, so this used to
    // say "It is next in line, and starts as soon as the worker has a free
    // slot" - and a free slot is exactly what does not start it.
    //
    // The reviewer swapped the two dependency branches and all loose assertions
    // still passed, so these must assert SPECIFIC phrases unique to the
    // waiting branch. The wrong sentence ("ended without succeeding") must
    // not satisfy these checks, and vice versa in the failed-dependency test.
    const dependency = entry({ id: 'tr1', kind: 'transcribe', state: 'running' })
    const dependent = entry({ id: 'de1', kind: 'detect', state: 'queued', after: 'tr1' })
    const note = waitNote(plan({ running: [dependency], queued: [dependent] }), dependent)
    expect(note).toContain('waits for')
    expect(note).toContain('transcribe')
    expect(note).toContain('finish first')
    expect(note).not.toContain('ended without succeeding')
  })

  it('waits on a dependency that has not started either', () => {
    // Same gate as above: assert specific phrases from the waiting branch
    // so a swap is caught. This is a queued (not running) dependency, still
    // not terminal, so it goes to the same "waits for" sentence.
    const dependency = entry({ id: 'tr1', kind: 'transcribe', state: 'queued', position: 0 })
    const dependent = entry({ id: 'de1', kind: 'detect', state: 'queued', after: 'tr1', position: 1 })
    const note = waitNote(plan({ queued: [dependency, dependent] }), dependent)
    expect(note).toContain('waits for')
    expect(note).toContain('transcribe')
    expect(note).toContain('finish first')
    expect(note).not.toContain('ended without succeeding')
  })

  it('says nothing about a dependency that is done', () => {
    const dependency = entry({ id: 'tr1', kind: 'transcribe', state: 'done' })
    const dependent = entry({ id: 'de1', kind: 'detect', state: 'queued', after: 'tr1' })
    const note = waitNote(plan({ finished: [dependency], queued: [dependent] }), dependent)
    expect(note).toBe('It is next in line, and starts as soon as the worker has a free slot.')
  })

  it('says nothing about a dependency the plan no longer holds', () => {
    // `_trim_finished` ages a long-since-done dependency out of the plan,
    // and `job_queue._dependency_status` treats an absent one as
    // SATISFIED. Saying it was still waiting would contradict the queue.
    const dependent = entry({ id: 'de1', kind: 'detect', state: 'queued', after: 'gone' })
    const note = waitNote(plan({ queued: [dependent] }), dependent)
    expect(note).toBe('It is next in line, and starts as soon as the worker has a free slot.')
  })

  it('still puts a stopped worker first', () => {
    // A dependency is temporary; a stopped worker is a dead end, and it is
    // the more useful thing to be told.
    const dependency = entry({ id: 'tr1', kind: 'transcribe', state: 'running' })
    const dependent = entry({ id: 'de1', kind: 'detect', state: 'queued', after: 'tr1' })
    const note = waitNote(
      plan({ running: [dependency], queued: [dependent], worker_running: false }), dependent)
    expect(note).toContain('worker is not running')
  })

  it('prefers a recorded reason over an unfinished dependency - both can genuinely be set', () => {
    // An entry can carry BOTH a queue-recorded defer reason (its event's
    // lock is held) and a still-running `after` dependency at once - this is
    // not a hypothetical, `job_queue.defer` and `after` are independent
    // fields. The recorded reason has to win: it is what the queue itself
    // most recently observed, and it is what a JobsScreen row is already
    // showing next to this entry (`entry.reason` is rendered directly
    // elsewhere). Swap the two checks in `waitNote` and this test fails -
    // that swap is exactly the regression this pins.
    const dependency = entry({ id: 'tr1', kind: 'transcribe', state: 'running' })
    const dependent = entry({
      id: 'de1', kind: 'detect', state: 'queued', after: 'tr1',
      reason: 'waiting for the event lock on erf/n24-2026: another render or job is using this event',
    })
    const note = waitNote(plan({ running: [dependency], queued: [dependent] }), dependent)
    expect(note).toBe(dependent.reason)
    expect(note).not.toContain('transcribe')
  })

  it('says a dependency that ended without succeeding makes this entry unrunnable', () => {
    // job_queue.claim_next fails a dependent whose dependency ended anything
    // but `done` on its next pass ("dependency … ended without succeeding;
    // this entry can never run"). Before this branch existed, a dependency
    // that had already failed/stopped/interrupted read as `activity !==
    // 'terminal'` being false, fell through to the ahead-count below, and
    // this entry - which the worker is about to fail outright - said "It is
    // next in line, and starts as soon as the worker has a free slot."
    //
    // Assert phrases UNIQUE to the failed-dependency branch so a swap is caught.
    // The wrong sentence ("waits for...finish first") must not satisfy these.
    for (const state of ['failed', 'stopped', 'interrupted']) {
      const dependency = entry({ id: 'tr1', kind: 'transcribe', state })
      const dependent = entry({ id: 'de1', kind: 'detect', state: 'queued', after: 'tr1' })
      const note = waitNote(plan({ finished: [dependency], queued: [dependent] }), dependent)
      expect(note).toContain('transcribe')
      expect(note).toContain('ended without succeeding')
      expect(note).toContain('will be failed')
      expect(note).not.toContain('waits for')
      expect(note).not.toContain('finish first')
      expect(note).not.toContain('next in line')
    }
  })

  it('does not claim a dependency in a state this build does not know will be failed', () => {
    // A `jobs.json` another build wrote can hold a dependency state this
    // build has never heard of. `activity()` answers 'terminal' for it (the
    // safe default for a panel FOLLOWING that row), but
    // `job_queue._dependency_status` answers "waiting" for the identical
    // state - it only treats done/failed/stopped/interrupted as settled.
    // The naive `dependency.state !== 'done'` check this branch used to use
    // agreed with `activity()` and disagreed with the queue: it told the
    // operator this entry "will be failed on the worker's next pass" for a
    // dependency the queue will in fact hold `queued` indefinitely.
    const dependency = entry({ id: 'tr1', kind: 'transcribe', state: 'archived' })
    const dependent = entry({ id: 'de1', kind: 'detect', state: 'queued', after: 'tr1' })
    const note = waitNote(plan({ finished: [dependency], queued: [dependent] }), dependent)
    expect(note).not.toContain('will be failed')
    expect(note).not.toContain('ended without succeeding')
  })
})

describe('batchNotice', () => {
  it('says exactly what endedNotice says for a batch of one - except done', () => {
    // A single-row action IS a batch of one, so there is one code path and
    // not two, for every outcome EXCEPT `done`. If these two ever disagree
    // for a not-done outcome, the same entry gets two different stories
    // depending on which button queued it.
    const outcomes = [{ outcome: 'failed', reason: 'yt-dlp exited 1' }]
    expect(batchNotice('Transcription', 'transcribe', outcomes))
      .toEqual(endedNotice('Transcription', 'transcribe', 'failed', 'yt-dlp exited 1'))
  })

  it('does NOT delegate to endedNotice for a done batch of one - on purpose', () => {
    // The deliberate exception, pinned rather than left as an unstated gap:
    // endedNotice is built for the not-done cases (KEPT_AFTER_A_STOP's
    // sentences, "See the job log for details." as its fallback), and
    // pointing a SUCCESSFUL job at a log for "details" of nothing is exactly
    // the kind of small lie this module exists to avoid. So a done batch of
    // one gets its own title and message rather than endedNotice's.
    const outcomes = [{ outcome: 'done', reason: null }]
    const notice = batchNotice('Transcription', 'transcribe', outcomes)
    expect(notice).toEqual({
      title: 'Transcription finished',
      message: 'It is done.',
      color: stateColor('done'),
    })
    expect(notice).not.toEqual(endedNotice('Transcription', 'transcribe', 'done', null))
  })

  it('handles an empty batch rather than emitting a malformed sentence', () => {
    // No caller reaches this today - every call site builds `outcomes` from
    // the batch it just acted on, which is never empty. Defended anyway: the
    // unguarded code produced ". The Jobs screen has each one." for an empty
    // array, which is exactly the kind of broken-looking sentence this
    // module exists to prevent.
    const notice = batchNotice('Transcriptions', 'transcribe', [])
    expect(notice.message).not.toMatch(/^\./)
    expect(notice.color).toBe(stateColor('done'))
  })

  it('is green when every one of them finished', () => {
    const outcomes = [
      { outcome: 'done', reason: null }, { outcome: 'done', reason: null }]
    const notice = batchNotice('Transcriptions', 'transcribe', outcomes)
    expect(notice.color).toBe(stateColor('done'))
    expect(notice.message).toContain('2 finished')
  })

  it('is the failure colour when any of them failed', () => {
    const outcomes = [
      { outcome: 'done', reason: null }, { outcome: 'failed', reason: 'nope' }]
    const notice = batchNotice('Transcriptions', 'transcribe', outcomes)
    expect(notice.color).toBe(stateColor('failed'))
    expect(notice.message).toContain('1 failed')
    expect(notice.message).toContain('1 finished')
  })

  it('never reports a stop in the failure colour', () => {
    // The rule this queue restates most often, and the one three separate
    // components each got wrong: an operator's own "stop this" must not
    // come back looking like a crash.
    const outcomes = [
      { outcome: 'done', reason: null }, { outcome: 'stopped', reason: null }]
    const notice = batchNotice('Transcriptions', 'transcribe', outcomes)
    expect(notice.color).toBe(stateColor('stopped'))
    expect(notice.color).not.toBe(stateColor('failed'))
    expect(notice.message).toContain('1 stopped')
  })
})
