import { useCallback, useEffect, useState } from 'react'
import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Card,
  Center,
  Group,
  Loader,
  Menu,
  Modal,
  Stack,
  Text,
  TextInput,
  UnstyledButton,
} from '@mantine/core'
import {
  ApiError,
  createChannel,
  deleteChannel,
  getChannels,
  renameChannel,
  updateChannel,
  type ChannelInfo,
} from '../api'
import { deleteConfirmed } from '../eventAdmin'
import { isValidSlug, MAX_SLUG_LENGTH } from '../slug'
import { routePath } from '../scopedApi'
import { navigate } from '../useRoute'
import { NavScreen } from './NavScreen'

/** The six required channel.json identity fields (see
 * yt_shorts.channel_admin.REQUIRED_FIELDS). Kept as a typed record so the New
 * and Edit dialogs share one shape. */
interface Fields {
  id: string
  channel_url: string
  handle: string
  display_name: string
  language: string
  footer: string
}

const EMPTY_FIELDS: Fields = {
  id: '',
  channel_url: '',
  handle: '',
  display_name: '',
  language: '',
  footer: '',
}

const FIELD_META: { key: keyof Fields; label: string; placeholder: string }[] = [
  { key: 'id', label: 'YouTube channel ID', placeholder: 'UCxxxxxxxxxxxxxxxxxxxxxx' },
  {
    key: 'channel_url',
    label: 'Channel URL',
    placeholder: 'https://www.youtube.com/channel/UC…',
  },
  { key: 'handle', label: 'Handle', placeholder: '@yourchannel' },
  { key: 'display_name', label: 'Display name', placeholder: 'Your Channel Name' },
  { key: 'language', label: 'Language', placeholder: 'en' },
  { key: 'footer', label: 'Footer', placeholder: 'NAME | @handle' },
]

const SLUG_ERROR = `Use letters, digits, '.', '-', '_'; no slashes, no leading dot, max ${MAX_SLUG_LENGTH} chars.`

function channelUrlFor(id: string): string {
  return `https://www.youtube.com/channel/${id}`
}

/** The start screen: every channel in the workspace (GET /api/channels).
 * A channel whose channel.json is missing/malformed is listed with its
 * error and is NOT openable (see workspace_listing.list_channels) - the
 * operator still sees it exists, and why it cannot be opened; a broken
 * channel can still be Deleted from its ⋯ menu so it can be removed.
 *
 * Stage G3a adds the write controls: a "New channel" button and, per row, a
 * ⋯ menu with Edit / Rename / Delete - each a dialog over the
 * POST/PATCH/DELETE routes (see api.ts, over yt_shorts.channel_admin). The
 * slug rule and the delete typed-confirmation gate are the pure helpers in
 * slug.ts / eventAdmin.ts. */
