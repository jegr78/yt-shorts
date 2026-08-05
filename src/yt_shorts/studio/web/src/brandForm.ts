/** Brand-form shaping shared by the channel Brand editor (BrandEditor.tsx)
 * and the event brand-override editor (EventBrandEditor.tsx) - both build
 * the same local form shape (the four brand colors, the two font roles,
 * subtitles.enabled, logo, output) out of brand.json's own loose
 * `Record<string, unknown>` shape (GET/PUT return this - see api.ts's
 * BrandResponse/EventBrandResponse), so the reading and field-table logic
 * lives here once rather than twice. Kept pure (no React import) so Vite's
 * fast-refresh boundary stays component-only, same convention as
 * brand.ts/eventBrand.ts. */
import type { BrandForm } from './brand'
import type { BrandBands, BrandLogo, BrandOutput } from './api'

/** The shared local form shape both editors hold: BrandForm's colors/fonts
 * plus the three fields brand.json also carries that BrandForm itself
 * doesn't need (subtitlesEnabled, logo, output). BrandEditor's own
 * `EditorForm` and EventBrandEditor's own `SectionForm` are each a type
 * alias to this - kept as local names in each file because one holds a
 * single working brand and the other holds either the channel's or the
 * merged effective values depending on context, which is worth documenting
 * separately at each call site. */
export interface BrandEditorForm extends BrandForm {
  subtitlesEnabled: boolean
  logo: BrandLogo | null
  output: BrandOutput
  bands: BrandBands
  /** brand.json's `detect.provider` - which vendor scores this channel's
   * moments. "" means the section is absent, i.e. the built-in default
   * (providers.DEFAULT_PROVIDER); it is NOT seeded with that default here,
   * because writing the default back would turn "inherited" into "chosen"
   * on the next save of an unrelated field. Read by both editors because
   * the form shape is shared, but only the CHANNEL editor offers it: the
   * event brand route refuses a `detect` section outright (400), which is
   * why detectSection below is called only from BrandEditor. */
  detectProvider: string
  /** brand.json's `detect.model` - "" for "use the provider's own default".
   * Never validated against a vendor catalogue, here or on the server. */
  detectModel: string
}

/** The four color keys brand_admin.REQUIRED_COLOR_KEYS requires, paired with
 * the label each editor's ColorInput shows. */
export const COLOR_FIELDS: { key: 'text' | 'base' | 'accent' | 'edge'; label: string }[] = [
  { key: 'text', label: 'Text color' },
  { key: 'base', label: 'Base color' },
  { key: 'accent', label: 'Accent color' },
  { key: 'edge', label: 'Edge color' },
]

/** The two band-opacity sliders, in display order. Labelled by where they
 * are on the picture rather than by their key, because "top" alone reads as
 * a position in the form rather than a third of the frame. */
export const BAND_FIELDS: { key: 'top' | 'bottom'; label: string }[] = [
  { key: 'top', label: 'Upper third' },
  { key: 'bottom', label: 'Lower third' },
]

/** The output/video-window fields, paired with the label each editor's
 * NumberInput shows, in display order. */
export const OUTPUT_FIELDS: [keyof BrandOutput, string][] = [
  ['width', 'Frame width'],
  ['height', 'Frame height'],
  ['video_width', 'Video width'],
  ['video_height', 'Video height'],
  ['video_y', 'Video top (y)'],
  ['accent_offset', 'Accent offset'],
]

/** Reads brand.json's own loose shape (GET/PUT return `Record<string,
 * unknown>` - see BrandResponse/EventBrandResponse, the latter's
 * `channel`/`effective` fields are the same loose shape) into the
 * strictly-typed local form, tolerating a missing/malformed section the
 * same way a freshly scaffolded channel's brand.json has one
 * (channel_admin.DEFAULT_BRAND has no `subtitles` guarantee beyond
 * `enabled: false`, and a hand-edited brand.json could be missing a color
 * entirely). */
