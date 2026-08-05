import { useEffect, useRef, useState } from 'react'
import {
  ActionIcon,
  Alert,
  Box,
  Button,
  Card,
  Center,
  ColorInput,
  Divider,
  FileButton,
  Grid,
  Group,
  Loader,
  NumberInput,
  SegmentedControl,
  Select,
  Slider,
  Stack,
  Switch,
  Text,
  Textarea,
  TextInput,
  Tooltip,
} from '@mantine/core'
import { useDebouncedValue } from '@mantine/hooks'
import { notifications } from '@mantine/notifications'
import {
  ApiError,
  brandPreview,
  deleteFont,
  getBrand,
  getPalette,
  getSettings,
  PreviewUnavailableError,
  saveBrand,
  uploadFont,
  type BrandLogo,
  type BrandOutput,
  type BrandPatch,
  type PaletteResponse,
  type ProviderState,
} from '../api'
import { brandReadyToSave, outputReadyToSave, fontFilename } from '../brand'
import {
  BAND_FIELDS,
  COLOR_FIELDS,
  detectSection,
  formFromBrand,
  OUTPUT_FIELDS,
  type BrandEditorForm,
} from '../brandForm'
import {
  findProvider,
  priceSentence,
  providerLabel,
  providerOptions,
  unrecognizedProviderNote,
  unverifiedCaveat,
} from '../providers'
import {
  CATEGORIES,
  DESCRIPTION_MAX,
  TAGS_MAX,
  metadataFieldsValid,
  parseTags,
  tagsToInput,
} from '../uploadMeta'

const CATEGORY_DATA = CATEGORIES.map((category) => ({ value: String(category.id), label: category.label }))

/** The channel-level upload-metadata defaults (Task 7): the description
 * TEMPLATE, tags, category and made-for-kids flag every clip's upload
 * inherits unless a per-clip override replaces it (see
 * yt_shorts.editorial.effective_upload / UploadPanel.tsx's own per-clip
 * editor, which this mirrors field-for-field). Tags are held as the raw
 * textarea string, parsed with parseTags only where the parsed list is
 * actually needed (validity check, the patch sent on save) - same idiom as
 * UploadPanel's localTagsInput, so retyping is never lossy mid-edit. */
interface UploadDefaultsForm {
  description: string
  tagsInput: string
  categoryId: string
  madeForKids: boolean
}

/** Channel-only: NOT part of the shared BrandEditorForm (brandForm.ts),
 * because EventBrandEditor must never gain this section - the 'upload'
 * block is intentionally excluded from event-level brand overrides (see
 * api.py's put_event_brand, which 400s "bad_field" if it is present). */
function uploadDefaultsFromBrand(
  brand: Record<string, unknown>,
): { form: UploadDefaultsForm; mode?: 'api' | 'manual' } {
  const uploadRaw = (brand.upload as Record<string, unknown>) ?? {}
  const tags = Array.isArray(uploadRaw.tags)
    ? uploadRaw.tags.filter((tag): tag is string => typeof tag === 'string')
    : []
  return {
    form: {
      description: typeof uploadRaw.description === 'string' ? uploadRaw.description : '',
      tagsInput: tagsToInput(tags),
      categoryId:
        typeof uploadRaw.category_id === 'string'
          ? uploadRaw.category_id
          : uploadRaw.category_id !== undefined
            ? String(uploadRaw.category_id)
            : '',
      madeForKids: uploadRaw.made_for_kids === true,
    },
    mode: uploadRaw.mode === 'api' || uploadRaw.mode === 'manual' ? uploadRaw.mode : undefined,
  }
}

/** The editor's own local form shape: brandForm's shared BrandEditorForm
 * (the four colors + the two font roles + subtitles.enabled + logo +
 * output) plus this editor's own upload-defaults section and the
 * channel's CURRENT upload.mode, carried along unedited (see the
 * `uploadMode` field's own docstring below). Kept as one object so "dirty"
 * and "ready to save" are each a single comparison/predicate over one
 * value, not three separately-tracked pieces of state that could drift out
 * of sync with each other. */
