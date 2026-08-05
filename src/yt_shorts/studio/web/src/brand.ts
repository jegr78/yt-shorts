/** Pure client-side helpers for the channel Brand & fonts editor (stage
 * G3b), kept in their own module - exporting no React - so Vite's
 * fast-refresh boundary stays component-only (same convention as
 * words.ts/format.ts/eventAdmin.ts) and each rule is unit-tested directly.
 *
 * These mirror the backend's own validation closely enough to give instant
 * feedback (yt_shorts.brand_admin._validate for colors/fonts,
 * yt_shorts.font_admin.save_font/pathnames.validate_segment for the
 * filename a font upload sends) but the server is still the real boundary -
 * a save or upload it rejects stays rejected no matter what this module
 * allowed client-side. */

const HEX_COLOR_PATTERN = /^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$/

/** Accepts the same `#RGB`/`#RRGGBB` shorthand PIL's ImageColor.getrgb does
 * for a hex string (brand_admin._validate uses getrgb to check each color).
 * Named CSS colors ("red") are NOT accepted here even though getrgb takes
 * them too - the ColorInput swatches this feeds always produce a hex
 * string, so accepting only hex keeps the client-side check simple without
 * narrowing what the backend itself allows. */
export function isValidHexColor(value: string): boolean {
  return HEX_COLOR_PATTERN.test(value)
}

/** The four color keys brand_admin.REQUIRED_COLOR_KEYS requires. */
const REQUIRED_COLOR_KEYS = ['text', 'base', 'accent', 'edge'] as const

/** The shape the Brand editor's form holds locally: the four brand colors
 * (keyed exactly like brand.json's own "colors" section) and the two font
 * roles, each a "fonts/<name>" reference once assigned (or undefined until
 * an upload/selection assigns one) - matches brand_admin.REQUIRED_FONT_KEYS. */
export interface BrandForm {
  colors: Record<string, string>
  fonts: { hook?: string; small?: string }
}

/** Whether the form is complete enough to PUT: mirrors
 * brand_admin._validate's two required sections - every color a valid hex
 * value, and both font roles assigned to a non-empty "fonts/<name>"
 * reference. Save stays disabled until this is true, so the operator never
 * round-trips an incomplete brand into a 400. */
export function brandReadyToSave(form: BrandForm): boolean {
  const colorsReady = REQUIRED_COLOR_KEYS.every((key) => isValidHexColor(form.colors[key] ?? ''))
  const fontsReady = Boolean(form.fonts.hook) && Boolean(form.fonts.small)
  return colorsReady && fontsReady
}

/** The two extensions font_admin.FONT_EXTENSIONS accepts. */
const FONT_EXTENSIONS = ['.ttf', '.otf']

/** Turns an uploaded File's own `name` into the filename sent to
 * POST /api/channels/{channel}/fonts/{filename} - one that
 * pathnames.validate_segment (`^[A-Za-z0-9][A-Za-z0-9._-]*\Z`) accepts, so
 * an upload from a real font vendor's ZIP (spaces, unicode, whatever) does
 * not round-trip into a 400 for its NAME alone.
 *
 * Keeps the file's own lowercased extension if it is .ttf/.otf (case
 * doesn't matter for a real font file, but the stored name should be
 * predictable); returns '' for anything else - the caller should treat
 * that as "not a font" and refuse the upload before ever calling the API,
 * the same way font_admin.save_font would reject it server-side (kind
 * "bad_type"). Every run of characters outside [A-Za-z0-9._-] in the STEM
 * (the part before the extension) becomes a single '-', and any leading
 * '.' that would survive from the stem is stripped - validate_segment
 * requires the first character to be alphanumeric. */
export function fontFilename(name: string): string {
  const dotIndex = name.lastIndexOf('.')
  if (dotIndex < 0) return ''
  const extension = name.slice(dotIndex).toLowerCase()
  if (!FONT_EXTENSIONS.includes(extension)) return ''
  const stem = name
    .slice(0, dotIndex)
    .replace(/[^A-Za-z0-9._-]+/g, '-')
    .replace(/^\.+/, '')
  if (!stem) return ''
  return `${stem}${extension}`
}

/** The logo variants (profile.LOGO_VARIANTS) and placements
 * (overlay.LOGO_POSITIONS) the studio offers. */
export const LOGO_VARIANTS = ['color', 'white', 'black'] as const
export const LOGO_POSITIONS = ['top', 'top-right', 'bottom-right'] as const

/** The required output dimensions (profile.REQUIRED_OUTPUT_KEYS). */
export const OUTPUT_KEYS = ['width', 'height', 'video_width', 'video_height', 'video_y'] as const

/** The output/video-window geometry (structurally the same as api.ts's
 * BrandOutput; kept here so this pure module stays free of the api layer). */
export interface OutputGeometry {
  width: number
  height: number
  video_width: number
  video_height: number
  video_y: number
  accent_offset?: number
}

/** Whether the output geometry is safe to PUT: mirrors profile._validate_brand's
 * guardrail (every required dimension a positive integer, the video window
 * fitting inside the frame) so the operator gets instant feedback instead of a
 * 400. The server stays the real boundary. */
export function outputReadyToSave(output: OutputGeometry): boolean {
  if (!OUTPUT_KEYS.every((key) => Number.isInteger(output[key]))) return false
  const { width, height, video_width, video_height, video_y } = output
  if (width <= 0 || height <= 0) return false
  if (!(video_width > 0 && video_width <= width)) return false
  if (video_height <= 0) return false
  if (video_y < 0 || video_y + video_height > height) return false
  return true
}
