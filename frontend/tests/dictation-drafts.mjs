// Browser storage failure/limit tests. Real capture/IndexedDB, synthetic upload responses, no inference.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir, open, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
const root = resolve(import.meta.dirname,'../..'), base = 'http://127.0.0.1:5176';
const output = resolve(root,process.env.DICTATION_TEST_OUTPUT || 'tmp/dictation-drafts');
await mkdir(output,{recursive:false}); const log = await open(`${output}/vite.log`,'w');
const server = spawn(process.execPath,[`${root}/frontend/node_modules/vite/bin/vite.js`,'--host','127.0.0.1','--port','5176','--strictPort'],{cwd:`${root}/frontend`,stdio:['ignore',log.fd,log.fd]});
let browser;
const checks = [], errors = [];
try {
  for (let i=0;i<100;i++) { try { if((await fetch(base)).ok)break; }catch{} await new Promise(r=>setTimeout(r,100)); }
  browser = await chromium.launch({headless:true,args:['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream']});
  const context = await browser.newContext({permissions:['microphone']});
  let limit = 30, uploads = 0, finishUpload;
  const waitingUpload = new Promise(resolve=>{finishUpload=resolve;});
  await context.route('**/api/dictation/**',async route=>{
    const path=new URL(route.request().url()).pathname;
    if(path.endsWith('/config')) return route.fulfill({json:{max_upload_mb:100,max_upload_minutes:limit}});
    if(path.endsWith('/uploads')) { uploads++; await waitingUpload; return route.fulfill({status:413,json:{detail:'测试上传失败，草稿保留'}}); }
    if(path.endsWith('/sessions')) return route.fulfill({json:[]});
    return route.fulfill({status:404,json:{detail:'no inference in this test'}});
  });
  const page=await context.newPage(); page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{
    const getMedia=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);window.__tracks=[];
    navigator.mediaDevices.getUserMedia=async c=>{const s=await getMedia(c);window.__tracks.push(...s.getTracks());return s;};
  });
  await page.goto(`${base}/dictation?mode=record`);
  const pane=page.getByRole('region',{name:'本地录音转写'});
  const saved=()=>page.evaluate(async()=>{
    const {DraftStore}=await import('/src/composables/dictationDrafts.ts');return new DraftStore().list();
  });
  async function begin(){await pane.getByRole('button',{name:'新建本地录音',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.draft-current > strong')?.textContent?.includes('正在录音'));}
  // Low frontend limit is a mock configuration, not a changed production default.
  limit=.025;await begin();
  await page.waitForFunction(()=>window.__tracks.length && window.__tracks.every(t=>t.readyState==='ended'));
  await pane.getByRole('button',{name:'试听录音',exact:true}).waitFor();
  let drafts=await saved();assert.equal(drafts[0].samples,Math.floor(.025*60*16000));
  await pane.getByRole('button',{name:'试听录音',exact:true}).click();
  await pane.getByRole('audio').count();
  await pane.locator('audio').waitFor();
  const wav=await page.evaluate(async()=>Array.from(new Uint8Array(await fetch(document.querySelector('audio').src).then(r=>r.arrayBuffer()))));
  const bytes=Buffer.from(wav);assert.equal(bytes.toString('ascii',0,4),'RIFF');assert.equal(bytes.readUInt32LE(24),16000);assert.equal(bytes.readUInt16LE(22),1);
  assert.equal(bytes.readUInt32LE(40),drafts[0].samples*2);assert.equal(bytes.length,44+drafts[0].samples*2);
  checks.push('exact recording limit stops hardware; WAV header/channel/rate/data length match committed samples');
  // Claim submission before Blob construction, including two clicks in one rendering turn.
  await pane.getByRole('button',{name:'开始转写',exact:true}).evaluate(button=>{button.click();button.click();});
  await page.waitForFunction(()=>document.querySelector('.draft-current')?.textContent?.includes('正在提交转写'));
  await page.waitForTimeout(200);assert.equal(uploads,1);finishUpload();
  await pane.getByText('上传失败，录音保留。可下载，或手动重新提交。',{exact:true}).waitFor();
  assert.equal((await saved())[0].samples,drafts[0].samples);assert(await pane.getByRole('button',{name:'下载录音 WAV'}).isEnabled());
  checks.push('double submit is single-flight before first await; HTTP failure preserves PCM and download');
  limit=30;
  await begin();await page.waitForTimeout(1300);
  await page.evaluate(()=>{
    const native=IDBObjectStore.prototype.add;
    IDBObjectStore.prototype.add=function(...args){if(this.name==='chunks'&&window.__quotaFail)throw new DOMException('test quota','QuotaExceededError');return native.apply(this,args);};
    window.__quotaFail=true;
  });
  await pane.getByRole('alert').filter({hasText:'保存失败'}).waitFor();
  await page.waitForFunction(()=>window.__tracks.every(t=>t.readyState==='ended'));
  await pane.getByRole('button',{name:'试听录音',exact:true}).waitFor();
  const kept=(await saved())[0];assert(kept.samples>=16000);assert.equal(kept.status,'interrupted');
  await pane.getByRole('button',{name:'试听录音',exact:true}).click();
  await pane.locator('audio').waitFor();
  const length=await page.evaluate(async()=>fetch(document.querySelector('audio').src).then(r=>r.arrayBuffer()).then(b=>b.byteLength));
  assert.equal(length,44+kept.samples*2);
  checks.push('quota failure rolls back chunk and position together, stops capture, preserves playable committed prefix');
  await page.evaluate(()=>{window.__quotaFail=false;});
  // Block persistence longer than the bounded five-second queue.
  await page.evaluate(async()=>{
    const {DraftStore}=await import('/src/composables/dictationDrafts.ts');const native=DraftStore.prototype.append;
    window.__slowWrites=true;window.__pendingWrites=[];
    DraftStore.prototype.append=async function(...args){if(window.__slowWrites)await new Promise(resolve=>window.__pendingWrites.push(resolve));return native.apply(this,args);};
  });
  await begin();
  await pane.getByRole('alert').filter({hasText:'积压超过5秒'}).waitFor();
  assert(await page.evaluate(()=>window.__tracks.every(t=>t.readyState==='ended')));
  await page.evaluate(()=>{window.__slowWrites=false;window.__pendingWrites.forEach(resolve=>resolve());});
  await pane.getByRole('button',{name:'试听录音',exact:true}).waitFor();
  const bounded=(await saved())[0];assert(bounded.samples<=16000*5);assert.equal(bounded.status,'interrupted');
  checks.push('bounded write backlog stops hardware before unbounded memory; queued prefix can finish saving');
  // Unexpected refresh restores only committed chunks and never starts capture.
  await begin();await page.waitForTimeout(1600);const beforeReload=(await saved())[0];assert(beforeReload.samples>=16000);
  await page.reload();await pane.getByRole('button',{name:'打开草稿',exact:true}).first().click();
  assert.equal(await page.evaluate(()=>window.__tracks.length),0);
  await pane.getByRole('button',{name:'试听录音',exact:true}).click();
  assert((await pane.locator('.draft-current').innerText()).includes('意外中断'));
  const afterReload=(await saved()).find(d=>d.id===beforeReload.id);assert(afterReload.samples>=beforeReload.samples);
  checks.push('refresh recovery opens saved interrupted draft without a microphone permission/capture restart');
  assert.deepEqual(errors,[]);
  await page.screenshot({path:`${output}/recovered-draft.png`,fullPage:true});
  await writeFile(`${output}/report.json`,JSON.stringify({checks,errors,synthetic_upload:true,real_indexeddb:true},null,2));console.log(checks.join('\n'));
}finally{await browser?.close();server.kill('SIGTERM');await new Promise(r=>server.exitCode!==null?r():server.once('exit',r));await log.close();}
