import { chromium } from '@playwright/test';
import { readFile, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
const browser=await chromium.launch({headless:true,args:['--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream',`--use-file-for-fake-audio-capture=${path.resolve('voice/results/input.wav')}`]});
const context=await browser.newContext({permissions:['microphone']});
const page=await context.newPage();
const errors=[]; page.on('pageerror',e=>errors.push(e.message));
const attempts=[]; page.on('request',r=>{if(/\/messages(?:\/stream)?$/.test(new URL(r.url()).pathname)&&r.method()==='POST')attempts.push({key:r.headers()['idempotency-key'],path:new URL(r.url()).pathname});});
try {
 await page.goto('http://127.0.0.1:5176/#/ai-workspace?new=1');
 await page.getByLabel('Workspace ID').fill('thozha-local');
 await page.getByLabel('Password',{exact:true}).fill((await readFile(`${os.homedir()}/.local/state/thozha-web/owner-password`,'utf8')).trim());
 await page.getByRole('button',{name:'Sign in',exact:true}).click();
 await page.getByRole('button',{name:'Start voice call'}).click();
 await page.waitForFunction(()=>!!JSON.parse(sessionStorage.getItem('thozha.voice.pending.v1')||'null')?.response,{},{timeout:60000});
 const saved=await page.evaluate(()=>JSON.parse(sessionStorage.getItem('thozha.voice.pending.v1')));
 await context.setOffline(true); await page.waitForTimeout(1500); await context.setOffline(false);
 // Refresh must release media and retain the same request, not create a second turn.
 await page.reload();
 await page.getByRole('button',{name:'Start voice call'}).click();
 await page.waitForFunction(()=>document.querySelector('[aria-label="Thozha voice call"]')?.getAttribute('data-state')==='speaking',{},{timeout:60000});
 const result=await page.evaluate(async saved=>{
   const detail=await fetch(`/v1/web/conversations/${saved.conversation}`).then(r=>r.json());
   return {users:detail.messages.filter(m=>m.role==='user').length, assistant:detail.messages.filter(m=>m.role==='assistant').length, responseIds:detail.messages.filter(m=>m.role==='assistant').map(m=>m.id)};
 },saved);
 await page.getByRole('button',{name:'End voice call'}).click();
 const summary={result,attempts,errors};
 if(result.users!==1||result.assistant!==1||!result.responseIds.includes(saved.response))throw new Error('Duplicate or mismatched recovered turn');
 console.log(JSON.stringify(summary,null,2));
 await writeFile('voice/results/recovery.json',JSON.stringify(summary,null,2));
}finally{await context.setOffline(false);await browser.close();}
