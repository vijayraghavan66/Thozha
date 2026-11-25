// Actual microphone/speaker path with SYNTHETIC stimulus, not human acceptance.
// Stores no audio or transcripts. Acoustic threshold is not verified answer onset.
import { chromium } from '@playwright/test';
import { readFile, writeFile } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import os from 'node:os';
const python = `${os.homedir()}/Documents/Jarvis-Lab/agent-evaluation/thozha-voice-mac-venv/bin/python`;
const browser = await chromium.launch({headless:false,args:['--autoplay-policy=no-user-gesture-required']});
const page = await browser.newPage();
const errors=[];
page.on('pageerror',e=>errors.push(e.message));
let meter;
try {
 await page.goto('http://127.0.0.1:5176/#/ai-workspace');
 await page.getByLabel('Workspace ID').fill('thozha-local');
 await page.getByLabel('Password',{exact:true}).fill((await readFile(`${os.homedir()}/.local/state/thozha-web/owner-password`,'utf8')).trim());
 await page.getByRole('button',{name:'Sign in',exact:true}).click();
 await page.locator('.th-workspace.th-v4').waitFor({timeout:30000});
 await page.evaluate(async()=>{
  const {CallSession}=await import('/src/voice/VoiceCall.tsx');
  const {createWebApi}=await import('/src/api/client.ts');
  const api=createWebApi(); const auth=await api.session();
  const states=[]; let last;
  const session=new CallSession(api.authorized(auth.csrf_token,()=>{}),null,s=>{
   if(s.state!==last){last=s.state;states.push({state:s.state,wall_ms:Date.now()});}
  },()=>{});
  window.latencyTest={session,states};
  await session.start();
 });
 meter=spawn(python,['-B','voice/audio-meter.py','65']);
 let raw='';meter.stdout.on('data',b=>raw+=b);
 const metered=new Promise(resolve=>meter.on('exit',resolve));
 await new Promise(r=>setTimeout(r,1500));
 await new Promise((resolve,reject)=>{const p=spawn('/usr/bin/afplay',['voice/results/kokoro-1.wav']);p.on('exit',resolve);p.on('error',reject);});
 const stimulusEnd=Date.now();
 await page.waitForFunction(()=>window.latencyTest.session.firstAudio,{}, {timeout:55000}).catch(()=>{});
 await page.waitForTimeout(2000);
 const result=await page.evaluate(()=>{
  const {session,states}=window.latencyTest;
  const metrics=session.snapshot.metrics;
  const report={states,metrics,capture:session.captureMetrics,state:session.snapshot.state,
    recognized_words:session.snapshot.user.trim().split(/\s+/).filter(Boolean).length,
    answer_characters:session.snapshot.assistant.length};
  session.end(); return report;
 });
 await metered;
 let acoustic;try{acoustic=JSON.parse(raw);}catch{acoustic={error:'meter unavailable'};}
 const onset=acoustic.threshold_onsets?.find(row=>row.wall_ms>=result.metrics.first_audio_wall_ms);
 const report={kind:'real hardware, synthetic acoustic stimulus, not human acceptance',...result,stimulusEnd,
  acoustic_threshold_after_schedule_ms:onset?onset.wall_ms-result.metrics.first_audio_wall_ms:null,
  estimated_speech_end_to_acoustic_threshold_ms:onset?result.metrics.first_audio_scheduled_ms+onset.wall_ms-result.metrics.first_audio_wall_ms:null,
  warning:'Threshold can be room noise. Not a verified first-audible measure.',errors};
 await writeFile('voice/results/latency-acoustic.json',JSON.stringify(report,null,2));
 console.log(JSON.stringify(report,null,2));
} finally {meter?.kill();await browser.close();}
