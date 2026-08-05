/**
 * Pure list logic for the hit list: sorting, filtering, category labels.
 *
 * Non-component for the same reason as streamTimeline.ts. Note the name: this
 * is about DETECTED moments, not the excitement lexicon that MomentsEditor.tsx
 * edits - two different things share the filename moments.json on disk, and
 * conflating them in a component name is how the next maintainer loses an hour.
 */

/** One detected moment (see moments.py's Moment / detect.py's writer) - the
 * shape GET …/streams/{video_id}/moments carries in its `moments` array.
 * This is also re-exported from api.ts (as the type its own StreamAnalysis
 * uses) rather than duplicated there - the two used to be two separate,
 * structurally-identical interfaces that could silently drift apart.
 * `hook_suggestion` is typed nullable even though the engines that WRITE it
 * (moment_scan.py, moments.py) always store a string: `get_stream_analysis`
 * returns a dict, not a Pydantic response model, so an on-disk moments.json
 * from an older version of this project, or one hand-edited, can legally
 * come back with a missing or null value - the type should not promise more
 * than the route actually validates. */
export interface Moment {
  start: number
  end: number
  category: string
  score: number
  reason: string
  hook_suggestion: string | null
}

export type SortKey = 'score' | 'time'

/** The channel's own order of importance, from the design. */
export const CATEGORY_ORDER = [
  'start_finish', 'incident', 'highlight', 'race_control', 'reaction',
] as const

export const CATEGORY_LABELS: Record<string, string> = {
  start_finish: 'Start / finish',
  incident: 'Incident',
  highlight: 'Highlight',
  race_control: 'Race control',
  reaction: 'Reaction',
}

export function categoryLabel(category: string): string {
  // An unknown category is still a real detection - showing the raw value beats
  // showing nothing, and beats silently dropping the row.
  return CATEGORY_LABELS[category] ?? category
}

export function sortMoments(moments: Moment[], key: SortKey): Moment[] {
  // A copy, never in place: the caller holds this array in state, and sorting
  // it under React would mutate state without a re-render.
  const copy = [...moments]
  if (key === 'time') return copy.sort((a, b) => a.start - b.start)
  // Time breaks a score tie, so equal-scoring rows keep a stable, meaningful
  // order instead of whatever the engine happened to emit.
  return copy.sort((a, b) => b.score - a.score || a.start - b.start)
}

export function filterMoments(moments: Moment[], categories: Set<string>): Moment[] {
  // An empty selection means "no filter", not "nothing matches": unticking every
  // box must not look like a stream with nothing in it.
  if (categories.size === 0) return moments
  return moments.filter((moment) => categories.has(moment.category))
}
