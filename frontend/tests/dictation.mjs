// Actual local ASR workers + real PCM replay through the browser AudioWorklet.
// Chrome virtual microphone uses recorded speech; this does not replace a physical-mic acceptance.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir, writeFile, open } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';

const root = resolve(import.meta.dirname, '../..');
const output = resolve(root, process.env.DICTATION_TEST_OUTPUT || 'tmp/dictation-browser-01');
await mkdir(output, { recursive: false });
// Set an explicit target to verify an already-deployed Docker service. Each run
// owns a fresh browser cookie and only edits/deletes sessions created by itself.
const external = process.env.DICTATION_TEST_BASE_URL;
const base = (external || 'http://127.0.0.1:8021').replace(/\/$/, '');
const log = external ? null : await open(`${output}/server.log`, 'w');
const server = external ? null : spawn(`${root}/.venv/bin/python`, ['-m','uvicorn','api_server:app','--host','127.0.0.1','--port','8021'],
  { cwd: root, env: process.env, stdio: ['ignore', log.fd, log.fd] });
let browser;
const errors = [], results = [];
const sample = resolve(root, 'data/models/dictation/paraformer/test_wavs/0.wav');
async function check(name, run) { await run(); results.push(name); console.log(`PASS ${name}`); }
try {
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(`${base}/api/health`)).ok) break; } catch {}
    await new Promise(r => setTimeout(r, 100));
  }
  browser = await chromium.launch({ headless: true, args: [
    '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream',
    `--use-file-for-fake-audio-capture=${sample}`, '--autoplay-policy=no-user-gesture-required',
  ] });
  const context = await browser.newContext({ viewport: { width: 1280, height: 1000 }, permissions: ['microphone', 'clipboard-read', 'clipboard-write'] });
  const page = await context.newPage();
  page.setDefaultTimeout(45000);
  page.on('pageerror', e => errors.push(e.message));
  await page.addInitScript(() => {
    const native = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    window.__dictationTracks = [];
    window.__dictationSockets = [];
    const NativeSocket = window.WebSocket;
    window.WebSocket = class extends NativeSocket {
      constructor(...args) { super(...args); window.__dictationSockets.push(this); }
    };
    navigator.mediaDevices.getUserMedia = async constraints => {
      const stream = await native(constraints);
      window.__dictationTracks.push(...stream.getTracks());
      return stream;
    };
  });
  let sid;
  await check('real virtual microphone → worklet → WS reconnect → two ASR passes → cleanup → immediate mic release', async () => {
    await page.goto(`${base}/dictation`);
    await page.getByRole('button', { name: '设置', exact: true }).click();
    await page.getByLabel('少量术语', { exact: false }).fill('WebSocket');
    await page.getByRole('button', { name: '设置', exact: true }).click();
    const created = page.waitForResponse(r => r.url().endsWith('/api/dictation/sessions') && r.request().method() === 'POST');
    await page.getByRole('button', { name: '开始听写', exact: true }).click();
    const response = await created;
    assert.equal(response.status(), 200, await response.text());
    sid = (await response.json()).id;
    await page.waitForFunction(() => document.querySelector('.dictation-indicator')?.classList.contains('is-on'));
    await page.waitForTimeout(4000);
    await page.evaluate(() => window.__dictationSockets.at(-1).close());
    await page.waitForFunction(() => window.__dictationSockets.length >= 2 && window.__dictationSockets.at(-1).readyState === WebSocket.OPEN);
    await page.waitForTimeout(7000);
    await page.getByRole('button', { name: '停止', exact: true }).click();
    await page.waitForFunction(() => window.__dictationTracks.length > 0 && window.__dictationTracks.every(track => track.readyState === 'ended'));
    let state;
    for (let attempt = 0; attempt < 600; attempt++) {
      state = await context.request.get(`${base}/api/dictation/sessions/${sid}`).then(r => r.json());
      if (state.status === 'completed' || state.status === 'failed') break;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    await writeFile(`${output}/live.json`, JSON.stringify(state, null, 2));
    assert.equal(state.status, 'completed', JSON.stringify(state));
    assert(state.utterances.length >= 2);
    assert(state.samples >= 16000 * 10, 'short reconnection lost a large part of the sample clock');
    assert.equal(new Set(state.utterances.map(u => u.id)).size, state.utterances.length);
    assert(state.utterances.every(u => u.raw_text || u.status === 'asr_failed'));
    assert(state.draft_text.includes('Monday') || state.draft_text.includes('monday'));
    await writeFile(`${output}/live.json`, JSON.stringify(state, null, 2));
    await page.screenshot({ path: `${output}/live-desktop.png`, fullPage: true });
  });
  await check('manual sentence edit, immutable clipboard snapshot, GET and TXT agree', async () => {
    await page.locator('.dictation-utterance').first().getByRole('button', { name: '编辑', exact: true }).click();
    await page.getByRole('textbox', { name: /编辑第/ }).fill('预算1500元，不是15000元。');
    await page.getByRole('button', { name: '保存', exact: true }).click();
    await page.getByText('预算1500元，不是15000元。', { exact: true }).waitFor();
    await page.getByRole('button', { name: '复制已完成文字' }).click();
    const clipboard = await page.evaluate(() => navigator.clipboard.readText());
    const snapshot = await page.evaluate(async id => fetch(`/api/dictation/sessions/${id}`).then(r => r.json()), sid);
    const txt = await page.evaluate(async id => fetch(`/api/dictation/sessions/${id}/draft.txt`).then(r => r.text()), sid);
    assert.equal(clipboard, snapshot.draft_text); assert.equal(txt, clipboard);
  });
  await check('whole-document diff preview → apply → save → GET/TXT consistency', async () => {
    await page.getByRole('button', { name: '整理全文', exact: true }).click();
    await page.getByRole('region', { name: '全文整理差异预览' }).waitFor();
    await page.getByRole('button', { name: '确认应用全文稿' }).click();
    await page.getByRole('button', { name: '编辑全文', exact: true }).click();
    await page.getByRole('textbox', { name: '编辑全文', exact: true }).fill('人工全文：不是不能做，是现在不做。');
    await page.getByRole('button', { name: '保存全文', exact: true }).click();
    await page.getByText('人工全文：不是不能做，是现在不做。', { exact: true }).waitFor();
    const state = await page.evaluate(async id => fetch(`/api/dictation/sessions/${id}`).then(r => r.json()), sid);
    const txt = await page.evaluate(async id => fetch(`/api/dictation/sessions/${id}/draft.txt`).then(r => r.text()), sid);
    assert.equal(state.draft_text, txt);
  });
  await check('compact popup shares session, mobile settings do not cover content', async () => {
    const popupPromise = page.waitForEvent('popup');
    await page.getByRole('button', { name: '紧凑小窗', exact: true }).click();
    const popup = await popupPromise;
    await popup.getByText('人工全文：不是不能做，是现在不做。', { exact: true }).waitFor();
    assert(new URL(popup.url()).searchParams.get('session') === sid);
    await popup.setViewportSize({ width: 390, height: 844 });
    await popup.getByRole('button', { name: '设置', exact: true }).click();
    assert(await popup.locator('.dictation-settings').isVisible());
    assert.equal(await popup.getByLabel('少量术语', { exact: false }).inputValue(), 'WebSocket');
    assert(await popup.locator('.app-sidebar').isHidden());
    assert(await popup.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    const paper = await popup.locator('.dictation-paper').boundingBox();
    const controls = await popup.locator('.dictation-controls').boundingBox();
    assert(paper && controls && controls.y >= paper.y + paper.height);
    await popup.screenshot({ path: `${output}/compact-settings.png`, fullPage: true });
    await popup.close();
  });
  await check('real WAV upload → Qwen → cleanup → historical result', async () => {
    await page.getByRole('button', { name: '上传录音', exact: true }).click();
    await page.getByLabel('选择录音文件').setInputFiles(sample);
    let uploadComplete = false;
    for (let attempt = 0; attempt < 600; attempt++) {
      const states = await context.request.get(`${base}/api/dictation/sessions`).then(r => r.json());
      if (states.some(s => s.mode === 'upload' && s.status === 'completed')) { uploadComplete = true; break; }
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert(uploadComplete, 'upload did not complete');
    await page.waitForTimeout(1200);
    const text = await page.locator('.dictation-paper').innerText();
    assert(text.includes('Monday') || text.includes('monday'));
    const states = await page.evaluate(() => fetch('/api/dictation/sessions').then(r => r.json()));
    const uploaded = states.find(s => s.mode === 'upload');
    assert(uploaded.utterances.every(u => !u.preview)); // no Paraformer in upload mode
    await writeFile(`${output}/upload.json`, JSON.stringify(uploaded, null, 2));
    await page.screenshot({ path: `${output}/upload-result.png`, fullPage: true });
    await page.getByRole('button', { name: '历史结果', exact: true }).click();
    await page.locator('.dictation-history article').nth(1).waitFor();
    assert(await page.locator('.dictation-history article').count() >= 2);
  });
  await check('history deletion requires confirmation and removes only the chosen session', async () => {
    const rows = page.locator('.dictation-history article');
    const before = await rows.count();
    page.once('dialog', dialog => dialog.dismiss());
    await rows.first().getByRole('button', { name: '删除', exact: true }).click();
    assert.equal(await rows.count(), before);
    const deleted = page.waitForResponse(r => r.request().method() === 'DELETE' && r.url().includes('/api/dictation/sessions/'));
    page.once('dialog', dialog => dialog.accept());
    await rows.first().getByRole('button', { name: '删除', exact: true }).click();
    assert.equal((await deleted).status(), 200);
    await page.waitForFunction(count => document.querySelectorAll('.dictation-history article').length === count, before - 1);
  });
  await check('permission denial creates no server-side phantom session', async () => {
    const denied = await browser.newContext();
    const p = await denied.newPage();
    await p.addInitScript(() => { navigator.mediaDevices.getUserMedia = async () => { throw new DOMException('Permission denied', 'NotAllowedError'); }; });
    let posts = 0;
    p.on('request', r => { if (r.url().endsWith('/api/dictation/sessions') && r.method() === 'POST') posts++; });
    await p.goto(`${base}/dictation`);
    await p.getByRole('button', { name: '开始听写', exact: true }).click();
    await p.getByRole('alert').waitFor();
    assert.equal(posts, 0);
    await denied.close();
  });
  assert.deepEqual(errors, []);
  await writeFile(`${output}/report.json`, JSON.stringify({ results, errors, base_url: base, managed_server: !external, audio_source: sample,
    physical_microphone_tested: false, virtual_microphone_real_audio: true }, null, 2));
  await context.close();
} finally {
  await browser?.close();
  if (server) {
    server.kill('SIGTERM');
    await new Promise(resolve => { if (server.exitCode !== null) resolve(); else server.once('exit', resolve); });
  }
  await log?.close();
}
