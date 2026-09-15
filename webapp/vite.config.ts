import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Two consumers, one config.
//
//   npm run dev    -- the local harness on :5174, proxying /api to the standalone server.
//   npm run build  -- the bundle the preview container serves at /tools/emi.
//
// `base` has to match the path the app is served under, because Vite bakes it into the
// asset URLs in index.html. Served at a sub-path with base "/", every bundle request goes
// to /assets/... , which on the real site is embeddedci-server's own asset route -- the app
// loads a blank page and the network tab shows 200s.
//
// This is still not how the finished integration ships: that build happens inside
// embeddedci-server's own webapp, from the same src/.
export default defineConfig({
  base: process.env.EMI_BASE || '/',
  plugins: [react()],
  server: {
    port: 5174,
    // The standalone EMI control plane from emi-analyzer/server.
    proxy: { '/api': 'http://localhost:8090' },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
  },
  appType: 'spa',
})
