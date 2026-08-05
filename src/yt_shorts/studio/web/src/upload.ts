import type { UploadPreview } from './api'

/** Tags as YouTube Studio's tag field expects them: a single
 * comma-separated line. Pure so it is unit-tested without a DOM. */
export function formatTagsForCopy(tags: string[]): string {
  return tags.join(', ')
}

/** Title, description and tags as one labelled block for a single "copy
 * all". Privacy and made-for-kids are deliberately NOT included: privacy is
 * always "private" on the API path (build_metadata) and is a YouTube Studio
 * toggle the operator sets themselves for a manual upload. */
export function composeCopyAll(preview: UploadPreview): string {
  return [
    `Title:\n${preview.title}`,
    `Description:\n${preview.description}`,
    `Tags:\n${formatTagsForCopy(preview.tags)}`,
  ].join('\n\n')
}
