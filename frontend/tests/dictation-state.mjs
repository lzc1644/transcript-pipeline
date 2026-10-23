import assert from "node:assert/strict";
import { DictationSelection } from "../src/composables/dictationSelection.ts";

// A delayed poll cannot reselect A after the user explicitly chooses B.
{
  const selection = new DictationSelection();
  let displayed = null;
  const commit = (value, token) => { if (selection.accept(value, token)) displayed = value; };
  const a = selection.select("A");
  let release;
  const delayedA = new Promise(resolve => { release = resolve; }).then(value => commit(value, a));
  const b = selection.select("B");
  commit({ id: "B", revision: 4 }, b);
  release({ id: "A", revision: 100 });
  await delayedA;
  assert.equal(displayed.id, "B");
  assert.equal(selection.current(a), false); // old sockets are rejected by the same generation
}
// An old GET cannot undo a newer editing/WS response for the same session.
{
  const selection = new DictationSelection();
  const token = selection.select("A");
  assert(selection.accept({ id: "A", revision: 2, event_sequence: 3 }, token));
  let release;
  const delayed = new Promise(resolve => { release = resolve; }).then(value => selection.accept(value, token));
  assert(selection.accept({ id: "A", revision: 5, event_sequence: 8 }, token));
  release({ id: "A", revision: 3, event_sequence: 6 });
  assert.equal(await delayed, false);
  assert.equal(selection.accept({ id: "A", revision: 5, event_sequence: 7 }, token), false);
  const reselected = selection.select("A");
  assert.equal(selection.accept({ id: "A", revision: 999 }, token), false);
  assert(selection.accept({ id: "A", revision: 5 }, reselected));
}
// Actual worklet protocol: a partial tail precedes the stop ACK, no future samples escape.
{
  const { readFile } = await import('node:fs/promises');
  const { runInNewContext } = await import('node:vm');
  let Processor;
  const sent = [];
  runInNewContext(await readFile(new URL('../public/dictation-worklet.js', import.meta.url), 'utf8'), {
    sampleRate: 48000, AudioWorkletProcessor: class { port = { postMessage: value => sent.push(value) }; },
    registerProcessor: (_, ctor) => { Processor = ctor; },
  });
  const processor = new Processor();
  processor.process([[new Float32Array(997 * 3).fill(.25)]]);
  assert.equal(sent.length, 0);
  processor.port.onmessage({ data: 'stop' });
  assert.equal(sent[0].pcm.byteLength, 997 * 2);
  assert(sent[1].flushed);
  assert.equal(processor.process([[new Float32Array(48000).fill(.9)]]), false);
  assert.equal(sent.length, 2);
}

