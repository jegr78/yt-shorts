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
  Tabs,
  Text,
  TextInput,
  UnstyledButton,
} from '@mantine/core'
import { notifications } from '@mantine/notifications'
import {
  ApiError,
  createEvent,
  deleteEvent,
  getChannel,
  getEvents,
  renameEvent,
  updateChannel,
  type EventInfo,
} from '../api'
import { deleteConfirmed, isValidEventName, MAX_EVENT_NAME_LENGTH } from '../eventAdmin'
import { routePath } from '../scopedApi'
import { navigate } from '../useRoute'
import { BrandEditor } from './BrandEditor'
import { GlossaryEditor } from './GlossaryEditor'
import { MomentsEditor } from './MomentsEditor'
import { NavScreen } from './NavScreen'

/** One channel's own screen (formerly EventsScreen, stage G3b's own
 * addition wraps it in tabs): Events (GET /api/channels/{channel}/events -
 * unchanged from before this stage, still the default tab so every existing
 * "open a channel" flow lands exactly where it used to), Brand (BrandEditor
 * - the channel's colors/fonts/subtitles, see brand.ts and api.ts's brand
 * calls), Moments (MomentsEditor at channel scope - the channel's own
 * moments-lexicon layer, see momentsLexicon.ts), Glossary (GlossaryEditor at
 * channel scope - the channel's own glossary layer, see glossaryLayers.ts)
 * and Channel (the channel's own identity fields). Every tab shares the ONE
 * NavScreen chrome/breadcrumbs at this level, rather than each carrying its
 * own, so switching tabs never re-renders the masthead. */
