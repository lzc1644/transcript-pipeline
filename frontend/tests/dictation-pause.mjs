// Real public speech -> browser capture -> WS -> actual CPU/GPU; no paid cleanup.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir, open, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
const root = resolve(import.meta.dirname, '../..');
const output = resolve(root, process.env.DICTATION_TEST_OUTPUT || 'tmp/dictation-optimization/a-real');
await mkdir(output, { recursive: false });
const log = await open(`${output}/server.log`, 'w');
const base = 'http://127.0.0.1:8023';
const server = spawn(`${root}/.venv/bin/python`, ['scripts/serve_dictation_test.py','--root',`${output}/project`,'--port','8023'],
  { cwd: root, env: process.env, stdio: ['ignore',log.fd,log.fd] });
const sample = resolve(root, 'data/models/dictation/paraformer/test_wavs/0.wav');
let browser, context;
const checks = [], errors = [], artifacts = [];
let outcome = 'failed';
try {
  for (let i = 0; i < 200; i++) {
    if (server.exitCode !== null) throw new Error('isolated server exited');
    try { if ((await fetch(`${base}/api/health`)).ok) break; } catch {}
    await new Promise(r => setTimeout(r, 100));
  }
  browser = await chromium.launch({ headless: true, args: ['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream',
    `--use-file-for-fake-audio-capture=${sample}`, '--autoplay-policy=no-user-gesture-required'] });
  context = await browser.newContext({ viewport: { width: 1280, height: 1000 }, permissions: ['microphone','clipboard-read','clipboard-write'] });
  const page = await context.newPage(); page.setDefaultTimeout(45000); page.on('pageerror', e => errors.push(e.message));
  await page.addInitScript(() => {
    const getMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices), Socket = window.WebSocket, Context = window.AudioContext;
    window.__tracks = []; window.__sockets = []; window.__packets = []; window.__contexts = 0; window.__resumeMessages = 0;
    window.WebSocket = class extends Socket {
      constructor(...args) { super(...args); window.__sockets.push(this); }
      send(data) {
        if (data instanceof ArrayBuffer) {
          const view = new DataView(data);
          window.__packets.push({ seq: view.getUint32(0,true), start: Number(view.getBigUint64(4,true)), pcm: Array.from(new Uint8Array(data,12)) });
        } else if (JSON.parse(data).type === 'resume') window.__resumeMessages++;
        return super.send(data);
      }
    };
    window.AudioContext = class extends Context { constructor(...args) { super(...args); window.__contexts++; } };
    navigator.mediaDevices.getUserMedia = async constraints => {
      if (window.__denyOnce) { window.__denyOnce = false; throw new DOMException('测试拒绝继续权限','NotAllowedError'); }
      const stream = await getMedia(constraints); window.__tracks.push(...stream.getTracks());
      if (window.__delayPermission) await new Promise(resolve => { window.__releasePermission = resolve; });
      return stream;
    };
  });
  const snapshot = async sid => context.request.get(`${base}/api/dictation/sessions/${sid}`).then(r => r.json());
  async function waitStatus(sid, status) {
    for (let i = 0; i < 1200; i++) {
      const state = await snapshot(sid);
      if (state.status === status) return state;
      if (['failed','cancelled','interrupted'].includes(state.status) && state.status !== status) throw new Error(JSON.stringify(state));
      await page.waitForTimeout(50);
    }
    throw new Error(`status ${status} timed out: ${JSON.stringify(await snapshot(sid))}`);
  }
  const dock = page.getByRole('complementary', { name: '常驻语音控制' });
  const start = async () => {
    const response = page.waitForResponse(r => r.url().endsWith('/api/dictation/sessions') && r.request().method() === 'POST');
    await page.getByRole('button', { name: '开始听写', exact: true }).click();
    const res = await response; assert.equal(res.status(),200,await res.text());
    const sid = (await res.json()).id;
    await page.waitForFunction(() => document.querySelector('.dictation-dock__signal')?.classList.contains('is-on'));
    assert.equal((await snapshot(sid)).config.cleanup,false);
    return sid;
  };
  const pause = async sid => {
    await dock.getByRole('button', { name: '暂停听写', exact: true }).click();
    await page.waitForFunction(() => window.__tracks.every(t => t.readyState === 'ended'));
    const state = await waitStatus(sid,'paused');
    await dock.getByRole('button', { name: '继续听写', exact: true }).waitFor();
    return state;
  };
  const resume = async sid => {
    await dock.getByRole('button', { name: '继续听写', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('.dictation-dock__signal')?.classList.contains('is-on'));
    assert.equal((await snapshot(sid)).status,'listening');
  };
  async function geometry(name) {
    // ResizeObserver and Vue commit the new dock height on the next rendering turn.
    await page.waitForFunction(() => {
      const dock = document.querySelector('.dictation-dock'), main = document.querySelector('.app-content');
      return dock && main && parseFloat(getComputedStyle(main).paddingBottom) >= dock.getBoundingClientRect().height + 16;
    });
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => { scrollTo(0, document.documentElement.scrollHeight); requestAnimationFrame(resolve); })));

    const metrics = await page.evaluate(() => {
      const panel = document.querySelector('.dictation-dock'), main = document.querySelector('.app-content'), controls = document.querySelector('.dictation-controls');
      return { width: document.documentElement.scrollWidth, viewport: innerWidth, dock: panel.getBoundingClientRect().toJSON(),
        padding: parseFloat(getComputedStyle(main).paddingBottom), controls: controls?.getBoundingClientRect().toJSON() };
    });
    assert(metrics.width <= metrics.viewport, JSON.stringify(metrics));
    assert(metrics.padding >= metrics.dock.height + 16,JSON.stringify(metrics));
    if (metrics.controls) assert(metrics.controls.bottom <= metrics.dock.y,JSON.stringify(metrics));
    await page.screenshot({ path: `${output}/${name}.png`, fullPage: true });
    artifacts.push({ name, ...metrics });
  }
  await page.goto(`${base}/dictation`);
  await page.getByRole('button', { name: '设置', exact: true }).click();
  await page.getByRole('checkbox', { name: /使用逐句轻度整理/ }).uncheck();
  await page.getByRole('button', { name: '设置', exact: true }).click();
  const sid = await start();
  await page.waitForTimeout(2200);
  const ownersBefore = await page.evaluate(() => ({ tracks: window.__tracks.length, sockets: window.__sockets.length, contexts: window.__contexts }));
  for (const [path, name] of [['/jobs','任务列表'],['/settings','运行设置'],['/dictation','语音输入']]) {
    await page.locator(`.app-sidebar a[href="${path}"]`).click();
    await page.waitForURL(`${base}${path}`);
    await dock.waitFor(); assert.equal((await snapshot(sid)).id,sid);
  }
  assert.deepEqual(await page.evaluate(() => ({ tracks: window.__tracks.length, sockets: window.__sockets.length, contexts: window.__contexts })), ownersBefore);
  checks.push('three internal routes retain the same microphone, AudioContext, socket and session');
  await page.waitForFunction(() => {
    const view = document.querySelector('.dictation-page');
    return view && getComputedStyle(view).opacity === '1' && !view.classList.contains('page-enter-active');
  });
  await geometry('desktop-listening');
  const paused = await pause(sid);
  const packetsAtPause = await page.evaluate(() => window.__packets.length);
  const pausedAt = Date.now();
  await page.waitForTimeout(70500);
  const afterWait = await snapshot(sid);
  assert.equal(afterWait.status,'paused'); assert.equal(afterWait.samples,paused.samples);
  assert.equal(await page.evaluate(() => window.__packets.length),packetsAtPause);
  assert(await page.evaluate(() => window.__tracks.every(t => t.readyState === 'ended')));
  checks.push(`paused ${((Date.now()-pausedAt)/1000).toFixed(1)} seconds: mic ended, zero packets/samples, no idle/no-audio termination`);
  // A short lost connection while paused reconnects but never acquires a microphone.
  const socketsBefore = await page.evaluate(() => window.__sockets.length);
  await page.evaluate(() => window.__sockets.at(-1).close());
  await page.waitForFunction(n => window.__sockets.length > n && window.__sockets.at(-1).readyState === WebSocket.OPEN,socketsBefore);
  assert.equal((await snapshot(sid)).status,'paused');
  assert(await page.evaluate(() => window.__tracks.every(t => t.readyState === 'ended')));
  await page.setViewportSize({ width: 390, height: 844 });
  await geometry('mobile-paused');
  await dock.getByRole('button', { name: '收起', exact: true }).click();
  await geometry('mobile-collapsed');
  await dock.getByRole('button', { name: '展开', exact: true }).click();
  // Failed permissions keep a real pause, not a green recording status.
  await page.evaluate(() => { window.__denyOnce = true; });
  await dock.getByRole('button', { name: '继续听写', exact: true }).click();
  await dock.getByRole('alert').filter({ hasText: '未能继续' }).waitFor();
  assert.equal((await snapshot(sid)).status,'paused');
  assert.equal((await snapshot(sid)).samples,paused.samples);
  await resume(sid);
  for (let i = 0; i < 20; i++) {
    await page.waitForTimeout(240 + (i % 3) * 30);
    await pause(sid); await resume(sid);
  }
  await page.waitForTimeout(370);
  await dock.getByRole('button', { name: '结束听写', exact: true }).click();
  await page.waitForFunction(() => window.__tracks.every(t => t.readyState === 'ended'));
  const final = await waitStatus(sid,'completed');
  await writeFile(`${output}/live.json`, JSON.stringify(final,null,2));
  const packets = await page.evaluate(() => window.__packets);
  const bySequence = new Map();
  for (const packet of packets) {
    if (bySequence.has(packet.seq)) assert.deepEqual(packet,bySequence.get(packet.seq));
    bySequence.set(packet.seq,packet);
  }
  const unique = [...bySequence.values()].sort((a,b) => a.seq-b.seq);
  let expected = 0;
  unique.forEach((p,i) => { assert.equal(p.seq,i); assert.equal(p.start,expected); expected += p.pcm.length/2; });
  assert.equal(final.samples,expected); assert.equal(final.next_sequence,unique.length);
  const source = await readFile(`${output}/project/data/dictation/${sid}/audio/source.pcm`);
  assert(source.equals(Buffer.concat(unique.map(p => Buffer.from(p.pcm)))),'persisted PCM differs from unique transmitted samples');
  assert(unique.some(p => p.pcm.length < 1280*2),'no non-standard tail packet observed');
  assert.equal(new Set(final.utterances.map(u => u.id)).size,final.utterances.length);
  assert(final.utterances.some(u => u.raw_text),'actual final ASR never ran');
  assert(final.metrics.paused_wall_seconds >= 70);
  for (const u of final.utterances) {
    if (u.final_latency_seconds !== undefined) assert(u.final_latency_seconds < 30,'pause gap polluted latency');
    const wav = await readFile(`${output}/project/data/dictation/${sid}/audio/${u.id}.wav`);
    assert(wav.subarray(44).equals(source.subarray(u.start_sample*2,u.end_sample*2)),`sentence ${u.id} differs from saved sample range`);
  }
  checks.push('20 pause/resume cycles, denied resume, short paused disconnect, stop tail: exact unique packet/PCM/WAV coverage, no duplicate IDs, pause-free latency');
  // Late permission must lose to end; there may be no resume command and no live track.
  await page.setViewportSize({ width: 1280, height: 1000 });
  const sid2 = await start(); await page.waitForTimeout(600); await pause(sid2);
  const resumeMessages = await page.evaluate(() => window.__resumeMessages);
  await page.evaluate(() => { window.__delayPermission = true; });
  await dock.getByRole('button', { name: '继续听写', exact: true }).click();
  await page.waitForFunction(() => !!window.__releasePermission);
  await dock.getByRole('button', { name: '结束听写', exact: true }).click();
  await page.evaluate(() => { window.__delayPermission = false; window.__releasePermission(); });
  await waitStatus(sid2,'completed');
  await page.waitForFunction(() => window.__tracks.every(t => t.readyState === 'ended'));
  assert.equal(await page.evaluate(() => window.__resumeMessages),resumeMessages);
  checks.push('end beats a delayed resume permission result; late track is stopped with no resume dispatch');
  const sid3 = await start(); await page.waitForTimeout(500); const beforeOffline = await pause(sid3);
  await context.setOffline(true); await page.evaluate(() => window.__sockets.at(-1).close());
  const interrupted = await waitStatus(sid3,'interrupted');
  assert.equal(interrupted.samples,beforeOffline.samples);
  await context.setOffline(false);
  await dock.waitFor({ state: 'hidden' });
  assert(await page.evaluate(() => window.__tracks.every(t => t.readyState === 'ended')));
  checks.push('paused disconnection beyond recovery window interrupts and releases resources, never reopens mic');
  assert.deepEqual(errors,[]); outcome = 'passed';
} finally {
  await writeFile(`${output}/report.json`,JSON.stringify({ outcome, checks, errors, artifacts, physical_microphone_tested: false,
    real_public_audio: sample, cleanup_enabled: false, isolated_data: `${output}/project/data/dictation` },null,2));
  await browser?.close(); server.kill('SIGTERM');
  await new Promise(r => server.exitCode !== null ? r() : server.once('exit',r)); await log.close();
  console.log(outcome,checks.join('\n'));
}
