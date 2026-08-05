/**
 * What the stream list shows, and what a bulk action will actually do.
 *
 * Pure, and NOT exported from a component file, for the reason every other
 * module beside it exists (see words.ts, jobs.ts, streamTimeline.ts): Vite's
 * fast-refresh boundary stays component-only and these rules are unit-tested
 * without rendering anything.
 *
 * `bulkPlan` is the one that matters. It decides what gets queued for a
 * selection of streams, and each of the two things it skips by default is
 * NOT expensive by nature - a re-transcription of a stream that already has
 * one ordinarily reuses its already-downloaded audio (`stream_transcribe.
 * ytdlp_downloader` probes the existing file with ffprobe and skips the
 * download when its duration still matches) and every chunk that decoded
 * cleanly then comes from the cache (a chunk that failed is never cached and
 * is retried every run), and a re-detection of one that already has a
 * MODEL-backed analysis ordinarily costs nothing at the model provider,
 * because a scored window is cached too (`detect.WindowCache`) - an analysis
 * produced by the lexicon fallback instead caches no windows at all, so the
 * first model-backed detect over it pays for the whole stream. Both skips
 * exist for the case where reuse is NOT available - a workspace whose audio
 * or cached chunks were cleared really does re-download and re-decode (hours
 * for an 8-hour race), and a changed provider, model, marker set or
 * re-decoded transcript really does spend money again - and neither is a
 * good gamble to take on one click over thirteen ticked rows. So both are
 * skipped unless the operator says otherwise, and the bar says so BEFORE the
 * click.
 */
import type { StreamCatalogue, StreamVideo } from './api'

/** The filter's two synthetic selections. Neither can collide with a real
 * playlist id, which is letters, digits, '-' and '_' only. */
export const ALL_STREAMS = '*all*'
export const NO_PLAYLIST = '*none*'

export interface PlaylistOption {
  value: string
  /** The complete, ready-to-render option text - the count, and for a
   * playlist with unavailable members its unavailable count too, are
   * already folded in. A caller displays this as-is; it must not append a
   * count of its own on top of it. */
  label: string
  count: number
}

/** One playlist's option label: "<title> (<count>)", or, when it dropped
 * unavailable members, "<title> (<count> + <unavailable> unavailable)" - so
 * a displayed "(6)" is not silently a 6 that came from 8 because of a
 * deleted or private member. Shared by every row `playlistOptions` builds,
 * not just playlists, so "All streams" and "In no playlist" get the
 * identical "(<count>)" shape for free. */
function withCount(title: string, count: number, unavailable = 0): string {
  return unavailable > 0
    ? `${title} (${count} + ${unavailable} unavailable)`
    : `${title} (${count})`
}

/**
 * The filter's rows: everything, then each playlist, then the leftovers.
 *
 * "All streams" counts the UNION the catalogue holds, not the Streams tab's
 * own total - that difference (99 against 91 on ERF) is the entire reason
 * playlist contents are shown rather than the Streams tab filtered, and a
 * count that disagreed with its own list would hide it.
 *
 * A playlist's `unavailable` (a deleted or private member yt-dlp reported
 * with no title, dropped from `count`) is folded into the label by
 * `withCount` whenever it is greater than zero, because nothing else in
 * this screen reads that field - `count` alone would silently hide that
 * kind of drop, which is exactly what this project keeps refusing to ship.
 * A playlist with no unavailable members gets the plain "<title> (<count>)"
 * label, unchanged from before. `list_playlist_videos` also drops an
 * unparseable JSON line and an entry with no `id` at all, but WITHOUT
 * counting them - those are yt-dlp output defects rather than a
 * YouTube-reported unavailability, so this label cannot and does not speak
 * to them.
 *
 * The leftovers row is omitted when it would be empty rather than shown as
 * "(0)": on ERF every stream is in a playlist, so an always-present empty
 * row would be the ordinary case and would read as a fault.
 */
export function playlistOptions(catalogue: StreamCatalogue): PlaylistOption[] {
  const options: PlaylistOption[] = [
    {
      value: ALL_STREAMS,
      label: withCount('All streams', catalogue.videos.length),
      count: catalogue.videos.length,
    },
  ]
  for (const playlist of catalogue.playlists) {
    options.push({
      value: playlist.id,
      label: withCount(playlist.title, playlist.count, playlist.unavailable),
      count: playlist.count,
    })
  }
  const loose = catalogue.videos.filter((video) => video.playlist_ids.length === 0).length
  if (loose > 0) {
    options.push({ value: NO_PLAYLIST, label: withCount('In no playlist', loose), count: loose })
  }
  return options
}

/** The videos the current filter shows, in catalogue order. */
export function visibleVideos(
  catalogue: StreamCatalogue, playlistId: string,
): StreamVideo[] {
  if (playlistId === ALL_STREAMS) return catalogue.videos
  if (playlistId === NO_PLAYLIST) {
    return catalogue.videos.filter((video) => video.playlist_ids.length === 0)
  }
  return catalogue.videos.filter((video) => video.playlist_ids.includes(playlistId))
}

