// Real capture/CPU/GPU/raw sentence playback with no paid cleanup. Device selection/terms are browser-local.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir, open, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
const root=resolve(import.meta.dirname,'../..'),base='http://127.0.0.1:8025';
const output=resolve(root,process.env.DICTATION_TEST_OUTPUT||'tmp/dictation-optimization/c-real');
await mkdir(output,{recursive:false});const log=await open(`${output}/server.log`,'w');
const server=spawn(`${root}/.venv/bin/python`,['scripts/serve_dictation_test.py','--root',`${output}/project`,'--port','8025'],{cwd:root,env:process.env,stdio:['ignore',log.fd,log.fd]});
const sample=resolve(root,'data/models/dictation/paraformer/test_wavs/0.wav');
let browser;const checks=[],errors=[];let outcome='failed';
try{
  for(let i=0;i<200;i++){if(server.exitCode!==null)throw new Error('isolated server exited');try{if((await fetch(`${base}/api/health`)).ok)break;}catch{}await new Promise(r=>setTimeout(r,100));}
  browser=await chromium.launch({headless:true,args:['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream',`--use-file-for-fake-audio-capture=${sample}`,'--autoplay-policy=no-user-gesture-required']});
  const context=await browser.newContext({permissions:['microphone'],viewport:{width:1280,height:1000}});
  const page=await context.newPage();page.setDefaultTimeout(45000);page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{
    const getMedia=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);window.__tracks=[];window.__acquirePlayback=[];window.__constraints=[];
    navigator.mediaDevices.getUserMedia=async c=>{
      window.__acquirePlayback.push([...document.querySelectorAll('audio')].every(audio=>audio.paused));window.__constraints.push(c);
      const s=await getMedia(c);window.__tracks.push(...s.getTracks());return s;
    };
  });
  const state=async id=>context.request.get(`${base}/api/dictation/sessions/${id}`).then(r=>r.json());
  async function until(id,status){for(let i=0;i<1000;i++){const s=await state(id);if(s.status===status)return s;if(['failed','cancelled','interrupted'].includes(s.status))throw new Error(JSON.stringify(s));await page.waitForTimeout(50);}throw new Error('status timed out');}
  await page.goto(`${base}/dictation`);
  await page.getByRole('button',{name:'设置',exact:true}).click();
  await page.getByRole('button',{name:'授权并列出设备',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#dictation-microphone')?.options.length>1);
  const device=await page.locator('#dictation-microphone option').evaluateAll(options=>options.find(o=>o.value)?.value);
  assert(device);await page.getByLabel('麦克风',{exact:true}).selectOption(device);
  await page.locator('.dictation-settings summary').click();
  await page.getByLabel('新常用术语',{exact:true}).fill('WebSocket');await page.getByRole('button',{name:'保存常用术语',exact:true}).click();
  await page.getByRole('checkbox',{name:'WebSocket',exact:true}).check();await page.getByRole('button',{name:'用于下次识别',exact:true}).click();
  await page.getByRole('checkbox',{name:/使用逐句轻度整理/}).uncheck();
  await page.getByRole('button',{name:'设置',exact:true}).click();
  const created=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/api/dictation/sessions'));
  await page.getByRole('button',{name:'开始听写',exact:true}).click();const sid=(await(await created).json()).id;
  const dock=page.getByRole('complementary',{name:'常驻语音控制'});
  await page.waitForFunction(()=>document.querySelector('.dictation-dock__signal')?.classList.contains('is-on'));
  await page.waitForTimeout(4200);await dock.getByRole('button',{name:'暂停听写',exact:true}).click();await until(sid,'paused');
  const listen=page.getByRole('button',{name:'听这一句',exact:true}).first();await listen.waitFor();await listen.click();
  const rawPlayer=page.getByLabel('原句回听（分句音频，非逐字对齐）',{exact:true});
  await page.waitForFunction(()=>[...document.querySelectorAll('audio')].some(a=>!a.paused&&a.currentTime>0));
  await dock.getByRole('button',{name:'继续听写',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.dictation-dock__signal')?.classList.contains('is-on'));
  assert(await rawPlayer.evaluate(audio=>audio.paused));assert(await page.evaluate(()=>window.__acquirePlayback.every(Boolean)));
  await page.waitForTimeout(1300);await dock.getByRole('button',{name:'结束听写',exact:true}).click();const completed=await until(sid,'completed');
  assert.deepEqual(completed.config.terms,['WebSocket']);
  await page.getByRole('button',{name:'设置',exact:true}).click();await page.locator('.dictation-settings summary').click();await page.getByLabel('新常用术语').fill('另一个词');await page.getByRole('button',{name:'保存常用术语',exact:true}).click();
  assert.deepEqual((await state(sid)).config.terms,['WebSocket']);
  await page.getByRole('button',{name:'设置',exact:true}).click();
  checks.push('exact microphone selection saved; opt-in terms frozen per session; raw sentence playback is paused before dock resume acquires mic');
  // Edit guard and explicit term opt-in remain independent.
  const sentence=page.locator('.dictation-utterance').first();await sentence.getByRole('button',{name:'编辑',exact:true}).click();
  await page.getByRole('textbox',{name:/编辑第/}).fill('预算1500元，不是15000元。');await page.getByRole('button',{name:'保存',exact:true}).click();
  await page.getByText('预算1500元，不是15000元。',{exact:true}).waitFor();
  page.once('dialog',dialog=>dialog.accept('预算'));await sentence.getByRole('button',{name:'存为常用术语',exact:true}).click();
  assert((await page.evaluate(()=>JSON.parse(localStorage.getItem('dictation:terms')))).includes('预算'));
  const edited=await state(sid);assert(edited.draft_text.includes('1500元，不是15000元'));assert.deepEqual(edited.config.terms,['WebSocket']);
  checks.push('manual edit protected, adding a corrected term requires explicit prompt and does not rewrite frozen session terms');
  await page.getByRole('button',{name:'录音转写',exact:true}).click();const pane=page.getByRole('region',{name:'本地录音转写'});
  await pane.getByRole('button',{name:'新建本地录音',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.draft-current > strong')?.textContent?.includes('正在录音'));
  await page.waitForTimeout(1800);await pane.getByRole('button',{name:'暂停录音',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.draft-current > p')?.textContent?.startsWith('已保存到本浏览器'));
  await pane.getByRole('button',{name:'试听已保存录音',exact:true}).click();await pane.locator('audio').waitFor();await pane.locator('audio').evaluate(audio=>audio.play());
  await page.waitForFunction(()=>[...document.querySelectorAll('audio')].some(a=>!a.paused));
  await dock.getByRole('region',{name:'本地录音控制'}).getByRole('button',{name:'继续录音',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.draft-current > strong')?.textContent?.includes('正在录音'));
  assert(await page.evaluate(()=>window.__acquirePlayback.every(Boolean)));
  await pane.getByRole('button',{name:'结束录音',exact:true}).click();await pane.getByRole('button',{name:'试听录音',exact:true}).waitFor();
  checks.push('draft playback is paused before dock local resume acquires mic; shared prepare/start hook covers page and dock');
  await pane.getByRole('button',{name:'试听录音',exact:true}).click();await pane.locator('audio').waitFor();
  await page.setViewportSize({width:390,height:844});await page.getByRole('button',{name:'设置',exact:true}).click();await page.screenshot({path:`${output}/mobile-device-terms.png`,fullPage:true});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.reload();await page.getByRole('button',{name:'设置',exact:true}).click();
  assert.equal(await page.evaluate(()=>localStorage.getItem('dictation:microphone')),device);
  assert((await page.evaluate(()=>JSON.parse(localStorage.getItem('dictation:terms')))).includes('WebSocket'));
  assert.equal(await page.evaluate(()=>window.__tracks.length),0);
  assert.deepEqual(errors,[]);outcome='passed';
}finally{
  await writeFile(`${output}/report.json`,JSON.stringify({outcome,checks,errors,cleanup_enabled:false,physical_microphone_tested:false},null,2));
  await browser?.close();server.kill('SIGTERM');await new Promise(r=>server.exitCode!==null?r():server.once('exit',r));await log.close();console.log(outcome,checks.join('\n'));
}
