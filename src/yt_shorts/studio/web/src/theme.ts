import { createTheme, type MantineColorsTuple } from '@mantine/core'

/**
 * The studio's own visual identity: a motorsport timing-tower aesthetic,
 * deliberately NOT derived from any one channel's brand (see the studio's
 * multi-channel boundary in ../api.py's module docstring - this tool edits
 * clips from whichever channel/event it was started against, and a second
 * league's footage will be reviewed in the same chrome). The preview panel
 * is where a channel's own brand colours appear, because that IS the
 * artifact being edited; everything around it stays neutral so the
 * preview still reads as the thing being made, not more studio chrome.
 *
 * Two colour vocabularies, kept deliberately separate:
 *  - "steel" is the ONE interactive accent (buttons, focus rings, the
 *    selected clip in the tower) - a desaturated slate blue, chosen
 *    specifically because it cannot be mistaken for any status colour.
 *  - green/yellow(amber)/violet/red are the sector-style status colours a
 *    timing screen already uses - kept, candidate, edited and danger -
 *    and NOTHING else borrows them, so an operator's read of "amber row"
 *    never has to compete with an unrelated UI use of the same hue.
 * Every one of these has been checked for contrast against the actual
 * surfaces it sits on (see the redesign report for the measured ratios) -
 * not carried over from a light theme by assumption.
 */

// Neutral graphite/blue-grey surfaces - shade 0 (lightest) -> 9 (darkest).
// Overrides Mantine's stock "dark" palette so every built-in dark-scheme
// surface (Paper, Card, Menu, Modal, Tooltip, ...) already lands on this
// ground without a parallel stylesheet fighting it.
const graphite: MantineColorsTuple = [
  '#E7EAEF', // 0 primary text
  '#C7CCD6', // 1 heading text (slightly softer than pure primary)
  '#9DA6B5', // 2 secondary text
  '#707886', // 3 tertiary / dimmed text / discarded-status ink
  '#4C5561', // 4 strong divider, muted icon
  '#3D434D', // 5 hover surface, strong border
  '#2B313B', // 6 default border
  '#1F232B', // 7 panel-raised (cards nested in a panel, row hover)
  '#181B21', // 8 panel surface (Navbar, Paper, Card)
  '#12151A', // 9 app ground (body, header, footer bars)
]

// The one interactive accent: a desaturated steel blue. Never used for
// status.
const steel: MantineColorsTuple = [
  '#DDE7EE',
  '#C2D4E0',
  '#A1BBCE',
  '#7FA3BD',
  '#5D8BAC',
  '#4C7694', // shade 5: primary filled-button/focus-ring colour
  '#40637D',
  '#365368',
  '#2B4354',
  '#1F303D',
]

// kept
const sectorGreen: MantineColorsTuple = [
  '#D9F2E6',
  '#BAE8D3',
  '#94DBBA',
  '#6ECFA1',
  '#47C289', // shade 4: text/dot ink
  '#38A874',
  '#2F8E61',
  '#287752',
  '#206042',
  '#17452F',
]

// candidate / undecided
const sectorAmber: MantineColorsTuple = [
  '#F6EAD5',
  '#EFD9B3',
  '#E6C489',
  '#DDAF5F', // shade 3: text/dot ink
  '#D49A35',
  '#B98427',
  '#9C6F21',
  '#825D1C',
  '#694B16',
  '#4C3610',
]

// has hand corrections
const sectorViolet: MantineColorsTuple = [
  '#E0D7F4',
  '#C7B8EA',
  '#A890DF', // shade 2: text/dot ink
  '#8969D3',
  '#6A41C8',
  '#5832AE',
  '#4A2A92',
  '#3E247B',
  '#321D63',
  '#241547',
]

// genuine errors and failures - never used for clip status, so "something
// is wrong" is never confused with "this clip is a candidate/undecided".
const danger: MantineColorsTuple = [
  '#F5D9D6',
  '#EEBAB5',
  '#E4948B',
  '#DA6E62', // shade 3: text/icon ink
  '#D04839',
  '#B6392B',
  '#993024',
  '#80281E',
  '#672018',
  '#4A1711',
]

export const theme = createTheme({
  primaryColor: 'steel',
  primaryShade: 5,
  autoContrast: true,
  fontFamily:
    '"IBM Plex Sans", -apple-system, "Segoe UI", sans-serif',
  fontFamilyMonospace:
    '"IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, monospace',
  // The condensed face is scoped deliberately to the clip tower's own
  // labels (see ClipList.tsx's TOWER_FONT) rather than applied here at
  // the theme's headings level - Mantine's Title is only ever the page's
  // own masthead, which is not "the tower's labels" the direction asks
  // the condensed face to earn its place in.
  headings: {
    fontWeight: '700',
  },
  defaultRadius: 'sm',
  colors: {
    dark: graphite,
    steel,
    green: sectorGreen,
    yellow: sectorAmber,
    amber: sectorAmber,
    violet: sectorViolet,
    red: danger,
  },
  black: '#0B0D10',
  white: '#F4F6F9',
  components: {
    Badge: {
      defaultProps: {
        radius: 'sm',
        tt: 'uppercase',
      },
    },
    Card: {
      defaultProps: {
        radius: 'md',
        withBorder: true,
      },
    },
    Paper: {
      defaultProps: {
        withBorder: true,
      },
    },
    Alert: {
      defaultProps: {
        radius: 'sm',
        variant: 'light',
      },
    },
    Button: {
      defaultProps: {
        radius: 'sm',
      },
    },
  },
})
