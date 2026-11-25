// Compare synthesis of the same completed answer, alternating old/new chunking.
import { chromium } from '@playwright/test';
import { readFile, writeFile } from 'node:fs/promises';
import os from 'node:os';
const browser=await chromium.launch({headless:true});const page=await browser.newPage();
try {
 await page.goto('http://127.0.0.1:5176');
 await page.getByLabel('Workspace ID').fill('thozha-local');
 await page.getByLabel('Password',{exact:true}).fill((await readFile(`${os.homedir()}/.local/state/thozha-web/owner-password`,'utf8')).trim());
 await page.getByRole('button',{name:'Sign in',exact:true}).click();
 await page.locator('.th-workspace.th-v4').waitFor({timeout:30000});
 const result=await page.evaluate(async()=>{
  const {splitSpeech}=await import('/src/voice/turns.ts');
  const {csrf_token:csrf}=await(await fetch('/v1/web/session')).json();
  const text='The best way to make a conversation feel natural is to listen carefully, preserve the complete question, and begin a clear response promptly, without rushing or interrupting someone who is still thinking about what they want to say.';
  const rows=[];
  for(const limit of [300,120,120,300,300,120]) {
   const phrase=splitSpeech(text,true,limit).sentences[0];
   const start=performance.now();
   const response=await fetch('/v1/web/voice/synthesize',{method:'POST',headers:{'Content-Type':'application/json','X-Thozha-CSRF':csrf},body:JSON.stringify({text:phrase,voice:'af_bella'})});
   const row=await response.json();
   rows.push({limit,characters:phrase.length,status:response.status,request_ms:Math.round(performance.now()-start),synthesis_ms:row.synthesis_ms});
  }
  return rows;
 });
 await writeFile('voice/results/phrase-benchmark.json',JSON.stringify(result,null,2)); console.log(JSON.stringify(result,null,2));
} finally {await browser.close();}