// Permission and context setup may finish after end/cancel: their old leases cannot record.
{
  const { PcmCapture } = await import('../src/composables/dictationCapture.ts');
  const track = () => ({ readyState: 'live', onended: null, stop() { this.readyState = 'ended'; } });
  const stream = t => ({ getTracks: () => [t], getAudioTracks: () => [t] });
  globalThis.window = { isSecureContext: true };
  let permission;
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: {
    mediaDevices: { getUserMedia: () => new Promise(resolve => { permission = resolve; }) },
  } });
  const capture = new PcmCapture(), t = track();
  const preparing = capture.prepare('live', () => assert.fail('old device callback'));
  await Promise.resolve(); await capture.stop('live', false);
  permission(stream(t));
  await assert.rejects(preparing, { name: 'AbortError' });
  assert.equal(t.readyState, 'ended'); assert(!capture.prepared);

  let moduleReady;
  let contextClosed = false;
  globalThis.AudioContext = class {
    state = 'running';
    audioWorklet = { addModule: () => new Promise(resolve => { moduleReady = resolve; }) };
    async close() { this.state = 'closed'; contextClosed = true; }
  };
  const t2 = track();
  navigator.mediaDevices.getUserMedia = async () => stream(t2);
  await capture.prepare('live', () => {});
  const starting = capture.start('live', () => assert.fail('late setup must not emit PCM'));
  await capture.stop('live', false); moduleReady();
  await assert.rejects(starting, { name: 'AbortError' });
  assert.equal(t2.readyState, 'ended'); assert(contextClosed);

  let node;
  const emitted = [];
  globalThis.AudioContext = class {
    state = 'running'; destination = {};
    audioWorklet = { addModule: async () => {} };
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    createGain() { return { gain: {}, connect() {} }; }
    async resume() {}
    async close() { this.state = 'closed'; }
  };
  globalThis.AudioWorkletNode = class {
    constructor() { node = this; }
    connect() {} disconnect() {}
    port = { onmessage: null, postMessage: () => {
      queueMicrotask(() => { this.port.onmessage?.({ data: { pcm: new ArrayBuffer(14), level: .3 } }); this.port.onmessage?.({ data: { flushed: true } }); });
    } };
  };
  const t3 = track(); let deviceErrors = 0;
  navigator.mediaDevices.getUserMedia = async () => stream(t3);
  await capture.prepare('local', () => { deviceErrors++; });
  await capture.start('local', pcm => emitted.push(pcm.byteLength));
  await capture.stop('live', false); // A stale live terminal cannot close a local lease.
  assert.equal(t3.readyState, 'live');
  assert(capture.isOwner('local'));
  await assert.rejects(capture.prepare('live', () => {}), /另一项采音/);
  assert.equal(t3.readyState, 'live');
  t3.onended(); assert.equal(deviceErrors, 1);
  const stop = capture.stop('local');
  assert.equal(t3.readyState, 'ended'); // Immediate hardware stop, not after tail ACK.
  await stop;
  node.port.onmessage?.({ data: { pcm: new ArrayBuffer(22) } });
  assert.deepEqual(emitted, [14]);
  assert(!capture.prepared);
}
// App playback owner: mutual exclusion and capture prepare stops sound BEFORE getUserMedia.
{
  const { DictationPlayback } = await import('../src/composables/dictationPlayback.ts');
  const { PcmCapture } = await import('../src/composables/dictationCapture.ts');
  let allowed = true;
  const playback = new DictationPlayback(() => allowed);
  const player = () => ({ paused: true, pause() { this.paused = true; }, async play() { this.paused = false; } });
  const a = player(), b = player();
  await playback.play(a); await playback.play(b);
  assert(a.paused && !b.paused);
  const capture = new PcmCapture(); capture.beforeCapture = () => playback.stop();
  let acquired = false;
  navigator.mediaDevices.getUserMedia = async () => {
    assert(a.paused && b.paused); acquired = true;
    const track = { stop() {}, getSettings: () => ({ deviceId: 'selected-device' }) };
    return { getTracks: () => [track], getAudioTracks: () => [track] };
  };
  await capture.prepare('live', () => {});
  assert(acquired); await capture.stop('live', false);
  capture.deviceId = 'missing-device';
  navigator.mediaDevices.getUserMedia = async constraints => {
    assert.equal(constraints.audio.deviceId.exact, 'missing-device');
    throw new DOMException('device missing', 'OverconstrainedError');
  };
  await assert.rejects(capture.prepare('live', () => {}), /重新选择/);
  await capture.stop('live', false);
  allowed = false;
  await assert.rejects(playback.play(a), /先暂停采音/);
  assert(a.paused);
}
{
  const { validateTerms, parseTerms } = await import('../src/composables/dictationTerms.ts');
  assert.deepEqual(parseTerms(' HTTP,HTTP，API\n项目名 '), ['HTTP','API','项目名']);
  assert.throws(() => validateTerms(Array.from({ length: 51 }, (_, i) => String(i))), /最多50/);
  assert.throws(() => validateTerms(['字'.repeat(81)]), /80/);
}
console.log("dictation state: selection, worklet, capture leases, playback arbitration, exact device failure and terms passed");
