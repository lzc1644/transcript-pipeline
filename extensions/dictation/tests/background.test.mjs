import test from 'node:test';
import assert from 'node:assert/strict';
import { build } from 'vite';
import { runInNewContext } from 'node:vm';
import { webcrypto } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { message } from '../../../shared/dictationExtensionProtocol.ts';

const bundle = await build({ configFile: false, logLevel: 'silent', build: { write: false, minify: false,
  lib: { entry: fileURLToPath(new URL('../src/background.ts', import.meta.url)), name: 'Background', formats: ['iife'] } } });
const code = (Array.isArray(bundle) ? bundle[0] : bundle).output[0].code;
const origin = 'https://192.168.0.252:8443';
const recorder = { tabId: 10, exactOrigin: origin, documentInstanceId: 'document-1' };
const event = () => { const listeners = []; return { addListener: fn => listeners.push(fn), emit: (...args) => listeners.forEach(fn => fn(...args)), listeners }; };
const pause = () => new Promise(resolve => setTimeout(resolve, 10));

function harness(options = {}) {
  const session = structuredClone(options.session ?? { recorder });
  const local = { serviceOrigin: origin }, calls = [], writes = [];
  const onMessage = event(), onCommand = event(), onRemoved = event(), permissionsRemoved = event();
  let permitted = true, pendingWrites = 0;
  const api = {
    runtime: { id: 'test-extension', onMessage, getURL: path => `moz-extension://test-extension/${path}`, getManifest: () => ({ version: '0.1.0', ...(options.sidebar ? { sidebar_action: { default_panel: 'sidebar.html' } } : {}) }),
      sendMessage: async body => { calls.push(body); return options.captureRPC ? options.captureRPC(body) : { ok: true }; } },
    storage: { local: { get: async () => structuredClone(local), set: async x => Object.assign(local, x), remove: async k => delete local[k] },
      session: { get: async () => structuredClone(session), set: async x => {
        assert.equal(pendingWrites++, 0, 'storage mutations overlapped'); await Promise.resolve();
        Object.assign(session, structuredClone(x)); writes.push(structuredClone(x)); pendingWrites--;
      } } },
    permissions: { contains: async () => permitted, remove: async () => true, onRemoved: permissionsRemoved },
    scripting: { registerContentScripts: async () => {}, unregisterContentScripts: async () => {} },
    tabs: { onRemoved, get: async () => ({ id: 10, url: origin + '/dictation/extension' }), update: async () => {},
      create: async () => { calls.push({ type: 'CREATE_TAB' }); return { id: 11 }; },
      sendMessage: async (tabId, body) => {
        calls.push(body);
        if (options.rpc) { const custom = await options.rpc(body); if (custom !== undefined) return custom; }
        if (body.type === 'HELLO') return { requestId: body.requestId, protocolVersion: 1, documentInstanceId: 'document-1', data: { enabled: true } };
        return { ...body, data: body.type === 'GET_OPERATION' ? { operationId: body.operationId, sessionId: body.sessionId, phase: 'recording' }
          : ['STOP', 'CANCEL'].includes(body.type) ? { closedOperationId: body.operationId } : {} };
      } },
    windows: { get: async id => ({ id, focused: true }), getLastFocused: async () => ({ id: 1, focused: true }), onRemoved: event() },
    commands: { onCommand, getAll: async () => [] }, action: { setBadgeText: async () => {}, setTitle: async () => {} },
  };
  runInNewContext(code, { browser: api, chrome: api, crypto: webcrypto, URL, navigator: { userAgent: 'Firefox/154.0' }, setTimeout, clearTimeout });
  const ui = { id: api.runtime.id, url: api.runtime.getURL('options.html'), tab: { id: 20 } };
  const sender = { id: api.runtime.id, tab: { id: 10 }, frameId: 0, url: origin + '/dictation/extension' };
  const send = (body, from = ui) => onMessage.listeners[0](body, from);
  const sidebarSender = { id: api.runtime.id, url: api.runtime.getURL('sidebar.html') };
  return { send, ui, sender, sidebarSender, calls, session, writes, onCommand, onRemoved, permissionsRemoved, revoke: () => { permitted = false; permissionsRemoved.emit({}); } };
}
test('start intent is persisted before RPC; cancel can overtake a blocked start', async () => {
  let release, entered;
  const starting = new Promise(r => { entered = r; });
  const h = harness({ rpc: async body => {
    if (body.type === 'START') { entered(); return new Promise(r => { release = () => r({ ...body, data: {} }); }); }
  } });
  const request = h.send(message('TOGGLE')); await starting;
  assert.equal(h.session.operation.phase, 'preparing');
  await h.send(message('CANCEL'));
  assert.equal(h.session.operation, null); assert.equal(h.calls.filter(x => x.type === 'CANCEL').length, 1);
  release(); await request;
  assert.equal(h.session.operation, null); assert.equal(h.calls.filter(x => x.type === 'START').length, 1);
});
test('a second toggle stops the same operation, and late state cannot undo stop', async () => {
  const h = harness(); await h.send(message('TOGGLE')); const op = h.session.operation;
  await h.send(message('TOGGLE'));
  await h.send(message('STATE', { operationId: op.operationId, documentInstanceId: 'document-1', sessionId: 'session-1', data: { phase: 'recording' } }), h.sender);
  assert.equal(h.session.operation.phase, 'draining'); assert.equal(h.calls.filter(x => x.type === 'START').length, 1);
  assert.equal(h.calls.find(x => x.type === 'STOP').operationId, op.operationId);
  h.onCommand.emit('insert-pending'); await pause(); assert.equal(h.calls.filter(x => x.type === 'START').length, 1);
});
test('wrong sender, origin, port, frame, operation, document, session and message type are rejected', async () => {
  const h = harness(); await h.send(message('TOGGLE')); const op = h.session.operation;
  const valid = message('STATE', { operationId: op.operationId, documentInstanceId: 'document-1', sessionId: 'session-1', data: { phase: 'recording' } });
  await h.send(valid, h.sender);
  for (const sender of [{ ...h.sender, id: 'other' }, { ...h.sender, frameId: 1 }, { ...h.sender, tab: { id: 11 } },
    { ...h.sender, url: 'https://192.168.0.252:8444/dictation/extension' }, { ...h.sender, tab: { id: 10, incognito: true } }]) {
    const response = h.send(valid, sender); if (response !== undefined) await assert.rejects(response);
  }
  for (const changed of [{ operationId: 'wrong' }, { documentInstanceId: 'wrong' }, { sessionId: 'wrong' }, { type: 'START' }])
    await assert.rejects(h.send({ ...valid, ...changed }, h.sender));
  await assert.rejects(h.send(message('START'), h.ui));
  assert.equal(h.session.operation.sessionId, 'session-1');
});
test('worker reconstruction queries the bound page without a second START', async () => {
  const h = harness(); await h.send(message('TOGGLE'));
  const before = h.session.operation.operationId;
  const recovered = harness({ session: h.session }); await recovered.send(message('UI_STATE'));
  assert.deepEqual(recovered.calls.map(x => x.type), ['GET_OPERATION']);
  assert.equal(recovered.session.operation.operationId, before); assert.equal(recovered.session.operation.phase, 'recording');
});
test('mismatched replies and lost recorder become explicit failures, not automatic restarts', async () => {
  const h = harness({ rpc: body => body.type === 'START' ? { ...body, requestId: 'other-request' } : undefined });
  await h.send(message('TOGGLE')); assert.equal(h.session.operation.phase, 'failed');
  const h2 = harness({ session: h.session, rpc: () => { throw new Error('closed'); } });
  await h2.send(message('UI_STATE')); assert.equal(h2.session.operation.phase, 'failed');
  assert(!h2.calls.some(x => ['START', 'CREATE_TAB'].includes(x.type)));
});
test('closing recorder and withdrawing permission stop new operations without claiming the mic stopped', async () => {
  const h = harness(); await h.send(message('TOGGLE')); h.onRemoved.emit(10); await pause();
  assert.match(h.session.operation.error, /已关闭/); assert.equal(h.session.operation.phase, 'failed');
  const h2 = harness(); await h2.send(message('TOGGLE')); h2.revoke(); await pause();
  assert.match(h2.session.operation.error, /独立停止麦克风/);
  await h2.send(message('CANCEL')); await h2.send(message('TOGGLE'));
  assert.equal(h2.calls.filter(x => x.type === 'START').length, 1);
});

