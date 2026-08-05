import { describe, expect, it } from 'vitest'
import { composeCopyAll, formatTagsForCopy } from './upload'
import type { UploadPreview } from './api'

describe('formatTagsForCopy', () => {
  it('joins tags with ", " as YouTube Studio expects', () => {
    expect(formatTagsForCopy(['sim racing', 'endurance', 'crash'])).toBe(
      'sim racing, endurance, crash',
    )
  })
  it('is empty for no tags', () => {
    expect(formatTagsForCopy([])).toBe('')
  })
})

describe('composeCopyAll', () => {
  it('labels title, description and tags for one paste', () => {
    const preview: UploadPreview = {
      title: 'CRASH!',
      description: 'A big one.',
      tags: ['a', 'b'],
      category_id: '17',
      made_for_kids: false,
    }
    expect(composeCopyAll(preview)).toBe(
      'Title:\nCRASH!\n\nDescription:\nA big one.\n\nTags:\na, b',
    )
  })
})
