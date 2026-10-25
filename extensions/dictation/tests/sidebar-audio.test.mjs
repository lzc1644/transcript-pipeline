import test from 'node:test';
import assert from 'node:assert/strict';
import { build } from 'vite';
import { fileURLToPath } from 'node:url';
async function load(path) {
  const result = await build({ configFile: false, logLevel: 'silent', build: { write: false, minify: false,
    lib: { entry: fileURLToPath(new URL(path, import.meta.url)), formats: ['es'] } } });
  return import('data:text/javascript;base64,' + Buffer.from((Array.isArray(result) ? result[0] : result).output[0].code).toString('base64'));
}
const { SidebarHost } = await load('../src/sidebarCapture.ts');
const { SidebarCapture } = await load('../../../frontend/src/composables/dictationExtensionAudio.ts');
globalThis.document = { hidden: false };
const frame = sequence => ({ leaseId: 'lease-1', sequence, pcm: [1, -2, 32767, -32768], level: .1 });
const tick = () => new Promise(r => setTimeout(r, 0));
function hostHarness(relay = async (_, x) => ({ ok: true, nextSequence: x.sequence + 1 })) {
  const errors = [], events = []; let deliver;
  const host = new SidebarHost(relay, () => {}, (...e) => errors.push(e));
  host.enabled = true;
  host.capture.prepare = async () => { events.push('prepare'); };
  host.capture.start = async (_, cb) => { deliver = cb; events.push('start'); };
  host.capture.stop = async (_, flush) => { events.push('stop:' + flush); if (flush && deliver) deliver(new Int16Array([9]).buffer, .1); };
  return { host, errors, events, feed: (pcm = [1, 2]) => deliver(new Int16Array(pcm).buffer, .1) };
}
const prepare = host => host.request('op-1', { action: 'PREPARE', leaseId: 'lease-1' });
const start = host => host.request('op-1', { action: 'START', leaseId: 'lease-1' });
const stop = (host, flush = true) => host.request('op-1', { action: 'STOP', leaseId: 'lease-1', flush });

