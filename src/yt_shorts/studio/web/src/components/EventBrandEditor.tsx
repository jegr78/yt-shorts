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
} from '@mantine/core'
import { useDebouncedValue } from '@mantine/hooks'
import { notifications } from '@mantine/notifications'
import {
  ApiError,
  deleteEventFont,
  eventBrandPreview,
  getEventBrand,
  PreviewUnavailableError,
  saveEventBrand,
  uploadEventFont,
  type BrandLogo,
  type BrandOutput,
} from '../api'
import { buildOverridePayload, overriddenSections, SECTIONS, type Section } from '../eventBrand'
import { fontFilename, isValidHexColor, LOGO_POSITIONS, LOGO_VARIANTS, outputReadyToSave } from '../brand'
import { BAND_FIELDS, COLOR_FIELDS, formFromBrand, OUTPUT_FIELDS, type BrandEditorForm } from '../brandForm'

/** The event editor's own local form shape - brandForm's shared
 * BrandEditorForm (four colors + two font roles + subtitles.enabled + logo +
 * output), but here it holds either the CHANNEL's values or the merged
 * EFFECTIVE values depending on which formFromBrand call built it (see
 * channelForm/effective below) - never a mix of the two within one instance,
 * so each is a plain, complete brand shape on its own. */
type SectionForm = BrandEditorForm

const LOGO_POSITION_LABELS: Record<(typeof LOGO_POSITIONS)[number], string> = {
  top: 'Badge — centered at top',
  'top-right': 'Watermark — top right',
  'bottom-right': 'Watermark — bottom right',
}

/** The whole value a section carries in the override payload (see
 * eventBrand.buildOverridePayload: each overridden section travels WHOLE,
 * never merged key-by-key on the client). `fonts` is the one section that
 * can be "incomplete" mid-edit (only one role chosen) - that state is
 * represented as `undefined` here so buildOverridePayload omits it rather
 * than sending a half-filled section the server would reject; Save itself
 * stays disabled meanwhile (see sectionReady). */
function sectionValue(form: SectionForm, section: Section): unknown {
  switch (section) {
    case 'colors':
      return form.colors
    case 'fonts':
      return form.fonts.hook && form.fonts.small
        ? { hook: form.fonts.hook, small: form.fonts.small }
        : undefined
    case 'logo':
      return form.logo
    case 'output':
      return form.output
    case 'subtitles':
      return { enabled: form.subtitlesEnabled }
    case 'bands':
      return form.bands
    default:
      return undefined
  }
}

function toEffectiveRecord(form: SectionForm): Record<string, unknown> {
  const record: Record<string, unknown> = {}
  for (const section of SECTIONS) record[section] = sectionValue(form, section)
  return record
}

/** Resets one section of `form` back to `channelForm`'s value for the same
 * section - what toggling a section's override OFF does (see eventBrand.ts's
 * own doc: an omitted section stays inherited, so the working value should
 * go back to matching the channel exactly, not linger at a stale edit). */
function withChannelSection(form: SectionForm, channelForm: SectionForm, section: Section): SectionForm {
  switch (section) {
    case 'colors':
      return { ...form, colors: channelForm.colors }
    case 'fonts':
      return { ...form, fonts: channelForm.fonts }
    case 'logo':
      return { ...form, logo: channelForm.logo }
    case 'output':
      return { ...form, output: channelForm.output }
    case 'subtitles':
      return { ...form, subtitlesEnabled: channelForm.subtitlesEnabled }
    case 'bands':
      return { ...form, bands: channelForm.bands }
    default:
      return form
  }
}

/** Whether a single overridden section is complete enough to save - mirrors
 * brand.ts's brandReadyToSave/outputReadyToSave but per-section, since here
 * only the OVERRIDDEN sections need to be valid (an inherited section is
 * always valid, because it came from the channel's own already-validated
 * brand.json). Logo and subtitles have no "incomplete" state of their own. */
