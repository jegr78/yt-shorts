import { describe, expect, it } from 'vitest'
import type { StreamCatalogue, StreamVideo } from './api'
import {
  ALL_STREAMS, NO_PLAYLIST, bulkPlan, playlistOptions, selectionNote,
  visibleVideos,
} from './streams'

function video(overrides: Partial<StreamVideo> = {}): StreamVideo {
  return {
    video_id: 'v1',
    title: 'Race Part 1',
    duration_seconds: 29975,
    view_count: 2200,
    playlist_ids: [],
    has_transcript: false,
    has_analysis: false,
    ...overrides,
  }
}

function catalogue(overrides: Partial<StreamCatalogue> = {}): StreamCatalogue {
  return {
    videos: [
      video({ video_id: 'a', playlist_ids: ['PL1'] }),
      video({ video_id: 'b', playlist_ids: ['PL1'] }),
      video({ video_id: 'c', playlist_ids: [] }),
    ],
    playlists: [{ id: 'PL1', title: '2026 Season', count: 2, unavailable: 0 }],
    failed_playlists: [],
    ...overrides,
  }
}

describe('playlistOptions', () => {
  it('counts "all" from the union, not from any one playlist', () => {
    // The whole reason playlist contents are shown rather than the Streams
    // tab filtered: the union is bigger. A count that disagreed with the
    // list it labels would hide exactly that.
    const [all] = playlistOptions(catalogue())
    expect(all).toEqual({ value: ALL_STREAMS, label: 'All streams (3)', count: 3 })
  })

  it('offers each playlist with its own size, folded into the label', () => {
    const options = playlistOptions(catalogue())
    expect(options[1]).toEqual({ value: 'PL1', label: '2026 Season (2)', count: 2 })
  })

  it('leaves the label as plain "(<count>)" when nothing is unavailable', () => {
    const withoutUnavailable = catalogue({
      playlists: [{ id: 'PL1', title: '2026 Season', count: 2, unavailable: 0 }],
    })
    const options = playlistOptions(withoutUnavailable)
    expect(options[1].label).toBe('2026 Season (2)')
  })

  it('folds a playlist\'s unavailable members into its label', () => {
    // A displayed "(6)" must never be silently a 6 that came from 8 -
    // nothing else in the Streams tab reads `unavailable`, so the label is
    // the only place this can surface at all.
    const withUnavailable = catalogue({
      playlists: [{ id: 'PL2', title: 'ERF Specials', count: 1, unavailable: 2 }],
    })
    const options = playlistOptions(withUnavailable)
    expect(options[1]).toEqual({
      value: 'PL2', label: 'ERF Specials (1 + 2 unavailable)', count: 1,
    })
  })

  it('offers the leftovers bucket when something is in no playlist', () => {
    const options = playlistOptions(catalogue())
    expect(options[options.length - 1]).toEqual({
      value: NO_PLAYLIST, label: 'In no playlist (1)', count: 1,
    })
  })

  it('omits the leftovers bucket entirely when it would be empty', () => {
    // Measured on ERF: every one of its 91 streams is in a playlist, so
    // this is the ordinary case. An always-present empty row reads as a
    // fault in the catalogue.
    const every = catalogue({
      videos: [video({ video_id: 'a', playlist_ids: ['PL1'] })],
    })
    expect(playlistOptions(every).map((o) => o.value)).toEqual([ALL_STREAMS, 'PL1'])
  })
})

describe('visibleVideos', () => {
  it('shows everything for the "all" selection', () => {
    expect(visibleVideos(catalogue(), ALL_STREAMS).map((v) => v.video_id))
      .toEqual(['a', 'b', 'c'])
  })

  it('shows a playlist\'s own members', () => {
    expect(visibleVideos(catalogue(), 'PL1').map((v) => v.video_id))
      .toEqual(['a', 'b'])
  })

  it('shows what is in no playlist at all', () => {
    expect(visibleVideos(catalogue(), NO_PLAYLIST).map((v) => v.video_id))
      .toEqual(['c'])
  })
})

