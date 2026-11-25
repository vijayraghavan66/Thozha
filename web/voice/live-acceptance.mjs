import { chromium } from '@playwright/test';
import { readFile, writeFile } from 'node:fs/promises';
import os from 'node:os';
const browser = await chromium.launch({ headless: false, args: ['--autoplay-policy=no-user-gesture-required'] });
const context = await browser.newContext();
const page = await context.newPage();
const errors = [], states = [], metrics = [], turns = [], requests = [];
page.on('pageerror', e => errors.push(e.message));
page.on('request', r => { if (r.url().includes('/v1/web/')) requests.push({ path: new URL(r.url()).pathname, method: r.method(), at: Date.now() }); });
const started = Date.now();
try {
 await page.goto('http://127.0.0.1:5176/#/ai-workspace?new=1');
 await page.getByLabel('Workspace ID').fill('thozha-local');
 await page.getByLabel('Password', { exact: true }).fill((await readFile(`${os.homedir()}/.local/state/thozha-web/owner-password`, 'utf8')).trim());
 await page.getByRole('button', { name: 'Sign in', exact: true }).click();
 await page.getByRole('button', { name: 'Start voice call' }).click();
 const dialog = page.getByRole('dialog', { name: 'Thozha voice call' });
 let last = '', previousTranscript = '';
 for (let i = 0; i < 650; i++) {
  if (!(await dialog.count())) break;
  const state = await dialog.getAttribute('data-state');
  if (state !== last) {
   last = state; states.push({ state, ms: Date.now() - started }); console.log('state', state, Date.now() - started);
   const value = await dialog.locator('pre').textContent();
   if(value && value !== '{}') metrics.push({ state, value, ms: Date.now() - started });
  }
  const transcript = await page.getByRole('log', { name: 'Live voice transcript' }).innerText();
  if (transcript && transcript !== previousTranscript) {
   previousTranscript = transcript; turns.push({ transcript, ms: Date.now() - started });
   console.log('transcript', transcript);
  }
  await page.waitForTimeout(200);
 }
 if(await dialog.count()) {
  await page.screenshot({ path: 'voice/results/live-acceptance.png' });
  metrics.push({ state: 'final', value: await dialog.locator('pre').textContent() });
  await page.getByRole('button', { name: 'End voice call' }).click();
 }
 await writeFile('voice/results/live-acceptance.json', JSON.stringify({ states, metrics, turns, errors, requests }, null, 2));
 console.log(JSON.stringify({ states, metrics, errors }, null, 2));
} finally { await browser.close(); }
