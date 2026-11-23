import { randomUUID } from 'node:crypto';
import { defineConfig, devices } from '@playwright/test';

const origin = 'http://127.0.0.1:45837';
const apiPort = '45838';
const runtime = process.env.THOZHA_E2E_PYTHON || 'python3';
if (!/^[A-Za-z0-9_./ -]+$/.test(runtime))
  throw new Error('THOZHA_E2E_PYTHON must be a Python interpreter path.');
process.env.THOZHA_E2E_PASSWORD ||= randomUUID();

export default defineConfig({
  testDir: './e2e',
  workers: 1,
  timeout: 40_000,
  retries: 0,
  use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 960 }, baseURL: origin },
  webServer: [
    {
      command: `"${runtime}" -B e2e/serve_fixture.py`,
      url: `http://127.0.0.1:${apiPort}/health`,
      reuseExistingServer: false,
      timeout: 30_000,
      gracefulShutdown: { signal: 'SIGTERM', timeout: 3000 },
      env: {
        THOZHA_E2E_PASSWORD: process.env.THOZHA_E2E_PASSWORD,
        THOZHA_E2E_API_PORT: apiPort,
        THOZHA_E2E_ORIGIN: origin,
      },
    },
    {
      command: 'npm run preview -- --port 45837',
      url: origin,
      reuseExistingServer: false,
      env: { THOZHA_WEB_UPSTREAM: `http://127.0.0.1:${apiPort}` },
    },
  ],
});