function sectionReady(form: SectionForm, section: Section): boolean {
  switch (section) {
    case 'colors':
      return COLOR_FIELDS.every(({ key }) => isValidHexColor(form.colors[key] ?? ''))
    case 'fonts':
      return Boolean(form.fonts.hook) && Boolean(form.fonts.small)
    case 'output':
      return outputReadyToSave(form.output)
    case 'logo':
    case 'subtitles':
    case 'bands':
      return true
    default:
      return true
  }
}

function readyToSave(form: SectionForm, overridden: Set<Section>): boolean {
  for (const section of overridden) {
    if (!sectionReady(form, section)) return false
  }
  return true
}

function setsEqual(a: Set<Section>, b: Set<Section>): boolean {
  if (a.size !== b.size) return false
  for (const value of a) {
    if (!b.has(value)) return false
  }
  return true
}

/** The header every section shares: its title plus the inherit/override
 * toggle. A local, unexported component - fine alongside the file's single
 * exported component, same as BrandEditor keeps its own field-level helpers
 * unexported in one file. */
function SectionToggle({
  label,
  overridden,
  onChange,
}: {
  label: string
  overridden: boolean
  onChange: (overridden: boolean) => void
}) {
  return (
    <Group justify="space-between" wrap="nowrap">
      <Text fw={600} size="sm" tt="uppercase" c="dimmed">
        {label}
      </Text>
      <SegmentedControl
        size="xs"
        value={overridden ? 'override' : 'inherit'}
        onChange={(value) => onChange(value === 'override')}
        data={[
          { label: 'Inherit', value: 'inherit' },
          { label: 'Override', value: 'override' },
        ]}
      />
    </Group>
  )
}

/**
 * The event-level Brand & fonts editor (stage G-event-brand): per section
 * (colors/fonts/logo/output/subtitles), the operator picks between
 * inheriting the channel's brand.json wholesale or overriding it entirely
 * for this event. This heavily reuses BrandEditor's own field widgets and
 * debounced-preview/object-URL lifecycle - the difference is every field is
 * duplicated read-only (the channel's value, disabled) alongside its
 * editable twin (the event's own working value), and only the latter is
 * shown once a section is switched to "Override".
 *
 * `effective` holds the working values the editable fields are bound to -
 * seeded from EventBrandResponse.effective (the already-merged brand), so an
 * inherited section's working value already equals the channel's, and
 * flipping it to "Override" starts from something sane rather than blank.
 * `channelForm` is read-only reference data for the disabled fields; it never
 * changes from user input, only from a fresh load or a save response.
 */
