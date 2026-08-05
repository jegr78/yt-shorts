import { describe, expect, it } from 'vitest'
import { categoryLabel, filterMoments, sortMoments } from './momentList'

const m = (start: number, score: number, category = 'incident') => ({
  start, end: start + 20, category, score, reason: 'x', hook_suggestion: '',
})

describe('sortMoments', () => {
  it('orders by score, strongest first', () => {
    const sorted = sortMoments([m(10, 3), m(20, 9), m(30, 6)], 'score')
    expect(sorted.map((x) => x.score)).toEqual([9, 6, 3])
  })

  it('orders by time, earliest first', () => {
    const sorted = sortMoments([m(30, 6), m(10, 3), m(20, 9)], 'time')
    expect(sorted.map((x) => x.start)).toEqual([10, 20, 30])
  })

  it('does not mutate its input', () => {
    const input = [m(30, 6), m(10, 3)]
    sortMoments(input, 'time')
    expect(input[0].start).toBe(30)
  })

  it('breaks a score tie by time so the order is stable to look at', () => {
    const sorted = sortMoments([m(30, 5), m(10, 5)], 'score')
    expect(sorted.map((x) => x.start)).toEqual([10, 30])
  })
})

describe('filterMoments', () => {
  it('keeps only the selected categories', () => {
    const list = [m(10, 5, 'incident'), m(20, 5, 'reaction')]
    expect(filterMoments(list, new Set(['incident']))).toHaveLength(1)
  })

  it('an empty selection means no filter, not no results', () => {
    // Unticking every box must not look like "the stream has nothing in it".
    const list = [m(10, 5, 'incident'), m(20, 5, 'reaction')]
    expect(filterMoments(list, new Set())).toHaveLength(2)
  })
})

describe('categoryLabel', () => {
  it('renders the five known categories readably', () => {
    expect(categoryLabel('start_finish')).toBe('Start / finish')
    expect(categoryLabel('race_control')).toBe('Race control')
  })

  it('passes an unknown category through rather than hiding it', () => {
    // A category this client does not know about is still a real detection;
    // showing the raw value beats showing nothing.
    expect(categoryLabel('weather')).toBe('weather')
  })
})
