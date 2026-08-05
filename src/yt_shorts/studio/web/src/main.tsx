import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { MantineProvider } from '@mantine/core'
import { Notifications } from '@mantine/notifications'
import '@mantine/core/styles.css'
import '@mantine/notifications/styles.css'

// Self-hosted fonts (see theme.ts's own docstring on why these are the
// studio's own identity, not the channel's): @fontsource bundles the
// woff2 files as build assets, so Vite emits them into static/ alongside
// the rest of the built page - no CDN, no runtime fetch from a third
// party, and the built bundle stays self-contained (see README.md,
// "Studio", on why that self-containment matters - the tool must run
// from a clone with no network access).
// Latin subset only (see CLAUDE.md, "Language": the interface is English
// only) - importing the subset-specific files instead of the umbrella
// weight files keeps cyrillic/greek/vietnamese glyphs this UI never uses
// out of the committed static/ bundle.
import '@fontsource/ibm-plex-sans/latin-400.css'
import '@fontsource/ibm-plex-sans/latin-500.css'
import '@fontsource/ibm-plex-sans/latin-600.css'
import '@fontsource/ibm-plex-sans/latin-700.css'
import '@fontsource/ibm-plex-sans-condensed/latin-500.css'
import '@fontsource/ibm-plex-sans-condensed/latin-600.css'
import '@fontsource/ibm-plex-sans-condensed/latin-700.css'
import '@fontsource/ibm-plex-mono/latin-400.css'
import '@fontsource/ibm-plex-mono/latin-500.css'

import './index.css'
import { theme } from './theme'
import { Root } from './Root.tsx'
import { ErrorBoundary } from './components/ErrorBoundary.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {/* The direction is a single dark broadcast-monitor identity, not a
        light/dark pair (see theme.ts) - forced rather than "auto" so the
        studio never gets read against an unreviewed light palette. */}
    <MantineProvider theme={theme} defaultColorScheme="dark" forceColorScheme="dark">
      <Notifications position="top-right" />
      <ErrorBoundary>
        <Root />
      </ErrorBoundary>
    </MantineProvider>
  </StrictMode>,
)