export function EventBrandEditor({ channel, event }: { channel: string; event: string }) {
  const [effective, setEffective] = useState<SectionForm | null>(null)
  const [channelForm, setChannelForm] = useState<SectionForm | null>(null)
  const [savedEffective, setSavedEffective] = useState<SectionForm | null>(null)
  const [overridden, setOverridden] = useState<Set<Section>>(new Set())
  const [savedOverridden, setSavedOverridden] = useState<Set<Section>>(new Set())
  const [fontsChannel, setFontsChannel] = useState<string[]>([])
  const [fontsEvent, setFontsEvent] = useState<string[]>([])
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

  // Load the event's brand override + the channel's brand + both font lists
  // once (and again if the operator is ever shown a different event without
  // a full remount).
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    getEventBrand(channel, event)
      .then((response) => {
        if (cancelled) return
        const eff = formFromBrand(response.effective)
        const chan = formFromBrand(response.channel)
        const overr = overriddenSections(response.override) as Set<Section>
        setEffective(eff)
        setChannelForm(chan)
        setSavedEffective(eff)
        setOverridden(overr)
        setSavedOverridden(overr)
        setFontsChannel(response.fonts.channel)
        setFontsEvent(response.fonts.event)
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
  }, [channel, event])

  const [debouncedEffective] = useDebouncedValue(effective, 300)

  // The live preview: re-fetched debounced on any field edit, plus
  // immediately on any section's inherit/override flip (not itself
  // debounced - `overridden` only changes on a discrete click, so there is
  // nothing to coalesce) - against the event's SAVED brand with the
  // override payload applied server-side, exactly like BrandEditor's own
  // preview effect.
  useEffect(() => {
    if (!debouncedEffective) return
    let cancelled = false
    setPreviewLoading(true)
    eventBrandPreview(channel, event, buildOverridePayload(toEffectiveRecord(debouncedEffective), overridden))
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
  }, [channel, event, debouncedEffective, overridden])

  // Revoke the last object URL on unmount (a replace is already revoked
  // above, right before the new one is adopted).
  useEffect(
    () => () => {
      if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current)
    },
    [],
  )

  function setOverride(section: Section, override: boolean) {
    setOverridden((prev) => {
      const next = new Set(prev)
      if (override) next.add(section)
      else next.delete(section)
      return next
    })
    if (!override) {
      setEffective((prev) => (prev && channelForm ? withChannelSection(prev, channelForm, section) : prev))
    }
  }

  function setColor(key: string, value: string) {
    setEffective((prev) => (prev ? { ...prev, colors: { ...prev.colors, [key]: value } } : prev))
  }

  function setFontRole(role: 'hook' | 'small', value: string | null) {
    setEffective((prev) =>
      prev ? { ...prev, fonts: { ...prev.fonts, [role]: value ?? undefined } } : prev,
    )
  }

  function setSubtitlesEnabled(value: boolean) {
    setEffective((prev) => (prev ? { ...prev, subtitlesEnabled: value } : prev))
  }

  function setBand(key: 'top' | 'bottom', value: number) {
    setEffective((prev) => (prev ? { ...prev, bands: { ...prev.bands, [key]: value } } : prev))
  }

  function setLogoField<K extends keyof BrandLogo>(key: K, value: BrandLogo[K]) {
    setEffective((prev) =>
      prev && prev.logo ? { ...prev, logo: { ...prev.logo, [key]: value } } : prev,
    )
  }

  function removeLogo() {
    setEffective((prev) => (prev ? { ...prev, logo: null } : prev))
  }

  function setOutputField(key: keyof BrandOutput, value: number | string) {
    if (typeof value !== 'number') return
    setEffective((prev) => (prev ? { ...prev, output: { ...prev.output, [key]: value } } : prev))
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
      const { fonts } = await uploadEventFont(channel, event, filename, bytes)
      setFontsEvent(fonts)
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
      const { fonts } = await deleteEventFont(channel, event, name)
      setFontsEvent(fonts)
      const ref = `fonts/${name}`
      setEffective((prev) => {
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
    if (!effective || !dirty || !ready) return
    setSaving(true)
    setSaveError(null)
    try {
      const patch = buildOverridePayload(toEffectiveRecord(effective), overridden)
      const response = await saveEventBrand(channel, event, patch)
      const eff = formFromBrand(response.effective)
      const chan = formFromBrand(response.channel)
      const overr = overriddenSections(response.override) as Set<Section>
      setEffective(eff)
      setChannelForm(chan)
      setSavedEffective(eff)
      setOverridden(overr)
      setSavedOverridden(overr)
      setFontsChannel(response.fonts.channel)
      setFontsEvent(response.fonts.event)
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

  if (loadError || !effective || !channelForm) {
    return (
      <Alert color="red" title="Could not load brand">
        {loadError} - check that the studio server is still running, then reload this page.
      </Alert>
    )
  }

  const dirty =
    savedEffective !== null &&
    (JSON.stringify(effective) !== JSON.stringify(savedEffective) || !setsEqual(overridden, savedOverridden))
  const ready = readyToSave(effective, overridden)

  const fontOptionsMap = new Map<string, string>()
  for (const name of fontsChannel) fontOptionsMap.set(`fonts/${name}`, name)
  for (const name of fontsEvent) fontOptionsMap.set(`fonts/${name}`, `${name} (event)`)
  const fontOptions = Array.from(fontOptionsMap, ([value, label]) => ({ value, label }))

  const colorsOverridden = overridden.has('colors')
  const fontsOverridden = overridden.has('fonts')
  const logoOverridden = overridden.has('logo')
  const outputOverridden = overridden.has('output')
  const subtitlesOverridden = overridden.has('subtitles')
  const bandsOverridden = overridden.has('bands')

  const displayedLogo = logoOverridden ? effective.logo : channelForm.logo

  return (
    <Grid gap="lg">
      <Grid.Col span={{ base: 12, md: 7 }}>
        <Stack gap="lg">
          <Text size="xs" c="dimmed">
            Every section inherits the channel's brand unless overridden here. Overriding a
            section replaces it entirely for this event; switching back to "Inherit" discards
            the event's own value for that section.
          </Text>

          <Card padding="md">
            <Stack gap="sm">
              <SectionToggle
                label="Fonts"
                overridden={fontsOverridden}
                onChange={(value) => setOverride('fonts', value)}
              />
              <Group justify="space-between">
                <Text size="xs" c="dimmed" tt="uppercase">
                  Event fonts
                </Text>
                <FileButton resetRef={resetFileButton} onChange={handleUpload} accept=".ttf,.otf">
                  {(props) => (
                    <Button {...props} variant="light" color="steel" loading={uploading} size="xs">
                      Upload font
                    </Button>
                  )}
                </FileButton>
              </Group>

              {fontsEvent.length === 0 ? (
                <Text size="xs" c="dimmed">
                  No event-specific fonts uploaded yet. Channel fonts are available below.
                </Text>
              ) : (
                <Stack gap={4}>
                  {fontsEvent.map((name) => (
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
                disabled={!fontsOverridden}
                value={(fontsOverridden ? effective.fonts.hook : channelForm.fonts.hook) ?? null}
                onChange={(value) => setFontRole('hook', value)}
                allowDeselect={false}
              />
              <Select
                label="Small font"
                placeholder="Choose a font"
                data={fontOptions}
                disabled={!fontsOverridden}
                value={(fontsOverridden ? effective.fonts.small : channelForm.fonts.small) ?? null}
                onChange={(value) => setFontRole('small', value)}
                allowDeselect={false}
              />
              {fontsOverridden && !sectionReady(effective, 'fonts') ? (
                <Text size="xs" c="red">
                  Both a hook and small font must be assigned.
                </Text>
              ) : null}
            </Stack>
          </Card>

          <Card padding="md">
            <Stack gap="sm">
              <SectionToggle
                label="Colors"
                overridden={colorsOverridden}
                onChange={(value) => setOverride('colors', value)}
              />
              {COLOR_FIELDS.map(({ key, label }) => (
                <ColorInput
                  key={key}
                  label={label}
                  format="hex"
                  disabled={!colorsOverridden}
                  value={(colorsOverridden ? effective.colors[key] : channelForm.colors[key]) ?? ''}
                  onChange={(value) => setColor(key, value)}
                />
              ))}
              {colorsOverridden && !sectionReady(effective, 'colors') ? (
                <Text size="xs" c="red">
                  Every color must be a valid hex value.
                </Text>
              ) : null}
            </Stack>
          </Card>

          <Card padding="md">
            <Stack gap="sm">
              <SectionToggle
                label="Subtitles"
                overridden={subtitlesOverridden}
                onChange={(value) => setOverride('subtitles', value)}
              />
              <Switch
                label="Enable subtitles"
                checked={subtitlesOverridden ? effective.subtitlesEnabled : channelForm.subtitlesEnabled}
                disabled={!subtitlesOverridden}
                onChange={(e) => setSubtitlesEnabled(e.currentTarget.checked)}
                color="steel"
              />
            </Stack>
          </Card>

          <Card padding="md">
            <Stack gap="sm">
              <SectionToggle
                label="Band opacity"
                overridden={bandsOverridden}
                onChange={(value) => setOverride('bands', value)}
              />
              <Text size="xs" c="dimmed">
                How solid this event's upper and lower thirds are. At 0% only the clip's own
                blurred backdrop shows there — the hook, footer and logo stay.
              </Text>
              {BAND_FIELDS.map(({ key, label }) => (
                <Stack key={key} gap={2}>
                  <Text size="sm">{label}</Text>
                  <Slider
                    min={0}
                    max={1}
                    step={0.05}
                    disabled={!bandsOverridden}
                    value={bandsOverridden ? effective.bands[key] : channelForm.bands[key]}
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
            <Stack gap="sm">
              <SectionToggle
                label="Logo"
                overridden={logoOverridden}
                onChange={(value) => setOverride('logo', value)}
              />
              {logoOverridden && displayedLogo ? (
                <Group justify="flex-end">
                  <Button variant="subtle" color="red" size="xs" onClick={removeLogo}>
                    Remove logo
                  </Button>
                </Group>
              ) : null}

              {displayedLogo ? (
                <>
                  <div>
                    <Text size="sm" mb={4}>
                      Variant
                    </Text>
                    <SegmentedControl
                      fullWidth
                      size="xs"
                      disabled={!logoOverridden}
                      value={displayedLogo.variant ?? 'color'}
                      onChange={(value) => setLogoField('variant', value as BrandLogo['variant'])}
                      data={LOGO_VARIANTS.map((variant) => ({
                        label: variant.charAt(0).toUpperCase() + variant.slice(1),
                        value: variant,
                      }))}
                    />
                  </div>
                  <Select
                    label="Placement"
                    allowDeselect={false}
                    disabled={!logoOverridden}
                    value={displayedLogo.position ?? 'top'}
                    onChange={(value) => value && setLogoField('position', value as BrandLogo['position'])}
                    data={LOGO_POSITIONS.map((position) => ({
                      value: position,
                      label: LOGO_POSITION_LABELS[position],
                    }))}
                  />
                  {displayedLogo.position && displayedLogo.position !== 'top' ? (
                    <div>
                      <Group justify="space-between">
                        <Text size="sm">Opacity</Text>
                        <Text size="xs" c="dimmed" className="tnum">
                          {Math.round((displayedLogo.opacity ?? 0.72) * 100)}%
                        </Text>
                      </Group>
                      <Slider
                        min={0.2}
                        max={1}
                        step={0.02}
                        color="steel"
                        disabled={!logoOverridden}
                        value={displayedLogo.opacity ?? 0.72}
                        onChange={(value) => setLogoField('opacity', value)}
                        label={(value) => `${Math.round(value * 100)}%`}
                      />
                    </div>
                  ) : null}
                </>
              ) : (
                <Text size="xs" c="dimmed">
                  {logoOverridden
                    ? "This event's logo is set to none — it renders with no logo even if the channel adds one later."
                    : "This channel has no logo image yet."}
                </Text>
              )}
            </Stack>
          </Card>

          <Card padding="md">
            <Stack gap="sm">
              <SectionToggle
                label="Output geometry"
                overridden={outputOverridden}
                onChange={(value) => setOverride('output', value)}
              />
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
                      disabled={!outputOverridden}
                      value={(outputOverridden ? effective.output[key] : channelForm.output[key]) ?? 0}
                      onChange={(value) => setOutputField(key, value)}
                    />
                  </Grid.Col>
                ))}
              </Grid>
              {outputOverridden && !sectionReady(effective, 'output') ? (
                <Text size="xs" c="red">
                  The video window must be positive and fit inside the frame.
                </Text>
              ) : null}
            </Stack>
          </Card>

          {saveError ? (
            <Alert color="red" title="Could not save brand">
              {saveError}
            </Alert>
          ) : null}

          <Group justify="flex-end">
            <Button color="steel" onClick={handleSave} loading={saving} disabled={!dirty || !ready}>
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
            Reflects the hook and footer rendered with this event's current overrides merged over
            the channel's brand - nothing is saved until you click Save brand.
          </Text>
        </Stack>
      </Grid.Col>
    </Grid>
  )
}
