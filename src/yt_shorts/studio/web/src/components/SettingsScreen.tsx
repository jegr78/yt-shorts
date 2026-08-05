import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Badge,
  Button,
  Card,
  Center,
  Code,
  Group,
  Loader,
  Modal,
  PasswordInput,
  SegmentedControl,
  Stack,
  Text,
  TextInput,
  Tooltip,
} from '@mantine/core'
import {
  ApiError,
  connectChannel,
  copyWorkspace,
  createWorkspace,
  deleteProviderKey,
  disconnectAuth,
  getSettings,
  getWorkspaces,
  putProviderKey,
  putQueueLimits,
  setUploadMode,
  switchWorkspace,
  type ProviderState,
  type SettingsResponse,
  type WorkspacesResponse,
} from '../api'
import { deleteConfirmed } from '../eventAdmin'
import { useJobPolling } from '../hooks/useJobPolling'
import { providerBlocker, providerLabel, unverifiedCaveat } from '../providers'
import { originLabel, parsePoolLimit } from '../settings'
import { routePath } from '../scopedApi'
import { navigate } from '../useRoute'
import { isValidWorkspaceName, joinPath } from '../workspaces'
import { FsBrowser } from './FsBrowser'
import { GlossaryEditor } from './GlossaryEditor'
import { MomentsEditor } from './MomentsEditor'
import { NavScreen } from './NavScreen'

type SettingsChannel = SettingsResponse['channels'][number]

/** The workspace-level Settings screen (stage G4): a read-only workspace
 * panel plus a connection row per channel (GET /api/settings), with
 * Connect/Switch/Disconnect actions over the channel-scoped auth routes.
 * Unlike AuthStatusBar (the editor's own connection indicator, scoped to
 * whichever channel/event is currently open), this screen has NO active
 * channel - it lists every channel in the workspace at once, which is why
 * it calls the scope-explicit `connectChannel` rather than AuthStatusBar's
 * `startConnect` (that one reads the editor's module-level scope, which is
 * never set here). The connect dialog otherwise mirrors AuthStatusBar's own
 * idiom (send a channel id, poll the returned job with useJobPolling); the
 * disconnect dialog reuses the same typed-confirmation gate
 * (deleteConfirmed) the channel/event delete dialogs use, typing the
 * channel's `channel_id` rather than its slug since that id is what a
 * mistaken disconnect would actually forget. */