async function armedSidebar(options = {}) {
  const h = harness({ ...options, sidebar: true });
  await h.send(message('SIDEBAR_HELLO', { documentInstanceId: 'sidebar-1', data: { windowId: 1, enabled: false } }), h.sidebarSender);
  await h.send(message('SIDEBAR_STATE', { documentInstanceId: 'sidebar-1', data: { windowId: 1, enabled: true } }), h.sidebarSender);
  await h.send(message('TOGGLE')); const op = h.session.operation;
  const capture = data => h.send(message('CAPTURE_REQUEST', { operationId: op.operationId, documentInstanceId: 'document-1', data }), h.sender);
  await capture({ action: 'PREPARE', leaseId: 'lease-1' });
  return { ...h, op, capture };
}
test('sidebar capture requires current-window readiness and exact page/document/lease sender binding', async () => {
  const unarmed = harness({ sidebar: true }); await assert.rejects(unarmed.send(message('TOGGLE')), /侧栏/);
  const h = await armedSidebar();
  assert.equal(h.session.operation.capture.documentInstanceId, 'sidebar-1');
  const frame = message('AUDIO_FRAME', { operationId: h.op.operationId, documentInstanceId: 'sidebar-1', data: { leaseId: 'lease-1', sequence: 0, pcm: [1, -2], level: .1 } });
  await h.send(frame, h.sidebarSender);
  const routed = h.calls.find(x => x.type === 'AUDIO_FRAME'); assert.equal(routed.documentInstanceId, 'document-1');
  for (const from of [h.sender, h.ui, { ...h.sidebarSender, frameId: 1 }, { ...h.sidebarSender, url: 'moz-extension://test-extension/sidebar.html?fake' }])
    await assert.rejects(h.send(frame, from));
  for (const changed of [{ documentInstanceId: 'sidebar-old' }, { operationId: 'op-old' }, { data: { ...frame.data, leaseId: 'other' } }])
    await assert.rejects(h.send({ ...frame, ...changed }, h.sidebarSender));
  await assert.rejects(h.capture({ action: 'START', leaseId: 'other' }), /lease/);
  assert(!JSON.stringify(h.session).includes('pcm')); // Audio is never persisted.
});
test('sidebar cancel/recorder close/revoke stop hardware even without a service reply', async () => {
  for (const action of ['cancel', 'close', 'revoke']) {
    const h = await armedSidebar();
    if (action === 'cancel') await h.send(message('CANCEL'));
    if (action === 'close') { h.onRemoved.emit(10); await pause(); }
    if (action === 'revoke') { h.revoke(); await pause(); }
    const stop = h.calls.find(x => x.type === 'CAPTURE_REQUEST' && x.data.action === 'STOP');
    assert(stop); assert.equal(stop.documentInstanceId, 'sidebar-1'); assert.equal(stop.data.flush, false);
    if (action !== 'cancel') assert.equal(h.session.operation.phase, 'failed');
  }
});
test('sidebar recovery inspects original lease without PREPARE/START; new document cannot take over', async () => {
  const h = await armedSidebar();
  const recovered = harness({ sidebar: true, session: h.session, captureRPC: body => ({ ok: true, phase: 'recording', operationId: body.operationId, leaseId: body.data.leaseId }) });
  await recovered.send(message('UI_STATE'));
  assert.deepEqual(recovered.calls.map(x => x.type), ['GET_OPERATION', 'CAPTURE_REQUEST']);
  assert.equal(recovered.calls[1].data.action, 'INSPECT');
  await h.send(message('SIDEBAR_HELLO', { documentInstanceId: 'sidebar-NEW', data: { windowId: 1, enabled: false } }), h.sidebarSender);
  assert.equal(h.session.operation.phase, 'failed'); assert.equal(h.session.operation.capture.documentInstanceId, 'sidebar-1');
  assert.equal(h.session.sidebars['1'].enabled, false);
});
test('late PREPARE cannot reopen a sidebar after a recorded stop intent', async () => {
  const h = await armedSidebar(); await h.send(message('TOGGLE'));
  const count = h.calls.filter(x => x.type === 'CAPTURE_REQUEST' && x.data.action === 'PREPARE').length;
  await assert.rejects(h.capture({ action: 'PREPARE', leaseId: 'lease-1' }), /已停止/);
  assert.equal(h.calls.filter(x => x.type === 'CAPTURE_REQUEST' && x.data.action === 'PREPARE').length, count);
});
test('SPA cleanup can only close the bound lease; other routes cannot reopen audio or clear fault evidence', async () => {
  const h = await armedSidebar(), away = { ...h.sender, url: origin + '/projects' };
  const request = data => h.send(message('CAPTURE_REQUEST', { operationId: h.op.operationId, documentInstanceId: 'document-1', data }), away);
  await request({ action: 'STOP', leaseId: 'lease-1', flush: false });
  await assert.rejects(request({ action: 'PREPARE', leaseId: 'lease-1' }));
  await assert.rejects(request({ action: 'START', leaseId: 'lease-1' }));
  h.onRemoved.emit(10); await pause(); const reason = h.session.operation.error;
  await h.send(message('STATE', { operationId: h.op.operationId, documentInstanceId: 'document-1', data: { phase: 'completed', error: '' } }), away);
  assert.equal(h.session.operation.phase, 'failed'); assert.equal(h.session.operation.error, reason);
});
test('a new explicit operation can bind a refreshed recorder, without mistaking its HELLO for an old-operation reload', async () => {
  let h;
  h = harness({ rpc: async body => {
    if (body.type === 'HELLO') {
      const response = await h.send(message('RECORDER_HELLO', { documentInstanceId: 'document-refreshed' }), h.sender);
      assert.equal(response.captureKind, 'webpage');
      return { requestId: body.requestId, protocolVersion: 1, documentInstanceId: 'document-refreshed', data: { enabled: true } };
    }
  } });
  await h.send(message('TOGGLE')); assert.equal(h.session.operation.phase, 'preparing');
  assert.equal(h.session.operation.recorder.documentInstanceId, 'document-refreshed');
  assert.equal(h.calls.filter(x => x.type === 'START').length, 1);
});
test('a lost START receipt stops the original document instead of leaving Chrome recording behind a failed badge', async () => {
  const h = harness({ rpc: body => body.type === 'START' ? { ...body, requestId: 'lost-start-receipt' } : undefined });
  await h.send(message('TOGGLE')); const op = h.session.operation;
  assert.equal(op.phase, 'failed');
  const stop = h.calls.find(x => x.type === 'STOP'); assert(stop);
  assert.equal(stop.operationId, op.operationId); assert.equal(stop.documentInstanceId, 'document-1');
  assert.equal(h.calls.filter(x => x.type === 'START').length, 1);
});
test('a cancelled old START rejection cannot stop or invalidate the successor', async () => {
  let rejectStart, entered, first = true;
  const began = new Promise(r => { entered = r; });
  const h = harness({ rpc: body => {
    if (body.type === 'START' && first) { first = false; entered(); return new Promise((_, reject) => { rejectStart = reject; }); }
  } });
  const old = h.send(message('TOGGLE')); await began; await h.send(message('CANCEL'));
  await h.send(message('TOGGLE')); const next = h.session.operation.operationId;
  rejectStart(new Error('late old failure')); await old;
  assert.equal(h.session.operation.operationId, next); assert.equal(h.session.operation.phase, 'preparing');
  assert(!h.calls.some(x => x.type === 'STOP' && x.operationId === next));
});