export function ChannelScreen({ channel }: { channel: string }) {
  const [events, setEvents] = useState<EventInfo[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [newOpen, setNewOpen] = useState(false)
  const [renameTarget, setRenameTarget] = useState<EventInfo | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<EventInfo | null>(null)

  const refresh = useCallback(() => {
    return getEvents(channel)
      .then((list) => {
        setEvents(list)
        setError(null)
      })
      .catch((err) => {
        setError(err instanceof ApiError ? err.message : String(err))
      })
  }, [channel])

  useEffect(() => {
    let cancelled = false
    getEvents(channel)
      .then((list) => {
        if (!cancelled) {
          setEvents(list)
          setError(null)
        }
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : String(err))
      })
    return () => {
      cancelled = true
    }
  }, [channel])

  return (
    <NavScreen
      crumbs={[{ label: 'Channels', path: routePath({ screen: 'channels' }) }, { label: channel }]}
      title={channel}
      subtitle="Pick an event to open it in the editor, or edit this channel's brand."
    >
      <Tabs defaultValue="events">
        <Tabs.List mb="md">
          <Tabs.Tab value="events">Events</Tabs.Tab>
          <Tabs.Tab value="brand">Brand</Tabs.Tab>
          <Tabs.Tab value="moments">Moments</Tabs.Tab>
          <Tabs.Tab value="glossary">Glossary</Tabs.Tab>
          <Tabs.Tab value="channel">Channel</Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="events">
          <Group justify="flex-end" mb="md">
            <Button color="steel" onClick={() => setNewOpen(true)}>
              New event
            </Button>
          </Group>

          {error ? (
            <Alert color="red" title="Could not load events">
              {error} - check that the studio server is still running, then reload this page.
            </Alert>
          ) : events === null ? (
            <Center py="xl">
              <Stack align="center" gap="xs">
                <Loader color="steel" />
                <Text size="xs" c="dimmed">
                  Loading events…
                </Text>
              </Stack>
            </Center>
          ) : events.length === 0 ? (
            <Alert color="gray" title="No events yet">
              This channel has no events. Create one with{' '}
              <Text span fw={600}>
                New event
              </Text>{' '}
              above (it starts empty), then populate it from Streams → detect or the CLI{' '}
              <Text span ff="monospace">
                harvest
              </Text>
              .
            </Alert>
          ) : (
            <Stack gap="sm">
              {events.map((event) => (
                <Card
                  key={event.name}
                  padding="md"
                  onMouseEnter={(e) => (e.currentTarget.style.borderColor = 'var(--mantine-color-steel-6)')}
                  onMouseLeave={(e) => (e.currentTarget.style.borderColor = '')}
                >
                  <Group justify="space-between" wrap="nowrap">
                    <UnstyledButton
                      style={{ flex: 1, minWidth: 0 }}
                      onClick={() =>
                        navigate(routePath({ screen: 'editor', channel, event: event.name }))
                      }
                    >
                      <Group justify="space-between" wrap="nowrap">
                        <Text fw={600} ff="monospace">
                          {event.name}
                        </Text>
                        <Group gap="xs" wrap="nowrap">
                          <Badge variant="light" color="dark.3">
                            {event.clip_count} clip{event.clip_count === 1 ? '' : 's'}
                          </Badge>
                          <Badge variant="light" color="green">
                            {event.kept_count} kept
                          </Badge>
                          <Badge variant="light" color="steel">
                            {event.rendered_count} rendered
                          </Badge>
                        </Group>
                      </Group>
                    </UnstyledButton>
                    <Menu position="bottom-end" withinPortal>
                      <Menu.Target>
                        <ActionIcon
                          variant="subtle"
                          color="gray"
                          aria-label={`Actions for ${event.name}`}
                          onClick={(e) => e.stopPropagation()}
                        >
                          <Text fw={700} lh={1}>
                            ⋯
                          </Text>
                        </ActionIcon>
                      </Menu.Target>
                      <Menu.Dropdown>
                        <Menu.Item
                          onClick={(e) => {
                            e.stopPropagation()
                            setRenameTarget(event)
                          }}
                        >
                          Rename
                        </Menu.Item>
                        <Menu.Item
                          color="red"
                          onClick={(e) => {
                            e.stopPropagation()
                            setDeleteTarget(event)
                          }}
                        >
                          Delete
                        </Menu.Item>
                      </Menu.Dropdown>
                    </Menu>
                  </Group>
                </Card>
              ))}
            </Stack>
          )}

          <NewEventModal
            opened={newOpen}
            channel={channel}
            onClose={() => setNewOpen(false)}
            onDone={refresh}
          />
          <RenameEventModal
            channel={channel}
            event={renameTarget}
            onClose={() => setRenameTarget(null)}
            onDone={refresh}
          />
          <DeleteEventModal
            channel={channel}
            event={deleteTarget}
            onClose={() => setDeleteTarget(null)}
            onDone={refresh}
          />
        </Tabs.Panel>

        <Tabs.Panel value="brand">
          <BrandEditor channel={channel} />
        </Tabs.Panel>

        <Tabs.Panel value="moments">
          <MomentsEditor channel={channel} />
        </Tabs.Panel>

        <Tabs.Panel value="glossary">
          <GlossaryEditor channel={channel} />
        </Tabs.Panel>

        <Tabs.Panel value="channel">
          <ChannelDetails channel={channel} />
        </Tabs.Panel>
      </Tabs>
    </NavScreen>
  )
}

const CHANNEL_FIELD_LABELS: { key: 'display_name' | 'handle' | 'footer' | 'language'; label: string; description?: string }[] = [
  { key: 'display_name', label: 'Display name' },
  { key: 'handle', label: 'Handle', description: 'The channel @handle, e.g. @ERFofficial.' },
  { key: 'footer', label: 'Footer', description: "Shown at the bottom of every short (e.g. 'ERF | @ERFofficial')." },
  { key: 'language', label: 'Language', description: 'ISO code, e.g. en or de.' },
]

type ChannelDetailsForm = { display_name: string; handle: string; footer: string; language: string }

