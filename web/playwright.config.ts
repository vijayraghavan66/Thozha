import { randomUUID } from 'node:crypto';
import { defineConfig, devices, type PlaywrightTestConfig } from '@playwright/test';

const origin = 'http://127.0.0.1:45837';
const apiPort = '45838';
const controlOrigin = 'http://127.0.0.1:45933';
const controlApiPort = '45934';
const runtime = process.env.THOZHA_E2E_PYTHON || 'python3';
if (!/^[A-Za-z0-9_./ -]+$/.test(runtime))
  throw new Error('THOZHA_E2E_PYTHON must be a Python interpreter path.');
process.env.THOZHA_E2E_PASSWORD ||= randomUUID();

type WebServers = Extract<PlaywrightTestConfig['webServer'], unknown[]>;

function servers(webOrigin: string, backendPort: string, control: boolean): WebServers {
  return [
    {
      command: `"${runtime}" -B e2e/serve_fixture.py`,
      url: `http://127.0.0.1:${backendPort}/health`,
      reuseExistingServer: false,
      timeout: 30_000,
      gracefulShutdown: { signal: 'SIGTERM', timeout: 3000 } as const,
      env: {
        THOZHA_E2E_PASSWORD: process.env.THOZHA_E2E_PASSWORD!,
        THOZHA_E2E_API_PORT: backendPort,
        THOZHA_E2E_ORIGIN: webOrigin,
        THOZHA_E2E_CONTROL: control ? '1' : '0',
        THOZHA_E2E_OLLAMA_PORT: '',
      },
    },
    {
      command: `npm run preview -- --port ${new URL(webOrigin).port}`,
      url: webOrigin,
      reuseExistingServer: false,
      env: { THOZHA_WEB_UPSTREAM: `http://127.0.0.1:${backendPort}` },
    },
  ];
}

export default defineConfig({
  testDir: './e2e',
  workers: 1,
  timeout: 40_000,
  retries: 0,
  use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 960 } },
  projects: [
    { name: 'workspace', testMatch: 'workspace.spec.ts', use: { baseURL: origin } },
    { name: 'control', testMatch: 'control.spec.ts', use: { baseURL: controlOrigin } },
  ],
  webServer: [...servers(origin, apiPort, false), ...servers(controlOrigin, controlApiPort, true)],
});