export function formFromBrand(brand: Record<string, unknown>): BrandEditorForm {
  const colorsRaw = (brand.colors as Record<string, unknown>) ?? {}
  const colors: Record<string, string> = {}
  for (const { key } of COLOR_FIELDS) {
    const value = colorsRaw[key]
    colors[key] = typeof value === 'string' ? value : ''
  }
  const fontsRaw = (brand.fonts as Record<string, unknown>) ?? {}
  const fonts: { hook?: string; small?: string } = {}
  if (typeof fontsRaw.hook === 'string') fonts.hook = fontsRaw.hook
  if (typeof fontsRaw.small === 'string') fonts.small = fontsRaw.small
  const subtitlesRaw = (brand.subtitles as Record<string, unknown>) ?? {}
  const subtitlesEnabled = subtitlesRaw.enabled === true

  const logoRaw = brand.logo as Record<string, unknown> | undefined
  let logo: BrandLogo | null = null
  if (logoRaw && typeof logoRaw.file === 'string') {
    logo = {
      file: logoRaw.file,
      variant: (logoRaw.variant as BrandLogo['variant']) ?? 'color',
      position: (logoRaw.position as BrandLogo['position']) ?? 'top',
      ...(typeof logoRaw.opacity === 'number' ? { opacity: logoRaw.opacity } : {}),
      ...(typeof logoRaw.max_height === 'number' ? { max_height: logoRaw.max_height } : {}),
    }
  }

  const outputRaw = (brand.output as Record<string, unknown>) ?? {}
  const num = (value: unknown, fallback: number) =>
    typeof value === 'number' ? value : fallback
  const output: BrandOutput = {
    width: num(outputRaw.width, 1080),
    height: num(outputRaw.height, 1920),
    video_width: num(outputRaw.video_width, 1080),
    video_height: num(outputRaw.video_height, 608),
    video_y: num(outputRaw.video_y, 600),
    ...(typeof outputRaw.accent_offset === 'number'
      ? { accent_offset: outputRaw.accent_offset }
      : {}),
  }

  const bandsRaw = (brand.bands as Record<string, unknown>) ?? {}
  const band = (value: unknown) => (typeof value === 'number' ? value : 1)
  const bands: BrandBands = { top: band(bandsRaw.top), bottom: band(bandsRaw.bottom) }

  // brand.json is hand-editable and this read does NOT go through
  // profile.load's validation, so `"detect": "gemini"` or
  // `{"provider": ["gemini"]}` reaches here intact - type-check both values
  // rather than merely defaulting them, the same way api.py's own settings
  // read of this section does. A malformed value reads as "unset" and the
  // editor shows the built-in default; it is never rendered as [object
  // Object] into a select.
  const detectRaw = (brand.detect as Record<string, unknown>) ?? {}
  const detectProvider = typeof detectRaw.provider === 'string' ? detectRaw.provider : ''
  const detectModel = typeof detectRaw.model === 'string' ? detectRaw.model : ''

  return {
    colors, fonts, subtitlesEnabled, logo, output, bands, detectProvider, detectModel,
  }
}

/** The `detect` section a channel brand save should carry, or undefined when
 * there is nothing to write.
 *
 * Undefined for an unchosen provider rather than `{provider: ""}`, for two
 * reasons: PUT …/brand replaces a named section wholesale, and
 * profile._validate_detect refuses a provider that is not a registered id -
 * so a blank one would turn every save of an unrelated field into a 400. The
 * model is likewise omitted while blank (that validator refuses an empty
 * string too), which is exactly what "use the provider's own default" means
 * on disk: no key at all. */
export function detectSection(
  form: BrandEditorForm,
): { provider: string; model?: string } | undefined {
  if (!form.detectProvider) return undefined
  const model = form.detectModel.trim()
  return { provider: form.detectProvider, ...(model !== '' ? { model } : {}) }
}