/** The channel's identity fields (channel.json) as an editable form: display
 * name, handle, footer and language. Loads via GET /api/channels/{channel} and
 * saves the changed fields via PATCH (updateChannel); the YouTube id and URL
 * are deliberately not editable here (changing the id would orphan the stored
 * auth token). A blanked required field is refused server-side, so Save stays
 * disabled until all four are non-empty. */
function ChannelDetails({ channel }: { channel: string }) {
  const [form, setForm] = useState<ChannelDetailsForm | null>(null)
  const [saved, setSaved] = useState<ChannelDetailsForm | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    getChannel(channel)
      .then((c) => {
        if (cancelled) return
        const next: ChannelDetailsForm = {
          display_name: String(c.display_name ?? ''),
          handle: String(c.handle ?? ''),
          footer: String(c.footer ?? ''),
          language: String(c.language ?? ''),
        }
        setForm(next)
        setSaved(next)
        setLoadError(null)
      })
      .catch((err) => {
        if (!cancelled) setLoadError(err instanceof ApiError ? err.message : String(err))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [channel])

  if (loading) {
    return (
      <Center py="xl">
        <Stack align="center" gap="xs">
          <Loader color="steel" />
          <Text size="xs" c="dimmed">
            Loading channel…
          </Text>
        </Stack>
      </Center>
    )
  }
  if (loadError || !form) {
    return (
      <Alert color="red" title="Could not load channel">
        {loadError} - check that the studio server is still running, then reload this page.
      </Alert>
    )
  }

  const setField = (key: keyof ChannelDetailsForm, value: string) =>
    setForm((prev) => (prev ? { ...prev, [key]: value } : prev))
  const dirty = saved !== null && JSON.stringify(form) !== JSON.stringify(saved)
  const ready = CHANNEL_FIELD_LABELS.every(({ key }) => form[key].trim().length > 0)

  const submit = () => {
    if (!dirty || !ready || saving) return
    setSaving(true)
    setSaveError(null)
    updateChannel(channel, form)
      .then(() => {
        setSaved(form)
        notifications.show({ message: 'Saved.', color: 'green' })
      })
      .catch((err) => setSaveError(err instanceof ApiError ? err.message : String(err)))
      .finally(() => setSaving(false))
  }

  return (
    <Card padding="md" maw={540}>
      <Stack gap="sm">
        {CHANNEL_FIELD_LABELS.map(({ key, label, description }) => (
          <TextInput
            key={key}
            label={label}
            description={description}
            value={form[key]}
            error={form[key].trim().length === 0 ? 'Required' : null}
            onChange={(e) => setField(key, e.currentTarget.value)}
          />
        ))}
        {saveError ? (
          <Alert color="red" title="Could not save channel">
            {saveError}
          </Alert>
        ) : null}
        <Group justify="flex-end">
          <Button color="steel" onClick={submit} loading={saving} disabled={!dirty || !ready}>
            Save channel
          </Button>
        </Group>
      </Stack>
    </Card>
  )
}

/** The New / Rename dialogs share the same name field, live validation and
 * inline server-error handling; this renders that shared body. */
function EventNameField({
  value,
  onChange,
  onSubmit,
  disabled,
}: {
  value: string
  onChange: (value: string) => void
  onSubmit: () => void
  disabled: boolean
}) {
  const invalid = value.length > 0 && !isValidEventName(value)
  return (
    <TextInput
      label="Event name"
      placeholder="e.g. round-3"
      data-autofocus
      value={value}
      disabled={disabled}
      error={
        invalid
          ? `Use letters, digits, '.', '-', '_'; no slashes, no leading dot, max ${MAX_EVENT_NAME_LENGTH} chars.`
          : null
      }
      onChange={(e) => onChange(e.currentTarget.value)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' && isValidEventName(value) && !disabled) onSubmit()
      }}
    />
  )
}