export function ChannelsScreen() {
  const [channels, setChannels] = useState<ChannelInfo[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [newOpen, setNewOpen] = useState(false)
  const [editTarget, setEditTarget] = useState<ChannelInfo | null>(null)
  const [renameTarget, setRenameTarget] = useState<ChannelInfo | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<ChannelInfo | null>(null)

  const refresh = useCallback(() => {
    return getChannels()
      .then((list) => {
        setChannels(list)
        setError(null)
      })
      .catch((err) => {
        setError(err instanceof ApiError ? err.message : String(err))
      })
  }, [])

  useEffect(() => {
    let cancelled = false
    getChannels()
      .then((list) => {
        if (!cancelled) {
          setChannels(list)
          setError(null)
        }
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : String(err))
      })
    return () => {
      cancelled = true
    }
  }, [])

  const rowMenu = (channel: ChannelInfo) => (
    <Menu position="bottom-end" withinPortal>
      <Menu.Target>
        <ActionIcon
          variant="subtle"
          color="gray"
          aria-label={`Actions for ${channel.name}`}
          onClick={(e) => e.stopPropagation()}
        >
          <Text fw={700} lh={1}>
            ⋯
          </Text>
        </ActionIcon>
      </Menu.Target>
      <Menu.Dropdown>
        {channel.error ? null : (
          <>
            <Menu.Item
              onClick={(e) => {
                e.stopPropagation()
                setEditTarget(channel)
              }}
            >
              Edit
            </Menu.Item>
            <Menu.Item
              onClick={(e) => {
                e.stopPropagation()
                setRenameTarget(channel)
              }}
            >
              Rename
            </Menu.Item>
          </>
        )}
        <Menu.Item
          color="red"
          onClick={(e) => {
            e.stopPropagation()
            setDeleteTarget(channel)
          }}
        >
          Delete
        </Menu.Item>
      </Menu.Dropdown>
    </Menu>
  )

  return (
    <NavScreen
      crumbs={[{ label: 'Channels' }]}
      title="Channels"
      subtitle="Pick a channel to edit its events."
    >
      <Group justify="flex-end" mb="md">
        <Button
          variant="default"
          onClick={() => navigate(routePath({ screen: 'jobs' }))}
        >
          Jobs
        </Button>
        <Button
          variant="default"
          onClick={() => navigate(routePath({ screen: 'settings' }))}
        >
          Settings
        </Button>
        <Button color="steel" onClick={() => setNewOpen(true)}>
          New channel
        </Button>
      </Group>

      {error ? (
        <Alert color="red" title="Could not load channels">
          {error} - check that the studio server is still running, then reload this page.
        </Alert>
      ) : channels === null ? (
        <Center py="xl">
          <Stack align="center" gap="xs">
            <Loader color="steel" />
            <Text size="xs" c="dimmed">
              Loading channels…
            </Text>
          </Stack>
        </Center>
      ) : channels.length === 0 ? (
        <Alert color="gray" title="No channels yet">
          No channels were found in this workspace. Create one with{' '}
          <Text span fw={600}>
            New channel
          </Text>{' '}
          above (a slug plus its channel.json identity), or add one under your workspace's{' '}
          <Text span ff="monospace">
            channels/
          </Text>{' '}
          directory, then reload this page.
        </Alert>
      ) : (
        <Stack gap="sm">
          {channels.map((channel) =>
            channel.error ? (
              <Card
                key={channel.name}
                padding="md"
                style={{ borderColor: 'var(--mantine-color-red-8)' }}
              >
                <Group justify="space-between" wrap="nowrap">
                  <div style={{ minWidth: 0 }}>
                    <Text fw={600} ff="monospace">
                      {channel.name}
                    </Text>
                    <Text size="sm" c="red.3">
                      {channel.error}
                    </Text>
                  </div>
                  <Group gap="xs" wrap="nowrap">
                    <Badge color="red" variant="light">
                      Not openable
                    </Badge>
                    {rowMenu(channel)}
                  </Group>
                </Group>
              </Card>
            ) : (
              <Card
                key={channel.name}
                padding="md"
                style={{ transition: 'border-color 120ms' }}
                onMouseEnter={(e) =>
                  (e.currentTarget.style.borderColor = 'var(--mantine-color-steel-6)')
                }
                onMouseLeave={(e) => (e.currentTarget.style.borderColor = '')}
              >
                <Group justify="space-between" wrap="nowrap">
                  <UnstyledButton
                    style={{ flex: 1, minWidth: 0 }}
                    onClick={() =>
                      navigate(routePath({ screen: 'events', channel: channel.name }))
                    }
                  >
                    <Group justify="space-between" wrap="nowrap">
                      <div style={{ minWidth: 0 }}>
                        <Text fw={600}>{channel.display_name || channel.name}</Text>
                        <Text size="sm" c="dimmed">
                          {channel.handle || channel.name}
                        </Text>
                      </div>
                      <Badge variant="light" color="steel">
                        {channel.event_count} event{channel.event_count === 1 ? '' : 's'}
                      </Badge>
                    </Group>
                  </UnstyledButton>
                  {rowMenu(channel)}
                </Group>
              </Card>
            ),
          )}
        </Stack>
      )}

      <NewChannelModal opened={newOpen} onClose={() => setNewOpen(false)} onDone={refresh} />
      <EditChannelModal
        channel={editTarget}
        onClose={() => setEditTarget(null)}
        onDone={refresh}
      />
      <RenameChannelModal
        channel={renameTarget}
        onClose={() => setRenameTarget(null)}
        onDone={refresh}
      />
      <DeleteChannelModal
        channel={deleteTarget}
        onClose={() => setDeleteTarget(null)}
        onDone={refresh}
      />
    </NavScreen>
  )
}

/** The six identity-field inputs, shared by the New and Edit dialogs. In Edit
 * mode a blank field means "leave unchanged" (see EditChannelModal), so the
 * placeholders differ per mode. */
function ChannelFieldsInputs({
  values,
  onField,
  disabled,
  editMode,
}: {
  values: Fields
  onField: (key: keyof Fields, value: string) => void
  disabled: boolean
  editMode: boolean
}) {
  return (
    <>
      {FIELD_META.map((meta) => (
        <TextInput
          key={meta.key}
          label={meta.label}
          placeholder={editMode ? 'Leave blank to keep unchanged' : meta.placeholder}
          value={values[meta.key]}
          disabled={disabled}
          onChange={(e) => onField(meta.key, e.currentTarget.value)}
        />
      ))}
    </>
  )
}

