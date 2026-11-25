import { chromium } from '@playwright/test';
import { readFile, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
const real = process.env.REAL_MIC === '1';
const args = ['--autoplay-policy=no-user-gesture-required'];
if (!real) args.push('--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream', `--use-file-for-fake-audio-capture=${path.resolve('voice/results/input.wav')}`);
const browser = await chromium.launch({ headless: !real, args });
const context = await browser.newContext(real ? {} : { permissions: ['microphone'] });
const page = await context.newPage();
const errors = [], calls = [], states = [];
page.on('pageerror', error => errors.push(error.message));
page.on('request', req => { if (req.url().includes('/v1/web/')) calls.push({path:new URL(req.url()).pathname, method:req.method(), at:Date.now()}); });
try {
 await page.goto('http://127.0.0.1:5176');
 await page.getByLabel('Workspace ID').fill('thozha-local');
 await page.getByLabel('Password',{exact:true}).fill((await readFile(`${os.homedir()}/.local/state/thozha-web/owner-password`,'utf8')).trim());
 await page.getByRole('button',{name:'Sign in',exact:true}).click();
 await page.locator('.th-workspace.th-v4').waitFor({timeout:30000});
 await page.goto('http://127.0.0.1:5176/#/ai-workspace?new=1');
 await page.getByRole('button',{name:'Start voice call'}).waitFor({timeout:30000});
 await page.getByRole('button',{name:'Start voice call'}).click();
 const dialog = page.getByRole('dialog',{name:'Thozha voice call'});
 await dialog.waitFor();
 const start = Date.now();
 let speaking = false, mute = false, ended = false;
 while (Date.now()-start < (real ? 120000 : 100000)) {
   const state = await dialog.getAttribute('data-state');
   if (states.at(-1)?.state !== state) { states.push({state,ms:Date.now()-start}); console.log('state',state,Date.now()-start); }
   if (state==='ended') { console.log('ended', await dialog.innerText()); ended=true; break; }
   if (state==='speaking' && !speaking) {
      speaking=true;
      await page.screenshot({path:'voice/results/call-speaking.png'});
      console.log('transcript', await page.getByRole('log',{name:'Live voice transcript'}).innerText());
      console.log('metrics', await dialog.locator('pre').innerText());
      await page.getByRole('button',{name:'Interrupt · ⌥I'}).click();
      console.log('interrupt state',await dialog.getAttribute('data-state'));
      await page.getByRole('button',{name:'Mute · ⌥M',exact:true}).click();
      console.log('mute state',await dialog.getAttribute('data-state'));
      await page.getByRole('button',{name:'Unmute · ⌥M',exact:true}).click();
      mute=true;
   }
   if (speaking && Date.now()-start > 50000 && state==='speaking') break;
   await page.waitForTimeout(200);
 }
 if (!ended) {
   console.log('final', await dialog.innerText());
   await page.getByRole('button',{name:'End voice call'}).click();
 }
 await page.screenshot({path:'voice/results/call-ended.png'});
 console.log(JSON.stringify({real, speaking, mute, states, errors, submissionRequests:calls.filter(c=>c.path.endsWith('/messages/stream')).length},null,2));
 await writeFile(`voice/results/${real?'physical':'synthetic'}-call.json`,JSON.stringify({real,speaking,mute,states,errors,calls},null,2));
} finally { await browser.close(); }
