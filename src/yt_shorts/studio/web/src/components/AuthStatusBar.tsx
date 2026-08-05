import { useEffect, useState } from 'react'
import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Group,
  Loader,
  Modal,
  Stack,
  Text,
  TextInput,
  Tooltip,
} from '@mantine/core'
import { ApiError, startConnect, type AuthStatus } from '../api'
import { useJobPolling } from '../hooks/useJobPolling'

interface AuthStatusBarProps {
  auth: AuthStatus | null
  error: string | null
  loading: boolean
  onRefresh: () => void
}

/**
 * The channel's connection to YouTube and today's remaining upload quota
 * (see GET /api/auth in api.py) - shown in the header so it stays visible
 * regardless of which clip is selected, true across the whole event
 * rather than one clip, like ClipList's own "N of M clips" summary line.
 *
 * The studio never touches the operator's Google password and never
 * renders a token or client secret anywhere (see CLAUDE.md's stage E
 * boundaries). When disconnected, "Connect channel" opens a dialog that
 * runs the OAuth browser consent flow ITSELF as a background job (POST
 * /api/auth/connect, see studio.jobs.start_connect_job) - the operator
 * approves access in their OWN browser window, which the server opens;
 * this component only ever sends a channel id and polls a job id, never
 * anything credential-shaped.
 *
 * "Switch account" is the SAME dialog and the SAME route, opened even
 * while connected, with `force: true` (see api.ts's startConnect and
 * api.py's ConnectBody) - the backend's authorize() otherwise reuses a
 * valid stored token, so this is the only way an operator who granted
 * consent for the wrong Google account (or who simply wants a different
 * one) can re-run the consent screen. A forced job can still fail with a
 * channel-mismatch reason from the server's post-consent verification
 * (see auth.py) - that reason surfaces in the same failed-Alert below and
 * the dialog stays open so the operator can retry, exactly like a
 * first-time connect failure.
 */
export function AuthStatusBar({ auth, error, loading, onRefresh }: AuthStatusBarProps) {
  const [modalOpen, setModalOpen] = useState(false)
  const [forceMode, setForceMode] = useState(false)
  const [channelId, setChannelId] = useState('')
  const [connectJobId, setConnectJobId] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)
  const connectJob = useJobPolling(connectJobId)

  const running = connectJob?.status === 'running'
  const failed = connectJob?.status === 'failed'
  const failureReason =
    connectJob && failed
      ? (connectJob.results[channelId.trim()]?.reason ?? 'See the job log for details.')
      : null

  function openModal(force: boolean) {
    // Pre-filled from the profile's own channel id each time the dialog
    // opens, so it reflects whatever GET /api/auth most recently reported
    // - editable, because a multi-channel operator may want to connect a
    // DIFFERENT channel than this studio's own (see api.py's ConnectBody).
    setChannelId(auth?.channel_id ?? '')
    setForceMode(force)
    setStartError(null)
    setConnectJobId(null)
    setModalOpen(true)
  }

  async function confirm() {
    setStartError(null)
    setStarting(true)
    try {
      const { job_id } = await startConnect(channelId.trim() || null, forceMode)
      setConnectJobId(job_id)
    } catch (err) {
      setStartError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setStarting(false)
    }
  }

  // The job finishing "done" means the token is stored - close the dialog
  // and re-fetch GET /api/auth so the bar itself flips to "Connected"
  // (this component never assumes success locally; onRefresh is the same
  // callback the manual refresh button uses).
  useEffect(() => {
    if (connectJob?.status === 'done') {
      setModalOpen(false)
      setConnectJobId(null)
      onRefresh()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectJob?.status])

  return (
    <Group gap="xs" wrap="nowrap">
      {loading && !auth && !error ? (
        <Loader size={14} color="steel" />
      ) : error ? (
        <Tooltip label={error}>
          <Badge color="dark.3" variant="dot" size="xs">
            Auth status unavailable
          </Badge>
        </Tooltip>
      ) : auth?.upload_mode === 'manual' ? (
        <Badge color="grape" variant="light" size="xs">
          Render-only (manual upload)
        </Badge>
      ) : auth?.connected ? (
        <>
          <Badge color="green" variant="dot" size="xs">
            Connected
          </Badge>
          <Text size="xs" c="dimmed" ff="monospace" className="tnum">
            {auth.remaining_uploads} upload{auth.remaining_uploads === 1 ? '' : 's'} left today
          </Text>
          <Tooltip label="Re-run consent to connect a different Google account">
            <Button size="xs" variant="subtle" color="steel" onClick={() => openModal(true)}>
              Switch account
            </Button>
          </Tooltip>
        </>
      ) : (
        <>
          <Badge color="dark.3" variant="dot" size="xs">
            Not connected
          </Badge>
          <Button size="xs" variant="default" onClick={() => openModal(false)}>
            Connect channel
          </Button>
        </>
      )}
      <Tooltip label="Re-check the connection and quota">
        <ActionIcon
          variant="subtle"
          color="steel"
          size="sm"
          onClick={onRefresh}
          aria-label="Refresh auth status"
        >
          ⟲
        </ActionIcon>
      </Tooltip>

      <Modal
        opened={modalOpen}
        onClose={() => setModalOpen(false)}
        title={forceMode ? 'Switch this channel to a different account' : 'Connect this channel to YouTube'}
        closeOnEscape
      >
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

          {forceMode && (
            <Alert color="steel" variant="light" title="This replaces the current connection">
              A token is already stored for this channel, so Google would normally skip straight
              back to it. Confirming forces the consent screen open again - sign in as the account
              you actually want this channel connected as. Nothing changes until you finish and
              grant access to a matching channel.
            </Alert>
          )}

          {running && (
            <Group gap="xs">
              <Loader size={16} color="steel" />
              <Text size="sm" c="dimmed">
                Waiting for consent in your browser… This can take a while - finish signing in and
                approving access over there, then come back here.
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
            <Button variant="default" onClick={() => setModalOpen(false)}>
              Cancel
            </Button>
            <Button
              color="steel"
              loading={starting || running}
              disabled={!channelId.trim()}
              onClick={confirm}
            >
              {forceMode ? 'Confirm and switch account' : 'Confirm and connect'}
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Group>
  )
}