for (const kind of ['stop', 'fault', 'cancel']) {
  test(`${kind} intent survives reconstruction before either shutdown endpoint receives the command`, async () => {
    let releasePage, releaseCapture, pageEntered, captureEntered;
    const pageGate = new Promise(r => { pageEntered = r; }), captureGate = new Promise(r => { captureEntered = r; });
    const h = await armedSidebar({
      rpc: body => {
        if (['STOP', 'CANCEL'].includes(body.type)) {
          pageEntered(); return new Promise(r => { releasePage = () => r({ ...body, data: { closedOperationId: body.operationId } }); });
        }
      },
      captureRPC: body => {
        if (body.data.action === 'STOP') {
          captureEntered(); return new Promise(r => { releaseCapture = () => r({ ok: true }); });
        }
        return { ok: true };
      },
    });
    const request = kind === 'fault'
      ? h.send(message('CAPTURE_ERROR', { operationId: h.op.operationId, documentInstanceId: 'sidebar-1', data: { leaseId: 'lease-1', error: 'device lost' } }), h.sidebarSender)
      : h.send(message(kind === 'cancel' ? 'CANCEL' : 'TOGGLE'));
    await Promise.all([pageGate, captureGate]);
    const checkpoint = structuredClone(h.session), old = checkpoint.operation;
    assert.equal(old.closeIntent.kind, kind); assert.equal(old.closeIntent.pagePending, true); assert.equal(old.closeIntent.capturePending, true);
    assert.equal(old.recorder.documentInstanceId, 'document-1'); assert.equal(old.capture.leaseId, 'lease-1');
    assert.equal(old.phase, kind === 'stop' ? 'draining' : kind === 'fault' ? 'failed' : 'cancelled');

    const recovered = harness({ sidebar: true, session: checkpoint }); await recovered.send(message('UI_STATE'));
    const pageClose = recovered.calls.find(x => x.type === (kind === 'cancel' ? 'CANCEL' : 'STOP'));
    assert(pageClose); assert.equal(pageClose.operationId, old.operationId); assert.equal(pageClose.documentInstanceId, 'document-1');
    const captureClose = recovered.calls.find(x => x.type === 'CAPTURE_REQUEST');
    assert.equal(captureClose.documentInstanceId, 'sidebar-1'); assert.equal(captureClose.operationId, old.operationId);
    assert.equal(captureClose.data.action, 'STOP'); assert.equal(captureClose.data.leaseId, 'lease-1');
    assert.equal(captureClose.data.flush, kind === 'stop'); assert.equal(captureClose.data.disable, kind === 'fault');
    assert(!recovered.calls.some(x => x.type === 'START' || (x.type === 'CAPTURE_REQUEST' && ['PREPARE', 'START'].includes(x.data.action))));
    if (kind === 'cancel') assert.equal(recovered.session.operation, null);
    else {
      assert.equal(recovered.session.operation.phase, old.phase, 'GET_OPERATION recording must not retract the intent');
      assert.equal(recovered.session.operation.closeIntent.pagePending, false); assert.equal(recovered.session.operation.closeIntent.capturePending, false);
      await recovered.send(message('STATE', { operationId: old.operationId, documentInstanceId: 'document-1', data: { phase: 'recording' } }), recovered.sender);
      assert.equal(recovered.session.operation.phase, old.phase);
      for (const action of ['PREPARE', 'START']) await assert.rejects(recovered.send(message('CAPTURE_REQUEST', {
        operationId: old.operationId, documentInstanceId: 'document-1', data: { action, leaseId: 'lease-1' },
      }), recovered.sender), /停止/);
    }
    releasePage(); releaseCapture(); await request;
  });
}

