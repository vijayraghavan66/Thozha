import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import type { ProxyOptions } from 'vite';

const upstream = new URL(process.env.THOZHA_WEB_UPSTREAM || 'http://127.0.0.1:8000');
if (
  !['127.0.0.1', '[::1]'].includes(upstream.hostname) ||
  upstream.protocol !== 'http:' ||
  upstream.href !== `${upstream.origin}/`
) {
  throw new Error(
    'THOZHA_WEB_UPSTREAM must be a loopback HTTP origin, without a path or credentials.'
  );
}
const uuid = '[a-fA-F0-9-]{36}';
const id = '[a-f0-9]{32}';
const webPaths = `^/v1/web/(?:${[
  'session(?:/login|/logout)?',
  `conversations(?:/${uuid}(?:/archive|/messages(?:/stream|/${uuid}/cancel)?)?)?`,
  `memory(?:/search|/${id})?`,
  `tasks(?:/${id}(?:/events(?:/stream)?|/cancel|/artifacts(?:/[a-f0-9]{64}/download)?)?)?`,
  `approvals/(?:pending|${id}(?:/decision)?)`,
  `devices(?:/${id}(?:/revoke)?)?`,
  'automations(?:/capabilities|/[a-f0-9]{16}(?:/history|/enable|/disable|/pause|/trigger)?)?',
].join('|')})(?:\\?|$)`;
const proxy: Record<string, ProxyOptions> = {
  [webPaths]: {
    target: upstream.origin,
    changeOrigin: false,
    configure(server) {
      server.on('proxyRes', response => {
        response.headers['cache-control'] = 'no-store';
      });
    },
  },
};

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { host: '127.0.0.1', port: 5173, strictPort: true, proxy },
  preview: { host: '127.0.0.1', port: 4173, strictPort: true, proxy },
  test: {
    include: ['src/**/*.test.{ts,tsx}'],
    environment: 'jsdom',
    setupFiles: ['src/test/setup.ts'],
  },
});