interface EditorForm extends BrandEditorForm {
  upload: UploadDefaultsForm
  /** The channel's brand.json upload.mode as last loaded/saved - "api",
   * "manual", or undefined when the loaded brand had no 'upload' section
   * at all yet. This editor NEVER lets the operator change it (that is the
   * Settings screen's api/manual toggle, PUT …/upload) - it is only ever
   * read once on load and echoed back unchanged in formToPatch's
   * `upload.mode`, so saving the description/tags/category/made-for-kids
   * below can never silently flip a `manual` channel to `api` (the backend
   * replaces the whole `upload` section wholesale - see
   * brand_admin.update_brand - and `mode` defaults to "api" when absent). */
  uploadMode?: 'api' | 'manual'
}

/** The patch PUT (save) and POST …/brand/preview both take - only `fonts`
 * is conditional: BrandPatch.fonts requires BOTH roles (mirrors the
 * backend, which rejects a brand with only one assigned - see
 * brand_admin.REQUIRED_FONT_KEYS), so it is omitted entirely until both are
 * chosen rather than sent half-filled. */
function formToPatch(form: EditorForm): BrandPatch {
  const patch: BrandPatch = {
    colors: form.colors,
    subtitles: { enabled: form.subtitlesEnabled },
    output: form.output,
    bands: form.bands,
    upload: {
      // mode is included ONLY when the loaded brand actually had one -
      // omitted entirely otherwise, so a channel with no 'upload' section
      // yet gets none written in by this save (see EditorForm.uploadMode's
      // own docstring: never sending a mode this editor never offered to
      // change, rather than sending a guessed default).
      ...(form.uploadMode !== undefined ? { mode: form.uploadMode } : {}),
      // description/category_id are likewise omitted entirely while blank -
      // NOT sent as "" - because update_brand REPLACES the whole 'upload'
      // section wholesale (there is no per-key merge with whatever is
      // already on disk), and youtube_upload.build_metadata's own fallback
      // (DEFAULT_DESCRIPTION's "{source_title}" template, DEFAULT_CATEGORY
      // "20"/Gaming) only fires when the KEY IS ABSENT - `meta.get(key,
      // default)` returns "" as-is when the key is present but blank. An
      // operator who never touches these fields must not silently bake an
      // empty description/category into every future upload of this
      // channel's clips. tags/made_for_kids have no such trap: an absent
      // key and an empty list/false key resolve to the exact same
      // effective value, so those are always sent as-is.
      ...(form.upload.description.trim() !== '' ? { description: form.upload.description } : {}),
      tags: parseTags(form.upload.tagsInput),
      ...(form.upload.categoryId !== '' ? { category_id: form.upload.categoryId } : {}),
      made_for_kids: form.upload.madeForKids,
    },
  }
  if (form.fonts.hook && form.fonts.small) {
    patch.fonts = { hook: form.fonts.hook, small: form.fonts.small }
  }
  if (form.logo) {
    patch.logo = form.logo
  }
  // Omitted entirely while no provider is chosen (see brandForm.detectSection):
  // a blank provider is not a registered id, so sending one would 400 every
  // save of an unrelated field, and an absent section already means "the
  // built-in default". Harmless in the preview patch too - brand_preview
  // reads only the renderable sections and ignores this one.
  const detect = detectSection(form)
  if (detect) {
    patch.detect = detect
  }
  return patch
}

/**
 * The channel-level Brand & fonts editor (stage G3b): upload/list/delete
 * font files, assign two of them to the hook/small roles, pick the four
 * brand colors and toggle subtitles - then Save writes it all to
 * brand.json in one PUT. The right column mirrors PreviewPane's own
 * pattern (debounced re-fetch, object-URL revoke on replace/unmount) but
 * against POST …/brand/preview instead of a clip's own preview route, so
 * the operator sees the channel's hook/footer text rendered with the
 * form's CURRENT (possibly unsaved) colors and fonts before ever saving.
 */