export type BulkAction = 'transcribe' | 'detect' | 'both'

/** One video's share of a bulk action. `transcribe` and `detect` are what
 * will actually be enqueued for it - a step with `transcribe: false,
 * detect: true` is a stream whose transcript already exists, so its detect
 * has nothing to wait for and is enqueued with no `after`. */
export interface BulkStep {
  videoId: string
  transcribe: boolean
  detect: boolean
}

/**
 * The four lists answer different questions and are NOT disjoint - summing
 * their lengths to count "how many videos are affected" double-counts.
 * Under `'both'`, a single video can appear in `steps` (its other leg was
 * queued) AND in one of the two skip lists (this leg was not) at the same
 * time - see `BulkStep`'s own doc comment for that exact case. And
 * `skippedEntirely` is not a fourth, disjoint bucket: it is the subset of
 * `skippedTranscribe`/`skippedDetect` whose EVERY requested leg was skipped,
 * so a video listed there also appears in one or both of those two lists. A
 * caller that wants a distinct-video count must dedupe by video id across
 * all of them, not sum lengths.
 */
export interface BulkPlan {
  steps: BulkStep[]
  skippedTranscribe: string[]
  skippedDetect: string[]
  /** Videos this action would do nothing at all for. */
  skippedEntirely: string[]
  /** The sentence the bar shows before the click, or null when nothing is
   * skipped. */
  note: string | null
}

/**
 * What a bulk action will really queue.
 *
 * Steps follow CATALOGUE order, not the order rows were ticked: the plan an
 * operator reads on the Jobs screen should match the list they were looking
 * at. The caller enqueues them in this order, sequentially - parallel
 * requests would scramble the queue's own order.
 */
export function bulkPlan(
  selectedIds: string[],
  videos: StreamVideo[],
  action: BulkAction,
  force: { transcribe: boolean; detect: boolean },
): BulkPlan {
  const selected = new Set(selectedIds)
  const steps: BulkStep[] = []
  const skippedTranscribe: string[] = []
  const skippedDetect: string[] = []
  const skippedEntirely: string[] = []

  for (const video of videos) {
    if (!selected.has(video.video_id)) continue
    const wantsTranscribe = action === 'transcribe' || action === 'both'
    const wantsDetect = action === 'detect' || action === 'both'
    let transcribe = false
    let detect = false

    if (wantsTranscribe) {
      if (video.has_transcript && !force.transcribe) skippedTranscribe.push(video.video_id)
      else transcribe = true
    }
    if (wantsDetect) {
      if (video.has_analysis && !force.detect) skippedDetect.push(video.video_id)
      else detect = true
    }

    if (transcribe || detect) steps.push({ videoId: video.video_id, transcribe, detect })
    else skippedEntirely.push(video.video_id)
  }

  // The note names the LEG that was skipped, never the video: under 'both' a
  // video can have one leg skipped while the other is queued (see
  // `BulkStep`'s doc comment), and a sentence about the video itself would
  // then claim it was skipped while a step for it sits in `steps` - exactly
  // the disagreement this module's own docstring says is worse than saying
  // nothing.
  const parts: string[] = []
  if (skippedTranscribe.length > 0) {
    parts.push(`${skippedTranscribe.length} ` +
      `${skippedTranscribe.length === 1 ? 'transcription' : 'transcriptions'} skipped: ` +
      `already transcribed.`)
  }
  if (skippedDetect.length > 0) {
    parts.push(`${skippedDetect.length} ` +
      `${skippedDetect.length === 1 ? 'detection' : 'detections'} skipped: ` +
      `already analysed.`)
  }
  return {
    steps,
    skippedTranscribe,
    skippedDetect,
    skippedEntirely,
    note: parts.length > 0 ? parts.join(' ') : null,
  }
}

/**
 * How much of the selection is off screen, or null when none of it is.
 *
 * Selection is by video id and deliberately SURVIVES a filter change - a
 * race weekend split across two playlists is an ordinary case - so the bar
 * has to say when the count it shows reaches past what is visible. Without
 * this, "6 selected" over a view holding four rows reads as a bug.
 */
export function selectionNote(
  selectedIds: string[], visibleIds: string[],
): string | null {
  const visible = new Set(visibleIds)
  const hidden = selectedIds.filter((id) => !visible.has(id)).length
  return hidden > 0 ? `${hidden} not in this view` : null
}

/** The queue entries a bulk action created for one video. Declared here
 * rather than in a component, so no component file exports a type and
 * Vite's fast-refresh boundary stays component-only. */
export interface StreamEntryIds {
  transcribe?: string
  detect?: string
}

/** The key one (video, kind) LEG is tracked under - by `queueingLegs` in
 * App.tsx and by the busy-leg lookups in StreamPanel.tsx. Written once and
 * exported so the two places that need it cannot drift apart on the format;
 * it used to be written twice (a function in App.tsx, two inline template
 * literals in StreamPanel.tsx), and the two only had to agree by
 * convention. */
export function legKey(videoId: string, kind: 'transcribe' | 'detect'): string {
  return `${videoId}:${kind}`
}
