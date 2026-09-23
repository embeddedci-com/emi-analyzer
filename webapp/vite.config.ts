import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The dev harness (index.html -> dev/main.tsx), against emi-server.
//
//   npm run dev    -- the harness on :5174, proxying /api to emi-server on :8090.
//                     /dev/ is the bare board renderer (dev/index.html).
//   EMI_BASE=/tools/emi/ npm run build
//                  -- dist/, for `emi-server -webapp dist` to serve at /tools/emi.
//
// `base` has to match the path the app is served under, because Vite bakes it into the
// asset URLs in index.html. Served at a sub-path with base "/", every bundle request goes
// to /assets/... , which is outside the sub-path -- the app loads a blank page.
//
// The local app has its own config, vite.app.config.ts.
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