test('legacy draining/fault records are upgraded and replayed before observing recording', async () => {
  for (const faulted of [false, true]) {
    const op = { operationId: 'legacy-op', recorder, phase: faulted ? 'failed' : 'draining', faulted,
      capture: { windowId: 1, documentInstanceId: 'sidebar-1', enabled: true, leaseId: 'lease-1' } };
    const h = harness({ sidebar: true, session: { recorder, operation: op } }); await h.send(message('UI_STATE'));
    assert.equal(h.session.operation.closeIntent.kind, faulted ? 'fault' : 'stop');
    assert.equal(h.session.operation.phase, op.phase);
    assert(h.writes[0].operation.closeIntent.pagePending); assert(h.writes[0].operation.closeIntent.capturePending);
    assert.equal(h.calls.find(x => x.type !== 'CAPTURE_REQUEST').type, 'STOP');
  }
});

test('cancel retains all bindings when a close receipt is lost, and replays only the pending endpoint', async () => {
  const h = await armedSidebar({ rpc: body => { if (body.type === 'CANCEL') throw new Error('receipt lost'); } });
  await h.send(message('CANCEL'));
  assert.equal(h.session.operation.operationId, h.op.operationId); assert.equal(h.session.operation.phase, 'cancelled');
  assert.equal(h.session.operation.closeIntent.pagePending, true); assert.equal(h.session.operation.closeIntent.capturePending, false);
  await h.send(message('TOGGLE')); assert.equal(h.calls.filter(x => x.type === 'START').length, 1);
  const recovered = harness({ sidebar: true, session: h.session }); await recovered.send(message('UI_STATE'));
  assert.deepEqual(recovered.calls.map(x => x.type), ['CANCEL']); assert.equal(recovered.session.operation, null);
});

