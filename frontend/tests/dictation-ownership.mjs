// Synthetic HTTP state test, deliberately no model or paid gateway requests.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir, open, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
const root = resolve(import.meta.dirname, '../..');
const output = resolve(root, process.env.DICTATION_TEST_OUTPUT || 'tmp/dictation-ownership');
await mkdir(output, { recursive: false });
const log = await open(`${output}/vite.log`, 'w');
const base = 'http://127.0.0.1:5175';
const server = spawn(process.execPath, [`${root}/frontend/node_modules/vite/bin/vite.js`, '--host','127.0.0.1','--port','5175','--strictPort'],
  { cwd: `${root}/frontend`, stdio: ['ignore',log.fd,log.fd] });
const historyId = 'a'.repeat(32), taskId = 'b'.repeat(32);
const makeState = (id, status, text, mode = 'live') => ({ id, status, phase: status === 'completed' ? '已完成' : '正在转录上传录音', mode,
  revision: 1, event_sequence: 1, samples: 16000, next_sequence: 0, created_at: new Date().toISOString(),
  config: { terms: [], cleanup: false, cleanup_model: 'gpt-6-luna' }, draft_text: text,
  utterances: [{ id: '1', revision: 1, raw_text: text, text, status: 'raw', preview: '', edited: false, needs_review: false }] });
