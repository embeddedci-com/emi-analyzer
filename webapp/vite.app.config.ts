import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The local (desktop) app: app/ is its shell, src/ is the same analyzer every host mounts.
//
//   npx vite --config vite.app.config.ts          dev on :5175, /api and /blob proxied to emi-local
//   npx vite build --config vite.app.config.ts    -> dist-app/, embedded into the emi-local binary
//
// Served from the root of its own origin, so base is "/".
export default defineConfig({
  root: 'app',
  base: '/',
  plugins: [react()],
  // The app's version, for the report's title page. `make local` and the release workflow set
  // VERSION; a build without one says "dev".
  define: {
    'import.meta.env.VITE_EMI_VERSION': JSON.stringify(process.env.VERSION || 'dev'),
  },
  build: { outDir: '../dist-app', emptyOutDir: true },
  server: {
    port: 5175,
    proxy: {
      '/api': 'http://127.0.0.1:7465',
      '/blob': 'http://127.0.0.1:7465',
    },
  },
  appType: 'spa',
})
