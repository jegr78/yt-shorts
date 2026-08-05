import { useCallback, useEffect, useState } from 'react'
import { Alert, Badge, Button, Group, Loader, ScrollArea, Stack, Text, UnstyledButton } from '@mantine/core'
import { ApiError, browseFs, type FsListing } from '../api'

/**
 * Server-side directory navigator for the workspace-manager dialog (see
 * GET /api/fs, api.ts's browseFs). Purely a picker: it never creates or
 * deletes anything, it only reports the highlighted path back to its
 * caller via `onChange` so the dialog can decide what to do with it (open
 * an existing workspace, or use it as the parent for a new one).
 *
 * A single click selects a row without leaving it (`onChange` + highlight
 * via `value`); a double click re-fetches into that row's directory. Both
 * fire on a double click (the browser sends click then dblclick), which is
 * fine here - selecting the directory you are about to enter is harmless.
 */
export function FsBrowser({
  value,
  onChange,
}: {
  value: string | null
  onChange: (path: string, isWorkspace: boolean) => void
}) {
  const [listing, setListing] = useState<FsListing | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback((path?: string) => {
    setLoading(true)
    browseFs(path)
      .then((data) => {
        setListing(data)
        setError(null)
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    load()
  }, [load])

  return (
    <Stack gap="xs">
      <Group gap="xs" wrap="nowrap">
        <Button
          size="xs"
          variant="default"
          disabled={!listing?.parent || loading}
          onClick={() => listing?.parent && load(listing.parent)}
        >
          ↑ Up
        </Button>
        <Text size="xs" ff="monospace" c="dimmed" style={{ overflowWrap: 'anywhere' }}>
          {listing?.path ?? '…'}
        </Text>
      </Group>
      {error ? (
        <Alert color="red" title="Could not read this folder">
          {error}
        </Alert>
      ) : (
        <ScrollArea.Autosize mah={320} type="auto">
          <Stack gap={2}>
            {loading && !listing ? (
              <Group gap="xs" p="sm">
                <Loader size={16} color="steel" />
                <Text size="xs" c="dimmed">
                  Loading…
                </Text>
              </Group>
            ) : listing && listing.entries.length === 0 ? (
              <Text size="xs" c="dimmed" p="sm">
                No sub-folders here.
              </Text>
            ) : (
              listing?.entries.map((entry) => (
                <UnstyledButton
                  key={entry.path}
                  onClick={() => onChange(entry.path, entry.is_workspace)}
                  onDoubleClick={() => load(entry.path)}
                  p="6px 8px"
                  style={{
                    borderRadius: 6,
                    background:
                      value === entry.path ? 'var(--mantine-color-dark-5)' : 'transparent',
                  }}
                >
                  <Group justify="space-between" wrap="nowrap">
                    <Text size="sm" ff="monospace">
                      📁 {entry.name}
                    </Text>
                    {entry.is_workspace ? (
                      <Badge size="xs" color="green" variant="light">
                        workspace
                      </Badge>
                    ) : null}
                  </Group>
                </UnstyledButton>
              ))
            )}
          </Stack>
        </ScrollArea.Autosize>
      )}
      <Text size="xs" c="dimmed">
        Single-click to select a folder, double-click to open it.
      </Text>
    </Stack>
  )
}
