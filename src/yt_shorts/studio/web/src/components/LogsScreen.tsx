import { useEffect, useState, type CSSProperties } from 'react'
import {
  Alert,
  Anchor,
  Badge,
  Box,
  Card,
  Center,
  Group,
  Loader,
  ScrollArea,
  Select,
  Stack,
  Text,
  UnstyledButton,
} from '@mantine/core'
import { ApiError, listLogs, readLog, type LogListing } from '../api'
import {
  appendLines,
  downloadFilename,
  downloadLogText,
  formatSize,
  initialLogFileFromSearch,
  jobKindFromLogName,
  logBaseName,
  parseLine,
  type LogLevel,
} from '../logs'
import { routePath } from '../scopedApi'
import { NavScreen } from './NavScreen'

const POLL_INTERVAL_MS = 2000

const LEVEL_COLOR: Record<LogLevel, string | undefined> = {
  ERROR: 'red.4',
  WARNING: 'yellow.4',
  INFO: undefined,
  OTHER: undefined,
}

function rowStyle(selected: boolean): CSSProperties {
  return {
    borderRadius: 'var(--mantine-radius-sm)',
    borderLeft: `3px solid ${selected ? 'var(--mantine-color-steel-4)' : 'transparent'}`,
    backgroundColor: selected ? 'var(--mantine-color-dark-7)' : 'transparent',
    display: 'block',
    width: '100%',
    textAlign: 'left',
  }
}

/** The workspace-level Logs screen: a read-only view over `<workspace>/logs/`
 * (see api.py's `GET /api/logs*` routes and logsetup.py) - the central
 * studio log, its rotated/gzipped date archives, and every background job's
 * own log file. Structural sibling of SettingsScreen (same NavScreen chrome,
 * loading/error states) but its own content is a two-pane layout: a left
 * list of files, a right pane rendering whichever one is selected.
 *
 * A live (non-archive) selection polls `readLog(name, {after: position})`
 * on a ~2s interval, feeding `appendLines` - the same tailing idiom
 * `useJobPolling` uses for jobs, but inlined here rather than shared since
 * the two poll different shapes (a job snapshot vs. a byte-position tail).
 * Polling stops the moment an archive date is chosen (it is immutable), and
 * also - see the `content.position === after` branch below - the moment a
 * "live" selection turns out to already be a finished job's gzipped log
 * (rotated out from under us mid-session by `finish_job_log`): the server's
 * own fallback then returns the WHOLE file every time with `position`
 * merely echoing back whatever was sent (`api.py`'s `_log_body`), which
 * would otherwise re-append the entire file on every poll forever.
 *
 * The per-job "View log" links (RenderPanel/StreamPanel/UploadPanel)
 * navigate to `/logs?file=<name>` - `parseRoute` only ever reads
 * `window.location.pathname` (see scopedApi.ts), so that query string is
 * read directly here, once, via `logs.ts`'s `initialLogFileFromSearch`. */
