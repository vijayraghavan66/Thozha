import { defineConfig } from 'vitest/config';
import { execFileSync } from 'node:child_process';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import type { IncomingMessage, ServerResponse } from 'node:http';
import type { Plugin, ProxyOptions } from 'vite';
import { localVoice } from './voice/plugin';

const upstreamValue = process.env.THOZHA_WEB_UPSTREAM;
const upstream = upstreamValue ? new URL(upstreamValue) : undefined;
if (
  upstream &&
  (!['127.0.0.1', '[::1]'].includes(upstream.hostname) ||
    upstream.protocol !== 'http:' ||
    upstream.href !== `${upstream.origin}/`)
) {
  throw new Error(
    'THOZHA_WEB_UPSTREAM must be a loopback HTTP origin, without a path or credentials.'
  );
}
const repositoryRoot = execFileSync('git', ['rev-parse', '--show-toplevel'], {
  encoding: 'utf8',
}).trim();
const revision = execFileSync('git', ['-C', repositoryRoot, 'rev-parse', '--short=12', 'HEAD'], {
  encoding: 'utf8',
}).trim();
const dirty = execFileSync('git', ['-C', repositoryRoot, 'status', '--porcelain', '--', 'web'], {
  encoding: 'utf8',
}).trim();
const frontendRevision = `${revision}${dirty ? '-dirty' : ''}`;
const uuid = '[a-fA-F0-9-]{36}';
const id = '[a-f0-9]{32}';
const webPaths = `^/v1/web/(?:${[
  'session(?:/login|/logout)?',
  `conversations(?:/${uuid}(?:/archive|/attachments(?:/${id})?|/messages(?:/stream|/${uuid}/cancel)?)?)?`,
  `memory(?:/search|/${id})?`,
  'supervisor/readiness',
  'device-platform/inventory',
  `tasks(?:/${id}(?:/events(?:/stream)?|/cancel|/artifacts(?:/[a-f0-9]{64}/download)?)?)?`,
  `approvals/(?:pending|${id}(?:/decision)?)`,
  `devices(?:/pairing-invitations(?:/${id}(?:/cancel)?)?|/pairing-requests(?:/${id}/decision)?|/${id}(?:/revoke)?)?`,
  'automations(?:/capabilities|/[a-f0-9]{16}(?:/history|/enable|/disable|/pause|/trigger)?)?',
].join('|')})(?:\\?|$)`;
const proxy: Record<string, ProxyOptions> = upstream
  ? {
      [webPaths]: {
        target: upstream.origin,
        changeOrigin: process.env.THOZHA_VOICE_DEV === '1',
        configure(server) {
          server.on('proxyRes', response => {
            response.headers['cache-control'] = 'no-store';
          });
        },
      },
    }
  : {};
const supportedWebPath = new RegExp(webPaths);
function guardWebApi(request: IncomingMessage, response: ServerResponse, next: () => void) {
  const url = request.url ?? '';
  const pathname = url.split('?')[0] ?? url;
  if (pathname !== '/v1/web' && !pathname.startsWith('/v1/web/')) return next();
  const status = !upstream ? 503 : supportedWebPath.test(url) ? 0 : 404;
  if (!status) return next();
  response.statusCode = status;
  response.setHeader('content-type', 'application/json; charset=utf-8');
  response.setHeader('cache-control', 'no-store');
  response.end(
    JSON.stringify({
      detail:
        status === 503
          ? 'THOZHA_WEB_UPSTREAM is not configured for this Vite process.'
          : 'Unknown Web v1 route.',
    })
  );
}
const webApiGuard: Plugin = {
  name: 'thozha-web-api-guard',
  configureServer(server) {
    server.middlewares.use((request, response, next) => guardWebApi(request, response, next));
  },
  configurePreviewServer(server) {
    server.middlewares.use((request, response, next) => guardWebApi(request, response, next));
  },
};

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    ...(process.env.THOZHA_VOICE_DEV === '1' ? [localVoice()] : []),
    webApiGuard,
  ],
  server: {
    host: '127.0.0.1',
    port: 5174,
    strictPort: true,
    proxy,
    headers: { 'X-Thozha-Frontend-Revision': frontendRevision },
  },
  preview: {
    host: '127.0.0.1',
    port: 4173,
    strictPort: true,
    proxy,
    headers: { 'X-Thozha-Frontend-Revision': frontendRevision },
  },
  test: {
    include: ['src/**/*.test.{ts,tsx}'],
    environment: 'jsdom',
    setupFiles: ['src/test/setup.ts'],
  },
});