describe('bulkPlan', () => {
  const videos = [
    video({ video_id: 'a' }),
    video({ video_id: 'b', has_transcript: true }),
    video({ video_id: 'c', has_transcript: true, has_analysis: true }),
  ]
  const nothingForced = { transcribe: false, detect: false }

  it('follows list order, not the order rows were ticked', () => {
    const plan = bulkPlan(['c', 'a'], videos, 'transcribe', { transcribe: true, detect: false })
    expect(plan.steps.map((s) => s.videoId)).toEqual(['a', 'c'])
  })

  it('skips a stream that already has a transcript', () => {
    const plan = bulkPlan(['a', 'b'], videos, 'transcribe', nothingForced)
    expect(plan.steps).toEqual([{ videoId: 'a', transcribe: true, detect: false }])
    expect(plan.skippedTranscribe).toEqual(['b'])
  })

  it('re-transcribes when the operator forces it', () => {
    const plan = bulkPlan(['a', 'b'], videos, 'transcribe', { transcribe: true, detect: false })
    expect(plan.steps.map((s) => s.videoId)).toEqual(['a', 'b'])
    expect(plan.skippedTranscribe).toEqual([])
  })

  it('skips a stream that already has an analysis', () => {
    // Symmetric with the transcript rule, and not out of tidiness: a
    // re-detection spends real money at the provider, so thirteen of them
    // must not be the default reading of one click.
    const plan = bulkPlan(['a', 'c'], videos, 'detect', nothingForced)
    expect(plan.steps.map((s) => s.videoId)).toEqual(['a'])
    expect(plan.skippedDetect).toEqual(['c'])
  })

  it('chains both, so the detect can name its transcribe', () => {
    const plan = bulkPlan(['a'], videos, 'both', nothingForced)
    expect(plan.steps).toEqual([{ videoId: 'a', transcribe: true, detect: true }])
  })

  it('queues the detect alone when the transcript is already there', () => {
    // Nothing to wait for: the caller enqueues this one with no `after`.
    const plan = bulkPlan(['b'], videos, 'both', nothingForced)
    expect(plan.steps).toEqual([{ videoId: 'b', transcribe: false, detect: true }])
  })

  it('contributes nothing for a stream that has both already', () => {
    const plan = bulkPlan(['c'], videos, 'both', nothingForced)
    expect(plan.steps).toEqual([])
    expect(plan.skippedEntirely).toEqual(['c'])
  })

  it('says what will happen before the click', () => {
    const plan = bulkPlan(['a', 'b'], videos, 'transcribe', nothingForced)
    expect(plan.note).toBe('1 transcription skipped: already transcribed.')
  })

  it('says nothing when nothing is skipped', () => {
    const plan = bulkPlan(['a'], videos, 'transcribe', nothingForced)
    expect(plan.note).toBeNull()
  })

  it('is empty, and says so, when everything is skipped', () => {
    // The bar disables the button on this and shows the reason. A click
    // that silently does nothing is the same lying control as a spinner
    // that never moves.
    const plan = bulkPlan(['b'], videos, 'transcribe', nothingForced)
    expect(plan.steps).toEqual([])
    expect(plan.note).toBe('1 transcription skipped: already transcribed.')
  })

  it('ignores a selected id the catalogue no longer holds', () => {
    // A refresh can drop a video while its row is ticked.
    const plan = bulkPlan(['a', 'ghost'], videos, 'transcribe', nothingForced)
    expect(plan.steps.map((s) => s.videoId)).toEqual(['a'])
  })

  it('pluralizes the note over more than one skipped transcription', () => {
    // A hard-coded singular would pass every other test in this file - this
    // is the one that would catch it.
    const many = [
      video({ video_id: 'a', has_transcript: true }),
      video({ video_id: 'b', has_transcript: true }),
    ]
    const plan = bulkPlan(['a', 'b'], many, 'transcribe', nothingForced)
    expect(plan.note).toBe('2 transcriptions skipped: already transcribed.')
  })

  it('names the leg skipped, not the video, when only one leg is queued', () => {
    // Finding 1: video 'b' has a transcript but no analysis. Under 'both'
    // its detect IS queued - the note must not read as if the video itself
    // were skipped.
    const plan = bulkPlan(['b'], videos, 'both', nothingForced)
    expect(plan.steps).toEqual([{ videoId: 'b', transcribe: false, detect: true }])
    expect(plan.note).toBe('1 transcription skipped: already transcribed.')
  })

  it('returns an empty plan for an empty selection', () => {
    const plan = bulkPlan([], videos, 'transcribe', nothingForced)
    expect(plan.steps).toEqual([])
    expect(plan.skippedTranscribe).toEqual([])
    expect(plan.skippedDetect).toEqual([])
    expect(plan.skippedEntirely).toEqual([])
    expect(plan.note).toBeNull()
  })
})

describe('selectionNote', () => {
  it('is silent when everything selected is on screen', () => {
    expect(selectionNote(['a', 'b'], ['a', 'b', 'c'])).toBeNull()
  })

  it('says how many are selected outside this view', () => {
    // Selection deliberately survives a filter change - a race weekend
    // split across two playlists is an ordinary case - so the count has to
    // say when it reaches past what is on screen.
    expect(selectionNote(['a', 'x', 'y'], ['a', 'b'])).toBe('2 not in this view')
  })
})