test('cancel also retains the original lease after page handoff until hardware closure is confirmed', async () => {
  const h = await armedSidebar({ captureRPC: body => body.data.action === 'STOP' ? { ok: false, error: 'not closed' } : { ok: true } });
  await h.send(message('CANCEL'));
  assert.equal(h.session.operation.closeIntent.pagePending, false); assert.equal(h.session.operation.closeIntent.capturePending, true);
  const recovered = harness({ sidebar: true, session: h.session }); await recovered.send(message('UI_STATE'));
  assert.deepEqual(recovered.calls.map(x => x.type), ['CAPTURE_REQUEST']); assert.equal(recovered.calls[0].data.action, 'STOP');
  assert.equal(recovered.calls[0].data.leaseId, 'lease-1'); assert.equal(recovered.session.operation, null);
});

test('completed observations stay completed on repeated stop; the durable close intent still fences reopening', async () => {
  const h = harness(); await h.send(message('TOGGLE')); const op = h.session.operation;
  await h.send(message('TOGGLE'));
  await h.send(message('STATE', { operationId: op.operationId, documentInstanceId: 'document-1', data: { phase: 'completed' } }), h.sender);
  await h.send(message('TOGGLE'));
  assert.equal(h.session.operation.phase, 'completed'); assert.equal(h.session.operation.closeIntent.kind, 'stop');
  assert.equal(h.calls.filter(x => x.type === 'START').length, 1);
  await h.send(message('DISCARD')); assert.equal(h.session.operation, null);
  assert(!h.calls.some(x => x.type === 'CANCEL'), 'discard must not cancel completed backend history');
});

