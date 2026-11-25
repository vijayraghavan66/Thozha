import { chromium } from '@playwright/test';
import { readFile, writeFile } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import os from 'node:os';
const browser = await chromium.launch({ headless:false, args:['--autoplay-policy=no-user-gesture-required'] });
const page = await browser.newPage();
const errors=[], states=[], requests=[], metrics=[], transcripts=[];
page.on('pageerror',e=>errors.push(e.message));
page.on('request',r=>requests.push({url:r.url(),method:r.method(),at:Date.now()}));
const play = file => new Promise(resolve=> { const p=spawn('/usr/bin/afplay',[file]); p.on('exit',resolve); });
const root=process.cwd();
const stamp=Date.now();
try {
 await page.goto('http://127.0.0.1:5176/#/ai-workspace?new=1');
 await page.getByLabel('Workspace ID').fill('thozha-local');
 await page.getByLabel('Password',{exact:true}).fill((await readFile(`${os.homedir()}/.local/state/thozha-web/owner-password`,'utf8')).trim());
 await page.getByRole('button',{name:'Sign in',exact:true}).click();
 await page.getByRole('button',{name:'Start voice call'}).click();
 const dialog=page.getByRole('dialog',{name:'Thozha voice call'});
 // Real permission prompt and real audio devices. No fake media flags or grants.
 await page.waitForFunction(()=>document.querySelector('[aria-label="Thozha voice call"]')?.getAttribute('data-state')==='listening',{},{timeout:90000});
 const devices=await page.evaluate(async()=> (await navigator.mediaDevices.enumerateDevices()).map(d=>({kind:d.kind,label:d.label,id:d.deviceId})));
 console.log('audio devices',devices.map(d=>({kind:d.kind,label:d.label})));
 const speaker=devices.find(d=>d.kind==='audiooutput' && d.label.includes('MacBook Pro Speakers (Built-in)'));
 if(speaker) { const select=page.getByLabel('Audio output'); if(await select.count()) await select.selectOption(speaker.id); }
 let injected=0, lastState='', speechCount=0, interruptInjected=false, muted=false;
 let inputEnd;
 const started=Date.now();
 while(Date.now()-started<100000) {
   const state=await dialog.getAttribute('data-state');
   if(state!==lastState) {states.push({state,ms:Date.now()-started});console.log('state',state,Date.now()-started);lastState=state;}
   if(state==='ended') throw new Error(await dialog.innerText());
   if(state==='listening' && injected===0) {
     injected++; await play(`${root}/voice/results/kokoro-1.wav`); inputEnd=Date.now();
   }
   if(state==='speaking') {
     speechCount++;
     if(!interruptInjected) {
       await page.waitForTimeout(500);
       metrics.push(await dialog.locator('pre').textContent());
       transcripts.push(await page.getByRole('log',{name:'Live voice transcript'}).innerText());
       interruptInjected=true;
       // Acoustic stimulus over real speakers while browser output is active.
       // This is NOT a claim of natural owner barge-in acceptance.
       void play(`${root}/voice/results/kokoro-1.wav`);
     }
   }
   if(interruptInjected && state==='listening' && !muted && Date.now()-inputEnd>20000) {
     muted=true;
     await page.getByRole('button',{name:'Mute · ⌥M',exact:true}).click();
     if(await dialog.getAttribute('data-state')!=='muted') throw new Error('mute failed');
     await page.getByRole('button',{name:'Unmute · ⌥M',exact:true}).click();
     if(injected===1) { injected++; await play(`${root}/voice/results/kokoro-1.wav`); }
   }
   if(muted && state==='speaking') {
     metrics.push(await dialog.locator('pre').textContent());
     transcripts.push(await page.getByRole('log',{name:'Live voice transcript'}).innerText());
     await page.waitForTimeout(1500);break;
   }
   await page.waitForTimeout(100);
 }
 metrics.push(await dialog.locator('pre').textContent());
 await page.screenshot({path:'voice/results/physical-call.png'});
 await page.getByRole('button',{name:'End voice call'}).click();
 console.log('call closed',await page.getByRole('dialog',{name:'Thozha voice call'}).count()===0);
 const summary={type:'real browser acoustic test; synthetic sentences through real speakers',elapsed:Date.now()-stamp,states,metrics,transcripts,errors,muted,speechCount,requests};
 await writeFile('voice/results/physical-acoustic.json',JSON.stringify(summary,null,2));
 console.log(JSON.stringify({states,metrics,transcripts,errors,muted},null,2));
} finally {await browser.close();}
