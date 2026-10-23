// Full 4h DATA-SIZE boundary, not four wall-clock hours of capture or semantic ASR.
// Actual IndexedDB/Blob/playback/download + local HTTP byte sink. No models/paid calls.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { mkdir, open, stat, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
const root = resolve(import.meta.dirname, '../..'), base = 'http://127.0.0.1:5177';
const output = resolve(root, process.env.DICTATION_TEST_OUTPUT || 'tmp/dictation-optimization/capacity-4h');
await mkdir(output, { recursive: false });
const log = await open(`${output}/vite.log`, 'w');
const server = spawn(process.execPath, [`${root}/frontend/node_modules/vite/bin/vite.js`, '--host', '127.0.0.1', '--port', '5177', '--strictPort'],
  { cwd: `${root}/frontend`, stdio: ['ignore', log.fd, log.fd] });
const expectedBytes = 4 * 3600 * 16000 * 2 + 44;
const checks = [], errors = [], heap = [];
let received = 0, header = Buffer.alloc(0), tail = Buffer.alloc(0), declaredLength = 0;
const sink = createServer((request, response) => {
  response.setHeader('Access-Control-Allow-Origin', base);
  response.setHeader('Access-Control-Allow-Headers', 'content-type');
  if (request.method === 'OPTIONS') { response.writeHead(204).end(); return; }
  declaredLength = Number(request.headers['content-length']);
  request.on('data', chunk => {
    if (header.length < 44) header = Buffer.concat([header, chunk.subarray(0, 44 - header.length)]);
    tail = Buffer.concat([tail, chunk]).subarray(-8);
    received += chunk.length;
  });
  request.on('end', () => response.end(JSON.stringify({ received })));
});
await new Promise(resolve => sink.listen(0, '127.0.0.1', resolve));
let browser, monitor, outcome = 'failed', diagnostics, fullUploadPassed = false;
try {
  for (let i = 0; i < 100; i++) {
    if (server.exitCode !== null) throw new Error('isolated Vite exited');
    try { if ((await fetch(base)).ok) break; } catch {}
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  browser = await chromium.launch({ headless: true, args: ['--autoplay-policy=no-user-gesture-required'] });
  const context = await browser.newContext({ acceptDownloads: true });
  await context.route('**/api/dictation/**', route => route.fulfill({ json: route.request().url().endsWith('/config')
    ? { max_upload_mb: 512, max_upload_minutes: 240, document_cleanup_timeout_seconds: 3600 } : [] }));
  const page = await context.newPage(); page.setDefaultTimeout(60000);
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(`${base}/dictation?mode=record`);
  const cdp = await context.newCDPSession(page);
  monitor = setInterval(() => { void cdp.send('Runtime.getHeapUsage').then(value => heap.push(value)).catch(() => {}); }, 500);
  const seeded = await page.evaluate(async () => {
    const { DraftStore } = await import('/src/composables/dictationDrafts.ts');
    const store = window.__capacityStore = new DraftStore(), id = 'capacity-4h';
    const seconds = 4 * 3600, samples = seconds * 16000, now = new Date().toISOString();
    const estimate = await navigator.storage.estimate();
    if (estimate.quota - estimate.usage < samples * 2 * 2) throw new Error('Insufficient test-origin quota for full 4h fixture + Blob');
    await store.put({ id, createdAt: now, updatedAt: now, sampleRate: 16000, channels: 1, format: 'pcm16le', status: 'ended', samples });
    const db = await new Promise((resolve, reject) => { const r = indexedDB.open('transcript-dictation-drafts', 1); r.onsuccess = () => resolve(r.result); r.onerror = () => reject(r.error); });
    // Seed realistic one-second chunks in bounded transactions, not a fake large metadata row.
    for (let first = 0; first < seconds; first += 128) {
      await new Promise((resolve, reject) => {
        const tx = db.transaction('chunks', 'readwrite'); tx.oncomplete = resolve; tx.onabort = tx.onerror = () => reject(tx.error);
        for (let i = first; i < Math.min(first + 128, seconds); i++) {
          const pcm = new ArrayBuffer(32000), view = new DataView(pcm);
          view.setUint32(0, i, true); view.setUint32(31996, i, true);
          tx.objectStore('chunks').add({ id, start: i * 16000, pcm });
        }
      });
    }
    db.close();
    window.__nativeBlob = Blob; window.__maxRawBytes = 0; window.__batchBlobs = 0;
    window.Blob = class extends window.__nativeBlob {
      constructor(parts = [], options) {
        const raw = parts.reduce((n, p) => n + (p instanceof ArrayBuffer || ArrayBuffer.isView(p) ? p.byteLength : 0), 0);
        window.__maxRawBytes = Math.max(window.__maxRawBytes, raw);
        if (raw > 44) {
          window.__batchBlobs++;
          if (window.__failBlobAt === window.__batchBlobs) throw new DOMException('simulated Blob resource exhaustion', 'QuotaExceededError');
        }
        super(parts, options);
      }
    };
    return { id, samples, seconds, estimate };
  });
  assert.equal(seeded.samples, 230400000);
  checks.push('actual IndexedDB fixture: 14,400 one-second PCM chunks / 230,400,000 samples / 460,800,000 bytes');
  const failure = await page.evaluate(async () => {
    window.__failBlobAt = 2;
    let message = '';
    try { await window.__capacityStore.wav('capacity-4h', 512 * 1024 ** 2); } catch (error) { message = error.message; }
    window.__failBlobAt = 0;
    const draft = await window.__capacityStore.get('capacity-4h');
    return { message, samples: draft.samples, status: draft.status };
  });
  assert.match(failure.message, /草稿未删除/); assert.equal(failure.samples, seeded.samples); assert.equal(failure.status, 'ended');
  checks.push('second-batch Blob resource failure is controlled; full draft metadata/PCM remain retryable');
  const exported = await page.evaluate(async () => {
    const pending = window.__capacityStore.wav('capacity-4h', 512 * 1024 ** 2);
    let concurrentError = '';
    try { await window.__capacityStore.wav('capacity-4h', 512 * 1024 ** 2); } catch (error) { concurrentError = error.message; }
    const { blob, draft } = await pending;
    window.__capacityWav = blob;
    const markers = [];
    for (const second of [0, 131, 132, 7200, 14399]) {
      const bytes = await blob.slice(44 + second * 32000, 48 + second * 32000).arrayBuffer();
      markers.push(new DataView(bytes).getUint32(0, true));
    }
    return { bytes: blob.size, samples: draft.samples, markers, concurrentError, maxRawBytes: window.__maxRawBytes, batchBlobs: window.__batchBlobs };
  });
  assert.equal(exported.bytes, expectedBytes); assert.deepEqual(exported.markers, [0, 131, 132, 7200, 14399]);
  assert.match(exported.concurrentError, /正在组装WAV/);
  assert(exported.maxRawBytes <= 4 * 1024 ** 2 + 32000, 'export retained an unbounded raw-PCM batch');
  assert(exported.batchBlobs > 100);
  checks.push('complete 4h WAV built as opaque Blob parts; each raw-PCM batch <= 4MiB + one stored block, with cross-batch/tail sample checks');
  await page.evaluate(() => {
    const audio = document.createElement('audio'); audio.id = 'capacity-player'; audio.controls = true; audio.muted = true;
    audio.src = window.__capacityURL = URL.createObjectURL(window.__capacityWav); document.body.append(audio);
  });
  await page.waitForFunction(() => document.querySelector('#capacity-player').duration === 14400);
  await page.evaluate(async () => { const audio = document.querySelector('#capacity-player'); audio.currentTime = 14399; await audio.play(); });
  await page.waitForFunction(() => document.querySelector('#capacity-player').currentTime > 14399.1);
  await page.evaluate(() => document.querySelector('#capacity-player').pause());
  const download = page.waitForEvent('download');
  await page.evaluate(() => { const a = document.createElement('a'); a.href = window.__capacityURL; a.download = 'four-hour.wav'; a.click(); });
  await (await download).saveAs(`${output}/four-hour.wav`);
  assert.equal((await stat(`${output}/four-hour.wav`)).size, expectedBytes);
  checks.push('browser decoded 4h duration, played near EOF and downloaded exact 460,800,044-byte WAV');
  // Full upload is opt-in and separately recorded. The first full-size run lost
  // its browser during transfer; the cause is unconfirmed, not a passed upload.
  if (process.env.DICTATION_CAPACITY_FULL_UPLOAD === '1') {
    const uploaded = await page.evaluate(async url => fetch(url, { method: 'POST', headers: { 'Content-Type': 'audio/wav' }, body: window.__capacityWav }).then(r => r.json()),
      `http://127.0.0.1:${sink.address().port}/upload`);
    assert.equal(uploaded.received, expectedBytes); assert.equal(received, expectedBytes); assert.equal(declaredLength, expectedBytes);
    assert.equal(header.toString('ascii', 0, 4), 'RIFF'); assert.equal(header.readUInt32LE(40), expectedBytes - 44);
    assert.equal(tail.readUInt32LE(4), 14399); fullUploadPassed = true;
    checks.push('sent all 460,800,044 bytes to isolated HTTP sink');
  }
  assert.deepEqual(errors, []);
  diagnostics = { seeded, exported, received, declaredLength, heap_peak: Object.fromEntries(['usedSize', 'totalSize', 'backingStorageSize', 'embedderHeapUsedSize']
    .map(key => [key, Math.max(0, ...heap.map(item => item[key] || 0))])) };
  outcome = 'passed';
} catch (error) { errors.push(String(error.stack || error)); throw error; }
finally {
  if (monitor) clearInterval(monitor);
  await writeFile(`${output}/report.json`, JSON.stringify({ outcome, checks, errors, diagnostics, synthetic_4h_data: true,
    four_hour_wallclock_capture_tested: false, full_http_upload_passed: fullUploadPassed,
    real_asr_or_supplier_tested: false }, null, 2));
  await browser?.close();
  await new Promise(resolve => sink.close(resolve));
  server.kill('SIGTERM'); await new Promise(resolve => server.exitCode !== null ? resolve() : server.once('exit', resolve)); await log.close();
  console.log(outcome, checks.join('\n'));
}
