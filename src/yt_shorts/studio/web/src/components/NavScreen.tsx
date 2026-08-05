import type { ReactNode } from 'react'
import { Anchor, Box, Container, Group, Text, Title } from '@mantine/core'
import { navigate } from '../useRoute'

/** A breadcrumb crumb: a link when it has a target, plain text for the
 * current location (the last crumb). */
export interface Crumb {
  label: string
  path?: string
}

/** The shared chrome for the two navigation screens (channels, events) -
 * the same dark broadcast-monitor masthead the editor's AppShell header
 * uses (see App.tsx / theme.ts), so moving between the start screens and
 * the editor never looks like a different application. The editor keeps its
 * own AppShell (it needs the navbar); these two lighter screens do not. */
export function NavScreen({
  crumbs,
  title,
  subtitle,
  children,
  fillHeight,
}: {
  crumbs: Crumb[]
  title: string
  subtitle?: string
  children: ReactNode
  /** When true, the content column FILLS the available height instead of
   * scrolling as a whole, so a child that owns its own scroll region (e.g.
   * StreamScreen's transcript pane, which must keep its search box in view
   * while the transcript scrolls beneath it) actually gets a bounded height
   * to size against. Default false/omitted keeps the original behaviour
   * every other caller of this component relies on: the whole content
   * column scrolls together, which is correct for a screen that is just a
   * list or a form with no internal scroll region of its own. */
  fillHeight?: boolean
}) {
  return (
    <Box
      style={{
        height: '100vh',
        display: 'flex',
        flexDirection: 'column',
        background: 'var(--mantine-color-dark-9)',
      }}
    >
      <Box
        component="header"
        h={52}
        px="md"
        style={{
          flex: '0 0 auto',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          background: 'var(--mantine-color-dark-9)',
          borderBottom: '1px solid var(--mantine-color-dark-6)',
        }}
      >
        <Group gap="sm" wrap="nowrap">
          <Title order={4} tt="uppercase" style={{ letterSpacing: '0.06em' }}>
            YT-Shorts Studio
          </Title>
          <Group gap={6} wrap="nowrap" visibleFrom="sm">
            {crumbs.map((crumb, index) => (
              <Group gap={6} wrap="nowrap" key={`${crumb.label}-${index}`}>
                <Text c="dimmed" size="sm">
                  /
                </Text>
                {crumb.path !== undefined ? (
                  <Anchor size="sm" c="steel.3" onClick={() => navigate(crumb.path as string)}>
                    {crumb.label}
                  </Anchor>
                ) : (
                  <Text size="sm">{crumb.label}</Text>
                )}
              </Group>
            ))}
          </Group>
        </Group>
        <Text size="xs" c="dimmed" ff="monospace" visibleFrom="sm">
          LOCAL EDITOR
        </Text>
      </Box>
      <Box
        style={{
          flex: '1 1 auto',
          minHeight: 0,
          ...(fillHeight ? { overflow: 'hidden' } : { overflowY: 'auto' }),
        }}
      >
        <Container
          size="md"
          py="xl"
          style={fillHeight ? { height: '100%', display: 'flex', flexDirection: 'column' } : undefined}
        >
          <Title order={2} mb={subtitle ? 4 : 'lg'}>
            {title}
          </Title>
          {subtitle ? (
            <Text c="dimmed" mb="lg">
              {subtitle}
            </Text>
          ) : null}
          {fillHeight ? (
            <Box style={{ flex: '1 1 auto', minHeight: 0, display: 'flex', flexDirection: 'column' }}>
              {children}
            </Box>
          ) : (
            children
          )}
        </Container>
      </Box>
    </Box>
  )
}