test('a missing or error close receipt is not a confirmed handoff; discard cannot bypass it', async () => {
  for (const reply of [{}, { error: 'not closed' }, { closedOperationId: 'other-operation' }]) {
    const h = harness({ rpc: body => ['STOP', 'CANCEL'].includes(body.type) ? { ...body, data: reply } : undefined });
    await h.send(message('TOGGLE')); await h.send(message('CANCEL')); await h.send(message('DISCARD'));
    assert.equal(h.session.operation.closeIntent.pagePending, true);
    assert.equal(h.session.operation.recorder.documentInstanceId, 'document-1');
    assert.equal(h.calls.filter(x => x.type === 'START').length, 1);
  }
});

test('discard of an observed terminal state still requires a STOP tombstone receipt, without cancelling history', async () => {
  const h = harness({ rpc: body => body.type === 'STOP' ? { ...body, data: { error: 'receipt unavailable' } } : undefined });
  await h.send(message('TOGGLE')); const op = h.session.operation;
  await h.send(message('STATE', { operationId: op.operationId, documentInstanceId: 'document-1', data: { phase: 'completed' } }), h.sender);
  await h.send(message('DISCARD'));
  assert.equal(h.session.operation.closeIntent.pagePending, true); assert.equal(h.session.operation.closeIntent.discard, true);
  assert(!h.calls.some(x => x.type === 'CANCEL'));
  const recovered = harness({ session: h.session }); await recovered.send(message('UI_STATE'));
  assert.deepEqual(recovered.calls.map(x => x.type), ['STOP']); assert.equal(recovered.session.operation, null);
});

