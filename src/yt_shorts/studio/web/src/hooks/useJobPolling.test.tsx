import { renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import type { Job } from '../api'
import { useJobPolling } from './useJobPolling'

const running: Job = { id: 'j', status: 'running', results: {}, log: [] }
const done: Job = { id: 'j', status: 'done', results: {}, log: [] }

describe('useJobPolling', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('does nothing and returns null for a null id', () => {
    const spy = vi.spyOn(api, 'getJob')
    const { result } = renderHook(() => useJobPolling(null))
    expect(result.current).toBeNull()
    expect(spy).not.toHaveBeenCalled()
  })

  it('polls until the job leaves running, then stops', async () => {
    const spy = vi
      .spyOn(api, 'getJob')
      .mockResolvedValueOnce(running)
      .mockResolvedValueOnce(done)
    const { result } = renderHook(() => useJobPolling('j'))

    // first poll -> running, schedules another at the interval
    await vi.waitFor(() => expect(result.current?.status).toBe('running'))
    await vi.advanceTimersByTimeAsync(750)
    await vi.waitFor(() => expect(result.current?.status).toBe('done'))

    // no further polling once done
    const callsAtDone = spy.mock.calls.length
    await vi.advanceTimersByTimeAsync(3000)
    expect(spy.mock.calls.length).toBe(callsAtDone)
  })

  it('retries a transient error at the interval, not in a tight loop', async () => {
    const spy = vi
      .spyOn(api, 'getJob')
      .mockRejectedValueOnce(new Error('network'))
      .mockResolvedValue(done)
    renderHook(() => useJobPolling('j'))

    await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(1))
    // must not have retried synchronously
    expect(spy).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(750)
    await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(2))
  })

  it('treats a 404 as terminal: stops polling and surfaces a failed job', async () => {
    const spy = vi
      .spyOn(api, 'getJob')
      .mockRejectedValue(new api.ApiError(404, 'no such job'))
    const { result } = renderHook(() => useJobPolling('j'))

    await vi.waitFor(() => expect(result.current?.status).toBe('failed'))
    const callsAtFail = spy.mock.calls.length
    await vi.advanceTimersByTimeAsync(3000)
    // no further polling after the terminal 404
    expect(spy.mock.calls.length).toBe(callsAtFail)
  })

  it('gives up after a run of transient errors instead of looping forever', async () => {
    const spy = vi.spyOn(api, 'getJob').mockRejectedValue(new Error('network'))
    const { result } = renderHook(() => useJobPolling('j'))

    // drive several retry intervals
    for (let i = 0; i < 6; i++) await vi.advanceTimersByTimeAsync(750)
    await vi.waitFor(() => expect(result.current?.status).toBe('failed'))
    const callsAtFail = spy.mock.calls.length
    await vi.advanceTimersByTimeAsync(3000)
    expect(spy.mock.calls.length).toBe(callsAtFail)
  })

  it('re-polls when the job id changes', async () => {
    const spy = vi.spyOn(api, 'getJob').mockResolvedValue(done)
    const { rerender } = renderHook(({ id }) => useJobPolling(id), {
      initialProps: { id: 'a' as string | null },
    })
    await vi.waitFor(() => expect(spy).toHaveBeenCalledWith('a'))
    rerender({ id: 'b' })
    await vi.waitFor(() => expect(spy).toHaveBeenCalledWith('b'))
  })
})
