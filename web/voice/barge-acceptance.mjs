// Real microphone/speaker harness. No injected microphone; no audio/transcript storage.
// Human participation is required. No human attempt => no acceptance denominator.
import { chromium } from '@playwright/test';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import os from 'node:os';
const seconds = Number(process.env.TEST_SECONDS || 100);
const browser = await chromium.launch({ headless: process.env.HEADED !== '1', args: ['--autoplay-policy=no-user-gesture-required'] });
const page = await browser.newPage();
const errors = [];
page.on('pageerror', e => errors.push(e.message));
try {
  await page.goto('http://127.0.0.1:5176/#/ai-workspace');
  await page.getByLabel('Workspace ID').fill('thozha-local');
  await page.getByLabel('Password', {exact:true}).fill((await readFile(`${os.homedir()}/.local/state/thozha-web/owner-password`, 'utf8')).trim());
  await page.getByRole('button', {name:'Sign in',exact:true}).click();
  await page.locator('.th-workspace.th-v4').waitFor({timeout:30000});
  await page.evaluate(async () => {
    const { CallSession } = await import('/src/voice/VoiceCall.tsx');
    const rows = [], states = [];
    const session = new CallSession({}, null, value => {
      if (states.at(-1)?.state !== value.state) states.push({state:value.state, wall_ms:Date.now()});
    }, () => {});
    // No model requests: isolate physical acoustic decisions from generation.
    session.respond = async audio => {
      session.busy = true;
      try {
        const started = performance.now();
        const result = await session.speech('transcribe', {audio});
        rows.push({kind:'recognition',words:String(result.text || '').trim().split(/\s+/).filter(Boolean).length,
          transcript_ms:Math.round(performance.now()-session.speechEnd),request_ms:Math.round(performance.now()-started),...result.metrics});
      } finally { session.busy = false; }
    };
    window.bargeTest = {session,rows,states};
    await session.start();
    if (session.snapshot.state === 'ended')
      throw new Error(`Microphone/session startup failed: ${session.snapshot.message}`);
    if (!session.media?.getAudioTracks().some(track => track.readyState === 'live'))
      throw new Error('No live physical microphone track; acoustic acceptance blocked');
  });
  console.log('Physical acoustic check only. Human barge-in requires independently labelled human attempts.');
  for (let round=0; round<3; round++) {
    await page.evaluate(async round => {
      const {session,rows} = window.bargeTest;
      const epoch = ++session.epoch;
      session.busy = true;
      session.speechEnd = performance.now();
      session.firstAudio = false;
      rows.push({kind:'playback_trial',round,wall_ms:Date.now()});
      session.enqueue('This is a microphone and speaker interruption test. First, listen quietly so we can check for speaker echo. When you are ready, speak a complete sentence over my voice. You can say, wait a moment, I would like to ask a different question. I will continue speaking until you interrupt or this test sentence ends.',epoch);
      void session.complete(epoch);
    },round);
    await page.waitForTimeout(seconds*1000/3);
  }
  const report = await page.evaluate(() => {
    const {session,rows,states} = window.bargeTest;
    const report = {rows,states,metrics:session.snapshot.metrics,capture:session.captureMetrics,
      microphone_settings:session.media?.getAudioTracks()[0]?.getSettings(),
      human_attempts:'requires owner labels; not inferable from sound alone'};
    session.end();
    return report;
  });
  await mkdir('voice/results',{recursive:true});
  await writeFile('voice/results/barge-physical.json',JSON.stringify({kind:'real microphone and speaker playback; not proof of human speech',...report,errors},null,2));
  console.log(JSON.stringify({...report,errors},null,2));
} finally { await browser.close(); }