test('recovery allows original tail packets and page STOP receipts while shutdown is being replayed', async () => {
  const old = { operationId: 'tail-op', recorder, phase: 'draining', sessionId: 'session-1',
    capture: { windowId: 1, documentInstanceId: 'sidebar-1', enabled: true, leaseId: 'lease-1' },
    closeIntent: { kind: 'stop', pagePending: true, capturePending: true, discard: false } };
  let h, delivered = 0;
  h = harness({ sidebar: true, session: { recorder, operation: old },
    rpc: body => body.type === 'AUDIO_FRAME' ? { ...body, data: { ok: true, nextSequence: 1 } } : undefined,
    captureRPC: async body => {
      assert.equal(body.data.action, 'STOP');
      if (++delivered === 1) {
        const tail = await h.send(message('AUDIO_FRAME', { operationId: old.operationId, documentInstanceId: 'sidebar-1',
          data: { leaseId: 'lease-1', sequence: 0, pcm: [1, -2], level: .1 } }), h.sidebarSender);
        assert.equal(tail.ok, true);
        const pageStop = await h.send(message('CAPTURE_REQUEST', { operationId: old.operationId, documentInstanceId: 'document-1',
          data: { action: 'STOP', leaseId: 'lease-1', flush: true } }), h.sender);
        assert.equal(pageStop.ok, true);
      }
      return { ok: true };
    } });
  await h.send(message('UI_STATE'));
  assert.equal(h.session.operation.closeIntent.capturePending, false); assert.equal(h.session.operation.closeIntent.pagePending, false);
  assert.equal(delivered, 2); assert(h.calls.some(x => x.type === 'AUDIO_FRAME'));
});

test('a late stop receipt from a superseded close intent cannot clear the next operation', async () => {
  let release, entered;
  const gate = new Promise(r => { entered = r; });
  const h = harness({ rpc: body => {
    if (body.type === 'STOP') { entered(); return new Promise(r => { release = () => r({ ...body, data: { closedOperationId: body.operationId } }); }); }
  } });
  await h.send(message('TOGGLE')); const stopping = h.send(message('TOGGLE')); await gate;
  await h.send(message('CANCEL')); assert.equal(h.session.operation, null);
  await h.send(message('TOGGLE')); const next = h.session.operation.operationId;
  release(); await stopping;
  assert.equal(h.session.operation.operationId, next); assert.equal(h.session.operation.phase, 'preparing');
  assert.equal(h.session.operation.closeIntent, undefined);
});
