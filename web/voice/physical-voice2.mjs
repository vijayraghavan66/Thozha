// Actual mic/speaker acoustic test. Synthetic spoken stimulus, NOT human acceptance.
import { chromium } from '@playwright/test';
import { readFile, writeFile } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import os from 'node:os';
const python = `${os.homedir()}/Documents/Jarvis-Lab/agent-evaluation/thozha-voice-mac-venv/bin/python`;
const browser = await chromium.launch({ headless: false, args: ['--autoplay-policy=no-user-gesture-required'] });
const page = await browser.newPage();
const states = [], errors = [];
page.on('pageerror', e => errors.push(e.message));
let meter;
try {
  await page.goto('http://127.0.0.1:5176/#/ai-workspace?new=1');
  await page.getByLabel('Workspace ID').fill('thozha-local');
  await page.getByLabel('Password', {exact:true}).fill((await readFile(`${os.homedir()}/.local/state/thozha-web/owner-password`, 'utf8')).trim());
  await page.getByRole('button', {name:'Sign in',exact:true}).click();
  await page.getByRole('button', {name:'Start voice call'}).click();
  const dialog = page.getByRole('dialog', {name:'Thozha voice call'});
  await page.waitForFunction(() => document.querySelector('[aria-label="Thozha voice call"]')?.getAttribute('data-state') === 'listening', {}, {timeout:90000});
  const devices = await page.evaluate(async () => (await navigator.mediaDevices.enumerateDevices()).map(d => ({kind:d.kind,label:d.label,id:d.deviceId})));
  const output = devices.find(d => d.kind === 'audiooutput' && d.label.includes('Speakers (Built-in)'));
  if (output && await page.getByLabel('Audio output').count()) await page.getByLabel('Audio output').selectOption(output.id);
  meter = spawn(python, ['-B', 'voice/audio-meter.py', '55']);
  let raw = ''; meter.stdout.on('data', b => raw += b.toString());
  const metered = new Promise(resolve => meter.on('exit', resolve));
  await new Promise((resolve,reject) => { const p = spawn('/usr/bin/afplay', ['voice/results/kokoro-1.wav']); p.on('exit', resolve); p.on('error',reject); });
  const inputEndWall = Date.now();
  const start = Date.now();
  let speakingWall, completed = false;
  while (Date.now() - start < 50000) {
    const state = await dialog.getAttribute('data-state');
    if (states.at(-1)?.state !== state) { states.push({state,wall_ms:Date.now()}); console.log(state, Date.now()); }
    if (state === 'speaking' && !speakingWall) speakingWall = Date.now();
    if (speakingWall && state === 'listening') { completed = true; break; }
    if (state === 'ended' || state === 'disconnected') break;
    await page.waitForTimeout(50);
  }
  const metrics = await dialog.locator('pre').textContent();
  const transcript = await page.getByRole('log',{name:'Live voice transcript'}).innerText();
  await page.getByRole('button',{name:'End voice call'}).click();
  await metered;
  let acoustic;
  try { acoustic = JSON.parse(raw); } catch { acoustic = {error:'meter failed'}; }
  const onset = acoustic.threshold_onsets?.find(row => speakingWall && row.wall_ms >= speakingWall);
  const result = {kind:'real microphone/speaker synthetic stimulus; not natural human acceptance',devices:devices.map(({kind,label})=>({kind,label})),inputEndWall,speakingWall,firstAcousticThresholdWall:onset?.wall_ms,speech_end_to_acoustic_threshold_ms:onset ? onset.wall_ms-inputEndWall : null,warning:'Room sounds can trigger acoustic threshold; inspect before treating as verified answer onset',completed,states,metrics,transcript,errors,acoustic};
  await writeFile('voice/results/voice2-acoustic-metered.json',JSON.stringify(result,null,2));
  console.log(JSON.stringify({...result,acoustic:undefined,devices:undefined},null,2));
} finally { meter?.kill(); await browser.close(); }
