/** Pure helpers for the event-level brand override editor, kept out of
 * components so Vite's fast-refresh boundary stays component-only (same
 * convention as brand.ts/words.ts/eventAdmin.ts) and each rule is
 * unit-tested directly. An event's brand.json is a PARTIAL override
 * deep-merged over the channel brand (see event_brand_admin.read_event_brand
 * / update_event_brand) - these shape the per-section override the editor
 * sends. The server (event_brand_admin._validate_merged) is the real
 * boundary; this module only decides WHICH sections travel and WHAT whole
 * section value each one carries. */
import type { BrandPatch } from './api'

/** The sections event_brand_admin.OVERRIDE_SECTIONS allows overriding at
 * the event level - notably NOT 'upload', which is refused with a 400 by
 * PUT .../brand (see api.py's put_event_brand). */
export const SECTIONS = ['colors', 'fonts', 'logo', 'output', 'subtitles', 'bands'] as const
export type Section = (typeof SECTIONS)[number]

/** Which of the known sections are present in the event's stored override
 * (the `override` field of EventBrandResponse) - i.e. which sections this
 * event currently overrides rather than inherits from the channel. Unknown
 * keys in `override` (there should not be any, but the server is the real
 * validator) are ignored rather than surfaced as a phantom section. */
export function overriddenSections(override: Record<string, unknown>): Set<string> {
  return new Set(SECTIONS.filter((s) => Object.prototype.hasOwnProperty.call(override, s)))
}

/** The override payload PUT .../brand receives: each overridden section
 * taken WHOLE from the effective (merged) brand, every inherited section
 * omitted entirely so the server leaves it alone (see
 * event_brand_admin.update_event_brand: an omitted key never enters the
 * patch, so it stays inherited; an empty patch even deletes an existing
 * event brand.json rather than writing `{}`). */
export function buildOverridePayload(
  effective: Record<string, unknown>,
  overridden: Set<string>,
): BrandPatch {
  const payload: Record<string, unknown> = {}
  for (const s of SECTIONS) {
    if (overridden.has(s) && effective[s] !== undefined) payload[s] = effective[s]
  }
  return payload as BrandPatch
}
