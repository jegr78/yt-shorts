import { useEffect, useState } from 'react'
import {
  Alert,
  Anchor,
  Badge,
  Box,
  Button,
  Checkbox,
  Code,
  Group,
  Loader,
  Progress,
  ScrollArea,
  SegmentedControl,
  Select,
  Stack,
  Switch,
  Text,
  Textarea,
  TextInput,
  Modal,
} from '@mantine/core'
import { notifications } from '@mantine/notifications'
import type { ClipDetail, Job, UploadPreview } from '../api'
import { ApiError, getUploadPreview, patchClip, safeExternalUrl } from '../api'
import { routePath } from '../scopedApi'
import { fromPair, isPending, trimNeedsAction } from '../trim'
import { navigate } from '../useRoute'
import {
  CATEGORIES,
  DESCRIPTION_MAX,
  TAGS_MAX,
  isFutureLocal,
  metadataFieldsValid,
  needsConfirm,
  parseTags,
  tagsEqual,
  tagsToInput,
  toRfc3339,
} from '../uploadMeta'

export interface UploadRecord {
  url: string
}

/** The four mutually-exclusive choices the confirm modal's own
 * SegmentedControl offers. Only the first three are `Visibility`
 * (uploadMeta.VISIBILITIES) values on their own - "scheduled" is a UI-only
 * fourth option that maps to `visibility: "private"` plus a `publishAt`
 * (see `youtube_upload.build_metadata`: a scheduled publish time is only
 * ever valid alongside private visibility; YouTube itself flips the video
 * to public automatically once `publishAt` passes). */
type VisibilityChoice = 'private' | 'unlisted' | 'public' | 'scheduled'

const VISIBILITY_DATA: { value: VisibilityChoice; label: string }[] = [
  { value: 'private', label: 'Private' },
  { value: 'unlisted', label: 'Unlisted' },
  { value: 'public', label: 'Public' },
  { value: 'scheduled', label: 'Scheduled' },
]

const CATEGORY_DATA = CATEGORIES.map((category) => ({ value: category.id, label: category.label }))

/** What confirming the upload actually sends - see App.tsx's
 * `handleStartUpload`, which is the only caller and is what converts this
 * into api.ts's `UploadOptions` (snake_case `publish_at`). Kept as its own
 * type (not `UploadOptions` itself) so this component never needs to know
 * the wire's field naming. */
export interface StartUploadOptions {
  visibility: string
  publishAt: string | null
  confirm: boolean
  force: boolean
}

interface UploadPanelProps {
  clip: ClipDetail
  /** The upload job THIS panel's own clip is currently tracking, or null
   * when no job for this clip is in flight/finished this render (see
   * App.tsx: a single upload job id is tracked globally, the same way
   * render/detect are, and only surfaced here when it actually belongs to
   * this clip - otherwise switching to an unrelated clip while an upload
   * runs would wrongly show that clip as uploading too). */
  job: Job | null
  jobStarting: boolean
  /** What THIS session has itself watched a job finish and report for
   * this clip - used only as an immediate, optimistic fallback for the
   * instant a job completes and before the parent's re-fetch of
   * GET /api/clips lands (see App.tsx's uploadJob effect). The
   * SERVER-AUTHORITATIVE state is `clip.has_upload`/`clip.upload_url` -
   * see the module docstring below. */
  uploadedRecord: UploadRecord | null
  remainingUploads: number | null
  authConnected: boolean | null
  /** A human label for whatever OTHER job currently holds this event's
   * lock ("A render", "Moment detection") - see studio/jobs.py's
   * EventLock, shared by render/detect/upload, so only one ever runs at
   * once. Null means nothing else is blocking an upload right now. */
  blockedBy: string | null
  onStartUpload: (name: string, opts: StartUploadOptions) => void
}

