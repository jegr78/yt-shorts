import { useEffect, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  CopyButton,
  Group,
  Stack,
  Text,
  Textarea,
  TextInput,
} from '@mantine/core'
import type { ClipDetail, UploadPreview } from '../api'
import { ApiError, getUploadPreview, shortUrl } from '../api'
import { fromPair, isPending, trimNeedsAction } from '../trim'
import { composeCopyAll, formatTagsForCopy } from '../upload'

interface ManualUploadPanelProps {
  clip: ClipDetail
}

function CopyField({ label, value, multiline = false }: {
  label: string; value: string; multiline?: boolean
}) {
  return (
    <Box>
      <Group justify="space-between" mb={2}>
        <Text size="xs" fw={600} tt="uppercase" c="dimmed">{label}</Text>
        <CopyButton value={value}>
          {({ copied, copy }) => (
            <Button size="compact-xs" variant="subtle" color="steel" onClick={copy}>
              {copied ? 'Copied' : 'Copy'}
            </Button>
          )}
        </CopyButton>
      </Group>
      {multiline
        ? <Textarea value={value} readOnly autosize minRows={2} maxRows={6} />
        : <TextInput value={value} readOnly />}
    </Box>
  )
}

/**
 * The manual-upload panel for a render-only channel (upload.mode=manual - a
 * YouTube manager/editor channel the Data API cannot upload to). Rendered
 * instead of UploadPanel (App.tsx chooses on auth.upload_mode). It never
 * offers a connect or an API upload - the backend 409s both (see
 * upload_policy) - and instead lets the operator download the rendered short
 * and copy the prepared metadata into YouTube Studio by hand.
 *
 * Gated exactly like UploadPanel: only a `kept` clip with a rendered short
 * (`has_short`) shows anything. Privacy and "made for kids" are shown as a
 * note, not as copy values: privacy is always "private" on the API path and
 * both are toggles the operator sets in YouTube Studio for a manual upload.
 */
export function ManualUploadPanel({ clip }: ManualUploadPanelProps) {
  const [preview, setPreview] = useState<UploadPreview | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setPreview(null)
    setError(null)
    if (clip.status !== 'kept' || !clip.has_short) return
    getUploadPreview(clip.name)
      .then((p) => { if (!cancelled) setPreview(p) })
      .catch((e) => { if (!cancelled) setError(e instanceof ApiError ? e.message : String(e)) })
    return () => { cancelled = true }
  }, [clip.name, clip.status, clip.has_short])

  if (clip.status !== 'kept' || !clip.has_short) return null

  // The server refuses the download form (as=download) while a trim is
  // pending OR unknown - see api.py's get_short and trim.is_pending, which
  // now also refuses when short.mp4's cut state cannot be trusted at all
  // (ClipSummary.trim_unknown - a crash between a cut landing and its state
  // being recorded, or a corrupted/deleted sidecar; see trim.py's
  // is_unknown). Disabling the link here (and never even building the href)
  // surfaces that BEFORE the click instead of after a 409.
  const trimPending = trimNeedsAction(
    isPending(fromPair(clip.trim), fromPair(clip.trim_applied)), clip.trim_unknown)

  return (
    <Box pt="sm" style={{ borderTop: '1px solid var(--mantine-color-dark-6)' }}>
      <Stack gap="xs">
        <Group justify="space-between" wrap="wrap" gap="xs">
          <Text fw={600} size="xs" tt="uppercase" c="dimmed">Manual upload</Text>
          <Button
            component="a"
            href={trimPending ? undefined : shortUrl(clip.name, clip.short_version, 'download')}
            download
            disabled={trimPending}
            size="xs"
            color="steel"
            variant="light"
          >
            Download short
          </Button>
        </Group>
        {trimPending && (
          <Text size="xs" c="dimmed">
            {clip.trim_unknown
              ? "This clip's applied trim state is unknown - repair it in the editor above "
                + 'before downloading.'
              : 'A trim is pending for this clip - apply it in the editor above before '
                + 'downloading.'}
          </Text>
        )}
        <Text size="xs" c="dimmed">
          This is a manager/editor channel - upload the downloaded short in YouTube Studio
          and paste the fields below. Set visibility and "made for kids" there yourself.
        </Text>
        {error && <Alert color="red" variant="light" title="Metadata unavailable">{error}</Alert>}
        {preview && (
          <Stack gap="xs">
            <CopyField label="Title" value={preview.title} />
            <CopyField label="Description" value={preview.description} multiline />
            <CopyField label="Tags" value={formatTagsForCopy(preview.tags)} />
            <Text size="xs" c="dimmed">Category id: {preview.category_id}</Text>
            <CopyButton value={composeCopyAll(preview)}>
              {({ copied, copy }) => (
                <Button size="xs" variant="default" onClick={copy}>
                  {copied ? 'Copied all' : 'Copy all'}
                </Button>
              )}
            </CopyButton>
          </Stack>
        )}
      </Stack>
    </Box>
  )
}