export function LogsScreen() {
  const [listing, setListing] = useState<LogListing | null>(null)
  const [listError, setListError] = useState<string | null>(null)

  const [selectedName, setSelectedName] = useState<string | null>(() =>
    initialLogFileFromSearch(window.location.search),
  )
  // Set only while an ARCHIVE date is chosen for the central log - null
  // means "the live file", which is the only case that ever polls.
  const [selectedDate, setSelectedDate] = useState<string | null>(null)

  const [lines, setLines] = useState<string[]>([])
  const [viewerLoading, setViewerLoading] = useState(false)
  const [viewerError, setViewerError] = useState<string | null>(null)
  // True only while the current selection is still being polled - false for
  // an explicit archive AND for a "live" job selection that turned out to
  // already be finished/gzipped (see the tailing effect below), so the
  // header badge never claims "live" for content that will not change again.
  const [tailing, setTailing] = useState(false)

  useEffect(() => {
    let cancelled = false
    listLogs()
      .then((data) => {
        if (cancelled) return
        setListing(data)
        setListError(null)
        // Default to the central log only if nothing was preselected via
        // ?file= - a query-preselected name is tried as-is even if it is
        // not (yet) in this listing; readLog's own fallback still resolves
        // it (see logBaseName's docstring), and a genuinely stale/unknown
        // name simply surfaces as a viewer error rather than being silently
        // overridden here.
        setSelectedName((current) => current ?? data.central.name)
      })
      .catch((err) => {
        if (!cancelled) setListError(err instanceof ApiError ? err.message : String(err))
      })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    if (!selectedName) return undefined
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const name = selectedName
    const date = selectedDate

    async function load(after: number) {
      if (after === 0) setViewerLoading(true)
      try {
        const content = date ? await readLog(name, { date }) : await readLog(name, { after })
        if (cancelled) return
        setViewerError(null)
        if (date || (content.position === after && content.lines.length > 0)) {
          // An explicit archive, or a live selection that turned out to
          // already be a finished job's gzipped log - either way the
          // content is now immutable: replace wholesale, never poll again.
          setLines(content.lines)
          setTailing(false)
        } else {
          setLines((existing) => appendLines(existing, content.lines))
          setTailing(true)
          timer = setTimeout(() => load(content.position), POLL_INTERVAL_MS)
        }
      } catch (error) {
        if (cancelled) return
        setViewerError(error instanceof ApiError ? error.message : String(error))
      } finally {
        if (!cancelled) setViewerLoading(false)
      }
    }

    setLines([])
    setViewerError(null)
    setTailing(false)
    load(0)

    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [selectedName, selectedDate])

  function selectLive(name: string) {
    setSelectedName(name)
    setSelectedDate(null)
  }

  function selectArchive(name: string, date: string) {
    setSelectedName(name)
    setSelectedDate(date)
  }

  // Built client-side from the lines already fetched, not from a link to
  // /api/logs/<name> - that route returns a JSON body, so a plain href
  // there handed the operator a JSON blob instead of a log (see logs.ts's
  // downloadLogText/downloadFilename docstrings). A Blob + object URL needs
  // no new backend route or traversal guard; the URL is revoked right after
  // the click so it does not leak for the life of the tab.
  function downloadLog() {
    if (!selectedName) return
    const blob = new Blob([downloadLogText(lines)], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = downloadFilename(selectedName, selectedDate)
    anchor.click()
    URL.revokeObjectURL(url)
  }

  return (
    <NavScreen
      crumbs={[{ label: 'Channels', path: routePath({ screen: 'channels' }) }, { label: 'Logs' }]}
      title="Logs"
      subtitle="The studio's own central log, its rotated archives, and every background job's log."
    >
      {listError ? (
        <Alert color="red" title="Could not load logs">
          {listError} - check that the studio server is still running, then reload this page.
        </Alert>
      ) : listing === null ? (
        <Center py="xl">
          <Stack align="center" gap="xs">
            <Loader color="steel" />
            <Text size="xs" c="dimmed">
              Loading logs…
            </Text>
          </Stack>
        </Center>
      ) : (
        <Box style={{ display: 'flex', gap: 16, height: '65vh', minHeight: 360, alignItems: 'stretch' }}>
          <Card
            padding="sm"
            withBorder
            style={{ width: 300, flexShrink: 0, display: 'flex', flexDirection: 'column', minHeight: 0 }}
          >
            <ScrollArea style={{ flex: '1 1 auto', minHeight: 0 }} offsetScrollbars>
              <Stack gap="md">
                <Stack gap={4}>
                  <UnstyledButton
                    onClick={() => selectLive(listing.central.name)}
                    p="xs"
                    style={rowStyle(selectedName === listing.central.name && selectedDate === null)}
                  >
                    <Group justify="space-between" wrap="nowrap">
                      <Text size="sm" fw={600} truncate>
                        {listing.central.name}
                      </Text>
                      <Text size="xs" c="dimmed" ff="monospace" style={{ flexShrink: 0 }}>
                        {formatSize(listing.central.size)}
                      </Text>
                    </Group>
                    <Text size="xs" c="dimmed">
                      Central log
                    </Text>
                  </UnstyledButton>
                  {listing.archives.length > 0 && (
                    <Select
                      size="xs"
                      placeholder="Live"
                      data={listing.archives}
                      value={selectedName === listing.central.name ? selectedDate : null}
                      onChange={(value) => value && selectArchive(listing.central.name, value)}
                      clearable
                      onClear={() => selectLive(listing.central.name)}
                    />
                  )}
                </Stack>

                <Stack gap={4}>
                  <Text size="xs" c="dimmed" fw={600} tt="uppercase">
                    Job logs
                  </Text>
                  {listing.jobs.length === 0 ? (
                    <Text size="xs" c="dimmed" p="xs">
                      No job logs yet.
                    </Text>
                  ) : (
                    listing.jobs.map((entry) => {
                      const kind = jobKindFromLogName(entry.name)
                      const selected =
                        selectedDate === null &&
                        selectedName !== null &&
                        logBaseName(selectedName) === logBaseName(entry.name)
                      return (
                        <UnstyledButton
                          key={entry.name}
                          onClick={() => selectLive(entry.name)}
                          p="xs"
                          style={rowStyle(selected)}
                        >
                          <Group justify="space-between" wrap="nowrap" gap="xs">
                            <Group gap={6} wrap="nowrap" style={{ minWidth: 0 }}>
                              {kind && (
                                <Badge
                                  size="xs"
                                  variant="dot"
                                  color="steel"
                                  tt="none"
                                  style={{ flexShrink: 0 }}
                                >
                                  {kind}
                                </Badge>
                              )}
                              <Text size="xs" ff="monospace" truncate>
                                {entry.name}
                              </Text>
                            </Group>
                            <Text size="xs" c="dimmed" ff="monospace" style={{ flexShrink: 0 }}>
                              {formatSize(entry.size)}
                            </Text>
                          </Group>
                        </UnstyledButton>
                      )
                    })
                  )}
                </Stack>
              </Stack>
            </ScrollArea>
          </Card>

          <Card
            padding="sm"
            withBorder
            style={{ flex: '1 1 auto', minWidth: 0, display: 'flex', flexDirection: 'column', minHeight: 0 }}
          >
            <Group justify="space-between" wrap="nowrap" mb="xs">
              <Group gap={6} wrap="nowrap" style={{ minWidth: 0 }}>
                <Text fw={600} size="sm" truncate ff="monospace">
                  {selectedName ?? '—'}
                </Text>
                {selectedDate ? (
                  <Badge size="xs" color="dark.3">
                    {selectedDate}
                  </Badge>
                ) : tailing ? (
                  <Badge size="xs" color="green" variant="dot">
                    live
                  </Badge>
                ) : selectedName && lines.length > 0 ? (
                  <Badge size="xs" color="dark.3">
                    finished
                  </Badge>
                ) : null}
              </Group>
              {selectedName && lines.length > 0 && (
                <Anchor size="xs" c="steel.3" component="button" onClick={downloadLog}>
                  Download
                </Anchor>
              )}
            </Group>

            <Box
              style={{
                flex: '1 1 auto',
                minHeight: 0,
                overflowY: 'auto',
                background: 'var(--mantine-color-dark-8)',
                borderRadius: 'var(--mantine-radius-sm)',
                padding: 8,
              }}
            >
              {viewerError ? (
                <Alert color="red" variant="light" title="Could not load this log">
                  {viewerError}
                </Alert>
              ) : viewerLoading && lines.length === 0 ? (
                <Center py="xl">
                  <Loader size={16} color="steel" />
                </Center>
              ) : lines.length === 0 ? (
                <Text size="xs" c="dimmed">
                  No lines yet.
                </Text>
              ) : (
                <Stack gap={2}>
                  {lines.map((line, index) => {
                    const level = parseLine(line).level
                    return (
                      <Text
                        key={index}
                        size="xs"
                        ff="monospace"
                        c={LEVEL_COLOR[level]}
                        style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}
                      >
                        {line}
                      </Text>
                    )
                  })}
                </Stack>
              )}
            </Box>
          </Card>
        </Box>
      )}
    </NavScreen>
  )
}