/**
 * The upload action for ONE selected clip - rendered only for a clip that
 * is `kept` AND has a rendered short (`has_short`); every other clip
 * (candidate, discarded, or kept-but-not-rendered) shows nothing at all,
 * because the backend 409s exactly those cases (see api.py's post_upload)
 * and this panel should not offer an action the server will refuse.
 *
 * Confirmation happens in a Modal that shows what is honestly known
 * client-side before the request fires: the effective title (the exact
 * hook that will be sent) is shown immediately from `clip` itself. Privacy
 * defaults to "Private" - the safe default `youtube_upload.build_metadata`
 * itself defaults to - but is a per-upload choice here (Private/Unlisted/
 * Public/Scheduled); anything other than an immediate private upload
 * requires an explicit confirmation checkbox, gated by `needsConfirm`
 * (uploadMeta.ts), mirroring the server's own confirm-required guard (see
 * api.py's post_upload). The moment the modal OPENS it also fetches
 * `GET /api/clips/{name}/upload-preview` (see api.ts's `getUploadPreview`)
 * - the exact description/tags/category/made-for-kids `build_metadata`
 * would compute for this clip right now - both to show the real payload
 * and to seed the editable metadata fields below it (Description/Tags/
 * Category/Made for kids), which PATCH back a per-clip override via
 * "Save metadata" independently of starting an upload. That fetch never
 * blocks the modal: the title is shown immediately from `clip` itself, and
 * a failed preview fetch only loses the extra detail (and the ability to
 * edit metadata for this open), shown as a small inline error, not the
 * ability to confirm the upload itself.
 *
 * Upload state is SERVER-AUTHORITATIVE and persists across a reload:
 * `GET /api/clips`/`GET /api/clips/{name}` carry `has_upload` and
 * `upload_url` straight from `upload.json` (see api.py's `_summary`), so
 * "uploaded" here reflects what is actually on disk, not just what THIS
 * browser session has watched a job complete for. `uploadedRecord` (the
 * session-only job result, see the prop's own docstring) is used only as
 * an immediate display for the moment between a job finishing and the
 * parent's next `GET /api/clips` landing - `clip.has_upload`/
 * `clip.upload_url` is the fallback-of-record either way. The backend's
 * own re-upload guard (a 409 without `?force=true`) is what actually
 * prevents an accidental double upload, regardless of what either of
 * these remembers client-side - see api.py's post_upload and
 * upload_record.is_uploaded.
 */