let historical = makeState(historyId, 'completed', '历史正文，不能被上传响应抢走。');
let task = makeState(taskId, 'listening', '活动任务正文', 'upload');
let releaseUpload, uploads = 0, sessions = 0;
const uploaded = new Promise(resolve => { releaseUpload = resolve; });
const stopTargets = [], errors = [], checks = [];
let browser;
try {
  for (let i = 0; i < 100; i++) { try { if ((await fetch(base)).ok) break; } catch {} await new Promise(r => setTimeout(r, 100)); }
  browser = await chromium.launch({ headless: true, args: ['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream'] });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await context.route('**/api/dictation/**', async route => {
    const req = route.request(), path = new URL(req.url()).pathname;
    if (path.endsWith('/uploads')) { uploads++; await uploaded; return route.fulfill({ json: task }); }
    if (req.method() === 'POST' && path.endsWith('/sessions')) { sessions++; return route.fulfill({ status: 409, json: { detail: 'must not start a second task' } }); }
    if (path.endsWith('/stop') || path.endsWith('/cancel')) {
      const id = path.split('/').at(-2); stopTargets.push(id);
      const state = id === taskId ? task : historical;
      state.status = 'completed'; state.phase = '已完成'; state.revision++;
      return route.fulfill({ json: state });
    }
    if (path.endsWith('/sessions')) return route.fulfill({ json: [historical] });
    return route.fulfill({ json: path.includes(taskId) ? task : historical });
  });
  const page = await context.newPage(); page.on('pageerror', e => errors.push(e.message));
  await page.goto(`${base}/dictation`);
  await page.getByRole('button', { name: '导入音频', exact: true }).click();
  await page.getByLabel('选择录音文件').setInputFiles({ name: 'own-test.wav', mimeType: 'audio/wav', buffer: Buffer.from('synthetic upload') });
  await page.waitForFunction(() => document.querySelector('[role="status"]')?.textContent?.includes('上传'));
  await page.getByRole('button', { name: '历史结果', exact: true }).click();
  await page.getByRole('button', { name: '打开', exact: true }).click();
  await page.getByText(historical.draft_text, { exact: true }).waitFor();
  releaseUpload();
  const dock = page.getByRole('complementary', { name: '常驻语音控制' });
  await dock.waitFor();
  assert.equal(uploads, 1);
  assert(await page.getByText(historical.draft_text, { exact: true }).isVisible());
  assert(await page.getByRole('button', { name: '开始听写', exact: true }).isDisabled());
  assert.equal(sessions, 0);
  checks.push('upload success registers active task after selection changed, without replacing historical view; second task disabled');
  // Explicit view-only actions cannot mutate a different active task, even with competing snapshots.
  historical.status = 'paused'; historical.phase = '已暂停'; historical.revision++;
  await page.getByRole('button', { name: '停止', exact: true }).waitFor();
  await page.getByRole('button', { name: '停止', exact: true }).click();
  await page.waitForFunction(() => !!document.querySelector('.dictation-dock'));
  assert.deepEqual(stopTargets, [historyId]);
  assert(await dock.isVisible());
  checks.push('view-only stop targets its explicit session ID, not the different active task');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: `${output}/history-with-active-task-mobile.png`, fullPage: true });
  await dock.getByRole('button', { name: '结束听写', exact: true }).click();
  await dock.waitFor({ state: 'hidden' });
  assert.deepEqual(stopTargets, [historyId, taskId]);
  checks.push('dock stop targets the owned active task and removes dock after completion');
  // A completed owned live token must not cancel the next task's creation generation.
  const c2 = await browser.newContext({ permissions: ['microphone'] });
  const predecessorId = 'c'.repeat(32), successorId = 'd'.repeat(32);
  let predecessor = makeState(predecessorId, 'preparing', '旧实时任务正文');
  const successor = makeState(successorId, 'listening', '新上传活动正文', 'upload');
  let finishUpload, hasPredecessor = true;
  const slowUpload = new Promise(resolve => { finishUpload = resolve; });
  let delayNext = false, releaseDelayed, delayedEntered;
  const delayedSeen = new Promise(resolve => { delayedEntered = resolve; });
  const delayed = new Promise(resolve => { releaseDelayed = resolve; });
  await c2.route('**/api/dictation/**', async route => {
    const req = route.request(), path = new URL(req.url()).pathname;
    if (path.endsWith('/config')) return route.fulfill({ json: { max_upload_mb: 100, max_upload_minutes: 30 } });
    if (req.method() === 'GET' && path.endsWith(predecessorId) && delayNext) {
      delayNext = false; const snapshot = structuredClone(predecessor); delayedEntered(); await delayed;
      return route.fulfill({ json: snapshot });
    }
    if (path.endsWith('/uploads')) { await slowUpload; return route.fulfill({ json: successor }); }
    if (req.method() === 'POST' && path.endsWith('/sessions')) return route.fulfill({ json: predecessor });
    if (req.method() === 'DELETE') { hasPredecessor = false; return route.fulfill({ json: { deleted: true } }); }
    if (path.endsWith('/sessions')) return route.fulfill({ json: hasPredecessor ? [predecessor] : [] });
    return route.fulfill({ json: path.includes(successorId) ? successor : predecessor });
  });
  const p2 = await c2.newPage(); p2.on('pageerror', e => errors.push(e.message));
  await p2.addInitScript(() => {
    const getMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    window.__tracks = [];
    navigator.mediaDevices.getUserMedia = async constraints => { const stream = await getMedia(constraints); window.__tracks.push(...stream.getTracks()); return stream; };
    window.WebSocket = class {
      readyState = 1;
      send() {}
      close() { this.readyState = 3; this.onclose?.(); }
    };
  });
  await p2.goto(`${base}/dictation`);
  await p2.getByRole('button', { name: '开始听写', exact: true }).click();
  const dock2 = p2.getByRole('complementary', { name: '常驻语音控制' });
  await dock2.waitFor();
  predecessor.status = 'completed'; predecessor.phase = '旧任务完成'; predecessor.revision++;
  await dock2.waitFor({ state: 'hidden' });
  delayNext = true; await delayedSeen;
  await p2.getByRole('button', { name: '录音转写', exact: true }).click();
  const pane = p2.getByRole('region', { name: '本地录音转写' });
  await pane.getByRole('button', { name: '新建本地录音', exact: true }).click();
  await p2.waitForFunction(() => document.querySelector('.draft-current > strong')?.textContent?.includes('正在录音'));
  await p2.waitForTimeout(1300); releaseDelayed();
  await p2.waitForTimeout(2300); // Includes repeated refreshes of the viewed old live snapshot.
  assert(await p2.evaluate(() => window.__tracks.at(-1).readyState === 'live'));
  assert((await pane.locator('.draft-current > strong').innerText()).includes('正在录音'));
  await pane.getByRole('button', { name: '结束录音', exact: true }).click();
  await pane.getByRole('button', { name: '试听录音', exact: true }).waitFor();
  assert(await p2.evaluate(() => window.__tracks.every(t => t.readyState === 'ended')));
  checks.push('local recording after live completion survives normal polls and an old delayed live terminal snapshot; hardware and UI agree');
  await p2.getByRole('button', { name: '导入音频', exact: true }).click();
  await p2.getByLabel('选择录音文件').setInputFiles({ name: 'successor.wav', mimeType: 'audio/wav', buffer: Buffer.from('synthetic') });
  await p2.getByRole('button', { name: '历史结果', exact: true }).click();
  await p2.getByRole('button', { name: '打开', exact: true }).click();
  await p2.getByText('旧实时任务正文', { exact: true }).waitFor();
  await p2.getByRole('button', { name: '历史结果', exact: true }).click();
  p2.once('dialog', dialog => dialog.accept());
  const deletedPredecessor = p2.waitForResponse(r => r.request().method() === 'DELETE');
  await p2.getByRole('button', { name: '删除服务器录音', exact: true }).click();
  await deletedPredecessor;
  finishUpload();
  await dock2.waitFor();
  await p2.getByRole('button', { name: '实时听写', exact: true }).click();
  assert(await p2.getByRole('button', { name: '开始听写', exact: true }).isDisabled());
  assert(await dock2.getByText('正在转录上传录音', { exact: true }).isVisible());
  assert.equal(hasPredecessor, false);
  checks.push('completed owned live → slow upload → open/delete predecessor → upload success retains the new active task');
  await c2.close();
  assert.deepEqual(errors, []);
  await writeFile(`${output}/report.json`, JSON.stringify({ checks, errors, synthetic: true, uploads, sessions, stopTargets }, null, 2));
  console.log(checks.join('\n'));
} finally {
  releaseUpload(); await browser?.close(); server.kill('SIGTERM');
  await new Promise(r => server.exitCode !== null ? r() : server.once('exit', r)); await log.close();
}
