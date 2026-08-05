import { describe, expect, it } from 'vitest'
import {
  fromPair,
  MIN_REMAINING_SECONDS,
  isPending,
  masterDuration,
  previewOffsets,
  remainingSeconds,
  trimActionLabel,
  trimNeedsAction,
  trimProblems,
} from './trim'

describe('fromPair', () => {
  it('keeps null as null', () => {
    expect(fromPair(null)).toBeNull()
  })

  it('converts a [head, tail] tuple to a Trim', () => {
    expect(fromPair([3, 2])).toEqual({ head: 3, tail: 2 })
  })
})

describe('remainingSeconds', () => {
  it('subtracts both ends', () => {
    expect(remainingSeconds(84, { head: 3, tail: 2 })).toBeCloseTo(79)
  })

  it('never goes below zero', () => {
    expect(remainingSeconds(10, { head: 9, tail: 9 })).toBe(0)
  })
})

describe('isPending', () => {
  it('is false when desired equals applied', () => {
    expect(isPending({ head: 3, tail: 2 }, { head: 3, tail: 2 })).toBe(false)
  })

  it('treats no trim and a zero trim as the same request', () => {
    expect(isPending({ head: 0, tail: 0 }, null)).toBe(false)
    expect(isPending(null, { head: 0, tail: 0 })).toBe(false)
  })

  it('is true once either end differs', () => {
    expect(isPending({ head: 3, tail: 2 }, { head: 3, tail: 1 })).toBe(true)
    expect(isPending({ head: 1, tail: 0 }, null)).toBe(true)
  })

  it('tolerates float noise from a JSON round trip', () => {
    expect(isPending({ head: 3.0000001, tail: 2 }, { head: 3, tail: 2 })).toBe(false)
  })
})

describe('trimProblems', () => {
  it('is empty for a trim that leaves enough', () => {
    expect(trimProblems(84, { head: 3, tail: 2 })).toEqual([])
  })

  it('flags a negative value', () => {
    expect(trimProblems(84, { head: -1, tail: 0 })).toHaveLength(1)
  })

  it('flags a trim that leaves less than the floor', () => {
    // Same floor the server enforces; the client says so before the 422.
    expect(trimProblems(10, { head: 4, tail: 4 })).toHaveLength(1)
    expect(MIN_REMAINING_SECONDS).toBe(3)
  })
})

describe('masterDuration', () => {
  it('adds back what is already applied', () => {
    // A 10s master cut head 2 / tail 3 leaves a 5s short.mp4 - reconstruct
    // the 10s master from that 5s file plus the 2+3 already removed.
    expect(masterDuration(5, { head: 2, tail: 3 })).toBe(10)
  })

  it('is the short duration itself when nothing is applied', () => {
    expect(masterDuration(10, null)).toBe(10)
    expect(masterDuration(10, { head: 0, tail: 0 })).toBe(10)
  })

  it('is what the real-browser bug measured: 5.04s + 2 + 3 = ~10.04s, not 5.04s', () => {
    // The regression this whole module exists to fix: before it, the UI
    // compared trimHead/trimTail (absolute cuts from the master) straight
    // against the ALREADY-CUT short's own duration, so "0:00 after the
    // cut" was shown for a healthy clip.
    expect(masterDuration(5.04, { head: 2, tail: 3 })).toBeCloseTo(10.04)
  })
})

describe('previewOffsets', () => {
  it('is the identity when nothing is applied yet', () => {
    expect(previewOffsets({ head: 2, tail: 3 }, null)).toEqual({ head: 2, tail: 3 })
  })

  it('subtracts what the loaded file has already had cut', () => {
    // Staging head 3 against a file that already lost 2 seconds off the
    // head only needs to seek 1 second further into what remains.
    expect(previewOffsets({ head: 3, tail: 3 }, { head: 2, tail: 3 })).toEqual({
      head: 1,
      tail: 0,
    })
  })

  it('goes negative when staging less of a cut than is already applied', () => {
    // Undoing part of a previous trim: that content is not back in
    // short.mp4 yet, so there is nothing to seek past on that end - the
    // caller clamps this at its own seek boundary, not this function.
    expect(previewOffsets({ head: 1, tail: 0 }, { head: 2, tail: 3 })).toEqual({
      head: -1,
      tail: -3,
    })
  })
})

describe('trimNeedsAction', () => {
  it('is false when nothing is pending and the state is known', () => {
    expect(trimNeedsAction(false, false)).toBe(false)
  })

  it('is true when a trim is pending, known or not', () => {
    expect(trimNeedsAction(true, false)).toBe(true)
    expect(trimNeedsAction(true, true)).toBe(true)
  })

  it('is true when the state is unknown even though nothing looks pending', () => {
    // THE BLOCKER: desired (null) and last-known-applied (null) agree, so
    // isPending alone reads false here - trim_unknown is what still forces
    // the repair path to be reachable, e.g. from ClipEditor's Apply button
    // and ManualUploadPanel/UploadPanel's delivery guards.
    expect(trimNeedsAction(false, true)).toBe(true)
  })
})

describe('trimActionLabel', () => {
  it('is "Apply trim" whenever a trim is genuinely pending', () => {
    expect(trimActionLabel(true, false)).toBe('Apply trim')
    expect(trimActionLabel(true, true)).toBe('Apply trim')
  })

  it('is "Repair trim" when nothing is pending but the state is unknown', () => {
    expect(trimActionLabel(false, true)).toBe('Repair trim')
  })

  it('is "Trim applied" only when both are false', () => {
    expect(trimActionLabel(false, false)).toBe('Trim applied')
  })
})

describe('the trim-journey regression this module fixes', () => {
  it('a larger second trim is offered, not blocked, once the first is applied', () => {
    // The branch's own motivating scenario: "trim 3s, then change your
    // mind and trim 5s" must be expressible even though the player has
    // already loaded the 3s-shorter file. Before this module existed,
    // remainingSeconds/trimProblems were checked against the raw
    // (already-cut) short duration and this larger trim read as leaving
    // less than the floor - it does not, against the reconstructed master.
    const appliedTrim = { head: 2, tail: 3 }
    const shortDuration = 5.04 // a 10.04s master, already cut by appliedTrim
    const duration = masterDuration(shortDuration, appliedTrim)
    const desired = { head: 3, tail: 3 }
    expect(remainingSeconds(duration, desired)).toBeCloseTo(4.04)
    expect(trimProblems(duration, desired)).toEqual([])
    expect(isPending(desired, appliedTrim)).toBe(true)
  })
})
