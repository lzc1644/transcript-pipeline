// Independent three-minute browser recording, actual download/playback/upload/ASR, no paid cleanup.
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { mkdir, open, readFile, readdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
const root=resolve(import.meta.dirname,'../..'),base='http://127.0.0.1:8024';
const output=resolve(root,process.env.DICTATION_TEST_OUTPUT||'tmp/dictation-optimization/b-real');
await mkdir(output,{recursive:false});const log=await open(`${output}/server.log`,'w');
const sample=resolve(root,'data/models/dictation/paraformer/test_wavs/0.wav');
// Reuse the installed decoder/encoders; no host ffmpeg installation or download.
const formats=spawnSync(`${root}/.venv/bin/python`,['-c',`
from pathlib import Path
import sys
import av
for ext,codec in [('mp3','libmp3lame'),('m4a','aac')]:
    path=Path(sys.argv[2])/('sample.'+ext)
    if path.exists(): raise ValueError('test output already exists')
    with av.open(sys.argv[1]) as source, av.open(str(path),'w') as output:
        stream=output.add_stream(codec,rate=16000)
        stream.layout='mono'
        stream.codec_context.format=av.Codec(codec,'w').audio_formats[0].name
        resampler=av.AudioResampler(format=stream.codec_context.format.name,layout='mono',rate=16000)
        for frame in source.decode(audio=0):
            for converted in resampler.resample(frame):
                for packet in stream.encode(converted): output.mux(packet)
        for converted in resampler.resample(None):
            for packet in stream.encode(converted): output.mux(packet)
        for packet in stream.encode(None): output.mux(packet)
`,sample,output]);
assert.equal(formats.status,0,formats.error?.message||formats.stderr?.toString());
const server=spawn(`${root}/.venv/bin/python`,['scripts/serve_dictation_test.py','--root',`${output}/project`,'--port','8024'],{cwd:root,env:process.env,stdio:['ignore',log.fd,log.fd]});
const formatsOnly=process.env.DICTATION_RECORDING_FORMATS_ONLY==='1';
let browser;const checks=[],errors=[],sessions=[];let outcome='failed';
try{
  for(let i=0;i<200;i++){if(server.exitCode!==null)throw new Error('isolated server exited');try{if((await fetch(`${base}/api/health`)).ok)break;}catch{}await new Promise(r=>setTimeout(r,100));}
  browser=await chromium.launch({headless:true,args:['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream',`--use-file-for-fake-audio-capture=${sample}`,'--autoplay-policy=no-user-gesture-required']});
  const context=await browser.newContext({permissions:['microphone'],acceptDownloads:true,viewport:{width:1280,height:1000}});
  const page=await context.newPage();page.setDefaultTimeout(45000);page.on('pageerror',e=>errors.push(e.message));
  let serverCreates=0;
  page.on('request',r=>{if(r.method()==='POST'&&r.url().endsWith('/api/dictation/sessions'))serverCreates++;});
  await page.addInitScript(()=>{
    const getMedia=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);window.__tracks=[];
    navigator.mediaDevices.getUserMedia=async c=>{const s=await getMedia(c);window.__tracks.push(...s.getTracks());return s;};
  });
  const state=async id=>context.request.get(`${base}/api/dictation/sessions/${id}`).then(r=>r.json());
  async function until(id,status){for(let i=0;i<1200;i++){const s=await state(id);if(s.status===status)return s;if(['failed','cancelled','interrupted'].includes(s.status))throw new Error(JSON.stringify(s));await page.waitForTimeout(50);}throw new Error(`timed out ${JSON.stringify(await state(id))}`);}
  await page.goto(`${base}/dictation?mode=record`);
  await page.getByRole('button',{name:'设置',exact:true}).click();
  await page.getByRole('checkbox',{name:/使用逐句轻度整理/}).uncheck();
  await page.getByRole('button',{name:'设置',exact:true}).click();
  const pane=page.getByRole('region',{name:'本地录音转写'}),dock=page.getByRole('complementary',{name:'常驻语音控制'});
  if(!formatsOnly){
  await pane.getByRole('button',{name:'新建本地录音',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.draft-current > strong')?.textContent?.includes('正在录音'));
  for(let i=0;i<3;i++){
    await page.waitForTimeout(60050);
    await pane.getByRole('button',{name:'暂停录音',exact:true}).click();
    await page.waitForFunction(()=>window.__tracks.every(t=>t.readyState==='ended'));
    await page.waitForFunction(()=>document.querySelector('.draft-current > p')?.textContent?.startsWith('已保存到本浏览器'));
    const before=await pane.locator('.draft-current > p').innerText();await page.waitForTimeout(400);
    assert.equal(await pane.locator('.draft-current > p').innerText(),before);
    await pane.getByRole('button',{name:'继续录音',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('.draft-current > strong')?.textContent?.includes('正在录音'));
  }
  await page.waitForTimeout(500);await pane.getByRole('button',{name:'结束录音',exact:true}).click();
  await pane.getByRole('button',{name:'试听录音',exact:true}).waitFor();
  assert.equal(serverCreates,0);assert.equal((await readdir(`${output}/project/data/dictation`)).length,0);
  await pane.getByRole('button',{name:'试听录音',exact:true}).click();await pane.locator('audio').waitFor();
  await page.waitForFunction(()=>document.querySelector('audio')?.duration>180);
  const downloaded=page.waitForEvent('download');await pane.getByRole('button',{name:'下载录音 WAV',exact:true}).click();
  await(await downloaded).saveAs(`${output}/recording.wav`);
  const wav=await readFile(`${output}/recording.wav`),samples=wav.readUInt32LE(40)/2;
  assert(samples>=180*16000);assert.equal(wav.length,44+samples*2);assert.equal(wav.readUInt32LE(24),16000);
  await pane.locator('audio').evaluate(audio=>audio.play());await page.waitForTimeout(150);
  assert(await pane.locator('audio').evaluate(audio=>audio.currentTime>0&&!audio.paused));
  await pane.locator('audio').evaluate(audio=>audio.pause());
  checks.push(`three-minute recording with three pauses: ${samples} committed samples, playable WAV/download, zero server session/worker creation`);
  await page.setViewportSize({width:390,height:844});await page.screenshot({path:`${output}/local-recording-mobile.png`,fullPage:true});await page.setViewportSize({width:1280,height:1000});
  // Freeze the exact local audio version before an actual upload. Start another local capture while GPU is occupied by the upload.
  const submitted=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().includes('/api/dictation/uploads?'));
  await pane.getByRole('button',{name:'开始转写',exact:true}).click();
  const response=await submitted;assert.equal(response.status(),200,await response.text());const sid=(await response.json()).id;sessions.push(sid);
  assert((await state(sid)).status!=='completed');
  await pane.getByRole('button',{name:'新建本地录音',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.draft-current > strong')?.textContent?.includes('正在录音'));
  // Pause the actual file pipeline, not the local microphone.
  const pauseButton=dock.getByRole('button',{name:'暂停转写',exact:true});await pauseButton.click();
  const paused=await until(sid,'paused');const directory=`${output}/project/data/dictation/${sid}`;
  const count=(await readdir(`${directory}/asr`)).length;await page.waitForTimeout(1000);
  const still=await state(sid);assert.equal(still.samples,paused.samples);assert.equal((await readdir(`${directory}/asr`)).length,count);
  assert(await page.evaluate(()=>window.__tracks.at(-1).readyState==='live'));
  await dock.getByRole('button',{name:'继续转写',exact:true}).click();
  const completed=await until(sid,'completed');assert.equal(completed.config.cleanup,false);
  assert(await page.evaluate(()=>window.__tracks.at(-1).readyState==='live'),'file terminal closed local owner');
  const pcm=await readFile(`${directory}/audio/source.pcm`);assert(pcm.equals(wav.subarray(44)),'local WAV/upload/decoded PCM are not the same version');
  assert.equal(completed.samples,samples);assert.equal(new Set(completed.utterances.map(u=>u.id)).size,completed.utterances.length);
  assert(completed.utterances.some(u=>u.raw_text));assert.equal(completed.progress.transcribed_samples,samples);
  await writeFile(`${output}/transcription.json`,JSON.stringify(completed,null,2));
  await pane.getByRole('button',{name:'结束录音',exact:true}).click();await pane.getByRole('button',{name:'试听录音',exact:true}).waitFor();
  checks.push('frozen recording WAV equals actual uploaded decoded PCM; real file pause/resume blocks new final ASR, no duplicate IDs; concurrent local recording survives model busy/file completion');
  // Refresh a live local draft: only committed data is restored and tracks are not re-acquired.
  await pane.getByRole('button',{name:'新建本地录音',exact:true}).click();await page.waitForTimeout(1500);await page.reload();
  await pane.getByRole('button',{name:'打开草稿',exact:true}).first().click();
  assert.equal(await page.evaluate(()=>window.__tracks.length),0);
  await pane.getByRole('button',{name:'试听录音',exact:true}).click();await pane.locator('audio').waitFor();
  checks.push('unexpected refresh recovers committed draft with no automatic microphone restart');
  }
  await page.getByRole('button',{name:'设置',exact:true}).click();await page.getByRole('checkbox',{name:/使用逐句轻度整理/}).uncheck();await page.getByRole('button',{name:'设置',exact:true}).click();
  for(const file of [sample,`${output}/sample.mp3`,`${output}/sample.m4a`]){
    await page.getByRole('button',{name:'导入音频',exact:true}).click();
    await page.waitForFunction(()=>{const input=document.querySelector('input[type=file]');return input&&!input.disabled;});
    const created=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().includes('/api/dictation/uploads?'));
    await page.getByLabel('选择录音文件').setInputFiles(file);const res=await created;assert.equal(res.status(),200);
    // Large Blob traffic can evict Chrome inspector response bodies. Read the owned history API instead.
    const history=await context.request.get(`${base}/api/dictation/sessions`).then(r=>r.json());
    const id=history.find(s=>!sessions.includes(s.id))?.id;assert(id);sessions.push(id);const s=await until(id,'completed');assert(s.utterances.some(u=>u.raw_text));assert.equal(s.config.cleanup,false);
  }
  checks.push('WAV, MP3 and M4A import through existing decoder/final-ASR contract with cleanup disabled');
  assert.deepEqual(errors,[]);outcome='passed';
}finally{
  await writeFile(`${output}/report.json`,JSON.stringify({outcome,profile:formatsOnly?'formats-only':'full',checks,errors,sessions,public_source:sample,cleanup_enabled:false,physical_microphone_tested:false},null,2));
  await browser?.close();server.kill('SIGTERM');await new Promise(r=>server.exitCode!==null?r():server.once('exit',r));await log.close();console.log(outcome,checks.join('\n'));
}
