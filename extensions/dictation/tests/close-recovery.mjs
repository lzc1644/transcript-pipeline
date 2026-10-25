// Real Chrome worker termination at the persisted-intent / unsent-command boundary.
// Uses the production build and original webpage capture, with synthetic HTTP/WS and a virtual mic.
import assert from 'node:assert/strict';
import { spawn, execFileSync } from 'node:child_process';
import { mkdir, open, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from '../../../frontend/node_modules/playwright/index.mjs';

const root = resolve(import.meta.dirname, '../../..');
const output = resolve(root, process.env.DICTATION_TEST_OUTPUT || `tmp/browser-dictation-extension/close-recovery-${Date.now()}`);
const base = `http://127.0.0.1:${process.env.DICTATION_TEST_PORT || '8035'}`;
await mkdir(output, { recursive: false });
const log = await open(`${output}/server.log`, 'w');
const server = spawn(`${root}/.venv/bin/python`, ['extensions/dictation/tests/serve_fixture.py', '--root', `${output}/fixture`, '--port', new URL(base).port],
  { cwd: root, stdio: ['ignore', log.fd, log.fd] });
const report = { outcome: 'failed', synthetic_http: true, virtual_microphone: true, automatic_test_permission: true,
  native_permission_evidence: false, asr_or_paid_calls: 0, cases: [], errors: [] };
let context;
const delay = ms => new Promise(r => setTimeout(r, ms));
async function until(read, condition, description) {
  let value;
  for (let i = 0; i < 120; i++) { value = await read(); if (condition(value)) return value; await delay(50); }
  throw Error(`Timed out: ${description}: ${JSON.stringify(value)}`);
}
try {
  await until(async () => { try { return (await fetch(base + '/api/health')).ok; } catch { return false; } }, Boolean, 'fixture health');
  const extension = resolve(root, 'extensions/dictation/dist/chrome');
  context = await chromium.launchPersistentContext(process.env.DICTATION_TEST_PROFILE || `${output}/profile`, {
    headless: false, executablePath: process.env.CHROME_TEST_EXECUTABLE, ignoreDefaultArgs: ['--disable-extensions'],
    args: ['--disable-extensions-except=' + extension, '--load-extension=' + extension,
      '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'],
  });
  await context.addInitScript(() => {
    if (!navigator.mediaDevices?.getUserMedia) return;
    window.__closeTracks = []; window.__closeGum = 0;
    const gum = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async constraints => {
      window.__closeGum++;
      const stream = await gum(constraints); window.__closeTracks.push(...stream.getTracks()); return stream;
    };
  });
  let worker = context.serviceWorkers()[0] ?? await context.waitForEvent('serviceworker');
  const id = new URL(worker.url()).hostname;
  report.initialWorker = worker.url();
  // Chrome can initially load a command-line unpacked package while rejecting its
  // reload until Developer mode is enabled. Use the ordinary browser UI, not a flag bypass.
  const management = await context.newPage(); await management.goto('chrome://extensions');
  const devMode = management.locator('#devMode');
  if (!await devMode.evaluate(element => element.checked)) await devMode.click();
  assert(await devMode.evaluate(element => element.checked)); await management.close();
  report.developerMode = 'enabled through chrome://extensions UI in the isolated test profile';
  // Rebuilding an unpacked extension does not invalidate a cached worker. Use the standard reload.
  const reloaded = context.waitForEvent('serviceworker'); await worker.evaluate(() => setTimeout(() => chrome.runtime.reload(), 0));
  void reloaded.catch(() => {});
  await delay(200);
  report.browser = context.browser().version();
  const options = await context.newPage(); await options.goto(`chrome-extension://${id}/options.html`);
  // Reloaded workers may stay stopped until the options page's UI_STATE wakes them.
  worker = await reloaded;
  const ui = type => options.evaluate(type => chrome.runtime.sendMessage({ channel: 'transcript-dictation', protocolVersion: 1,
    requestId: crypto.randomUUID(), type }), type);
  const stored = () => options.evaluate(async () => (await chrome.storage.session.get('operation')).operation);
  await options.locator('#origin').fill(base);
  const needsHostGrant = !await options.evaluate(() => chrome.permissions.contains({ origins: ['http://127.0.0.1/*'] }));
  const opened = context.waitForEvent('page'); await options.locator('#connect').click();
  // A fresh profile displays a native optional-host permission prompt; explicitly allow it.
  if (needsHostGrant && process.env.DICTATION_TEST_XDOTOOL) {
    await delay(500);
    execFileSync(process.env.DICTATION_TEST_XDOTOOL, ['key', 'Tab', 'Return']);
  }
  report.optionalHostPermission = needsHostGrant ? 'allowed through the native browser prompt' : 'reused isolated profile grant';
  const recorder = await opened; await recorder.waitForURL('**/dictation/extension');
  recorder.on('pageerror', e => report.errors.push(e.message));
  await recorder.getByRole('button', { name: '启用扩展控制并试音' }).click();
  await recorder.getByRole('button', { name: '停止扩展控制' }).waitFor();
  const client = await context.newCDPSession(options), versions = [];
  client.on('ServiceWorker.workerVersionUpdated', e => versions.push(...e.versions));
  await client.send('ServiceWorker.enable');

  for (const kind of ['stop', 'fault', 'cancel']) {
    // Intercept only outgoing close delivery, AFTER production code has saved the intent.
    // The wrapper disappears with the worker. No persisted state or source is edited by this test.
    await worker.evaluate(kind => {
      const original = chrome.tabs.sendMessage.bind(chrome.tabs);
      let corrupt = kind === 'fault'; globalThis.__blockedClose = [];
      chrome.tabs.sendMessage = function (...args) {
        const body = args[1], callback = typeof args.at(-1) === 'function' ? args.pop() : null;
        if (['STOP', 'CANCEL'].includes(body?.type)) {
          globalThis.__blockedClose.push(body);
          return callback ? undefined : new Promise(() => {});
        }
        const lose = corrupt && body?.type === 'START'; if (lose) corrupt = false;
        const change = reply => lose ? { ...reply, requestId: 'injected-lost-start-receipt' } : reply;
        if (callback) return original(...args, reply => lose ? setTimeout(() => callback(change(reply)), 700) : callback(reply));
        return original(...args).then(reply => lose ? new Promise(r => setTimeout(() => r(change(reply)), 700)) : reply);
      };
    }, kind);
    // Do not wait on the deliberately blocked request. Observe durable state independently.
    const fire = type => options.evaluate(type => {
      void chrome.runtime.sendMessage({ channel: 'transcript-dictation', protocolVersion: 1,
        requestId: crypto.randomUUID(), type }).catch(() => {});
    }, type);
    await fire('TOGGLE');
    await until(() => recorder.evaluate(() => window.__closeTracks.some(t => t.readyState === 'live')), Boolean, 'actual live track');
    if (kind !== 'fault') {
      await until(stored, op => op?.phase === 'recording', 'recording state'); await delay(250);
      await fire(kind === 'cancel' ? 'CANCEL' : 'TOGGLE');
    }
    const checkpoint = await until(stored, op => op?.closeIntent?.kind === kind && op.closeIntent.pagePending, 'persisted unsent close intent');
    const blocked = await worker.evaluate(() => globalThis.__blockedClose);
    assert(blocked.length); assert.equal(blocked[0].operationId, checkpoint.operationId);
    const live = await recorder.evaluate(() => ({ tracks: window.__closeTracks.map(t => t.readyState), gum: window.__closeGum }));
    assert(live.tracks.includes('live'), 'no close has reached the real microphone yet');
    const sessionsBefore = await fetch(base + '/api/dictation/sessions').then(r => r.json());
    const currentCase = { kind, checkpoint, blocked, live }; report.cases.push(currentCase);
    await options.reload(); await options.getByText('关闭尚待确认', { exact: false }).waitFor();
    await options.screenshot({ path: `${output}/${kind}-pending.png`, fullPage: true });

    await until(async () => versions.findLast(v => v.scriptURL.includes(id) && v.runningStatus === 'running'), Boolean, 'live worker version');
    const version = versions.findLast(v => v.scriptURL.includes(id) && v.runningStatus === 'running');
    await client.send('ServiceWorker.stopWorker', { versionId: version.versionId });
    currentCase.terminatedWorker = version.versionId;
    currentCase.stopped = await until(async () => versions.findLast(v => v.versionId === version.versionId),
      v => v?.runningStatus === 'stopped', 'CDP confirms worker stopped');
    const recovered = await ui('UI_STATE');
    currentCase.recovered = recovered;
    // Playwright need not emit a new Worker wrapper when Chrome restarts the same
    // registered version. Assert the real stopped state and a fresh JS global instead.
    worker = context.serviceWorkers().find(w => w.url() === report.initialWorker);
    assert(worker); assert.equal(await worker.evaluate(() => globalThis.__blockedClose), undefined);
    await until(() => recorder.evaluate(() => window.__closeTracks.every(t => t.readyState === 'ended')), Boolean, 'all tracks ended after reconstruction');
    const backend = await until(() => fetch(base + '/api/dictation/sessions/' + checkpoint.sessionId).then(r => r.json()),
      state => state.status === (kind === 'cancel' ? 'cancelled' : 'completed'), 'original backend session closed');
    const ended = await recorder.evaluate(() => ({ tracks: window.__closeTracks.map(t => t.readyState), gum: window.__closeGum }));
    assert.equal(ended.gum, live.gum, 'recovery must not reopen the microphone');
    assert.equal((await fetch(base + '/api/dictation/sessions').then(r => r.json())).length, sessionsBefore.length);
    assert(backend.samples > 0);
    if (kind === 'cancel') assert.equal(recovered.operation, null);
    else {
      assert.equal(recovered.operation.operationId, checkpoint.operationId);
      assert.equal(recovered.operation.sessionId, checkpoint.sessionId);
      assert.equal(recovered.operation.closeIntent.pagePending, false);
      if (kind === 'fault') assert.equal(recovered.operation.phase, 'failed');
    }
    Object.assign(currentCase, { recovered, ended, backendStatus: backend.status, samples: backend.samples });
    await options.reload();
    await ui('DISCARD'); assert.equal((await ui('UI_STATE')).operation, null);
  }
  await options.screenshot({ path: `${output}/after-recovery.png`, fullPage: true });
  assert.deepEqual(report.errors, []); report.outcome = 'passed';
} catch (error) {
  report.failure = error.stack; throw error;
} finally {
  await writeFile(`${output}/report.json`, JSON.stringify(report, null, 2));
  await context?.close(); server.kill('SIGTERM'); if (server.exitCode === null) await new Promise(r => server.once('exit', r));
  await log.close(); console.log(output, report.outcome);
}