export function BrandEditor({ channel }: { channel: string }) {
  const [form, setForm] = useState<EditorForm | null>(null)
  const [savedForm, setSavedForm] = useState<EditorForm | null>(null)
  const [fontsList, setFontsList] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)

  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const resetFileButton = useRef<() => void>(null)

  const [deletingFont, setDeletingFont] = useState<string | null>(null)
  const [fontError, setFontError] = useState<string | null>(null)

  const [previewUrl, setPreviewUrl] = useState<string | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const previewUrlRef = useRef<string | null>(null)

  const [swatches, setSwatches] = useState<PaletteResponse['swatches']>([])
  const [deriving, setDeriving] = useState(false)
  const [paletteError, setPaletteError] = useState<string | null>(null)

  // The registered providers, for the Moment detection section below. They
  // come from GET /api/settings because that is where the registry is
  // published (shipped constants and booleans only - no key ever crosses
  // it); this editor only READS them. A failure here must not break brand
  // editing, so it degrades to an inline note and an empty select rather
  // than to the screen's load error.
  const [providerStates, setProviderStates] = useState<ProviderState[] | null>(null)
  const [providersError, setProvidersError] = useState<string | null>(null)

  async function deriveFromLogo() {
    setDeriving(true)
    setPaletteError(null)
    try {
      const result = await getPalette(channel)
      setSwatches(result.swatches)
      // A PROPOSAL: it fills the fields and nothing more. Only the roles the
      // logo actually supports are set - a single-colour mark returns base
      // and text alone, and the other fields keep what the operator had
      // rather than being overwritten with an invented colour.
      setForm((current) => (current ? { ...current, colors: { ...current.colors, ...result.colors } } : current))
      notifications.show({
        message: 'Colours proposed from the logo. Nothing is saved yet.',
        color: 'green',
      })
    } catch (err) {
      setPaletteError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setDeriving(false)
    }
  }

  // Load the channel's brand.json + fonts once (and again if the operator
  // is ever shown a different channel without a full remount - ChannelScreen
  // keys the whole screen by channel, so in practice this only fires once).
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    getBrand(channel)
      .then(({ brand, fonts }) => {
        if (cancelled) return
        const { form: upload, mode } = uploadDefaultsFromBrand(brand)
        const initial: EditorForm = { ...formFromBrand(brand), upload, uploadMode: mode }
        setForm(initial)
        setSavedForm(initial)
        setFontsList(fonts)
        setLoadError(null)
      })
      .catch((err) => {
        if (cancelled) return
        setLoadError(err instanceof ApiError ? err.message : String(err))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [channel])

  useEffect(() => {
    let cancelled = false
    getSettings()
      .then((settings) => {
        if (!cancelled) {
          setProviderStates(settings.providers)
          setProvidersError(null)
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setProviderStates([])
          setProvidersError(err instanceof ApiError ? err.message : String(err))
        }
      })
    return () => {
      cancelled = true
    }
  }, [])

  const [debouncedForm] = useDebouncedValue(form, 300)

  // The live preview: re-fetched debounced on any form change (colors,
  // fonts, subtitles), against the SAVED brand with the form's patch
  // overlaid server-side (see api.py's brand_preview) - so this always
  // reflects unsaved edits, exactly like PreviewPane's clip-level preview.
  useEffect(() => {
    if (!debouncedForm) return
    let cancelled = false
    setPreviewLoading(true)
    brandPreview(channel, formToPatch(debouncedForm))
      .then((url) => {
        if (cancelled) {
          URL.revokeObjectURL(url)
          return
        }
        if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current)
        previewUrlRef.current = url
        setPreviewUrl(url)
        setPreviewError(null)
      })
      .catch((err) => {
        if (cancelled) return
        setPreviewUrl(null)
        if (err instanceof PreviewUnavailableError) {
          setPreviewError('upload and assign a font to see the preview')
        } else {
          setPreviewError(err instanceof ApiError ? err.message : String(err))
        }
      })
      .finally(() => {
        if (!cancelled) setPreviewLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [channel, debouncedForm])

  // Revoke the last object URL on unmount (a replace is already revoked
  // above, right before the new one is adopted).
  useEffect(
    () => () => {
      if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current)
    },
    [],
  )

  function setColor(key: string, value: string) {
    setForm((prev) => (prev ? { ...prev, colors: { ...prev.colors, [key]: value } } : prev))
  }

  function setBand(key: 'top' | 'bottom', value: number) {
    setForm((current) => (current ? { ...current, bands: { ...current.bands, [key]: value } } : current))
  }

  function setFontRole(role: 'hook' | 'small', value: string | null) {
    setForm((prev) =>
      prev
        ? { ...prev, fonts: { ...prev.fonts, [role]: value ?? undefined } }
        : prev,
    )
  }

  function setSubtitlesEnabled(value: boolean) {
    setForm((prev) => (prev ? { ...prev, subtitlesEnabled: value } : prev))
  }

  function setLogoField<K extends keyof BrandLogo>(key: K, value: BrandLogo[K]) {
    setForm((prev) => (prev && prev.logo ? { ...prev, logo: { ...prev.logo, [key]: value } } : prev))
  }

  function removeLogo() {
    setForm((prev) => (prev ? { ...prev, logo: null } : prev))
  }

  function setOutputField(key: keyof BrandOutput, value: number | string) {
    if (typeof value !== 'number') return
    setForm((prev) => (prev ? { ...prev, output: { ...prev.output, [key]: value } } : prev))
  }

  function setUploadField<K extends keyof UploadDefaultsForm>(key: K, value: UploadDefaultsForm[K]) {
    setForm((prev) => (prev ? { ...prev, upload: { ...prev.upload, [key]: value } } : prev))
  }

  /** Switching provider clears the model field rather than seeding it with
   * the new provider's default (M2 - see brandForm.ts's own comment on
   * `detectProvider`, which this carries the same reasoning over to): writing
   * the default back would PIN it into brand.json on save, so the channel
   * stops tracking a future change to that provider's own default. It is
   * still cleared rather than carried across from the old provider - a model
   * name is vendor-specific, and keeping `claude-opus-5` selected after
   * switching to OpenAI is the kind of mistake that surfaces three hours into
   * a run rather than at the moment it is made. A blank field means "the
   * provider's own default", which `DetectSection`'s placeholder and
   * `priceSentence`'s `effectiveModel` both already read correctly. */
  function setDetectProvider(id: string) {
    setForm((prev) =>
      prev
        ? {
            ...prev,
            detectProvider: id,
            detectModel: '',
          }
        : prev,
    )
  }

  async function handleUpload(file: File | null) {
    if (!file) return
    const filename = fontFilename(file.name)
    if (!filename) {
      setUploadError(`"${file.name}" is not a .ttf or .otf font file.`)
      resetFileButton.current?.()
      return
    }
    setUploading(true)
    setUploadError(null)
    try {
      const bytes = await file.arrayBuffer()
      const { fonts } = await uploadFont(channel, filename, bytes)
      setFontsList(fonts)
    } catch (err) {
      setUploadError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setUploading(false)
      resetFileButton.current?.()
    }
  }

  async function handleDeleteFont(name: string) {
    setDeletingFont(name)
    setFontError(null)
    try {
      const { fonts } = await deleteFont(channel, name)
      setFontsList(fonts)
      const ref = `fonts/${name}`
      setForm((prev) => {
        if (!prev) return prev
        const next = { ...prev.fonts }
        let changed = false
        if (next.hook === ref) {
          delete next.hook
          changed = true
        }
        if (next.small === ref) {
          delete next.small
          changed = true
        }
        return changed ? { ...prev, fonts: next } : prev
      })
    } catch (err) {
      setFontError(err instanceof ApiError ? `Could not delete "${name}": ${err.message}` : String(err))
    } finally {
      setDeletingFont(null)
    }
  }

  async function handleSave() {
    if (!form || !dirty || !readyToSave) return
    setSaving(true)
    setSaveError(null)
    try {
      const { brand } = await saveBrand(channel, formToPatch(form))
      const { form: upload, mode } = uploadDefaultsFromBrand(brand)
      const next: EditorForm = { ...formFromBrand(brand), upload, uploadMode: mode }
      setForm(next)
      setSavedForm(next)
      notifications.show({ message: 'Saved.', color: 'green' })
    } catch (err) {
      setSaveError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  if (loading) {
    return (
      <Center py="xl">
        <Stack align="center" gap="xs">
          <Loader color="steel" />
          <Text size="xs" c="dimmed">
            Loading brand…
          </Text>
        </Stack>
      </Center>
    )
  }

  if (loadError || !form) {
    return (
      <Alert color="red" title="Could not load brand">
        {loadError} - check that the studio server is still running, then reload this page.
      </Alert>
    )
  }

  const dirty = savedForm !== null && JSON.stringify(form) !== JSON.stringify(savedForm)
  const uploadTags = parseTags(form.upload.tagsInput)
  const uploadTagsLength = uploadTags.reduce((sum, tag) => sum + tag.length, 0)
  const uploadDescriptionOverLimit = form.upload.description.length > DESCRIPTION_MAX
  const uploadTagsOverLimit = uploadTagsLength > TAGS_MAX
  // Mirrors UploadPanel's own "Save metadata" gate (metadataFieldsValid) -
  // a channel default that already exceeds YouTube's caps would only ever
  // fail later, at the moment an actual upload builds its metadata (see
  // youtube_upload.build_metadata), so it is refused here instead.
  const uploadDefaultsValid = metadataFieldsValid({
    description: form.upload.description,
    tags: uploadTags,
  })
  const readyToSave =
    brandReadyToSave(form) && outputReadyToSave(form.output) && uploadDefaultsValid

  const fontOptions = fontsList.map((name) => ({ value: `fonts/${name}`, label: name }))

  return (
    <Grid gap="lg">
      <Grid.Col span={{ base: 12, md: 7 }}>
        <Stack gap="lg">
          <Card padding="md">
            <Stack gap="sm">
              <Group justify="space-between">
                <Text fw={600} size="sm" tt="uppercase" c="dimmed">
                  Fonts
                </Text>
                <FileButton resetRef={resetFileButton} onChange={handleUpload} accept=".ttf,.otf">
                  {(props) => (
                    <Button {...props} variant="light" color="steel" loading={uploading} size="xs">
                      Upload font
                    </Button>
                  )}
                </FileButton>
              </Group>

              {fontsList.length === 0 ? (
                <Text size="xs" c="dimmed">
                  No fonts uploaded yet. Upload a .ttf or .otf file to assign it below.
                </Text>
              ) : (
                <Stack gap={4}>
                  {fontsList.map((name) => (
                    <Group key={name} justify="space-between" wrap="nowrap">
                      <Text size="sm" ff="monospace" style={{ overflowWrap: 'anywhere' }}>
                        {name}
                      </Text>
                      <ActionIcon
                        variant="subtle"
                        color="red"
                        aria-label={`Delete ${name}`}
                        loading={deletingFont === name}
                        disabled={deletingFont !== null}
                        onClick={() => handleDeleteFont(name)}
                      >
                        <Text fw={700} lh={1}>
                          ×
                        </Text>
                      </ActionIcon>
                    </Group>
                  ))}
                </Stack>
              )}

              {uploadError ? (
                <Alert color="red" title="Could not upload font">
                  {uploadError}
                </Alert>
              ) : null}
              {fontError ? (
                <Alert color="red" title="Could not delete font">
                  {fontError}
                </Alert>
              ) : null}

              <Select
                label="Hook font"
                placeholder="Choose a font"
                data={fontOptions}
                value={form.fonts.hook ?? null}
                onChange={(value) => setFontRole('hook', value)}
                allowDeselect={false}
              />
              <Select
                label="Small font"
                placeholder="Choose a font"
                data={fontOptions}
                value={form.fonts.small ?? null}
                onChange={(value) => setFontRole('small', value)}
                allowDeselect={false}
              />
            </Stack>
          </Card>

          <Card padding="md">
            <Stack gap="sm">
              <Group justify="space-between" align="center">
                <Text fw={600} size="sm" tt="uppercase" c="dimmed">
                  Colors
                </Text>
                <Button size="xs" variant="light" loading={deriving} onClick={deriveFromLogo}>
                  Derive from logo
                </Button>
              </Group>
              {paletteError ? (
                <Alert color="red" title="Could not read the logo">
                  {paletteError}
                </Alert>
              ) : null}
              {swatches.length > 0 ? (
                <Group gap={6}>
                  {swatches.map((swatch) => (
                    <Tooltip key={swatch.hex} label={`${swatch.hex} · ${Math.round(swatch.share * 100)}%`}>
                      <Box
                        style={{
                          width: 28,
                          height: 28,
                          borderRadius: 4,
                          background: swatch.hex,
                          border: '1px solid rgba(128,128,128,0.4)',
                        }}
                      />
                    </Tooltip>
                  ))}
                </Group>
              ) : null}
              {COLOR_FIELDS.map(({ key, label }) => (
                <ColorInput
                  key={key}
                  label={label}
                  format="hex"
                  value={form.colors[key] ?? ''}
                  onChange={(value) => setColor(key, value)}
                />
              ))}
              <Divider my="xs" />
              <Text fw={600} size="sm" tt="uppercase" c="dimmed">
                Band opacity
              </Text>
              <Text size="xs" c="dimmed">
                How solid the upper and lower thirds are. At 0% only the clip's own
                blurred backdrop shows there — the hook, footer and logo stay.
              </Text>
              {BAND_FIELDS.map(({ key, label }) => (
                <Stack key={key} gap={2}>
                  <Text size="sm">{label}</Text>
                  <Slider
                    min={0}
                    max={1}
                    step={0.05}
                    value={form.bands[key]}
                    onChange={(value) => setBand(key, value)}
                    label={(value) => `${Math.round(value * 100)}%`}
                    marks={[
                      { value: 0, label: '0%' },
                      { value: 1, label: '100%' },
                    ]}
                  />
                </Stack>
              ))}
            </Stack>
          </Card>

          <Card padding="md">
            <Switch
              label="Enable subtitles"
              checked={form.subtitlesEnabled}
              onChange={(e) => setSubtitlesEnabled(e.currentTarget.checked)}
              color="steel"
            />
          </Card>

          <Card padding="md">
            <Stack gap="sm">
              <Group justify="space-between">
                <Text fw={600} size="sm" tt="uppercase" c="dimmed">
                  Logo
                </Text>
                {form.logo ? (
                  <Button variant="subtle" color="red" size="xs" onClick={removeLogo}>
                    Remove logo
                  </Button>
                ) : null}
              </Group>

              {form.logo ? (
                <>
                  <div>
                    <Text size="sm" mb={4}>
                      Variant
                    </Text>
                    <SegmentedControl
                      fullWidth
                      size="xs"
                      value={form.logo.variant ?? 'color'}
                      onChange={(value) => setLogoField('variant', value as BrandLogo['variant'])}
                      data={[
                        { label: 'Color', value: 'color' },
                        { label: 'White', value: 'white' },
                        { label: 'Black', value: 'black' },
                      ]}
                    />
                  </div>
                  <Select
                    label="Placement"
                    allowDeselect={false}
                    value={form.logo.position ?? 'top'}
                    onChange={(value) => value && setLogoField('position', value as BrandLogo['position'])}
                    data={[
                      { value: 'top', label: 'Badge — centered at top' },
                      { value: 'top-right', label: 'Watermark — top right' },
                      { value: 'bottom-right', label: 'Watermark — bottom right' },
                    ]}
                  />
                  {form.logo.position && form.logo.position !== 'top' ? (
                    <div>
                      <Group justify="space-between">
                        <Text size="sm">Opacity</Text>
                        <Text size="xs" c="dimmed" className="tnum">
                          {Math.round((form.logo.opacity ?? 0.72) * 100)}%
                        </Text>
                      </Group>
                      <Slider
                        min={0.2}
                        max={1}
                        step={0.02}
                        color="steel"
                        value={form.logo.opacity ?? 0.72}
                        onChange={(value) => setLogoField('opacity', value)}
                        label={(value) => `${Math.round(value * 100)}%`}
                      />
                    </div>
                  ) : null}
                </>
              ) : (
                <Text size="xs" c="dimmed">
                  This channel has no logo image yet. Its variants (color / white / black) are
                  generated from an <Text span ff="monospace">assets/logo.png</Text>, added outside
                  the studio for now.
                </Text>
              )}
            </Stack>
          </Card>

          <Card padding="md">
            <Stack gap="sm">
              <Text fw={600} size="sm" tt="uppercase" c="dimmed">
                Output geometry
              </Text>
              <Text size="xs" c="dimmed">
                Advanced. The sharp video window inside the portrait frame. The window must fit
                inside the frame — invalid geometry is refused on save.
              </Text>
              <Divider />
              <Grid gap="xs">
                {OUTPUT_FIELDS.map(([key, label]) => (
                  <Grid.Col span={6} key={key}>
                    <NumberInput
                      label={label}
                      size="xs"
                      allowNegative={false}
                      allowDecimal={false}
                      value={form.output[key] ?? 0}
                      onChange={(value) => setOutputField(key, value)}
                    />
                  </Grid.Col>
                ))}
              </Grid>
              {!outputReadyToSave(form.output) ? (
                <Text size="xs" c="red">
                  The video window must be positive and fit inside the frame.
                </Text>
              ) : null}
            </Stack>
          </Card>

          <DetectSection
            provider={form.detectProvider}
            model={form.detectModel}
            states={providerStates}
            loadError={providersError}
            onProviderChange={setDetectProvider}
            onModelChange={(value) =>
              setForm((prev) => (prev ? { ...prev, detectModel: value } : prev))
            }
          />

          <Card padding="md">
            <Stack gap="sm">
              <Text fw={600} size="sm" tt="uppercase" c="dimmed">
                Upload defaults
              </Text>
              <Text size="xs" c="dimmed">
                What every clip's upload inherits unless a per-clip override replaces it (see the
                Upload panel on an individual clip). Never changes whether this channel uploads
                via the API or stays manual - that stays on the Settings screen.
              </Text>
              <Textarea
                label="Description template"
                placeholder="Clip from {source_title}."
                description={`Filled in per clip via {source_title}/{title} placeholders - leave blank to use the built-in template: Clip from {source_title}. ${form.upload.description.length} / ${DESCRIPTION_MAX} characters`}
                autosize
                minRows={3}
                maxRows={8}
                value={form.upload.description}
                onChange={(e) => setUploadField('description', e.currentTarget.value)}
                error={uploadDescriptionOverLimit ? "Over YouTube's limit" : undefined}
              />
              <TextInput
                label="Tags"
                description={`comma or newline separated - ${uploadTagsLength} / ${TAGS_MAX} characters combined (${uploadTags.length} tag${uploadTags.length === 1 ? '' : 's'})`}
                value={form.upload.tagsInput}
                onChange={(e) => setUploadField('tagsInput', e.currentTarget.value)}
                error={uploadTagsOverLimit ? "Over YouTube's limit" : undefined}
              />
              <Group grow align="flex-end">
                <Select
                  label="Category"
                  description="Leave unset to default to Gaming"
                  data={CATEGORY_DATA}
                  value={form.upload.categoryId || null}
                  onChange={(value) => setUploadField('categoryId', value ?? '')}
                  placeholder="Choose a category"
                  clearable
                />
                <Switch
                  label="Made for kids"
                  checked={form.upload.madeForKids}
                  onChange={(e) => setUploadField('madeForKids', e.currentTarget.checked)}
                  color="steel"
                />
              </Group>
            </Stack>
          </Card>

          {saveError ? (
            <Alert color="red" title="Could not save brand">
              {saveError}
            </Alert>
          ) : null}

          <Group justify="flex-end">
            <Button
              color="steel"
              onClick={handleSave}
              loading={saving}
              disabled={!dirty || !readyToSave}
            >
              Save brand
            </Button>
          </Group>
        </Stack>
      </Grid.Col>

      <Grid.Col span={{ base: 12, md: 5 }}>
        <Stack gap="xs">
          <Text fw={600} size="xs" tt="uppercase" c="dimmed">
            Preview
          </Text>
          <Box className="monitorBezel">
            <Box className="monitorScreen">
              {!previewError && previewUrl && (
                <img
                  src={previewUrl}
                  alt="Brand preview"
                  style={{
                    width: '100%',
                    height: '100%',
                    objectFit: 'contain',
                    opacity: previewLoading ? 0.55 : 1,
                  }}
                />
              )}
              {!previewError && !previewUrl && (
                <Text size="xs" c="dimmed">
                  {previewLoading ? 'Loading…' : 'No preview yet'}
                </Text>
              )}
              {previewError && (
                <Text size="xs" c="dimmed" tt="uppercase" ff="monospace" ta="center" px="sm">
                  {previewError}
                </Text>
              )}
            </Box>
          </Box>
          <Text size="xs" c="dimmed">
            Reflects the hook and footer rendered with this form's current colors and fonts -
            nothing is saved until you click Save brand.
          </Text>
        </Stack>
      </Grid.Col>
    </Grid>
  )
}

/**
 * The channel's Moment-detection section: which provider scores this
 * channel's streams, and with what model (brand.json's `detect` - see
 * profile._validate_detect).
 *
 * Channel-level ONLY, deliberately: the event brand route refuses a `detect`
 * override outright (400) because the choice is account-scoped - it decides
 * whose API key and whose quota get spent - which places it with `upload`
 * rather than with colors and fonts.
 *
 * Two disclosures are the point of this section, not decoration on it:
 *
 * - An UNVERIFIED provider is marked in the select's own option AND spelled
 *   out in full once selected, so an operator choosing one here never has to
 *   have visited the Settings screen to learn it.
 * - What it COSTS, at the moment of choosing (priceSentence): the selected
 *   model's published rate plus this provider's cheapest priced model beside
 *   it. `openai_api.DEFAULT_MODEL` is deliberately not the cheapest entry -
 *   picking cheapest is the argument the Anthropic bake-off refuted on this
 *   project's own transcripts - but OpenAI is never measured, so the gap has
 *   no evidence behind it either way. Disclosing the exposure is the remedy
 *   that was chosen over measuring it. Never a bill: see priceSentence.
 *
 * The select is deliberately NOT clearable. PUT …/brand leaves a section it
 * was not sent untouched, so "clearing" back to inherited would look like it
 * saved and change nothing on disk - a silent no-op, the failure shape this
 * project keeps paying for. Going back to the built-in default is a hand
 * edit of brand.json - said on the card itself now (not only here, where no
 * operator reads it), because M1's placeholder fix (below) makes the field
 * look populated for a value that might in fact just be "never chosen".
 *
 * M1: an unrecognised `provider` (a value `states` does not contain - an old
 * choice for a since-removed provider, or a `GET /api/settings` that failed,
 * which leaves `states` at `[]`, not `null`) used to resolve to `undefined`
 * in Mantine's `Select` and render the PLACEHOLDER instead - "Anthropic (the
 * built-in default)" shown for a channel that had actually chosen something
 * else, with the caveat/price disclosure silently gone too. `providerOptions`
 * fixes the blank-and-lying half by always giving the select something to
 * resolve `value` to; `unrecognizedProviderNote` fixes the vanished-
 * disclosure half by putting a plain "not recognised" note where the caveat/
 * price would otherwise have been, rather than leaving that spot empty.
 */
function DetectSection({
  provider,
  model,
  states,
  loadError,
  onProviderChange,
  onModelChange,
}: {
  provider: string
  model: string
  /** Null while still loading - the select is disabled meanwhile rather than
   * offered empty, so a click never lands on a list that is about to fill. */
  states: ProviderState[] | null
  loadError: string | null
  onProviderChange: (id: string) => void
  onModelChange: (value: string) => void
}) {
  const loaded = states ?? []
  const selected = findProvider(loaded, provider)
  // What detection would actually use: the field when set, else the
  // provider's own default. Blank means "the default", so the price shown
  // must be the default's, not nothing.
  const effectiveModel = model.trim() || selected?.default_model || ''
  const caveat = selected ? unverifiedCaveat(selected) : null
  const price = selected ? priceSentence(selected, effectiveModel) : null
  // Set once loaded (states !== null) whenever the stored provider is not
  // among the known ones - see the M1 note above. Never fires while still
  // loading, so a not-yet-fetched list does not flash this note first.
  const unrecognized =
    states !== null && provider !== '' && !selected ? unrecognizedProviderNote(provider) : null

  return (
    <Card padding="md">
      <Stack gap="sm">
        <Text fw={600} size="sm" tt="uppercase" c="dimmed">
          Moment detection
        </Text>
        <Text size="xs" c="dimmed">
          Which model scores this channel's streams for clip-worthy moments. With no
          key stored for the chosen provider (Settings → Model providers), detection
          falls back to the offline lexicon engine and says so in the job log.
        </Text>

        {loadError ? (
          <Alert color="gray" variant="light" title="Could not load the provider list">
            {loadError} - the current selection is shown as-is and can still be saved.
          </Alert>
        ) : null}

        <Select
          label="Provider"
          placeholder={
            states === null ? 'Loading…' : 'Anthropic (the built-in default)'
          }
          allowDeselect={false}
          disabled={states === null}
          data={providerOptions(loaded, provider)}
          value={provider || null}
          onChange={(value) => value && onProviderChange(value)}
        />
        <Text size="xs" c="dimmed">
          There is no clear button here - saving with a section untouched leaves it as
          whatever it already was. To go back to the built-in default, remove this
          channel's <code>detect</code> section from <code>brand.json</code> by hand.
        </Text>
        <TextInput
          label="Model"
          description={
            selected
              ? `Leave blank to use ${providerLabel(selected.id)}'s own default, ${selected.default_model}. Not checked against the vendor's catalogue - a name it does not know fails at call time and falls back to the lexicon.`
              : 'Leave blank to use the provider’s own default.'
          }
          placeholder={selected ? `${selected.default_model} (the provider's default)` : ''}
          value={model}
          onChange={(event) => onModelChange(event.currentTarget.value)}
        />

        {unrecognized ? (
          <Alert color="yellow" variant="light" title="Not recognised">
            {unrecognized}
          </Alert>
        ) : null}
        {caveat ? (
          <Alert color="yellow" variant="light" title="Never measured">
            {caveat}
          </Alert>
        ) : null}
        {price ? (
          <Text size="xs" c="dimmed">
            {price}
          </Text>
        ) : null}
      </Stack>
    </Card>
  )
}
