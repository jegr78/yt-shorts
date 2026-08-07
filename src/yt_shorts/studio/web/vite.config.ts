/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The studio is served by FastAPI (see ../api.py) at the WORKSPACE ROOT,
// and the page is a client-side router: the browser path becomes
// /{channel} or /{channel}/{event}. A RELATIVE base ("./") would resolve
// every asset against that deep path (e.g. /erf/assets/… on a reload of
// /erf/studio-test) and 404, so the base must be ABSOLUTE ("/") - assets
// then always resolve from the root the app is actually served from. The
// SPA fallback in api.py serves /assets/… and /favicon.svg by absolute
// path, so this is exactly what it expects.
//
// build.outDir points OUTSIDE this project, into ../static - the directory
// api.py serves from. It is git-ignored build output; a wheel and the release
// binary each build it on the way in, so nobody installing the result needs
// npm (see this directory's README.md).
export default defineConfig({
  plugins: [react()],
  base: '/',
  build: {
    outDir: '../static',
    emptyOutDir: true,
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test-setup.ts'],
  },
})
