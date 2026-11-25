// Recognition-only checks: no conversation submission, no TTS, no audio persistence.
// Default: injected synthetic fixture. REAL_MIC=1: owner speech, not synthesized input.
import { chromium } from '@playwright/test';
import { readFile, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
const real = process.env.REAL_MIC === '1';
const seconds = Number(process.env.TEST_SECONDS || (real ? 90 : 35));
const args = real ? [] : ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream', `--use-file-for-fake-audio-capture=${path.resolve('voice/results/input.wav')}`];
const browser = await chromium.launch({ headless: !real, args });
const context = await browser.newContext(real ? {} : { permissions: ['microphone'] });
const page = await context.newPage();
const errors = [];
page.on('pageerror', e => errors.push(e.message));
try {
  await page.goto('http://127.0.0.1:5176/#/ai-workspace');
  await page.getByLabel('Workspace ID').fill('thozha-local');
  await page.getByLabel('Password', { exact: true }).fill((await readFile(`${os.homedir()}/.local/state/thozha-web/owner-password`, 'utf8')).trim());
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await page.locator('.th-workspace.th-v4').waitFor({timeout:30000});
  await page.evaluate(async () => {
    const { CallSession } = await import('/src/voice/VoiceCall.tsx');
    const turns = [], states = [];
    const session = new CallSession({}, null, value => {
      if (states.at(-1)?.state !== value.state) states.push({state:value.state,at:Date.now()});
    }, () => {});
    // Only intercept AFTER normal capture/VAD; preserve normal authenticated ASR.
    session.respond = async audio => {
      if (session.busy) throw new Error('Overlapping turn');
      session.busy = true;
      const start = performance.now();
      try {
        const row = await session.speech('transcribe', { audio });
        // Do not retain transcript/audio in diagnostics. Only counts and timings.
        turns.push({characters: String(row.text || '').length, words: String(row.text || '').trim().split(/\s+/).filter(Boolean).length,
          transcript_ms:Math.round(performance.now()-session.speechEnd), upload_to_transcript_ms:Math.round(performance.now()-start),
          ...session.segmentMetrics, ...row.metrics});
      } catch(e) { turns.push({error:String(e)}); }
      finally { session.busy = false; }
    };
    window.recognitionTest = {session,turns,states};
    await session.start();
  });
  console.log(real ? 'REAL MICROPHONE: speak the fixed phrases now; no AI submissions or transcript logging.' : 'SYNTHETIC injected audio regression; NOT physical acceptance.');
  await page.waitForTimeout(seconds*1000);
  const result = await page.evaluate(() => {
    const {session,turns,states} = window.recognitionTest;
    session.end();
    return {turns,states,capture:session.captureMetrics,remainingFrames:session.frames.length,ended:session.snapshot.state};
  });
  const report = {kind:real?'physical microphone; owner speech required':'injected synthetic microphone; not physical acceptance',...result,errors};
  await writeFile(`voice/results/recognition-${real?'physical':'synthetic'}.json`,JSON.stringify(report,null,2));
  console.log(JSON.stringify(report,null,2));
} finally {await browser.close();}
