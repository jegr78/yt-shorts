import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import type { JobEntry, JobPlan } from '../jobs'
import { useQueuedEntries } from './useQueuedEntries'

function entry(overrides: Partial<JobEntry> = {}): JobEntry {
  return {
    id: 'e1', kind: 'transcribe', state: 'queued', params: {}, reason: null,
    progress: null, created_at: 0, after: null, job_id: null, position: 0,
    pool: 'cpu', stop_point: 'the end of the current chunk',
    hard_stop_allowed: true, stoppable: true, ...overrides,
  }
}

function plan(overrides: Partial<JobPlan> = {}): JobPlan {
  return {
    running: [], queued: [], finished: [], limits: {},
    worker_running: true, load_error: null, ...overrides,
  }
}

describe('useQueuedEntries', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

  it('reads the plan ONCE for many entries', async () => {
    // The reason this hook exists rather than N useQueuedJob instances:
    // the plan is a single GET, and thirteen followers would fetch it
    // thirteen times a second.
    const listJobs = vi.spyOn(api, 'listJobs').mockResolvedValue(
      plan({ queued: [entry({ id: 'a' }), entry({ id: 'b' })] }))
    renderHook(() => useQueuedEntries(['a', 'b']))
    await act(async () => { await Promise.resolve() })
    expect(listJobs).toHaveBeenCalledTimes(1)
  })

  it('follows each id separately', async () => {
    vi.spyOn(api, 'listJobs').mockResolvedValue(plan({
      running: [entry({ id: 'a', state: 'running' })],
      queued: [entry({ id: 'b' })],
    }))
    const { result } = renderHook(() => useQueuedEntries(['a', 'b']))
    await act(async () => { await Promise.resolve() })
    expect(result.current.byId.a.running).toBe(true)
    expect(result.current.byId.b.pending).toBe(true)
  })

  it('reports an entry that left the plan, and stops keeping it pending', async () => {
    // `allowedActions` offers `remove` on a queued entry, so this is an
    // ordinary supported flow. A panel keyed on `pending` would otherwise
    // sit disabled for the life of the screen, still claiming work was
    // queued - the same lie as a button that claims to have started
    // something, pointed the other way.
    const listJobs = vi.spyOn(api, 'listJobs')
    listJobs.mockResolvedValueOnce(plan({ queued: [entry({ id: 'a' })] }))
    listJobs.mockResolvedValue(plan({ queued: [] }))
    const { result } = renderHook(() => useQueuedEntries(['a']))
    await act(async () => { await Promise.resolve() })
    await act(async () => { await vi.advanceTimersByTimeAsync(800) })
    expect(result.current.byId.a.error).toContain('no longer in the plan')
    expect(result.current.byId.a.entry).toBeNull()
    expect(result.current.byId.a.pending).toBe(false)
  })

  it('does not call an entry gone before it was ever seen', async () => {
    // A poll that raced the enqueue's own write. Retried, not reported.
    const listJobs = vi.spyOn(api, 'listJobs')
    listJobs.mockResolvedValueOnce(plan({ queued: [] }))
    listJobs.mockResolvedValue(plan({ queued: [entry({ id: 'a' })] }))
    const { result } = renderHook(() => useQueuedEntries(['a']))
    await act(async () => { await Promise.resolve() })
    expect(result.current.byId.a.error).toBeNull()
    await act(async () => { await vi.advanceTimersByTimeAsync(800) })
    expect(result.current.byId.a.entry?.id).toBe('a')
  })

  it('gives up after the error budget and says so, keeping the entry', async () => {
    // Unlike a removal, a failed read says nothing about whether the entry
    // is still there - so `entry` is KEPT and the sentence says as much.
    const listJobs = vi.spyOn(api, 'listJobs')
    listJobs.mockResolvedValueOnce(plan({ queued: [entry({ id: 'a' })] }))
    listJobs.mockRejectedValue(new api.ApiError(503, 'queue unavailable'))
    const { result } = renderHook(() => useQueuedEntries(['a']))
    await act(async () => { await Promise.resolve() })
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(result.current.byId.a.error).toContain('Lost contact')
    expect(result.current.byId.a.entry?.id).toBe('a')
    expect(result.current.byId.a.pending).toBe(false)
  })

  it('stops polling once every tracked entry is terminal', async () => {
    const listJobs = vi.spyOn(api, 'listJobs').mockResolvedValue(plan({
      finished: [entry({ id: 'a', state: 'done' }), entry({ id: 'b', state: 'failed' })],
    }))
    renderHook(() => useQueuedEntries(['a', 'b']))
    await act(async () => { await Promise.resolve() })
    const after = listJobs.mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(listJobs.mock.calls.length).toBe(after)
  })

  it('keeps following when one is terminal and another is not', async () => {
    const listJobs = vi.spyOn(api, 'listJobs').mockResolvedValue(plan({
      finished: [entry({ id: 'a', state: 'done' })],
      queued: [entry({ id: 'b' })],
    }))
    renderHook(() => useQueuedEntries(['a', 'b']))
    await act(async () => { await Promise.resolve() })
    const after = listJobs.mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(800) })
    expect(listJobs.mock.calls.length).toBeGreaterThan(after)
  })

  it('carries the queue\'s own wait reason through', async () => {
    vi.spyOn(api, 'listJobs').mockResolvedValue(plan({
      queued: [entry({ id: 'a', reason: 'waiting for the event lock on erf/x' })],
    }))
    const { result } = renderHook(() => useQueuedEntries(['a']))
    await act(async () => { await Promise.resolve() })
    expect(result.current.byId.a.waiting).toContain('event lock')
  })

  it('adding an id to the tracked set does not clear what is already known about the existing ids', async () => {
    // The slow-leak bug this closes: the effect used to reset ALL of
    // rows/errors/seen on every id-set change, so a later batch tracking one
    // more leg reset every already-tracked id too - a running row's button
    // would read idle for a tick, and an id that had aged out of the plan
    // could never settle again (see this hook's own module docstring).
    const listJobs = vi.spyOn(api, 'listJobs')
    listJobs.mockResolvedValue(plan({ running: [entry({ id: 'a', state: 'running' })] }))
    const { result, rerender } = renderHook(
      ({ ids }) => useQueuedEntries(ids),
      { initialProps: { ids: ['a'] } as { ids: string[] } },
    )
    await act(async () => { await Promise.resolve() })
    expect(result.current.byId.a.running).toBe(true)

    listJobs.mockResolvedValue(plan({
      running: [entry({ id: 'a', state: 'running' })],
      queued: [entry({ id: 'b' })],
    }))
    rerender({ ids: ['a', 'b'] })
    // Checked BEFORE the new poll resolves: 'a' must still read running
    // right through the id-set change, not blank out for even one tick.
    expect(result.current.byId.a.entry?.id).toBe('a')
    expect(result.current.byId.a.running).toBe(true)

    await act(async () => { await Promise.resolve() })
    expect(result.current.byId.a.running).toBe(true)
    expect(result.current.byId.b.pending).toBe(true)
  })

  it('keeps a terminal entry settled when it ages out of the plan on a later id-set change', async () => {
    // MUST-FIX 2 from the whole-branch review: an id whose last known row was
    // TERMINAL must not be re-interrogated on the next id-set change - an
    // entry that has genuinely aged out of `_trim_finished`'s 50-row cap is
    // an ordinary consequence of an hours-long transcription finishing and
    // time passing, not a removal. Before this, resetting `settled` to empty
    // on every id-set change made the next poll re-check 'a', find it absent
    // (aged out) and `seen.current['a']` true, and report it GONE - false for
    // an entry that really ran and really finished. Keep this passing
    // alongside "reports an entry that left the plan..." above: that one
    // covers a genuine removal (an id last seen ACTIVE), this one covers an
    // id last seen TERMINAL - the two must not collapse into one behaviour.
    const listJobs = vi.spyOn(api, 'listJobs')
    listJobs.mockResolvedValue(plan({ finished: [entry({ id: 'a', state: 'done' })] }))
    const { result, rerender } = renderHook(
      ({ ids }) => useQueuedEntries(ids),
      { initialProps: { ids: ['a'] } as { ids: string[] } },
    )
    await act(async () => { await Promise.resolve() })
    expect(result.current.byId.a.outcome).toBe('done')
    expect(result.current.byId.a.error).toBeNull()

    // A second batch tracks one more leg - the id SET changes, and 'a' has
    // since aged out of the plan entirely (as a long-finished entry
    // genuinely would).
    listJobs.mockResolvedValue(plan({ queued: [entry({ id: 'b' })] }))
    rerender({ ids: ['a', 'b'] })
    await act(async () => { await Promise.resolve() })

    expect(result.current.byId.a.outcome).toBe('done')
    expect(result.current.byId.a.error).toBeNull()
    expect(result.current.byId.b.pending).toBe(true)
  })

  it('removing an id drops its state, so re-adding it starts fresh rather than reporting it gone at once', async () => {
    const listJobs = vi.spyOn(api, 'listJobs')
    listJobs.mockResolvedValue(plan({
      running: [entry({ id: 'a', state: 'running' })],
      queued: [entry({ id: 'b' })],
    }))
    const { result, rerender } = renderHook(
      ({ ids }) => useQueuedEntries(ids),
      { initialProps: { ids: ['a', 'b'] } as { ids: string[] } },
    )
    await act(async () => { await Promise.resolve() })
    expect(result.current.byId.b.entry?.id).toBe('b') // b has been SEEN

    rerender({ ids: ['a'] }) // b removed - its rows/errors/seen must be dropped

    // Re-add b under a plan that does not (yet) contain it. If b's `seen`
    // flag had survived the removal, this would report it gone immediately
    // (the same "absent after being seen" rule the GONE test above pins);
    // the fix means it is retried quietly instead, exactly like a genuinely
    // new id racing its own enqueue.
    listJobs.mockResolvedValue(plan({ running: [entry({ id: 'a', state: 'running' })] }))
    rerender({ ids: ['a', 'b'] })
    await act(async () => { await Promise.resolve() })
    expect(result.current.byId.b.error).toBeNull()
    expect(result.current.byId.b.entry).toBeNull()
  })

  it('does not restart polling when the same ids arrive in a different order', async () => {
    // The effect keys on the id SET, not on the array. A caller that
    // rebuilds its list - App derives `streamEntryIds` from Object.values
    // of a map it mutates - can hand over the same ids in another order,
    // and restarting would drop every id's known state for a poll and
    // re-fetch the whole plan for no new information.
    const listJobs = vi.spyOn(api, 'listJobs').mockResolvedValue(
      plan({ running: [entry({ id: 'a', state: 'running' })],
             queued: [entry({ id: 'b' })] }))
    const { result, rerender } = renderHook(
      ({ ids }) => useQueuedEntries(ids), { initialProps: { ids: ['a', 'b'] } })
    await act(async () => { await Promise.resolve() })
    const callsBefore = listJobs.mock.calls.length

    rerender({ ids: ['b', 'a'] })
    await act(async () => { await Promise.resolve() })

    expect(listJobs.mock.calls.length).toBe(callsBefore)
    expect(result.current.byId.a.running).toBe(true)
  })

  it('starts clean for a genuinely different id set', async () => {
    // The other half, so the fix above cannot be "never restart". It has to
    // assert the same two things its sibling does, in the same currency: that
    // the effect RE-RAN (one more listJobs call) and that the new id ends up
    // carrying its own entry. Asserting `byId.c.entry` is null right after the
    // rerender - which this test did - pins nothing at all: `byId` is derived
    // from `ids`, so c is present-and-empty whether the effect restarted or
    // never noticed, and no poll has resolved yet either way. It passed under
    // a hook that ignored the id change completely.
    const listJobs = vi.spyOn(api, 'listJobs').mockResolvedValue(
      plan({ queued: [entry({ id: 'a' }), entry({ id: 'c' })] }))
    const { result, rerender } = renderHook(
      ({ ids }) => useQueuedEntries(ids), { initialProps: { ids: ['a'] } })
    await act(async () => { await Promise.resolve() })
    expect(result.current.byId.a.entry?.id).toBe('a')
    const callsBefore = listJobs.mock.calls.length

    rerender({ ids: ['c'] })
    await act(async () => { await Promise.resolve() })

    expect(listJobs.mock.calls.length).toBeGreaterThan(callsBefore)
    expect(result.current.byId.c.entry?.id).toBe('c')
    expect(result.current.byId.a).toBeUndefined()   // and a is gone with it
  })
})
