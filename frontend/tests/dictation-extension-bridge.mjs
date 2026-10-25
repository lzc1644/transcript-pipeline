// Synthetic ownership regression. Automatic test permission is NOT native-permission evidence.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir, open, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
const root = resolve(import.meta.dirname, '../..');
const output = resolve(root, process.env.DICTATION_TEST_OUTPUT || `tmp/browser-dictation-extension/bridge-${Date.now()}`);
const port = process.env.DICTATION_TEST_PORT || '8032', base = `http://127.0.0.1:${port}`;
await mkdir(output, { recursive: false }); const log = await open(`${output}/server.log`, 'w');
const server = spawn(`${root}/.venv/bin/python`, ['extensions/dictation/tests/serve_fixture.py', '--root', `${output}/fixture`, '--port', port],
  { cwd: root, stdio: ['ignore', log.fd, log.fd] });
let browser, page, outcome = 'failed'; const checks = [], errors = [], cancelled = []; let creates = 0;
try {
  for (let i = 0; i < 100; i++) { try { if ((await fetch(base + '/api/health')).ok) break; } catch {} await new Promise(r => setTimeout(r, 100)); }
  browser = await chromium.launch({ headless: true, args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'] });
  const context = await browser.newContext({ permissions: ['microphone'], viewport: { width: 1280, height: 1000 } });
  page = await context.newPage(); page.on('pageerror', e => errors.push(e.message));
  page.on('request', request => {
    if (request.method() === 'POST' && request.url().endsWith('/api/dictation/sessions')) creates++;
    if (request.method() === 'POST' && request.url().endsWith('/cancel')) cancelled.push(new URL(request.url()).pathname.split('/').at(-2));
  });
  await page.addInitScript(() => {
    window.__tracks = [];
    const get = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async constraints => {
      const stream = await get(constraints); window.__tracks.push(...stream.getTracks());
      if (window.__delayNext) { window.__delayNext = false; await new Promise(resolve => { window.__releasePermission = resolve; }); }
      return stream;
    };
  });
  await page.goto(base + '/dictation/extension');
  const send = (type, operationId, data) => page.evaluate(({ type, operationId, data }) => new Promise((resolve, reject) => {
    const body = { channel: 'transcript-dictation', protocolVersion: 1, type, requestId: crypto.randomUUID(), documentInstanceId: 'test-document', operationId, data };
    const timer = setTimeout(() => { window.removeEventListener('message', listen); reject(Error('bridge reply timed out')); }, 3000);
    function listen(event) { if (event.data?.direction === 'page' && event.data.body?.requestId === body.requestId) { clearTimeout(timer); window.removeEventListener('message', listen); resolve(event.data.body.data); } }
    window.addEventListener('message', listen); window.postMessage({ direction: 'extension', body }, location.origin);
  }), { type, operationId, data });
  assert.equal((await send('HELLO')).enabled, false);
  assert.match((await send('START', 'unarmed')).error, /启用/); assert.equal(creates, 0);
  await page.evaluate(() => { window.__delayNext = true; });
  await page.getByRole('button', { name: '启用扩展控制并试音' }).click();
  await page.waitForFunction(() => !!window.__releasePermission);
  await page.getByRole('button', { name: '取消回填并停止', exact: true }).click();
  await page.evaluate(() => window.__releasePermission());
  await page.waitForFunction(() => window.__tracks.every(t => t.readyState === 'ended'));
  assert.equal((await send('HELLO')).enabled, false); assert.equal(creates, 0);
  checks.push('cancelling the foreground probe invalidates late permission without enabling extension control');
  await page.getByRole('button', { name: '启用扩展控制并试音' }).click();
  await page.getByRole('button', { name: '停止扩展控制' }).waitFor();
  assert(await page.evaluate(() => window.__tracks.every(t => t.readyState === 'ended'))); assert.equal(creates, 0);
  checks.push('unarmed control rejects; explicit probe closes tracks without creating a server session');

  await page.evaluate(() => { window.__delayNext = true; }); await send('START', 'late-permission');
  await page.waitForFunction(() => !!window.__releasePermission); await send('CANCEL', 'late-permission');
  await page.waitForTimeout(20); await send('START', 'new-operation');
  await page.waitForFunction(() => document.querySelector('.extension-actions strong')?.textContent.includes('正在听写'));
  const sid = (await send('GET_OPERATION')).sessionId;
  for (const type of ['STOP', 'CANCEL']) {
    const oldId = `closed-before-start-${type}`;
    assert.equal((await send(type, oldId)).closedOperationId, oldId);
    assert.match((await send('START', oldId)).error, /已结束/);
    assert.equal((await send('GET_OPERATION')).sessionId, sid);
    assert(await page.evaluate(() => window.__tracks.at(-1).readyState === 'live'));
  }
  checks.push('close receipts tombstone an undelivered START; closing another operation cannot affect the current microphone');
  await page.evaluate(() => window.__releasePermission()); await page.waitForTimeout(200);
  assert.equal(creates, 1); assert.equal((await send('GET_OPERATION')).sessionId, sid);
  assert(await page.evaluate(() => window.__tracks.at(-1).readyState === 'live' && window.__tracks.slice(0, -1).every(t => t.readyState === 'ended')));
  await send('STOP', 'new-operation'); await page.waitForFunction(() => window.__tracks.every(t => t.readyState === 'ended'));
  checks.push('cancelled late permission loses its lease; next start owns exactly one live track/session');

  let releaseCreation, delayed = false;
  const gate = new Promise(r => { releaseCreation = r; });
  await page.route('**/api/dictation/sessions', async route => {
    if (!delayed && route.request().method() === 'POST') { delayed = true; await gate; }
    await route.continue();
  });
  await send('START', 'late-creation'); await page.waitForFunction(() => window.__tracks.at(-1).readyState === 'live');
  while (!delayed) await page.waitForTimeout(10);
  await send('CANCEL', 'late-creation'); await page.waitForTimeout(20);
  await send('START', 'replacement');
  await page.waitForFunction(() => document.querySelector('.extension-actions strong')?.textContent.includes('正在听写'));
  const current = (await send('GET_OPERATION')).sessionId; assert.notEqual(current, sid);
  releaseCreation();
  for (let i = 0; i < 100 && !cancelled.length; i++) await page.waitForTimeout(20);
  assert.equal(cancelled.length, 1); assert.notEqual(cancelled[0], current);
  assert.equal((await send('GET_OPERATION')).sessionId, current);
  assert(await page.evaluate(() => window.__tracks.at(-1).readyState === 'live'));
  await send('STOP', 'replacement'); await page.waitForFunction(() => window.__tracks.every(t => t.readyState === 'ended'));
  checks.push('late creation cancels only its returned session ID; cannot stop or replace the newer operation');

  await page.unroute('**/api/dictation/sessions');
  await page.route('**/api/dictation/sessions', route => route.fulfill({ status: 409, json: { detail: '模型正忙（协议夹具）' } }));
  await send('START', 'rejected-start');
  await page.getByRole('alert').filter({ hasText: '模型正忙' }).waitFor();
  const rejected = await send('GET_OPERATION'); assert.equal(rejected.phase, 'failed'); assert(!rejected.sessionId);
  assert(await page.evaluate(() => window.__tracks.every(t => t.readyState === 'ended')));
  checks.push('rejected start reports failed without inheriting an old session or a live microphone');

  await page.unroute('**/api/dictation/sessions');
  await send('START', 'retry-after-rejection');
  await page.waitForFunction(() => document.querySelector('.extension-actions strong')?.textContent.includes('正在听写'));
  assert.equal((await send('GET_OPERATION')).operationId, 'retry-after-rejection');
  await send('STOP', 'retry-after-rejection');
  await page.waitForFunction(() => window.__tracks.every(t => t.readyState === 'ended'));
  checks.push('an explicit new operation can retry a rejected start without inheriting its failed preparation');

  await page.screenshot({ path: `${output}/desktop.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 }); await page.screenshot({ path: `${output}/mobile.png`, fullPage: true });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  assert.equal(await page.getByRole('complementary', { name: '常驻语音控制' }).count(), 0);
  const routeTo = path => page.evaluate(path => { history.pushState({}, '', path); dispatchEvent(new PopStateEvent('popstate')); }, path);
  await routeTo('/dictation');
  await page.getByRole('button', { name: '开始听写', exact: true }).click();
  await page.waitForFunction(() => window.__tracks.at(-1).readyState === 'live');
  await page.getByRole('button', { name: '暂停听写', exact: true }).first().waitFor();
  await routeTo('/dictation/extension');
  await page.getByRole('region', { name: '扩展录音页' }).waitFor();
  await send('HELLO', undefined, { captureKind: 'sidebar' });
  await page.getByRole('button', { name: '启用服务端控制', exact: true }).click();
  await send('START', 'busy-normal-owner');
  for (let i = 0; i < 50 && (await send('GET_OPERATION')).phase !== 'failed'; i++) await page.waitForTimeout(20);
  const busy = await send('GET_OPERATION');
  assert.equal(busy.phase, 'failed'); assert(!busy.sessionId);
  assert(await page.evaluate(() => window.__tracks.at(-1).readyState === 'live'));
  await page.getByRole('region', { name: '扩展录音页' }).getByRole('button', { name: '结束', exact: true }).click();
  await page.waitForFunction(() => window.__tracks.every(t => t.readyState === 'ended'));
  checks.push('normal-page recording keeps its explicit session owner after SPA navigation; a busy-rejected extension start cannot hide its dedicated stop');
  assert.deepEqual(errors, []); outcome = 'passed';
} finally {
  if (outcome !== 'passed' && page) { await writeFile(`${output}/page.txt`, await page.locator('body').innerText().catch(() => 'page unavailable')); await page.screenshot({ path: `${output}/failure.png`, fullPage: true }).catch(() => {}); }
  await writeFile(`${output}/report.json`, JSON.stringify({ outcome, synthetic_http: true, automatic_test_permission: true,
    native_permission_evidence: false, asr_or_paid_calls: 0, checks, creates, cancelled, errors }, null, 2));
  await browser?.close(); server.kill('SIGTERM'); if (server.exitCode === null) await new Promise(r => server.once('exit', r));
  await log.close(); console.log(output, outcome, checks);
}
