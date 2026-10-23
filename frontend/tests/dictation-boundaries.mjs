// Review regressions: two same-origin windows, real IndexedDB/Web Locks/capture,
// and route-driven dock navigation. HTTP state is synthetic; no ASR or paid calls.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir, open, writeFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { chromium } from 'playwright';

const root = resolve(import.meta.dirname, '../..'), base = 'http://127.0.0.1:5189';
const output = resolve(root, process.env.DICTATION_TEST_OUTPUT || 'tmp/dictation-boundaries');
await mkdir(dirname(output), { recursive: true }); await mkdir(output);
const log = await open(`${output}/vite.log`, 'w');
const server = spawn(process.execPath, [`${root}/frontend/node_modules/vite/bin/vite.js`, '--host', '127.0.0.1', '--port', '5189', '--strictPort'],
  { cwd: `${root}/frontend`, stdio: ['ignore', log.fd, log.fd] });
let browser, releaseUpload;
let outcome = 'failed'; const checks = [], errors = [];
try {
  for (let i = 0; i < 100; i++) {
    if (server.exitCode !== null) throw new Error('isolated Vite server exited');
    try { if ((await fetch(base)).ok) break; } catch {}
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  browser = await chromium.launch({ headless: true, args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'] });
  const context = await browser.newContext({ permissions: ['microphone'], viewport: { width: 1280, height: 960 } });
  let task = null, uploads = 0, holdUpload = true, uploadEntered;
  const uploading = new Promise(resolve => { uploadEntered = resolve; });
  const uploadRelease = new Promise(resolve => { releaseUpload = resolve; });
  await context.route('**/api/dictation/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (path.endsWith('/config')) return route.fulfill({ json: { max_upload_mb: 512, max_upload_minutes: 240 } });
    if (path.endsWith('/uploads')) {
      uploads++;
      if (holdUpload) {
        uploadEntered(); await uploadRelease;
        return route.fulfill({ status: 413, json: { detail: '定向测试上传失败，草稿保留' } });
      }
      task = { id: 'b'.repeat(32), status: 'listening', phase: '正在转录上传录音', mode: 'upload', revision: 1,
        event_sequence: 1, samples: 16000, next_sequence: 0, created_at: new Date().toISOString(),
        config: { terms: [], cleanup: false, cleanup_model: 'gpt-6-luna' }, draft_text: '面板返回正文测试',
        utterances: [{ id: '1', revision: 1, text: '面板返回正文测试', raw_text: '面板返回正文测试', status: 'raw', preview: '', edited: false }] };
      return route.fulfill({ json: task });
    }
    if (path.endsWith('/sessions')) return route.fulfill({ json: task ? [task] : [] });
    if (path.endsWith('/stop')) { task.status = 'completed'; task.phase = '已完成'; task.revision++; }
    return route.fulfill({ json: task ?? [] });
  });
  await context.addInitScript(() => {
    const native = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices); window.__tracks = [];
    navigator.mediaDevices.getUserMedia = async constraints => { const stream = await native(constraints); window.__tracks.push(...stream.getTracks()); return stream; };
  });
  async function windowPage() {
    const page = await context.newPage(); page.setDefaultTimeout(12000); page.on('pageerror', e => errors.push(e.message));
    await page.goto(`${base}/dictation?mode=record`);
    await page.evaluate(async () => { const { DraftStore } = await import('/src/composables/dictationDrafts.ts'); window.__store = new DraftStore(); });
    return page;
  }
  const pane = page => page.getByRole('region', { name: '本地录音转写' });
  const dock = page => page.getByRole('complementary', { name: '常驻语音控制' });
  const latest = page => page.evaluate(() => window.__store.list().then(rows => rows[0]));
  const get = (page, id) => page.evaluate(id => window.__store.get(id), id);
  const live = page => page.evaluate(() => window.__tracks.filter(track => track.readyState === 'live').length);
  async function savedBeyond(page, id, samples) {
    // waitForFunction treats an async predicate's Promise as truthy; poll the
    // resolved IDB value instead, so an empty first transaction cannot pass.
    for (let i = 0; i < 120; i++) {
      const value = await get(page, id); if (value.samples > samples) return value;
      await page.waitForTimeout(50);
    }
    throw new Error(`draft ${id} did not persist beyond ${samples} samples`);
  }
  async function begin(page) {
    await pane(page).getByRole('button', { name: '新建本地录音', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('.draft-current > strong')?.textContent?.includes('正在录音'));
    const draft = await latest(page); await savedBeyond(page, draft.id, 16000); return draft.id;
  }
  const a = await windowPage(), id = await begin(a);
  // Original failure: mode=record URL, switch tabs, then use the dock's recording link.
  await a.getByRole('button', { name: '导入音频', exact: true }).click();
  await a.getByLabel('选择录音文件').waitFor(); assert.equal(new URL(a.url()).searchParams.get('mode'), 'upload');
  await dock(a).getByRole('button', { name: '录音草稿', exact: true }).click();
  await pane(a).waitFor(); assert.equal(new URL(a.url()).searchParams.get('mode'), 'record');
  checks.push('record → upload → dock recording link returns to recording; URL and visible tab stay synchronized');

  const b = await windowPage();
  await pane(b).getByRole('button', { name: '打开草稿', exact: true }).first().click();
  await pane(b).getByRole('button', { name: '重新检查草稿', exact: true }).waitFor();
  assert.equal((await get(b, id)).status, 'recording');
  assert.equal(await dock(b).count(), 0); assert.equal(await live(b), 0);
  for (const name of ['继续录音', '暂停录音', '结束录音', '删除本地草稿', '开始转写']) assert.equal(await pane(b).getByRole('button', { name, exact: true }).count(), 0);
  // Exercise the shared boundary, not just disabled/hidden buttons.
  const rejected = await b.evaluate(async id => {
    const { createLocalRecorder } = await import('/src/composables/dictationDrafts.ts');
    const { PcmCapture } = await import('/src/composables/dictationCapture.ts');
    const recorder = createLocalRecorder(new PcmCapture(), () => true), value = await window.__store.get(id);
    const errors = [];
    for (const operation of [() => window.__store.put(value), () => window.__store.update(id, { status: 'interrupted' }),
      () => window.__store.append(id, value.samples, new ArrayBuffer(2)), () => window.__store.remove(id)]) {
      try { await operation(); errors.push('unexpected success'); } catch (e) { errors.push(e.message); }
    }
    await recorder.select(id); await recorder.begin(id); errors.push(recorder.error.value);
    for (const operation of [() => recorder.end(), () => recorder.remove(id), () => recorder.freezeForSubmission(),
      () => recorder.submission(id, 'failed', value.samples)]) {
      try { await operation(); errors.push('unexpected success'); } catch (e) { errors.push(e.message); }
    }
    return { errors, state: { samples: recorder.draft.value?.samples, active: recorder.active.value, busy: recorder.busy.value, submittingId: recorder.submittingId.value } };
  }, id);
  await writeFile(`${output}/rejected-operations.json`, JSON.stringify(rejected, null, 2));
  assert.equal(rejected.errors.length, 9); for (const [index, error] of rejected.errors.entries()) assert.match(error, /另一窗口/, `operation ${index}: ${error}`);
  const before = await get(b, id); await savedBeyond(a, id, before.samples);
  assert.equal((await get(b, id)).status, 'recording'); assert.equal(await live(a), 1); assert.equal(await live(b), 0);
  assert(await dock(a).getByRole('button', { name: '暂停录音', exact: true }).isVisible());
  await a.screenshot({ path: `${output}/owner-still-recording.png`, fullPage: true });
  await b.screenshot({ path: `${output}/second-window-read-only.png`, fullPage: true });
  checks.push('second window opens active draft read-only; writes/resume/end/delete/submit are denied, owner keeps microphone and controls and continues saving');

  await dock(a).getByRole('button', { name: '暂停录音', exact: true }).click();
  await dock(a).getByRole('button', { name: '继续录音', exact: true }).waitFor();
  await a.waitForFunction(() => document.querySelector('.draft-current > p')?.textContent?.startsWith('已保存到本浏览器'));
  assert.equal(await live(a), 0);
  await pane(b).getByRole('button', { name: '重新检查草稿', exact: true }).click();
  assert.equal((await get(b, id)).status, 'paused'); assert.equal(await dock(b).count(), 0);
  assert.equal(await b.evaluate(async id => (await window.__store.claim(id)) === null, id), true);
  await dock(a).getByRole('button', { name: '继续录音', exact: true }).click();
  await a.waitForFunction(() => window.__tracks.some(track => track.readyState === 'live'));
  await dock(a).getByRole('button', { name: '结束录音', exact: true }).click(); await dock(a).waitFor({ state: 'hidden' });
  await pane(b).getByRole('button', { name: '重新检查草稿', exact: true }).click();
  await pane(b).getByRole('button', { name: '试听录音', exact: true }).waitFor();
  assert.equal((await get(b, id)).status, 'ended'); assert.equal(await live(a), 0);
  checks.push('pause retains exclusive draft ownership without microphone; owner can resume/end; explicit recheck enables the other window only after release');

  const abandoned = await begin(a); const prefix = (await get(a, abandoned)).samples; await a.close();
  await b.reload();
  await b.evaluate(async () => { const { DraftStore } = await import('/src/composables/dictationDrafts.ts'); window.__store = new DraftStore(); });
  await pane(b).getByRole('button', { name: '打开草稿', exact: true }).first().click();
  await pane(b).getByRole('button', { name: '试听录音', exact: true }).waitFor();
  assert.equal((await get(b, abandoned)).status, 'interrupted'); assert.equal(await live(b), 0);
  assert((await get(b, abandoned)).samples >= prefix);
  await pane(b).getByRole('button', { name: '继续录音', exact: true }).click();
  await savedBeyond(b, abandoned, prefix);
  await dock(b).getByRole('button', { name: '结束录音', exact: true }).click(); await dock(b).waitFor({ state: 'hidden' });
  await pane(b).getByRole('button', { name: '试听录音', exact: true }).click(); await pane(b).locator('audio').waitFor();
  const wav = await b.evaluate(async () => {
    const buffer = await fetch(document.querySelector('.draft-current audio').src).then(r => r.arrayBuffer());
    return { bytes: buffer.byteLength, samples: new DataView(buffer).getUint32(40, true) / 2 };
  });
  assert.equal(wav.samples, (await get(b, abandoned)).samples); assert.equal(wav.bytes, 44 + wav.samples * 2);
  checks.push('closing owner releases browser lock; interrupted draft recovers committed prefix without auto-capture and can append/export a complete short WAV');

  // B keeps an ended UI snapshot. A writes uploading then closes/releases;
  // C later recovers unknown. B must validate current under its newly acquired
  // lease, not overwrite unknown using its still-ended local snapshot.
  await b.evaluate(async id => {
    const { createLocalRecorder } = await import('/src/composables/dictationDrafts.ts');
    const { PcmCapture } = await import('/src/composables/dictationCapture.ts');
    window.__staleRecorder = createLocalRecorder(new PcmCapture(), () => true);
    await window.__staleRecorder.select(id);
  }, abandoned);
  const writer = await windowPage();
  await writer.evaluate(async id => {
    const value = await window.__store.get(id);
    await window.__store.update(id, { submission: { status: 'uploading', samples: value.samples } });
  }, abandoned);
  await writer.close();
  const unconfirmed = await b.evaluate(async () => {
    const recorder = window.__staleRecorder;
    try { await recorder.freezeForSubmission(); return 'unexpected success'; } catch (e) { return e.message; }
  });
  assert.match(unconfirmed, /提交尚未确认/);
  assert.equal((await get(b, abandoned)).submission.status, 'uploading');
  checks.push('newly acquired submission lease rejects persisted uploading even when local snapshot was ended; status is not overwritten');
  const observer = await windowPage();
  await pane(observer).getByRole('button', { name: '打开草稿', exact: true }).first().click();
  await pane(observer).getByRole('button', { name: '试听录音', exact: true }).waitFor();
  assert.equal((await get(observer, abandoned)).submission.status, 'unknown');
  await pane(b).getByRole('button', { name: '开始转写', exact: true }).click();
  await pane(b).getByRole('alert').filter({ hasText: '上次提交结果不明确' }).waitFor();
  assert.equal(uploads, 0); assert.equal((await get(b, abandoned)).submission.status, 'unknown');
  assert(await pane(b).getByRole('button', { name: '开始转写', exact: true }).isDisabled());
  await observer.close();
  await pane(b).getByRole('button', { name: '检查服务器历史', exact: true }).click();
  await b.getByRole('region', { name: '听写历史', exact: true }).waitFor();
  await b.getByRole('button', { name: '录音转写', exact: true }).click();
  await pane(b).getByRole('button', { name: '已检查历史，允许手动重试', exact: true }).click();
  checks.push('B ended snapshot → A uploading/release → C unknown recovery → B submit is rejected with zero uploads; UI refreshes unknown and requires explicit history confirmation');

  // Frozen submission also owns the draft until the upload outcome is persisted.
  await pane(b).getByRole('button', { name: '开始转写', exact: true }).click(); await uploading;
  const c = await windowPage(); await pane(c).getByRole('button', { name: '打开草稿', exact: true }).first().click();
  await pane(c).getByRole('button', { name: '重新检查草稿', exact: true }).waitFor();
  assert.equal((await get(c, abandoned)).submission.status, 'uploading');
  releaseUpload();
  await pane(b).getByText('上传失败，录音保留。可下载，或手动重新提交。', { exact: true }).waitFor();
  await pane(c).getByRole('button', { name: '重新检查草稿', exact: true }).click();
  assert.equal((await get(c, abandoned)).submission.status, 'failed');
  assert((await get(c, abandoned)).samples >= prefix); await c.close();
  checks.push('opening another window cannot turn a live upload into unknown; failed upload preserves PCM and releases ownership');

  holdUpload = false;
  await b.getByRole('button', { name: '导入音频', exact: true }).click();
  await b.getByLabel('选择录音文件').setInputFiles({ name: 'navigation.wav', mimeType: 'audio/wav', buffer: Buffer.from('synthetic navigation input') });
  await dock(b).getByRole('button', { name: '正文', exact: true }).waitFor();
  await b.getByRole('region', { name: '已完成正文', exact: true }).waitFor();
  for (const tab of ['导入音频', '历史结果']) {
    await b.getByRole('button', { name: tab, exact: true }).click();
    await b.locator('.dictation-tabs button[aria-current="page"]').filter({ hasText: tab }).waitFor();
    await dock(b).getByRole('button', { name: '正文', exact: true }).click();
    await b.getByRole('region', { name: '已完成正文', exact: true }).waitFor();
    assert.equal(new URL(b.url()).searchParams.get('mode'), 'live');
  }
  await dock(b).getByRole('button', { name: '结束听写', exact: true }).click(); await dock(b).waitFor({ state: 'hidden' });
  checks.push('live → upload/history → dock text link reliably returns to body, including repeated navigation to the same target');
  assert.equal(uploads, 2); assert.deepEqual(errors, []); outcome = 'passed';
} finally {
  releaseUpload?.(); await browser?.close(); server.kill('SIGTERM');
  await new Promise(resolve => server.exitCode !== null ? resolve() : server.once('exit', resolve)); await log.close();
  await writeFile(`${output}/report.json`, JSON.stringify({ outcome, checks, errors, real_indexeddb: true, real_web_locks: true,
    real_capture: true, physical_microphone_tested: false, synthetic_http: true, inference_calls: 0 }, null, 2));
  console.log(outcome, checks.join('\n'));
}