test('proxy validates PCM and deduplicates lost receipts before the backend consumer; stop accepts final tail', async () => {
  const packets = []; let proxy;
  proxy = new SidebarCapture(async request => {
    if (request.action === 'STOP') proxy.feed(frame(1));
    return { ok: true };
  });
  await proxy.prepare('live', () => {}); // use its actual lease for valid protocol packets
  const lease = proxy.lease;
  const f = n => ({ ...frame(n), leaseId: lease });
  await proxy.start('live', pcm => packets.push(Array.from(new Int16Array(pcm))));
  assert.deepEqual(proxy.feed(f(0)), { ok: true, nextSequence: 1 });
  proxy.feed(f(0)); assert.equal(packets.length, 1);
  assert.throws(() => proxy.feed({ ...f(1), pcm: [40000] }));
  assert.throws(() => proxy.feed(f(2)), /缺口/);
  proxy.request = async request => { if (request.action === 'STOP') proxy.feed(f(1)); return { ok: true }; };
  await proxy.stop('live'); assert.equal(packets.length, 2); assert(!proxy.prepared);
  assert.throws(() => proxy.feed(f(2)));
});
test('uncertain consumer exception faults the proxy instead of replaying a packet', async () => {
  const p = new SidebarCapture(async () => ({ ok: true })); let count = 0;
  await p.prepare('live', () => {}); await p.start('live', () => { count++; throw Error('uncertain'); });
  const f = { ...frame(0), leaseId: p.lease };
  assert.throws(() => p.feed(f), /uncertain/); assert.throws(() => p.feed(f)); assert.equal(count, 1); await p.stop('live', false);
});
test('carrier orders/retries same packet; releases hardware before final tail receipt; stop reply waits for drain', async () => {
  let lost = true, release; const seen = [];
  const h = hostHarness(async (_, f) => {
    seen.push(f);
    if (f.sequence === 0 && lost) { lost = false; throw Error('lost reply'); }
    if (f.sequence === 1) await new Promise(r => { release = r; });
    return { ok: true, nextSequence: f.sequence + 1 };
  });
  await prepare(h.host); await start(h.host); h.feed(); await tick();
  let stopped = false; const stopping = stop(h.host).then(() => { stopped = true; }); await tick();
  assert(h.events.includes('stop:true')); assert(!stopped); assert.deepEqual(seen.map(f => f.sequence), [0, 0, 1]);
  release(); await stopping; assert(stopped); assert.deepEqual(seen.at(-1).pcm, [9]);
  await assert.rejects(prepare(h.host), /已结束/); assert.equal(h.errors.length, 0);
});
test('STOP overtakes permission and tombstones a delayed PREPARE/START', async () => {
  const h = hostHarness(); let grant;
  h.host.capture.prepare = () => new Promise(r => { grant = r; });
  const preparing = prepare(h.host); const rejected = assert.rejects(preparing, /取消/);
  await stop(h.host, false); await rejected; grant(); await tick();
  await assert.rejects(start(h.host), /已结束/); await assert.rejects(prepare(h.host), /已结束/);
  assert(!h.events.includes('start')); assert.equal(h.errors.length, 0);
  const h2 = hostHarness(); await stop(h2.host, false); await assert.rejects(prepare(h2.host), /已结束/);
});
test('delivery failure closes the microphone locally without waiting for coordinator recovery', async () => {
  const h = hostHarness(async () => { throw Error('service disconnected'); });
  await prepare(h.host); await start(h.host); h.feed(); await tick(); await tick();
  assert(h.events.includes('stop:false')); assert.equal(h.host.enabled, false); assert.equal(h.errors.length, 1);
});
test('bounded carrier queue fails closed rather than silently dropping samples', async () => {
  const h = hostHarness(() => new Promise(() => {}));
  await prepare(h.host); await start(h.host); for (let i = 0; i < 63; i++) h.feed();
  assert(h.events.includes('stop:false')); assert.equal(h.errors.length, 1); assert.match(h.errors[0][2], /积压/);
});
test('emergency stop disables sidebar eligibility until a new explicit probe', async () => {
  const h = hostHarness(); await prepare(h.host); await start(h.host);
  await h.host.request('op-1', { action: 'STOP', leaseId: 'lease-1', flush: false, disable: true });
  assert.equal(h.host.enabled, false); assert.equal(h.host.phase, 'unarmed');
  await assert.rejects(h.host.request('op-2', { action: 'PREPARE', leaseId: 'lease-2' }), /授权/);
});
test('a late cancelled probe cannot stop a newer probe or enable control', async () => {
  const h = hostHarness(); let release; let first = true;
  h.host.capture.prepare = () => first ? (first = false, new Promise(r => { release = r; })) : Promise.resolve();
  h.host.capture.start = async (_, cb) => cb(new Int16Array([1, 2]).buffer, .1);
  const old = h.host.probe(); await h.host.disable();
  const next = h.host.probe(); release(); await old;
  // Old finally must not call stop('probe') against the successor's lease.
  assert.equal(h.events.filter(e => e === 'stop:false').length, 1);
  await next; assert.equal(h.host.enabled, true);
});
test('a late emergency STOP cannot disable a newer lease or claim its live microphone is closed', async () => {
  const h = hostHarness(); await prepare(h.host); await start(h.host); await stop(h.host, false);
  await h.host.request('op-2', { action: 'PREPARE', leaseId: 'lease-2' });
  await h.host.request('op-2', { action: 'START', leaseId: 'lease-2' });
  const releases = h.events.filter(x => x.startsWith('stop:')).length;
  await h.host.request('op-1', { action: 'STOP', leaseId: 'lease-1', flush: false, disable: true });
  assert.equal(h.host.enabled, true); assert.equal(h.host.phase, 'recording');
  assert.equal(h.events.filter(x => x.startsWith('stop:')).length, releases);
  await assert.rejects(h.host.request('op-wrong', { action: 'STOP', leaseId: 'lease-2' }), /不匹配/);
  await h.host.stop(false);
});
test('an old release finalizer cannot relabel a newer capture as ready', async () => {
  const h = hostHarness(); await prepare(h.host); await start(h.host); let release;
  h.host.capture.stop = () => new Promise(r => { release = r; });
  const stopping = stop(h.host, false);
  await h.host.request('op-2', { action: 'PREPARE', leaseId: 'lease-2' });
  await h.host.request('op-2', { action: 'START', leaseId: 'lease-2' });
  release(); await stopping; assert.equal(h.host.phase, 'recording');
  h.host.capture.stop = async () => {}; await h.host.stop(false);
});
test('explicit reauthorization is not revoked by an old closed-lease emergency', async () => {
  const h = hostHarness(); await prepare(h.host); await start(h.host); await stop(h.host, false);
  h.host.capture.start = async (_, cb) => cb(new Int16Array([1, 2]).buffer, .1);
  await h.host.probe(); assert.equal(h.host.enabled, true);
  await h.host.request('op-1', { action: 'STOP', leaseId: 'lease-1', flush: false, disable: true });
  assert.equal(h.host.enabled, true); assert.equal(h.host.phase, 'ready');
});