export function UploadPanel({
  clip,
  job,
  jobStarting,
  uploadedRecord,
  remainingUploads,
  authConnected,
  blockedBy,
  onStartUpload,
}: UploadPanelProps) {
  const [modalOpen, setModalOpen] = useState(false)
  const [reuploadAck, setReuploadAck] = useState(false)
  const [preview, setPreview] = useState<UploadPreview | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)

  // Visibility/schedule - per-upload choices, reset to the safe default
  // (private, unscheduled) each time the modal opens (see openModal below),
  // never carried over from a previous upload of a different clip.
  const [visibilityChoice, setVisibilityChoice] = useState<VisibilityChoice>('private')
  const [scheduledAt, setScheduledAt] = useState('')
  const [confirmAck, setConfirmAck] = useState(false)

  // Editable per-clip upload metadata - seeded from `preview` once it loads
  // (see the effect below) and PATCHed independently of the upload itself
  // via "Save metadata" (handleSaveMetadata), which is why these are not
  // reset in openModal the way the visibility choices are: they track the
  // fetched preview, not the upload action.
  const [localDescription, setLocalDescription] = useState('')
  const [localTagsInput, setLocalTagsInput] = useState('')
  const [localCategory, setLocalCategory] = useState('')
  const [localMadeForKids, setLocalMadeForKids] = useState(false)
  const [metadataSaving, setMetadataSaving] = useState(false)

  // Fetches the real metadata each time the modal opens for this clip -
  // keyed on clip.name too, so switching the selected clip while a stale
  // effect is still in flight cannot land a PREVIOUS clip's description
  // under this one's title (the cleanup flips `cancelled` on either the
  // modal closing or the clip changing). Never fires while the modal is
  // closed - this is purely for the confirmation, not prefetched for
  // every clip in the tower. Also seeds the editable metadata fields from
  // whatever this fetch returns - not on every render of `preview`, only
  // on an actual fetch landing, so a field the operator is mid-typing is
  // never clobbered by anything except a fetch that just completed.
  useEffect(() => {
    if (!modalOpen) return
    let cancelled = false
    setPreview(null)
    setPreviewError(null)
    setPreviewLoading(true)
    getUploadPreview(clip.name)
      .then((data) => {
        if (cancelled) return
        setPreview(data)
        setLocalDescription(data.description)
        setLocalTagsInput(tagsToInput(data.tags))
        setLocalCategory(data.category_id)
        setLocalMadeForKids(data.made_for_kids)
        setPreviewLoading(false)
      })
      .catch((error: unknown) => {
        if (cancelled) return
        setPreviewError(
          error instanceof ApiError ? error.message : 'Could not load the upload preview.',
        )
        setPreviewLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [modalOpen, clip.name])

  if (clip.status !== 'kept' || !clip.has_short) return null

  const running = job?.status === 'running'
  const failed = job?.status === 'failed'
  const result = job?.results[clip.name]
  // Server-authoritative first (see the module docstring): a page reload
  // has no `uploadedRecord` at all, only `clip.has_upload`/`upload_url`
  // from disk, and this must still show "uploaded" correctly.
  const uploaded = clip.has_upload || uploadedRecord !== null
  const uploadedUrl = uploadedRecord?.url ?? clip.upload_url
  // Only ever render an http(s) URL as a link; a tampered value with another
  // scheme is shown as plain text, never as an href (see safeExternalUrl).
  const uploadedHref = safeExternalUrl(uploadedUrl)
  const isReupload = uploaded
  // The server never even offers an upload while a trim is pending OR
  // unknown (the deliverable it would send might not be the file the
  // operator thinks it is - see trim.py's is_pending/is_unknown) - see
  // ManualUploadPanel's own guard on the download link for the same
  // underlying reason.
  const trimPending = trimNeedsAction(
    isPending(fromPair(clip.trim), fromPair(clip.trim_applied)), clip.trim_unknown)
  const disabled = running || jobStarting || Boolean(blockedBy) || trimPending

  // A scheduled upload is only ever valid as private + publishAt (see
  // youtube_upload.build_metadata) - YouTube itself flips it to public once
  // that time passes, which is why the modal's own copy says "becomes
  // public at this time" rather than offering a separate "scheduled
  // visibility". Every other choice maps straight onto a Visibility.
  const scheduled = visibilityChoice === 'scheduled'
  const effectiveVisibility = scheduled ? 'private' : visibilityChoice
  const confirmRequired = needsConfirm(effectiveVisibility, scheduled)
  const tags = parseTags(localTagsInput)
  const tagsCombinedLength = tags.reduce((sum, tag) => sum + tag.length, 0)
  const descriptionOverLimit = localDescription.length > DESCRIPTION_MAX
  const tagsOverLimit = tagsCombinedLength > TAGS_MAX
  // Gates ONLY the "Save metadata" button - it must track exactly what that
  // PATCH sends (description/tags), never the title, so an over-long hook
  // title (a real, pre-existing condition unrelated to this save) can never
  // permanently block saving a valid description/tags fix. See
  // metadataFieldsValid's own docstring.
  const metadataFieldsOk = metadataFieldsValid({ description: localDescription, tags })
  // Whether the editable fields differ from what "Save metadata" last wrote
  // (i.e. what `preview` reflects) - "Confirm and upload" builds its payload
  // from the SERVER's stored metadata, not from these local fields, so a
  // divergence here means an upload right now would silently use the OLD
  // metadata. Only meaningful once a preview has actually loaded - before
  // that, an empty/seeding local state is not an "edit". Tags compared
  // order-insensitively (tagsEqual) so retyping the same tags in a
  // different order never reads as dirty.
  const metadataDirty =
    preview !== null &&
    (localDescription !== preview.description ||
      !tagsEqual(tags, preview.tags) ||
      localCategory !== preview.category_id ||
      localMadeForKids !== preview.made_for_kids)
  const confirmDisabled =
    (isReupload && !reuploadAck) ||
    (confirmRequired && !confirmAck) ||
    (scheduled && (!scheduledAt || !isFutureLocal(scheduledAt)))

  function openModal() {
    setReuploadAck(false)
    setVisibilityChoice('private')
    setScheduledAt('')
    setConfirmAck(false)
    setModalOpen(true)
  }

  function confirm() {
    setModalOpen(false)
    onStartUpload(clip.name, {
      visibility: effectiveVisibility,
      publishAt: scheduled ? toRfc3339(scheduledAt) : null,
      confirm: confirmRequired,
      force: isReupload,
    })
  }

  async function handleSaveMetadata() {
    setMetadataSaving(true)
    try {
      await patchClip(clip.name, {
        upload: {
          description: localDescription,
          tags,
          category_id: localCategory,
          made_for_kids: localMadeForKids,
        },
      })
      const refreshed = await getUploadPreview(clip.name)
      setPreview(refreshed)
      setLocalDescription(refreshed.description)
      setLocalTagsInput(tagsToInput(refreshed.tags))
      setLocalCategory(refreshed.category_id)
      setLocalMadeForKids(refreshed.made_for_kids)
      notifications.show({ message: 'Upload metadata saved.', color: 'green' })
    } catch (error) {
      notifications.show({
        title: 'Could not save metadata',
        message: error instanceof ApiError ? error.message : String(error),
        color: 'red',
      })
    } finally {
      setMetadataSaving(false)
    }
  }

  return (
    <Box pt="sm" style={{ borderTop: '1px solid var(--mantine-color-dark-6)' }}>
      <Stack gap="xs">
        <Group justify="space-between" wrap="wrap" gap="xs">
          <Group gap="xs" wrap="nowrap">
            <Text fw={600} size="xs" tt="uppercase" c="dimmed">
              Upload
            </Text>
            <Button
              size="xs"
              color="steel"
              variant={isReupload ? 'default' : 'light'}
              disabled={disabled}
              loading={running || jobStarting}
              onClick={openModal}
            >
              {isReupload ? 'Upload again' : 'Upload to YouTube'}
            </Button>
            {remainingUploads !== null && (
              <Text
                size="xs"
                c="dimmed"
                fw={remainingUploads <= 1 ? 700 : 400}
                ff="monospace"
                className="tnum"
              >
                {remainingUploads} left today
              </Text>
            )}
          </Group>
          {job && (
            <Group gap="xs" wrap="nowrap">
              <Badge
                color={running ? 'steel' : job.status === 'done' ? 'green' : 'red'}
                variant="dot"
                size="xs"
              >
                {job.status}
              </Badge>
              {job.log_name && (
                <Anchor
                  size="xs"
                  c="steel.3"
                  onClick={() => navigate(`${routePath({ screen: 'logs' })}?file=${encodeURIComponent(job.log_name as string)}`)}
                >
                  View log
                </Anchor>
              )}
            </Group>
          )}
        </Group>

        {blockedBy && !running && (
          <Text size="xs" c="dimmed">
            {blockedBy} is running for this event - upload will be available once it finishes.
          </Text>
        )}

        {trimPending && !running && (
          <Text size="xs" c="dimmed">
            {clip.trim_unknown
              ? "This clip's applied trim state is unknown - repair it above before uploading."
              : 'A trim is pending for this clip - apply it above before uploading.'}
          </Text>
        )}

        {running && <Progress value={100} animated color="steel" size="sm" />}

        {uploaded && uploadedUrl && !running && (
          <Alert color="green" variant="light" title="Uploaded">
            {/* Mantine's default Anchor colour measured ~4.5:1 here - right
                at this project's body-text floor, not comfortably past it.
                steel.2 is the same interactive accent used everywhere else
                in the studio, at a brighter shade for a real margin. */}
            {uploadedHref ? (
              <Anchor
                href={uploadedHref}
                target="_blank"
                rel="noopener noreferrer"
                size="sm"
                c="steel.2"
              >
                {uploadedHref}
              </Anchor>
            ) : (
              <Text size="sm">{uploadedUrl}</Text>
            )}
            <Text size="xs" c="dimmed" mt={4}>
              Uploaded with whatever visibility/schedule was chosen when this upload was
              started - manage or change it further any time from YouTube Studio.
            </Text>
          </Alert>
        )}

        {failed && result && (
          <Alert color="red" variant="light" title="Upload failed">
            {result.reason ?? 'See the job log for details.'}
          </Alert>
        )}
      </Stack>

      <Modal
        opened={modalOpen}
        onClose={() => setModalOpen(false)}
        title={isReupload ? 'Upload again' : 'Upload this short to YouTube'}
      >
        <Stack gap="sm">
          {isReupload && uploadedUrl && (
            <Alert color="steel" variant="light" title="Already uploaded">
              <Text size="sm">
                This clip was uploaded earlier:{' '}
                {uploadedHref ? (
                  <Anchor href={uploadedHref} target="_blank" rel="noopener noreferrer" c="steel.2">
                    {uploadedHref}
                  </Anchor>
                ) : (
                  uploadedUrl
                )}
                . Uploading again creates a SECOND, separate video on the channel and spends
                today&apos;s quota again.
              </Text>
            </Alert>
          )}

          <Stack gap={2}>
            <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
              Title
            </Text>
            <Text size="sm">{clip.effective_title}</Text>
          </Stack>

          <Stack gap={4}>
            <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
              Visibility
            </Text>
            <SegmentedControl
              size="xs"
              fullWidth
              value={visibilityChoice}
              onChange={(value) => setVisibilityChoice(value as VisibilityChoice)}
              data={VISIBILITY_DATA}
            />
            {visibilityChoice === 'private' ? (
              <Text size="xs" c="dimmed">
                Private - only you (and anyone you share the link with) can see it.
              </Text>
            ) : scheduled ? (
              <>
                <TextInput
                  type="datetime-local"
                  label="Publish at"
                  size="xs"
                  value={scheduledAt}
                  min={new Date().toISOString().slice(0, 16)}
                  onChange={(event) => setScheduledAt(event.currentTarget.value)}
                />
                <Text size="xs" c="dimmed">
                  Uploaded private now - YouTube itself makes it{' '}
                  <Text component="span" fw={700} c="dimmed">
                    public
                  </Text>{' '}
                  at this time.
                </Text>
              </>
            ) : (
              <Text size="xs" c="dimmed">
                {visibilityChoice === 'unlisted'
                  ? 'Unlisted - viewable by anyone with the link, not shown in search or your channel.'
                  : 'Public - visible to anyone on YouTube immediately.'}
              </Text>
            )}
          </Stack>

          <Stack gap={2}>
            <Group justify="space-between" align="center">
              <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
                Description
              </Text>
              {descriptionOverLimit && (
                <Badge color="red" variant="light" size="xs">
                  Over YouTube&apos;s limit
                </Badge>
              )}
            </Group>
            {previewLoading && (
              <Group gap={6}>
                <Loader size={12} color="steel" />
                <Text size="xs" c="dimmed">
                  Loading the generated description…
                </Text>
              </Group>
            )}
            {preview && !previewLoading && (
              <ScrollArea.Autosize mah={160} type="auto" offsetScrollbars>
                <Textarea
                  autosize
                  minRows={3}
                  value={localDescription}
                  onChange={(event) => setLocalDescription(event.currentTarget.value)}
                  description={`${localDescription.length} / ${DESCRIPTION_MAX} characters`}
                />
              </ScrollArea.Autosize>
            )}
            {previewError && !previewLoading && (
              <Text size="xs" c="dimmed">
                Could not load the description and tags ({previewError}) - the title above is
                still accurate, so you can still confirm.
              </Text>
            )}
          </Stack>

          {preview && !previewLoading && (
            <Stack gap={4}>
              <Group justify="space-between" align="center">
                <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
                  Tags
                </Text>
                {tagsOverLimit && (
                  <Badge color="red" variant="light" size="xs">
                    Over YouTube&apos;s limit
                  </Badge>
                )}
              </Group>
              <TextInput
                value={localTagsInput}
                onChange={(event) => setLocalTagsInput(event.currentTarget.value)}
                placeholder="comma or newline separated"
                description={`${tagsCombinedLength} / ${TAGS_MAX} characters combined (${tags.length} tag${tags.length === 1 ? '' : 's'})`}
              />
            </Stack>
          )}

          {preview && !previewLoading && (
            <Group gap="xl" align="flex-end">
              <Select
                label="Category"
                size="xs"
                data={CATEGORY_DATA}
                value={localCategory}
                onChange={(value) => value && setLocalCategory(value)}
                allowDeselect={false}
              />
              <Switch
                label="Made for kids"
                checked={localMadeForKids}
                onChange={(event) => setLocalMadeForKids(event.currentTarget.checked)}
              />
            </Group>
          )}

          {preview && !previewLoading && (
            <Group>
              <Button
                size="xs"
                variant="default"
                loading={metadataSaving}
                disabled={!metadataFieldsOk}
                onClick={handleSaveMetadata}
              >
                Save metadata
              </Button>
              <Text size="xs" c="dimmed">
                Saves the description/tags/category/made-for-kids above for this clip's uploads.
              </Text>
            </Group>
          )}

          {authConnected === false && (
            <Alert color="steel" variant="light" title="This channel may not be connected">
              If the upload fails for that reason, run <Code>bin/yt-shorts auth &lt;channel&gt;</Code>{' '}
              in a terminal first, then try again.
            </Alert>
          )}

          {remainingUploads !== null && (
            <Text size="xs" c="dimmed">
              {remainingUploads > 0
                ? `${remainingUploads} upload${remainingUploads === 1 ? '' : 's'} left in today's local estimate.`
                : "Today's local quota estimate is used up - YouTube itself is the actual authority, so this may still succeed, but it may also fail with a quota error."}
            </Text>
          )}

          {isReupload && (
            <Checkbox
              checked={reuploadAck}
              onChange={(event) => setReuploadAck(event.currentTarget.checked)}
              label="I understand this uploads a second, separate copy."
            />
          )}

          {/* Composes WITH the reupload-ack above rather than replacing it -
              a re-upload of an already-public clip needs BOTH boxes ticked,
              since each names a different, independent risk (a duplicate
              video vs. an exposure change). See needsConfirm in
              uploadMeta.ts for the exact rule this mirrors. */}
          {confirmRequired && (
            <Checkbox
              checked={confirmAck}
              onChange={(event) => setConfirmAck(event.currentTarget.checked)}
              label={
                scheduled
                  ? 'I understand this upload will be scheduled to go public automatically.'
                  : `I understand this upload will be ${visibilityChoice}.`
              }
            />
          )}

          {metadataDirty && (
            <Alert color="yellow" variant="light" title="Unsaved metadata changes">
              <Text size="sm">
                Unsaved metadata changes won&apos;t be included in this upload - Save metadata
                first.
              </Text>
            </Alert>
          )}

          <Group justify="flex-end">
            <Button variant="default" onClick={() => setModalOpen(false)}>
              Cancel
            </Button>
            <Button color="steel" disabled={confirmDisabled} onClick={confirm}>
              Confirm and upload
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Box>
  )
}
