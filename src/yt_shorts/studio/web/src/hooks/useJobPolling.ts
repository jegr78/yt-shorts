import { useEffect, useState } from 'react'
import { ApiError, getJob, type Job } from '../api'

const POLL_INTERVAL_MS = 750
// After this many consecutive transient errors, stop retrying and surface a
// terminal failure rather than polling forever behind the operator's back.
const MAX_CONSECUTIVE_ERRORS = 5

/**
 * Polls GET /api/jobs/{id} (see api.py, studio/jobs.py) until the job
 * leaves "running". Starting a new job id resets the polling; no job id
 * means nothing runs.
 *
 * Terminal errors do NOT loop forever: a 404 (the job was pruned, or the studio
 * restarted and lost its in-memory jobs - see studio/jobs.py) and a run of
 * consecutive transient errors both resolve to a synthetic "failed" job, so the
 * consumer's status effects fire and the UI leaves its "running" state instead
 * of being wedged on a job that will never resolve.
 */
export function useJobPolling(jobId: string | null) {
  const [job, setJob] = useState<Job | null>(null)

  useEffect(() => {
    if (!jobId) {
      setJob(null)
      return
    }
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let failures = 0

    function fail(message: string) {
      if (!cancelled) setJob({ id: jobId as string, status: 'failed', results: {}, log: [message] })
    }

    async function poll() {
      try {
        const result = await getJob(jobId as string)
        if (cancelled) return
        failures = 0
        setJob(result)
        if (result.status === 'running') {
          timer = setTimeout(poll, POLL_INTERVAL_MS)
        }
      } catch (error) {
        if (cancelled) return
        if (error instanceof ApiError && error.status === 404) {
          fail('the job is no longer available (the studio may have restarted)')
          return
        }
        failures += 1
        if (failures >= MAX_CONSECUTIVE_ERRORS) {
          fail('lost contact with the studio while tracking this job')
          return
        }
        // Transient error: retry, but never faster than the normal interval.
        timer = setTimeout(poll, POLL_INTERVAL_MS)
      }
    }
    poll()

    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [jobId])

  return job
}