export function SettingsScreen() {
  const [settings, setSettings] = useState<SettingsResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [connectTarget, setConnectTarget] = useState<{
    row: SettingsChannel
    force: boolean
  } | null>(null)
  const [disconnectTarget, setDisconnectTarget] = useState<SettingsChannel | null>(null)

  const refresh = useCallback(() => {
    return getSettings()
      .then((data) => {
        setSettings(data)
        setError(null)
      })
      .catch((err) => {
        setError(err instanceof ApiError ? err.message : String(err))
      })
  }, [])

  useEffect(() => {
    let cancelled = false
    getSettings()
      .then((data) => {
        if (!cancelled) {
          setSettings(data)
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

  return (
    <NavScreen
      crumbs={[{ label: 'Channels', path: routePath({ screen: 'channels' }) }, { label: 'Settings' }]}
      title="Settings"
      subtitle="Workspace location and each channel's connection to YouTube."
    >
      {error ? (
        <Alert color="red" title="Could not load settings">
          {error} - check that the studio server is still running, then reload this page.
        </Alert>
      ) : settings === null ? (
        <Center py="xl">
          <Stack align="center" gap="xs">
            <Loader color="steel" />
            <Text size="xs" c="dimmed">
              Loading settings…
            </Text>
          </Stack>
        </Center>
      ) : (
        <Stack gap="lg">
          <Group justify="flex-end">
            <Button variant="default" onClick={() => navigate(routePath({ screen: 'jobs' }))}>
              View jobs
            </Button>
            <Button variant="default" onClick={() => navigate(routePath({ screen: 'logs' }))}>
              View logs
            </Button>
          </Group>
          <WorkspacePanel googleUploadAvailable={settings.workspace.google_upload_available} />
          <QueueLimitsPanel queue={settings.queue} onRefresh={refresh} />
          <Stack gap="sm">
            {settings.channels.map((row) => (
              <ConnectionRow
                key={row.channel}
                row={row}
                onConnect={(force) => setConnectTarget({ row, force })}
                onDisconnect={() => setDisconnectTarget(row)}
                onRefresh={refresh}
              />
            ))}
          </Stack>
          <ProvidersPanel providers={settings.providers} onRefresh={refresh} />
          <Card padding="md">
            <Stack gap="sm">
              <Text fw={600} size="sm" tt="uppercase" c="dimmed">
                Moments lexicon
              </Text>
              <MomentsEditor />
            </Stack>
          </Card>
          <Card padding="md">
            <Stack gap="sm">
              <Text fw={600} size="sm" tt="uppercase" c="dimmed">
                Glossary
              </Text>
              <GlossaryEditor />
            </Stack>
          </Card>
        </Stack>
      )}

      <ConnectDialog
        target={connectTarget}
        onClose={() => setConnectTarget(null)}
        onDone={refresh}
      />
      <DisconnectDialog
        target={disconnectTarget}
        onClose={() => setDisconnectTarget(null)}
        onDone={refresh}
      />
    </NavScreen>
  )
}

/** The interactive workspace panel (Task 10): shows where the studio is
 * currently pointed (GET /api/workspaces, not GET /api/settings's own
 * `workspace` summary - that one stays the source for the Google-upload-libs
 * notice below, since it is fetched once for the whole screen and this panel
 * would otherwise need a second round trip just for that one boolean) and
 * opens WorkspaceManagerModal for anything that changes it. "Manage
 * workspaces…" is disabled with a tooltip while `current.locked`
 * (YT_SHORTS_DATA pins the workspace server-side - see api.py's
 * `_guard_reroot`), so the operator never reaches a 409 by clicking it. */
function WorkspacePanel({ googleUploadAvailable }: { googleUploadAvailable: boolean }) {
  const [ws, setWs] = useState<WorkspacesResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [manageOpen, setManageOpen] = useState(false)

  const refresh = useCallback(
    () =>
      getWorkspaces()
        .then((data) => {
          setWs(data)
          setError(null)
        })
        .catch((err) => setError(err instanceof ApiError ? err.message : String(err))),
    [],
  )

  useEffect(() => {
    refresh()
  }, [refresh])

  if (error) {
    return (
      <Alert color="red" title="Could not load workspace">
        {error}
      </Alert>
    )
  }

  if (!ws) {
    return (
      <Card padding="md">
        <Group gap="xs">
          <Loader size={16} color="steel" />
          <Text size="sm" c="dimmed">
            Loading workspace…
          </Text>
        </Group>
      </Card>
    )
  }

  return (
    <Card padding="md">
      <Stack gap="xs">
        <Group justify="space-between">
          <Text fw={600}>Workspace</Text>
          <Tooltip
            disabled={!ws.current.locked}
            label="Set by the YT_SHORTS_DATA environment variable - unset it to manage workspaces here."
          >
            <Button
              size="xs"
              variant="default"
              disabled={ws.current.locked}
              onClick={() => setManageOpen(true)}
            >
              Manage workspaces…
            </Button>
          </Tooltip>
        </Group>
        <Text size="sm">{ws.current.name}</Text>
        <Text size="xs" ff="monospace" c="dimmed" style={{ overflowWrap: 'anywhere' }}>
          {ws.current.path}
        </Text>
        <Text size="xs" c="dimmed">
          Source: {originLabel(ws.current.origin)}
        </Text>
        {!googleUploadAvailable ? (
          <Alert color="gray" variant="light" title="Upload libraries not installed" mt="xs">
            Connecting a channel or uploading a short needs the optional Google API
            libraries. Install them with{' '}
            <Text span ff="monospace">
              .venv/bin/pip install fastapi uvicorn google-api-python-client
              google-auth-oauthlib
            </Text>
            .
          </Alert>
        ) : null}
      </Stack>
      <WorkspaceManagerModal
        opened={manageOpen}
        workspaces={ws}
        onClose={() => setManageOpen(false)}
      />
    </Card>
  )
}

/** Task 11's control for `PUT /api/settings/limits` (see api.py's
 * `_queue_pools`/`_validated_limits`) - until this task the only way to
 * change a pool's limit was to hand-edit `<workspace>/settings.json` and
 * restart the studio, even though the route that applies it live already
 * existed and was already tested.
 *
 * **Rule 1 (say what the number does before it is changed):** the
 * explanatory text sits directly above the fields, not in a tooltip -
 * a limit is per WORKSPACE, never per event or per channel, and raising
 * `cpu` past the machine's own core count makes rendering/transcribing
 * SLOWER, not faster, because they start fighting each other for the same
 * cores rather than running one at a time.
 *
 * **Rule 4 (lowering a limit is safe):** stated here too, because an
 * operator watching three transcriptions in flight and setting `cpu: 1`
 * could otherwise reasonably worry one of them just got killed. Nothing
 * running is ever touched - `_pool_has_room` (job_queue.py) only gates
 * what `claim_next` picks up NEXT, so running/stopping work keeps its pool
 * slot and finishes normally; the pool just claims no new work until it is
 * back under the new limit.
 *
 * One field and one Save button per pool, rather than a single form-wide
 * Save, so saving `cpu` can never accidentally resend a `net` value the
 * operator had mid-edited to something invalid - each PUT is a partial
 * patch naming only the pool whose Save was clicked (see putQueueLimits).
 * `parsePoolLimit` (settings.ts) is a CLIENT-SIDE HINT only: it disables
 * Save for an obviously-unusable value, but the actual refusal always
 * comes from the server (`_validated_limits`) and is shown verbatim on
 * failure - the two are not required to agree on every edge case, only for
 * the client to never accept something the server would refuse silently. */
function QueueLimitsPanel({
  queue,
  onRefresh,
}: {
  queue: SettingsResponse['queue']
  onRefresh: () => Promise<void>
}) {
  return (
    <Card padding="md">
      <Stack gap="sm">
        <Text fw={600} size="sm" tt="uppercase" c="dimmed">
          Job queue limits
        </Text>
        <Text size="xs" c="dimmed">
          How many jobs each pool runs at once, for this workspace as a whole -
          not per event, not per channel. Raising a pool&apos;s limit past your
          machine&apos;s own core count makes everything using it slower, not
          faster. Lowering a limit never touches work already running: it
          finishes normally, and the pool simply stops claiming new work until
          it is back under the new limit.
        </Text>
        {!queue.available ? (
          <Alert color="yellow" variant="light" title="No job queue is running right now">
            This studio could not open a workspace to run the queue from. A
            change here is still saved to this workspace&apos;s settings and
            takes effect the moment a queue is - check the workspace above,
            then reload.
          </Alert>
        ) : null}
        <Group gap="lg" wrap="wrap" align="flex-start">
          {queue.pools.map((pool) => (
            <PoolLimitField
              key={pool}
              pool={pool}
              value={queue.limits[pool]}
              onRefresh={onRefresh}
            />
          ))}
        </Group>
      </Stack>
    </Card>
  )
}

function PoolLimitField({
  pool,
  value,
  onRefresh,
}: {
  pool: string
  value: number
  onRefresh: () => Promise<void>
}) {
  const [text, setText] = useState(String(value))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Picks up the server's own value after a successful save (onRefresh
  // re-fetches GET /api/settings) and after any OTHER pool's save, which
  // is why this does not merely run once on mount.
  useEffect(() => {
    setText(String(value))
  }, [value])

  const parsed = parsePoolLimit(text)
  const invalid = text.trim() !== '' && parsed === null
  const unchanged = parsed === value

  const save = () => {
    if (parsed === null || saving) return
    setSaving(true)
    setError(null)
    putQueueLimits({ [pool]: parsed })
      .then(() => onRefresh())
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
      .finally(() => setSaving(false))
  }

  return (
    <Stack gap={4} style={{ width: 160 }}>
      <Group gap="xs" align="flex-end" wrap="nowrap">
        <TextInput
          label={`${pool} pool`}
          description="at a time"
          size="xs"
          value={text}
          disabled={saving}
          onChange={(event) => setText(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') save()
          }}
          error={invalid ? 'a whole number, at least 1' : null}
        />
        <Button
          size="xs"
          color="steel"
          loading={saving}
          disabled={parsed === null || unchanged}
          onClick={save}
          // Every field on this panel is labelled "Save" - the label an
          // operator actually reads - but that leaves two identical
          // accessible names on the page (cpu, net), which is ambiguous for
          // anything driving the page by role. aria-label disambiguates
          // without changing what is shown.
          aria-label={`Save the ${pool} pool limit`}
        >
          Save
        </Button>
      </Group>
      {error ? (
        <Text size="xs" c="red.4">
          {error}
        </Text>
      ) : null}
    </Stack>
  )
}

type ManageMode = 'recent' | 'open' | 'new' | 'copy'

/** The workspace-manager dialog (Task 10): a recents quick-pick, a
 * directory browser to open an existing workspace, and forms to create a
 * new workspace or copy the current one - mirrors ConnectDialog's own
 * idioms (Modal + Stack, inline Alert on failure, `loading`/`disabled`
 * buttons, poll a returned job with useJobPolling). Any successful switch/
 * create/copy reloads the whole app onto the new dataset (`applied`) rather
 * than trying to patch this screen's state in place - the entire workspace
 * (every channel, every event) just changed under it. A 409 (a job already
 * running, or the workspace being env-locked after all - see
 * `_guard_reroot`) surfaces as an inline Alert with the server's own
 * `detail`, the same as every other dialog in this file. */
function WorkspaceManagerModal({
  opened,
  workspaces,
  onClose,
}: {
  opened: boolean
  workspaces: WorkspacesResponse
  onClose: () => void
}) {
  const [mode, setMode] = useState<ManageMode>('recent')
  const [selected, setSelected] = useState<string | null>(null)
  const [selectedIsWorkspace, setSelectedIsWorkspace] = useState(false)
  const [name, setName] = useState('')
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [copyJobId, setCopyJobId] = useState<string | null>(null)
  const copyJob = useJobPolling(copyJobId)

  useEffect(() => {
    if (opened) {
      setMode('recent')
      setSelected(null)
      setSelectedIsWorkspace(false)
      setName('')
      setStarting(false)
      setError(null)
      setCopyJobId(null)
    }
  }, [opened])

  function changeMode(value: string) {
    setMode(value as ManageMode)
    setSelected(null)
    setSelectedIsWorkspace(false)
    setName('')
    setError(null)
  }

  const applied = () => window.location.assign('/')

  const copying = copyJob?.status === 'running'
  const copyFailed = copyJob?.status === 'failed'
  const copyFailureReason =
    copyJob && copyFailed
      ? (copyJob.results[name.trim()]?.reason ?? 'See the job log for details.')
      : null

  useEffect(() => {
    if (copyJob?.status === 'done') applied()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [copyJob?.status])

  const busy = starting || copying
  const validName = isValidWorkspaceName(name)

  function switchTo(path: string) {
    setSelected(path)
    setStarting(true)
    setError(null)
    switchWorkspace(path)
      .then(applied)
      .catch((err) => {
        setError(err instanceof ApiError ? err.message : String(err))
        setStarting(false)
      })
  }

  function create() {
    if (!selected || !validName) return
    setStarting(true)
    setError(null)
    createWorkspace(selected, name.trim())
      .then(applied)
      .catch((err) => {
        setError(err instanceof ApiError ? err.message : String(err))
        setStarting(false)
      })
  }

  function copy() {
    if (!selected || !validName) return
    setStarting(true)
    setError(null)
    copyWorkspace(selected, name.trim())
      .then(({ job_id }) => setCopyJobId(job_id))
      .catch((err) => {
        setError(err instanceof ApiError ? err.message : String(err))
      })
      .finally(() => setStarting(false))
  }

  return (
    <Modal
      opened={opened}
      onClose={() => {
        // Locked while an operation is running (starting a switch/create, or
        // a copy job in flight) - the same guard the Close button already
        // uses, extended to the overlay-click and ESC dismissal paths so a
        // stray click can't drop the copy job's auto-reload mid-flight.
        if (!busy) onClose()
      }}
      title="Manage workspaces"
      size="lg"
      closeOnEscape={!busy}
      closeOnClickOutside={!busy}
      centered
    >
      <Stack gap="md">
        <SegmentedControl
          value={mode}
          onChange={changeMode}
          disabled={busy}
          fullWidth
          data={[
            { label: 'Recent', value: 'recent' },
            { label: 'Open', value: 'open' },
            { label: 'New', value: 'new' },
            { label: 'Copy', value: 'copy' },
          ]}
        />

        {mode === 'recent' ? (
          <Stack gap="xs">
            {(() => {
              const others = workspaces.recent.filter(
                (entry) => entry.path !== workspaces.current.path,
              )
              if (others.length === 0) {
                return (
                  <Text size="sm" c="dimmed">
                    No other recent workspaces.
                  </Text>
                )
              }
              return others.map((entry) => (
                <Group key={entry.path} justify="space-between" wrap="nowrap">
                  <div style={{ minWidth: 0 }}>
                    <Text size="sm" truncate>
                      {entry.name}
                    </Text>
                    <Text size="xs" c="dimmed" ff="monospace" truncate>
                      {entry.path}
                    </Text>
                  </div>
                  <Button
                    size="xs"
                    variant="default"
                    disabled={!entry.valid || busy}
                    loading={starting && selected === entry.path}
                    onClick={() => switchTo(entry.path)}
                  >
                    Switch
                  </Button>
                </Group>
              ))
            })()}
          </Stack>
        ) : null}

        {mode === 'open' ? (
          <Stack gap="sm">
            <FsBrowser
              value={selected}
              onChange={(path, isWorkspace) => {
                setSelected(path)
                setSelectedIsWorkspace(isWorkspace)
              }}
            />
            <Group justify="flex-end">
              <Button
                color="steel"
                loading={starting}
                disabled={!selected || !selectedIsWorkspace}
                onClick={() => selected && switchTo(selected)}
              >
                Open
              </Button>
            </Group>
          </Stack>
        ) : null}

        {mode === 'new' ? (
          <Stack gap="sm">
            <FsBrowser value={selected} onChange={(path) => setSelected(path)} />
            <TextInput
              label="New workspace name"
              value={name}
              disabled={starting}
              onChange={(event) => setName(event.currentTarget.value)}
              error={
                name.length > 0 && !validName
                  ? 'Letters, digits, ".", "_", "-" only - must start with a letter or digit.'
                  : null
              }
            />
            {selected && name && validName ? (
              <Text size="xs" c="dimmed" ff="monospace" style={{ overflowWrap: 'anywhere' }}>
                Will create at: {joinPath(selected, name)}
              </Text>
            ) : null}
            <Group justify="flex-end">
              <Button color="steel" loading={starting} disabled={!selected || !validName} onClick={create}>
                Create
              </Button>
            </Group>
          </Stack>
        ) : null}

        {mode === 'copy' ? (
          <Stack gap="sm">
            <Text size="xs" c="dimmed">
              Copies the current workspace&apos;s entire contents to a new location,
              including its stored YouTube connections (auth/).
            </Text>
            <FsBrowser value={selected} onChange={(path) => setSelected(path)} />
            <TextInput
              label="New workspace name"
              value={name}
              disabled={busy}
              onChange={(event) => setName(event.currentTarget.value)}
              error={
                name.length > 0 && !validName
                  ? 'Letters, digits, ".", "_", "-" only - must start with a letter or digit.'
                  : null
              }
            />
            {selected && name && validName ? (
              <Text size="xs" c="dimmed" ff="monospace" style={{ overflowWrap: 'anywhere' }}>
                Will copy to: {joinPath(selected, name)}
              </Text>
            ) : null}
            {copying ? (
              <Group gap="xs">
                <Loader size={16} color="steel" />
                <Text size="sm" c="dimmed">
                  Copying workspace… this can take a while for a large one.
                </Text>
              </Group>
            ) : null}
            {copyFailed ? (
              <Alert color="red" variant="light" title="Copy failed">
                {copyFailureReason}
              </Alert>
            ) : null}
            <Group justify="flex-end">
              <Button color="steel" loading={busy} disabled={!selected || !validName} onClick={copy}>
                Copy
              </Button>
            </Group>
          </Stack>
        ) : null}

        {error ? (
          <Alert color="red" variant="light" title="Could not complete this action">
            {error}
          </Alert>
        ) : null}

        <Group justify="flex-end">
          <Button variant="default" onClick={onClose} disabled={busy}>
            Close
          </Button>
        </Group>
      </Stack>
    </Modal>
  )
}

/** The workspace-level "Model providers" block: one row per registered
 * provider (GET /api/settings's `providers`, in the server's own display
 * order - the default first, see providers.ordered()), where an operator
 * pastes or forgets the API key moment detection spends.
 *
 * The key is workspace-scoped, not channel-scoped, which is why this lives
 * here rather than in a channel's Brand editor: that editor picks WHICH
 * provider a channel uses (brand.json's `detect` section), this screen holds
 * WHOSE key pays for it. Both surfaces disclose an unverified provider, so
 * an operator choosing one over there never has to have come here to learn
 * it. */
function ProvidersPanel({
  providers,
  onRefresh,
}: {
  providers: ProviderState[]
  onRefresh: () => Promise<void>
}) {
  return (
    <Card padding="md">
      <Stack gap="sm">
        <Text fw={600} size="sm" tt="uppercase" c="dimmed">
          Model providers
        </Text>
        <Text size="xs" c="dimmed">
          Which vendor scores a stream's moments. A key is stored per workspace, at{' '}
          <Text span ff="monospace">
            auth/&lt;provider&gt;.json
          </Text>{' '}
          with owner-only permissions, and is never shown again - not here, not
          anywhere. With no key at all, detection still runs on the offline lexicon
          engine, and says so. Each channel picks which provider it uses in its own
          Brand editor.
        </Text>
        {providers.map((state) => (
          <ProviderRow key={state.id} state={state} onRefresh={onRefresh} />
        ))}
      </Stack>
    </Card>
  )
}

/** One provider: its state, whatever is blocking it, and the two actions
 * (paste a key, forget it).
 *
 * The field is DELIBERATELY never pre-filled and never reflects what is
 * stored: the server never returns a key, so an input that looked populated
 * would imply the studio holds something it does not. "Key stored" is a
 * badge, not a value. */
function ProviderRow({
  state,
  onRefresh,
}: {
  state: ProviderState
  onRefresh: () => Promise<void>
}) {
  const [key, setKey] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [confirmRemove, setConfirmRemove] = useState(false)
  const [removing, setRemoving] = useState(false)

  const blocker = providerBlocker(state)
  const caveat = unverifiedCaveat(state)

  const save = () => {
    if (!key.trim() || saving) return
    setSaving(true)
    setError(null)
    putProviderKey(state.id, key)
      .then(() => {
        setKey('')
        return onRefresh()
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
      .finally(() => setSaving(false))
  }

  const remove = () => {
    setRemoving(true)
    setError(null)
    deleteProviderKey(state.id)
      .then(() => onRefresh())
      .then(() => setConfirmRemove(false))
      .catch((err) => {
        // A 404 ("no key was stored") still leaves the end state the
        // operator asked for - refresh rather than showing a failure for a
        // key that is already gone. Same stance as DisconnectDialog's.
        if (err instanceof ApiError && err.status === 404) {
          onRefresh().then(() => setConfirmRemove(false))
          return
        }
        setError(err instanceof ApiError ? err.message : String(err))
      })
      .finally(() => setRemoving(false))
  }

  return (
    <Card padding="sm" withBorder>
      <Stack gap="xs">
        <Group justify="space-between" wrap="nowrap">
          <div style={{ minWidth: 0 }}>
            <Text fw={600}>{providerLabel(state.id)}</Text>
            <Text size="xs" c="dimmed" ff="monospace" style={{ overflowWrap: 'anywhere' }}>
              {state.default_model}
            </Text>
          </div>
          <Group gap="xs" wrap="nowrap">
            {state.verified ? null : (
              <Badge color="yellow" variant="light">
                unverified
              </Badge>
            )}
            <Badge color={state.key_present ? 'green' : 'dark.3'} variant="dot">
              {state.key_present ? 'key stored' : 'no key'}
            </Badge>
          </Group>
        </Group>

        {blocker ? (
          <Alert color="gray" variant="light" p="xs">
            <Text size="xs">{blocker}</Text>
            {state.sdk_installed ? null : (
              <Code block mt={6}>
                {state.install}
              </Code>
            )}
          </Alert>
        ) : null}

        {caveat ? (
          <Alert color="yellow" variant="light" p="xs" title="Never measured">
            <Text size="xs">{caveat}</Text>
          </Alert>
        ) : null}

        {error ? (
          <Alert color="red" variant="light" p="xs" title="Could not change the key">
            <Text size="xs">{error}</Text>
          </Alert>
        ) : null}

        <Group align="flex-end" gap="xs" wrap="nowrap">
          <PasswordInput
            style={{ flex: 1 }}
            size="xs"
            label={state.key_present ? 'Replace the stored key' : 'API key'}
            placeholder="Paste the key - it is never shown again"
            value={key}
            disabled={saving || removing}
            onChange={(event) => setKey(event.currentTarget.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') save()
            }}
          />
          <Button size="xs" color="steel" loading={saving} disabled={!key.trim()} onClick={save}>
            Save key
          </Button>
          {state.key_present ? (
            <Button
              size="xs"
              color="red"
              variant="light"
              disabled={saving || removing}
              onClick={() => setConfirmRemove(true)}
            >
              Remove
            </Button>
          ) : null}
        </Group>
      </Stack>

      <Modal
        opened={confirmRemove}
        onClose={() => setConfirmRemove(false)}
        title={`Remove the ${providerLabel(state.id)} key`}
        centered
      >
        <Stack gap="md">
          <Text size="sm">
            Forget the stored key for{' '}
            <Text span fw={600}>
              {providerLabel(state.id)}
            </Text>
            ? Detection for every channel using this provider falls back to the offline
            lexicon engine until a key is pasted again. Nothing else in{' '}
            <Text span ff="monospace">
              auth/
            </Text>{' '}
            is touched.
          </Text>
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setConfirmRemove(false)} disabled={removing}>
              Cancel
            </Button>
            <Button color="red" loading={removing} onClick={remove}>
              Remove key
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Card>
  )
}

function ConnectionRow({
  row,
  onConnect,
  onDisconnect,
  onRefresh,
}: {
  row: SettingsChannel
  onConnect: (force: boolean) => void
  onDisconnect: () => void
  onRefresh: () => Promise<void>
}) {
  const [savingMode, setSavingMode] = useState(false)
  const [modeError, setModeError] = useState<string | null>(null)

  const changeMode = (mode: 'api' | 'manual') => {
    if (mode === row.upload_mode || savingMode) return
    setSavingMode(true)
    setModeError(null)
    setUploadMode(row.channel, mode)
      .then(onRefresh)
      .catch((err) => setModeError(err instanceof ApiError ? err.message : String(err)))
      .finally(() => setSavingMode(false))
  }

  return (
    <Card padding="md">
      <Group justify="space-between" wrap="nowrap">
        <div style={{ minWidth: 0 }}>
          <Text fw={600} ff="monospace">
            {row.channel}
          </Text>
          <Text size="sm" c="dimmed" ff="monospace">
            {row.channel_id || '—'}
          </Text>
          {/* Which provider this channel's moment detection spends, so the
              key rows below have a visible consumer. Read-only here - it is
              set in the channel's own Brand editor. */}
          <Text size="xs" c="dimmed">
            Detection: {providerLabel(row.detect_provider)}
            {row.detect_model ? ` · ${row.detect_model}` : ''}
          </Text>
        </div>
        <Group gap="xs" wrap="nowrap">
          <Tooltip
            multiline
            w={260}
            label="API: you own this channel and upload via OAuth. Manual: you are a manager/editor - the studio renders the short and you upload it by hand in YouTube Studio."
          >
            <SegmentedControl
              size="xs"
              value={row.upload_mode}
              disabled={savingMode}
              onChange={(value) => changeMode(value as 'api' | 'manual')}
              data={[
                { label: 'API', value: 'api' },
                { label: 'Manual', value: 'manual' },
              ]}
            />
          </Tooltip>
          {row.error === null && row.upload_mode === 'api' ? (
            <>
              <Badge color={row.connected ? 'green' : 'dark.3'} variant="dot">
                {row.connected ? 'connected' : 'not connected'}
              </Badge>
              <Text size="xs" c="dimmed" ff="monospace" className="tnum" w={110} ta="right">
                {row.remaining_uploads === null
                  ? '—'
                  : `~${row.remaining_uploads} upload${row.remaining_uploads === 1 ? '' : 's'} left today`}
              </Text>
            </>
          ) : null}
        </Group>
      </Group>

      {modeError !== null ? (
        <Alert color="red" variant="light" mt="sm" title="Could not change the upload mode">
          {modeError}
        </Alert>
      ) : null}

      {row.error !== null ? (
        <Alert color="red" variant="light" mt="sm" title="Could not read this channel">
          {row.error}
        </Alert>
      ) : row.upload_mode === 'manual' ? (
        <Text size="sm" c="dimmed" mt="sm">
          Render-only - this channel cannot upload through the API, so there is
          nothing to connect.
        </Text>
      ) : (
        <Group justify="flex-end" mt="sm">
          {row.connected ? (
            <>
              <Button size="xs" variant="default" onClick={() => onConnect(true)}>
                Switch account
              </Button>
              <Button size="xs" color="red" variant="light" onClick={onDisconnect}>
                Disconnect
              </Button>
            </>
          ) : (
            <Button size="xs" color="steel" onClick={() => onConnect(false)}>
              Connect
            </Button>
          )}
        </Group>
      )}
    </Card>
  )
}

/** The Connect/Switch dialog - mirrors AuthStatusBar's own connect dialog
 * (see that component's docstring), differing only in calling the
 * scope-explicit `connectChannel(row.channel, ...)` since this screen has
 * no active editor scope for `startConnect` to read. */
function ConnectDialog({
  target,
  onClose,
  onDone,
}: {
  target: { row: SettingsChannel; force: boolean } | null
  onClose: () => void
  onDone: () => Promise<void>
}) {
  const [channelId, setChannelId] = useState('')
  const [connectJobId, setConnectJobId] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)
  const connectJob = useJobPolling(connectJobId)

  useEffect(() => {
    if (target) {
      setChannelId(target.row.channel_id)
      setStarting(false)
      setStartError(null)
      setConnectJobId(null)
    }
  }, [target])

  const running = connectJob?.status === 'running'
  const failed = connectJob?.status === 'failed'
  const failureReason =
    connectJob && failed
      ? (connectJob.results[channelId.trim()]?.reason ?? 'See the job log for details.')
      : null

  useEffect(() => {
    if (connectJob?.status === 'done') {
      setConnectJobId(null)
      onDone().then(onClose)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectJob?.status])

  async function confirm() {
    if (!target) return
    setStartError(null)
    setStarting(true)
    try {
      const { job_id } = await connectChannel(target.row.channel, channelId.trim() || null, target.force)
      setConnectJobId(job_id)
    } catch (err) {
      setStartError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setStarting(false)
    }
  }

  return (
    <Modal
      opened={target !== null}
      onClose={onClose}
      title={
        target?.force
          ? `Switch ${target.row.channel} to a different account`
          : `Connect ${target?.row.channel ?? ''} to YouTube`
      }
      closeOnEscape
      centered
    >
      {target ? (
        <Stack gap="sm">
          <TextInput
            label="YouTube channel ID"
            description="The common case is to leave this as-is; edit it only to connect a different channel."
            value={channelId}
            onChange={(event) => setChannelId(event.currentTarget.value)}
            disabled={starting || running}
            data-autofocus
          />
          <Text size="sm">
            Confirming opens <strong>your own browser</strong> to Google&apos;s sign-in and
            consent screen - the studio never sees or displays your password.
          </Text>

          {target.force && (
            <Alert color="steel" variant="light" title="This replaces the current connection">
              A token is already stored for this channel, so Google would normally skip
              straight back to it. Confirming forces the consent screen open again - sign in
              as the account you actually want this channel connected as. Nothing changes
              until you finish and grant access to a matching channel.
            </Alert>
          )}

          {running && (
            <Group gap="xs">
              <Loader size={16} color="steel" />
              <Text size="sm" c="dimmed">
                Waiting for consent in your browser… This can take a while - finish signing
                in and approving access over there, then come back here.
              </Text>
            </Group>
          )}

          {startError && (
            <Alert color="red" variant="light" title="Could not start the connection">
              {startError}
            </Alert>
          )}

          {failed && (
            <Alert color="red" variant="light" title="Connection failed">
              {failureReason}
            </Alert>
          )}

          <Group justify="flex-end">
            <Button variant="default" onClick={onClose} disabled={starting || running}>
              Cancel
            </Button>
            <Button
              color="steel"
              loading={starting || running}
              disabled={!channelId.trim()}
              onClick={confirm}
            >
              {target.force ? 'Confirm and switch account' : 'Confirm and connect'}
            </Button>
          </Group>
        </Stack>
      ) : null}
    </Modal>
  )
}

/** The Disconnect dialog: forgets ONLY the locally stored token
 * (DELETE .../auth, see auth.forget_credentials) - full revocation of the
 * app's access stays a manual step in the operator's Google account
 * settings, stated here rather than implied. Gated by typing the channel's
 * `channel_id` (not its slug), since the id is the thing actually being
 * forgotten and a multi-channel operator could otherwise disconnect the
 * wrong row's slug by habit. */
function DisconnectDialog({
  target,
  onClose,
  onDone,
}: {
  target: SettingsChannel | null
  onClose: () => void
  onDone: () => Promise<void>
}) {
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [serverError, setServerError] = useState<string | null>(null)

  useEffect(() => {
    if (target) {
      setTyped('')
      setBusy(false)
      setServerError(null)
    }
  }, [target])

  const confirmed = target !== null && deleteConfirmed(typed, target.channel_id)

  const submit = () => {
    if (!target || !confirmed || busy) return
    setBusy(true)
    setServerError(null)
    disconnectAuth(target.channel)
      .then(() => onDone())
      .then(() => onClose())
      .catch((err) => {
        // A 404 ("was not connected") still means the end state is
        // "disconnected" - refresh the list so the row catches up, rather
        // than leaving the dialog stuck showing a stale "connected" row.
        if (err instanceof ApiError && err.status === 404) {
          onDone().then(onClose)
          return
        }
        setServerError(err instanceof ApiError ? err.message : String(err))
        setBusy(false)
      })
  }

  return (
    <Modal opened={target !== null} onClose={onClose} title="Disconnect channel" centered>
      {target ? (
        <Stack gap="md">
          <Text>
            Forget the stored YouTube connection for{' '}
            <Text span fw={600} ff="monospace">
              {target.channel}
            </Text>
            ?
          </Text>
          <Alert color="steel" variant="light" title="This only forgets the local token">
            The studio will no longer be able to upload as this channel until you connect
            again. This does NOT revoke the app's access in your Google account - do that
            separately, in your Google account's security settings, if you want to fully
            revoke it.
          </Alert>
          <TextInput
            label="Type the channel ID to confirm"
            placeholder={target.channel_id}
            data-autofocus
            value={typed}
            disabled={busy}
            onChange={(e) => setTyped(e.currentTarget.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && confirmed && !busy) submit()
            }}
          />
          {serverError ? (
            <Alert color="red" title="Could not disconnect">
              {serverError}
            </Alert>
          ) : null}
          <Group justify="flex-end">
            <Button variant="default" onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            <Button color="red" onClick={submit} loading={busy} disabled={!confirmed}>
              Disconnect
            </Button>
          </Group>
        </Stack>
      ) : null}
    </Modal>
  )
}