function NewEventModal({
  opened,
  channel,
  onClose,
  onDone,
}: {
  opened: boolean
  channel: string
  onClose: () => void
  onDone: () => Promise<void>
}) {
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [serverError, setServerError] = useState<string | null>(null)

  // Reset the field each time the dialog is (re)opened.
  useEffect(() => {
    if (opened) {
      setName('')
      setBusy(false)
      setServerError(null)
    }
  }, [opened])

  const submit = () => {
    if (!isValidEventName(name) || busy) return
    setBusy(true)
    setServerError(null)
    createEvent(channel, name)
      .then(() => onDone())
      .then(() => onClose())
      .catch((err) => {
        setServerError(err instanceof ApiError ? err.message : String(err))
        setBusy(false)
      })
  }

  return (
    <Modal opened={opened} onClose={onClose} title="New event" centered>
      <Stack gap="md">
        <EventNameField value={name} onChange={setName} onSubmit={submit} disabled={busy} />
        {serverError ? (
          <Alert color="red" title="Could not create event">
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
            disabled={!isValidEventName(name)}
          >
            Create
          </Button>
        </Group>
      </Stack>
    </Modal>
  )
}

function RenameEventModal({
  channel,
  event,
  onClose,
  onDone,
}: {
  channel: string
  event: EventInfo | null
  onClose: () => void
  onDone: () => Promise<void>
}) {
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [serverError, setServerError] = useState<string | null>(null)

  // Pre-fill with the current name whenever a new rename target is opened.
  useEffect(() => {
    if (event) {
      setName(event.name)
      setBusy(false)
      setServerError(null)
    }
  }, [event])

  const submit = () => {
    if (!event || !isValidEventName(name) || busy) return
    setBusy(true)
    setServerError(null)
    renameEvent(channel, event.name, name)
      .then(() => onDone())
      .then(() => onClose())
      .catch((err) => {
        setServerError(err instanceof ApiError ? err.message : String(err))
        setBusy(false)
      })
  }

  return (
    <Modal opened={event !== null} onClose={onClose} title="Rename event" centered>
      <Stack gap="md">
        <EventNameField value={name} onChange={setName} onSubmit={submit} disabled={busy} />
        {serverError ? (
          <Alert color="red" title="Could not rename event">
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
            disabled={!isValidEventName(name) || name === event?.name}
          >
            Rename
          </Button>
        </Group>
      </Stack>
    </Modal>
  )
}

function DeleteEventModal({
  channel,
  event,
  onClose,
  onDone,
}: {
  channel: string
  event: EventInfo | null
  onClose: () => void
  onDone: () => Promise<void>
}) {
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [serverError, setServerError] = useState<string | null>(null)

  useEffect(() => {
    if (event) {
      setTyped('')
      setBusy(false)
      setServerError(null)
    }
  }, [event])

  const confirmed = event !== null && deleteConfirmed(typed, event.name)

  const submit = () => {
    if (!event || !confirmed || busy) return
    setBusy(true)
    setServerError(null)
    deleteEvent(channel, event.name)
      .then(() => onDone())
      .then(() => onClose())
      .catch((err) => {
        setServerError(err instanceof ApiError ? err.message : String(err))
        setBusy(false)
      })
  }

  return (
    <Modal opened={event !== null} onClose={onClose} title="Delete event" centered>
      {event ? (
        <Stack gap="md">
          <Text>
            Permanently delete the event{' '}
            <Text span fw={600} ff="monospace">
              {event.name}
            </Text>
            ? This cannot be undone.
          </Text>
          {event.clip_count > 0 ? (
            <Alert color="red" title="This event has content">
              Deleting it removes {event.clip_count} clip{event.clip_count === 1 ? '' : 's'} and
              its {event.rendered_count} rendered short
              {event.rendered_count === 1 ? '' : 's'} - those rendered shorts are lost.
            </Alert>
          ) : null}
          <TextInput
            label="Type the event name to confirm"
            placeholder={event.name}
            data-autofocus
            value={typed}
            disabled={busy}
            onChange={(e) => setTyped(e.currentTarget.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && confirmed && !busy) submit()
            }}
          />
          {serverError ? (
            <Alert color="red" title="Could not delete event">
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
