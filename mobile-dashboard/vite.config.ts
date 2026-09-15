import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import { VitePWA } from 'vite-plugin-pwa';

/**
 * ATLAS mobile dashboard build configuration.
 *
 * Two build targets share this config:
 *   1. the browser PWA (installable, served from any static host);
 *   2. the Capacitor Android shell, which just bundles `dist/` as its webDir.
 *
 * The dev server proxies `/api` and `/ws` at the FastAPI middleware described by
 * FROZEN CONTRACT B (IMPLEMENTATION_ARTIFACT.md §4) so the client never needs a
 * different base URL between `npm run dev` and a packaged build.
 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_');
  const backend = env.VITE_DEV_PROXY_TARGET ?? 'http://localhost:8000';

  return {
    plugins: [
      react(),
      VitePWA({
        registerType: 'autoUpdate',
        injectRegister: 'auto',
        includeAssets: ['favicon.svg', 'icon-192.png', 'icon-512.png', 'icon-maskable-512.png'],
        // The static copy in `public/manifest.webmanifest` is the checked-in
        // source of truth; this block keeps the generated one byte-compatible.
        manifest: {
          id: '/',
          name: 'ATLAS Trading',
          short_name: 'ATLAS',
          description: 'ATLAS human-in-the-loop 1-tap trade confirmation console.',
          start_url: '/',
          scope: '/',
          display: 'standalone',
          orientation: 'portrait',
          background_color: '#05070c',
          theme_color: '#05070c',
          categories: ['finance', 'productivity'],
          icons: [
            { src: 'icon-192.png', sizes: '192x192', type: 'image/png', purpose: 'any' },
            { src: 'icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'any' },
            { src: 'icon-maskable-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
          ],
        },
        workbox: {
          globPatterns: ['**/*.{js,css,html,svg,png,ico,woff2}'],
          cleanupOutdatedCaches: true,
          clientsClaim: true,
          skipWaiting: true,
          navigateFallback: 'index.html',
          // Never let the app-shell service worker swallow live trading traffic
          // or the FCM background worker.
          navigateFallbackDenylist: [/^\/api\//, /^\/ws\//, /^\/firebase-messaging-sw\.js$/],
          runtimeCaching: [
            {
              // Confirmation state is time-critical (60s TTL) — always hit the network.
              urlPattern: /\/api\/v1\/.*/,
              handler: 'NetworkOnly',
            },
          ],
        },
        devOptions: {
          enabled: false,
          type: 'module',
        },
      }),
    ],
    server: {
      host: true,
      port: 5173,
      proxy: {
        '/api': {
          target: backend,
          changeOrigin: true,
          secure: false,
        },
        '/ws': {
          target: backend.replace(/^http/, 'ws'),
          ws: true,
          changeOrigin: true,
          secure: false,
        },
      },
    },
    preview: {
      port: 4173,
      proxy: {
        '/api': { target: backend, changeOrigin: true, secure: false },
        '/ws': { target: backend.replace(/^http/, 'ws'), ws: true, changeOrigin: true },
      },
    },
    build: {
      target: 'es2020',
      outDir: 'dist',
      sourcemap: mode !== 'production',
      chunkSizeWarningLimit: 900,
      rollupOptions: {
        output: {
          manualChunks: {
            react: ['react', 'react-dom'],
            firebase: ['firebase/app', 'firebase/messaging'],
          },
        },
      },
    },
  };
});
