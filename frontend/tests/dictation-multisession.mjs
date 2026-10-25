// UI states only: synthetic HTTP, real browser capture setup, no ASR/paid gateway.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir, writeFile, open } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
const root = resolve(import.meta.dirname, '../..');
const output = resolve(root, process.env.DICTATION_TEST_OUTPUT || 'tmp/dictation-multisession-browser');
await mkdir(output, { recursive: false });
const log = await open(`${output}/vite.log`, 'w');
const base = 'http://127.0.0.1:5176';
const server = spawn(process.execPath, [`${root}/frontend/node_modules/vite/bin/vite.js`, '--host', '127.0.0.1', '--port', '5176', '--strictPort'],
  { cwd: `${root}/frontend`, stdio: ['ignore', log.fd, log.fd] });
const id = 'e'.repeat(32);
let state = { id, status: 'listening', phase: '正在听音', revision: 1, event_sequence: 1, next_sequence: 0, samples: 16000,
  created_at: new Date().toISOString(), mode: 'live', config: { terms: [], cleanup: false, cleanup_model: 'gpt-6-luna' },
  draft_text: '', utterances: [{ id: '1', revision: 1, status: 'finalizing', asr_queue_state: 'queued',
    raw_text: '', text: '', preview: 'Alice 的预算是1500元', edited: false, needs_review: false }] };
let browser, retries = 0, uploads = 0;
let busyMessage = '当前语音识别容量已满，请稍后重试';
const checks = [], errors = [];
try {
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(base)).ok) break; } catch {}
    await new Promise(r => setTimeout(r, 100));
  }
  browser = await chromium.launch({ headless: true, args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'] });
  const context = await browser.newContext({ permissions: ['microphone'], viewport: { width: 1280, height: 900 } });
  await context.route('**/api/dictation/**', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.pathname.endsWith('/config')) return route.fulfill({ json: { max_active_sessions: 2, active_sessions: 2,
      max_upload_mb: 512, max_upload_minutes: 240, cleanup_model: 'gpt-6-luna', document_cleanup_model: 'gpt-6-luna' } });
    if (url.pathname.endsWith('/sessions') && request.method() === 'POST') return route.fulfill({ status: 409, json: { detail: busyMessage } });
    if (url.pathname.endsWith('/sessions')) return route.fulfill({ json: [state] });
    if (url.pathname.endsWith('/uploads')) { uploads++; return route.fulfill({ json: state }); }
    if (url.pathname.endsWith('/resume')) {
      retries++;
      state = { ...state, status: 'preparing', phase: '准备 CPU 预览和 GPU 最终识别模型', revision: state.revision + 1 };
    }
    return route.fulfill({ json: state });
  });
  const page = await context.newPage();
  page.on('pageerror', e => errors.push(e.message));
  await page.goto(`${base}/dictation?session=${id}`);
  await page.getByText('正在等待共享识别', { exact: true }).waitFor();
  await page.getByText('识别容量：最多 2 个活动会话', { exact: false }).waitFor();
  await page.screenshot({ path: `${output}/queued-desktop.png`, fullPage: true });
  state.utterances[0].asr_queue_state = 'running'; state.revision++;
  await page.getByText('正在最终识别', { exact: true }).waitFor();
  checks.push('queued and running are distinct; capacity is visible');
  state = { ...state, mode: 'upload', status: 'waiting', phase: 'GPU 正被批量转录占用，请等待完成；文件已保留，请重试开始转写', utterances: [], revision: state.revision + 1 };
  await page.goto(`${base}/dictation?session=${id}`);
  await page.getByRole('button', { name: '重试开始转写', exact: true }).waitFor();
  await page.setViewportSize({ width: 390, height: 844 });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
  await page.screenshot({ path: `${output}/waiting-mobile.png`, fullPage: true });
  await page.getByRole('button', { name: '重试开始转写', exact: true }).click();
  await page.getByText('模型准备期间请稍候', { exact: false }).waitFor();
  assert.equal(retries, 1); assert.equal(uploads, 0);
  checks.push('waiting upload has an explicit retry, no duplicate upload, 390px controls fit');
  state = { ...state, status: 'waiting', phase: '当前语音识别容量已满，请稍后重试；文件已保留，请重试开始转写', revision: state.revision + 1 };
  await page.goto(`${base}/dictation?mode=upload`);
  await page.locator('input[type=file]').setInputFiles({ name: 'clip.wav', mimeType: 'audio/wav', buffer: Buffer.alloc(60) });
  const dock = page.getByRole('region', { name: '活动识别控制', exact: true });
  await dock.getByRole('button', { name: '重试开始转写', exact: true }).waitFor();
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
  await page.screenshot({ path: `${output}/waiting-dock-mobile.png`, fullPage: true });
  await dock.getByRole('button', { name: '重试开始转写', exact: true }).click();
  await dock.getByText('准备 CPU 预览和 GPU 最终识别模型', { exact: true }).waitFor();
  assert.equal(retries, 2); assert.equal(uploads, 1);
  checks.push('owned upload dock also retries saved material without reuploading, including mobile');
  // A fresh view has no owned activity; reject creation only after browser media preparation.
  state = { ...state, status: 'completed', phase: '已完成', revision: state.revision + 1 };
  for (const message of ['当前语音识别容量已满，请稍后重试', 'GPU 正被批量转录占用，请等待完成']) {
    busyMessage = message;
    await page.goto(`${base}/dictation`);
    await page.getByRole('button', { name: '开始听写', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: message }).waitFor();
  }
  checks.push('capacity-full and batch-GPU-busy creation errors are distinct');
  assert.deepEqual(errors, []);
  await writeFile(`${output}/report.json`, JSON.stringify({ checks, errors, synthetic_http: true, real_asr_or_llm: false }, null, 2));
  console.log(checks.join('\n'));
} finally {
  await browser?.close();
  server.kill('SIGTERM');
  await new Promise(r => server.exitCode !== null ? r() : server.once('exit', r));
  await log.close();
}