function NewChannelModal({
  opened,
  onClose,
  onDone,
}: {
  opened: boolean
  onClose: () => void
  onDone: () => Promise<void>
}) {
  const [slug, setSlug] = useState('')
  const [fields, setFields] = useState<Fields>(EMPTY_FIELDS)
  // channel_url auto-fills from the id until the operator hand-edits it.
  const [urlEdited, setUrlEdited] = useState(false)
  const [busy, setBusy] = useState(false)
  const [serverError, setServerError] = useState<string | null>(null)

  useEffect(() => {
    if (opened) {
      setSlug('')
      setFields(EMPTY_FIELDS)
      setUrlEdited(false)
      setBusy(false)
      setServerError(null)
    }
  }, [opened])

  const onField = (key: keyof Fields, value: string) => {
    setFields((prev) => {
      const next = { ...prev, [key]: value }
      // Keep channel_url in step with id until the operator touches it.
      if (key === 'id' && !urlEdited) next.channel_url = channelUrlFor(value)
      if (key === 'channel_url') setUrlEdited(true)
      return next
    })
  }

  const slugValid = isValidSlug(slug)
  const allFilled = FIELD_META.every((m) => fields[m.key].trim() !== '')
  const canSubmit = slugValid && allFilled && !busy

  const submit = () => {
    if (!canSubmit) return
    setBusy(true)
    setServerError(null)
    createChannel({ slug, ...fields })
      .then(() => onDone())
      .then(() => onClose())
      .catch((err) => {
        setServerError(err instanceof ApiError ? err.message : String(err))
        setBusy(false)
      })
  }

  return (
    <Modal opened={opened} onClose={onClose} title="New channel" centered>
      <Stack gap="md">
        <TextInput
          label="Slug"
          description="The channel's directory name and URL segment."
          placeholder="e.g. erf"
          data-autofocus
          value={slug}
          disabled={busy}
          error={slug.length > 0 && !slugValid ? SLUG_ERROR : null}
          onChange={(e) => setSlug(e.currentTarget.value)}
        />
        <ChannelFieldsInputs values={fields} onField={onField} disabled={busy} editMode={false} />
        <Text size="xs" c="dimmed">
          Upload and assign fonts in the channel's Brand tab before its events can be opened.
        </Text>
        {serverError ? (
          <Alert color="red" title="Could not create channel">
            {serverError}
          </Alert>
        ) : null}
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button color="steel" onClick={submit} loading={busy} disabled={!slugValid || !allFilled}>
            Create
          </Button>
        </Group>
      </Stack>
    </Modal>
  )
}

function EditChannelModal({
  channel,
  onClose,
  onDone,
}: {
  channel: ChannelInfo | null
  onClose: () => void
  onDone: () => Promise<void>
}) {
  // The channel list (GET /api/channels) only carries display_name/handle, not
  // the full channel.json, and G3a adds no GET-one route. So the dialog
  // prefills those two and leaves id/channel_url/language/footer blank meaning
  // "leave unchanged"; on submit only the non-empty, actually-changed fields
  // are sent, relying on the backend's partial merge (see updateChannel).
  const [fields, setFields] = useState<Fields>(EMPTY_FIELDS)
  const [initial, setInitial] = useState<Fields>(EMPTY_FIELDS)
  const [busy, setBusy] = useState(false)
  const [serverError, setServerError] = useState<string | null>(null)

  useEffect(() => {
    if (channel) {
      const seeded: Fields = {
        ...EMPTY_FIELDS,
        display_name: channel.display_name,
        handle: channel.handle,
      }
      setFields(seeded)
      setInitial(seeded)
      setBusy(false)
      setServerError(null)
    }
  }, [channel])

  const onField = (key: keyof Fields, value: string) => {
    setFields((prev) => ({ ...prev, [key]: value }))
  }

  // Only non-empty fields the operator actually changed are sent.
  const changed: Partial<Fields> = {}
  for (const meta of FIELD_META) {
    const value = fields[meta.key].trim()
    if (value !== '' && value !== initial[meta.key]) changed[meta.key] = value
  }
  const hasChanges = Object.keys(changed).length > 0

  const submit = () => {
    if (!channel || !hasChanges || busy) return
    setBusy(true)
    setServerError(null)
    updateChannel(channel.name, changed)
      .then(() => onDone())
      .then(() => onClose())
      .catch((err) => {
        setServerError(err instanceof ApiError ? err.message : String(err))
        setBusy(false)
      })
  }

  return (
    <Modal opened={channel !== null} onClose={onClose} title="Edit channel" centered>
      {channel ? (
        <Stack gap="md">
          <Text size="sm" c="dimmed">
            Editing{' '}
            <Text span fw={600} ff="monospace">
              {channel.name}
            </Text>
            . A blank field is left unchanged; only the fields you change are saved.
          </Text>
          <ChannelFieldsInputs values={fields} onField={onField} disabled={busy} editMode />
          {serverError ? (
            <Alert color="red" title="Could not save channel">
              {serverError}
            </Alert>
          ) : null}
          <Group justify="flex-end">
            <Button variant="default" onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            <Button color="steel" onClick={submit} loading={busy} disabled={!hasChanges}>
              Save changes
            </Button>
          </Group>
        </Stack>
      ) : null}
    </Modal>
  )
}

