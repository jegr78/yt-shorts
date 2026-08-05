import { renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import type { JobEntry, JobPlan } from '../jobs'
import { useQueuedJob } from './useQueuedJob'

/**
 * The hook's own rules, without rendering a panel.
 *
 * Its sibling `useJobPolling` has had this harness since it was written;
 * this one carried the harder parts - the `seen` race guard, the terminal
 * stop, the error budget, the reset when the tracked id changes, and the
 * wedge all four of those can end in - covered only indirectly through E2E.
 * Ten lines of `renderHook` plus fake timers is what proved the wedge, so
 * the rules live here now rather than only in a browser.
 */

function entry(overrides: Partial<JobEntry> = {}): JobEntry {
  return {
    id: 'e1',
    kind: 'render',
    state: 'queued',
    params: {},
    reason: null,
    progress: null,
    created_at: 0,
    after: null,
    job_id: null,
    position: 0,
    pool: null,
    stop_point: 'the end of the current clip',
    hard_stop_allowed: false,
    stoppable: true,
    ...overrides,
  }
}

function plan(overrides: Partial<JobPlan> = {}): JobPlan {
  return {
    running: [],
    queued: [],
    finished: [],
    limits: {},
    worker_running: true,
    load_error: null,
    ...overrides,
  }
}

describe('useQueuedJob', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    // The inner useJobPolling must never reach for a job: every entry below
    // has job_id null, and a stray call would mean this hook built a job id
    // out of the entry's own (two disjoint id spaces - see jobs.ts).
    vi.spyOn(api, 'getJob').mockRejectedValue(new api.ApiError(404, 'no such job'))
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('does nothing and reports idle for a null id', () => {
    const spy = vi.spyOn(api, 'listJobs')
    const { result } = renderHook(() => useQueuedJob(null))
    expect(result.current.pending).toBe(false)
    expect(result.current.entry).toBeNull()
    expect(spy).not.toHaveBeenCalled()
  })

  it('follows a queued entry and reports it pending, with the reason it waits', async () => {
    vi.spyOn(api, 'listJobs').mockResolvedValue(
      plan({ queued: [entry({ reason: 'another job holds this event lock' })] }))
    const { result } = renderHook(() => useQueuedJob('e1'))

    await vi.waitFor(() => expect(result.current.pending).toBe(true))
    expect(result.current.running).toBe(false)
    expect(result.current.outcome).toBeNull()
    expect(result.current.waiting).toBe('another job holds this event lock')
    expect(result.current.error).toBeNull()
  })

  it('stops polling once the entry is terminal', async () => {
    const spy = vi.spyOn(api, 'listJobs')
      .mockResolvedValueOnce(plan({ running: [entry({ state: 'running' })] }))
      .mockResolvedValue(plan({ finished: [entry({ state: 'done' })] }))
    const { result } = renderHook(() => useQueuedJob('e1'))

    await vi.waitFor(() => expect(result.current.running).toBe(true))
    await vi.advanceTimersByTimeAsync(750)
    await vi.waitFor(() => expect(result.current.outcome).toBe('done'))

    const callsAtDone = spy.mock.calls.length
    await vi.advanceTimersByTimeAsync(3000)
    expect(spy.mock.calls.length).toBe(callsAtDone)
    expect(result.current.pending).toBe(false)
    expect(result.current.running).toBe(false)
  })

  it('keeps polling for an entry it has not seen yet - the enqueue may have raced it', async () => {
    // A plan read that overtook the enqueue's own write answers with no such
    // row. Giving up there would abandon an entry that is about to appear.
    const spy = vi.spyOn(api, 'listJobs')
      .mockResolvedValueOnce(plan())
      .mockResolvedValueOnce(plan())
      .mockResolvedValue(plan({ queued: [entry()] }))
    const { result } = renderHook(() => useQueuedJob('e1'))

    await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(1))
    // Absent before it was ever seen is NOT "gone": no error, no wedge, and
    // the panel is not left claiming anything.
    expect(result.current.error).toBeNull()
    expect(result.current.pending).toBe(false)

    await vi.advanceTimersByTimeAsync(750)
    await vi.advanceTimersByTimeAsync(750)
    await vi.waitFor(() => expect(result.current.pending).toBe(true))
    expect(result.current.entry?.id).toBe('e1')
  })

  it('an entry that disappears after being seen ends pending, and says it is gone', async () => {
    // The wedge this hook was fixed for, and it is reached by a supported
    // flow: `allowedActions` offers `remove` on a queued entry, so an
    // operator queueing a trim and then dropping it on the Jobs screen used
    // to leave the panel's controls disabled until a page reload.
    const spy = vi.spyOn(api, 'listJobs')
      .mockResolvedValueOnce(plan({ queued: [entry()] }))
      .mockResolvedValue(plan())
    const { result } = renderHook(() => useQueuedJob('e1'))

    await vi.waitFor(() => expect(result.current.pending).toBe(true))
    await vi.advanceTimersByTimeAsync(750)
    await vi.waitFor(() => expect(result.current.error).not.toBeNull())

    expect(result.current.pending).toBe(false)
    expect(result.current.running).toBe(false)
    // No badge left claiming "queued" for a row that does not exist.
    expect(result.current.entry).toBeNull()
    // And no "queued - not started yet" note contradicting the message.
    expect(result.current.waiting).toBeNull()
    expect(result.current.error).toMatch(/no longer in the plan/i)

    // Stopped, rather than polling for the life of the screen.
    const callsAtGone = spy.mock.calls.length
    await vi.advanceTimersByTimeAsync(3000)
    expect(spy.mock.calls.length).toBe(callsAtGone)
  })

  it('gives up after a run of read failures, and ends pending rather than wedging', async () => {
    const spy = vi.spyOn(api, 'listJobs')
      .mockResolvedValueOnce(plan({ queued: [entry()] }))
      .mockRejectedValue(new api.ApiError(503, 'the queue is unavailable'))
    const { result } = renderHook(() => useQueuedJob('e1'))

    await vi.waitFor(() => expect(result.current.pending).toBe(true))
    for (let i = 0; i < 7; i++) await vi.advanceTimersByTimeAsync(750)
    await vi.waitFor(() => expect(result.current.error).not.toBeNull())

    expect(result.current.pending).toBe(false)
    expect(result.current.running).toBe(false)
    // A read failure says nothing about whether the entry is still there, so
    // the last row seen is KEPT and the message must not call it gone.
    expect(result.current.entry?.id).toBe('e1')
    expect(result.current.error).toMatch(/lost contact/i)
    expect(result.current.error).toMatch(/the queue is unavailable/)
    expect(result.current.error).not.toMatch(/no longer in the plan/i)

    const callsAtGiveUp = spy.mock.calls.length
    await vi.advanceTimersByTimeAsync(3000)
    expect(spy.mock.calls.length).toBe(callsAtGiveUp)
  })

  it('retries a single read failure at the interval instead of giving up', async () => {
    const spy = vi.spyOn(api, 'listJobs')
      .mockRejectedValueOnce(new Error('network'))
      .mockResolvedValue(plan({ queued: [entry()] }))
    const { result } = renderHook(() => useQueuedJob('e1'))

    await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(1))
    expect(result.current.error).toBeNull()
    await vi.advanceTimersByTimeAsync(750)
    await vi.waitFor(() => expect(result.current.pending).toBe(true))
  })

  it('starts over when the tracked entry id changes', async () => {
    // Including the `seen` guard: the second id has never been seen, so a
    // plan that lacks it must be retried rather than reported gone. A guard
    // left over from the first id would report the new one gone at once.
    const spy = vi.spyOn(api, 'listJobs')
      .mockResolvedValueOnce(plan({ queued: [entry({ id: 'a' })] }))
      .mockResolvedValueOnce(plan({ queued: [entry({ id: 'a' })] }))
      .mockResolvedValue(plan({ queued: [entry({ id: 'b', state: 'paused' })] }))
    const { result, rerender } = renderHook(({ id }) => useQueuedJob(id), {
      initialProps: { id: 'a' as string | null },
    })

    await vi.waitFor(() => expect(result.current.entry?.id).toBe('a'))
    rerender({ id: 'b' })
    // The previous entry is dropped immediately, not carried across.
    expect(result.current.entry).toBeNull()
    expect(result.current.error).toBeNull()

    await vi.waitFor(() => expect(result.current.entry?.id).toBe('b'))
    expect(result.current.pending).toBe(true)
    expect(result.current.waiting).toMatch(/paused/i)
    expect(spy.mock.calls.length).toBeGreaterThan(0)
  })

  it('clearing the id returns the hook to idle', async () => {
    vi.spyOn(api, 'listJobs').mockResolvedValue(plan({ queued: [entry()] }))
    const { result, rerender } = renderHook(({ id }) => useQueuedJob(id), {
      initialProps: { id: 'e1' as string | null },
    })
    await vi.waitFor(() => expect(result.current.pending).toBe(true))
    rerender({ id: null })
    expect(result.current.pending).toBe(false)
    expect(result.current.entry).toBeNull()
    expect(result.current.error).toBeNull()
  })
})
