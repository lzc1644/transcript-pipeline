import browser from 'webextension-polyfill';
import { PcmCapture } from '../../../../frontend/src/composables/dictationCapture';
const capture = new PcmCapture(), documentInstanceId = crypto.randomUUID();
let enabled = false, generation = 0, phase = 'unarmed', samples = 0, op = '';
const observations: Record<string, unknown>[] = [];
const tracks: MediaStreamTrack[] = [], packets: number[] = [], contexts: Record<string, unknown>[] = [];
Object.assign(window, { observations, __probeTracks: tracks });
const OriginalContext = window.AudioContext;
window.AudioContext = class extends OriginalContext {
  constructor(...args: ConstructorParameters<typeof AudioContext>) {
    super(...args);
    const item = { created: Date.now(), hidden: document.hidden, initial: this.state, states: [this.state] };
    contexts.push(item); this.addEventListener('statechange', () => item.states.push(this.state));
  }
};
const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
navigator.mediaDevices.getUserMedia = async constraints => {
  const observation: Record<string, unknown> = { called: Date.now(), hidden: document.hidden };
  observations.push(observation);
  const stream = await original(constraints);
  tracks.push(...stream.getTracks());
  observation.resolved = Date.now(); observation.resolvedHidden = document.hidden;
  return stream;
};
function publish() {
  document.querySelector('#phase')!.textContent = phase;
  document.querySelector('#metrics')!.textContent = JSON.stringify({ samples, observations }, null, 2);
  return browser.runtime.sendMessage({ type: 'STATE', documentInstanceId, phase, op, samples });
}
async function stop() { ++generation; await capture.stop('live'); await capture.stop('probe', false); phase = 'stopped'; await publish(); }
document.querySelector('#enable')!.addEventListener('click', () => void (async () => {
  const button = document.querySelector<HTMLButtonElement>('#enable')!;
  if (button.disabled || phase === 'capturing' || phase === 'preparing') return;
  button.disabled = true;
  try {
    await capture.prepare('probe', () => {});
    await capture.start('probe', () => {});
    await new Promise(resolve => setTimeout(resolve, 400));
    await capture.stop('probe'); enabled = true; phase = 'ready';
  } catch (e) { phase = String(e); } finally { await capture.stop('probe', false); button.disabled = false; await publish(); }
})());
document.querySelector('#stop')!.addEventListener('click', () => void stop());
browser.runtime.onMessage.addListener((body: unknown, sender: browser.Runtime.MessageSender) => {
  if (sender.id !== browser.runtime.id || !body || typeof body !== 'object') return undefined;
  const b = body as Record<string, unknown>;
  if (b.documentInstanceId !== documentInstanceId || !['START', 'STOP', 'OBSERVE'].includes(String(b.type))) return undefined;
  if (b.type === 'OBSERVE') return Promise.resolve({ phase, samples, observations, contexts, trackStates: tracks.map(t => t.readyState), packets, hidden: document.hidden });
  if (b.type === 'STOP') { void stop(); return Promise.resolve({ accepted: true }); }
  if (!enabled || phase === 'capturing' || phase === 'preparing') return Promise.resolve({ rejected: true });
  const own = ++generation; op = String(b.op); samples = 0; packets.length = 0; phase = 'preparing'; void publish();
  void (async () => {
    try {
      await capture.prepare('live', () => void stop());
      if (own !== generation) return;
      await capture.start('live', pcm => { packets.push(pcm.byteLength / 2); samples += pcm.byteLength / 2; document.querySelector('#metrics')!.textContent = JSON.stringify({ samples, observations }, null, 2); });
      if (own === generation) { phase = 'capturing'; await publish(); }
    } catch (e) { phase = String(e); await publish(); }
  })();
  return Promise.resolve({ accepted: true });
});
window.addEventListener('pagehide', () => { ++generation; void capture.stop('live', false); void capture.stop('probe', false); });
void browser.runtime.sendMessage({ type: 'BIND', documentInstanceId });