function RenameChannelModal({
  channel,
  onClose,
  onDone,
}: {
  channel: ChannelInfo | null
  onClose: () => void
  onDone: () => Promise<void>
}) {
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [serverError, setServerError] = useState<string | null>(null)

  useEffect(() => {
    if (channel) {
      setName(channel.name)
      setBusy(false)
      setServerError(null)
    }
  }, [channel])

  const valid = isValidSlug(name)
  const canSubmit = channel !== null && valid && name !== channel.name && !busy

  const submit = () => {
    if (!channel || !canSubmit) return
    setBusy(true)
    setServerError(null)
    renameChannel(channel.name, name)
      .then(() => onDone())
      .then(() => onClose())
      .catch((err) => {
        setServerError(err instanceof ApiError ? err.message : String(err))
        setBusy(false)
      })
  }

  return (
    <Modal opened={channel !== null} onClose={onClose} title="Rename channel" centered>
      <Stack gap="md">
        <TextInput
          label="New slug"
          placeholder="e.g. erf"
          data-autofocus
          value={name}
          disabled={busy}
          error={name.length > 0 && !valid ? SLUG_ERROR : null}
          onChange={(e) => setName(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && canSubmit) submit()
          }}
        />
        {serverError ? (
          <Alert color="red" title="Could not rename channel">
            {serverError}
          </Alert>
        ) : null}
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button
            color="steel"
            onClick={submit}
            loading={busy}
            disabled={!valid || name === channel?.name}
          >
            Rename
          </Button>
        </Group>
      </Stack>
    </Modal>
  )
}

function DeleteChannelModal({
  channel,
  onClose,
  onDone,
}: {
  channel: ChannelInfo | null
  onClose: () => void
  onDone: () => Promise<void>
}) {
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [serverError, setServerError] = useState<string | null>(null)

  useEffect(() => {
    if (channel) {
      setTyped('')
      setBusy(false)
      setServerError(null)
    }
  }, [channel])

  const confirmed = channel !== null && deleteConfirmed(typed, channel.name)

  const submit = () => {
    if (!channel || !confirmed || busy) return
    setBusy(true)
    setServerError(null)
    deleteChannel(channel.name)
      .then(() => onDone())
      .then(() => onClose())
      .catch((err) => {
        setServerError(err instanceof ApiError ? err.message : String(err))
        setBusy(false)
      })
  }

  return (
    <Modal opened={channel !== null} onClose={onClose} title="Delete channel" centered>
      {channel ? (
        <Stack gap="md">
          <Text>
            Permanently delete the channel{' '}
            <Text span fw={600} ff="monospace">
              {channel.name}
            </Text>
            ? This cannot be undone.
          </Text>
          <Alert color="red" title="This removes everything in the channel">
            Deleting it removes the channel and all {channel.event_count} of its event
            {channel.event_count === 1 ? '' : 's'} - every clip and rendered short is lost.
          </Alert>
          <TextInput
            label="Type the channel slug to confirm"
            placeholder={channel.name}
            data-autofocus
            value={typed}
            disabled={busy}
            onChange={(e) => setTyped(e.currentTarget.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && confirmed && !busy) submit()
            }}
          />
          {serverError ? (
            <Alert color="red" title="Could not delete channel">
              {serverError}
            </Alert>
          ) : null}
          <Group justify="flex-end">
            <Button variant="default" onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            <Button color="red" onClick={submit} loading={busy} disabled={!confirmed}>
              Delete
            </Button>
          </Group>
        </Stack>
      ) : null}
    </Modal>
  )
}
