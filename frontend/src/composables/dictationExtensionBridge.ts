import { ref, watch, onBeforeUnmount } from 'vue';
import { envelope, message, record, RECORDER_PATH, type Envelope, type CaptureRequest } from '../../../shared/dictationExtensionProtocol';
import type { DictationController } from './useDictation';
import { SidebarCapture } from './dictationExtensionAudio';

/** The service page alone owns the session and network, with an optional hardware-only sidebar. */
export function useDictationExtensionBridge(d: DictationController, terms: () => string[]) {
  const enabled = ref(false), connected = ref(false), error = ref(''), captureKind = ref('webpage');
  let documentInstanceId = '', enableGeneration = 0, disposing = false;
  const closedOperations = new Set<string>();
  type Op = { id: string; sessionId?: string; abort: AbortController; stopped: boolean; terminal?: 'cancelled' | 'failed'; capture?: SidebarCapture };
  let operation: Op | null = null;
  const waiting = new Map<string, { operationId: string; resolve: (value: unknown) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> }>();
  function state() {
    const s = d.recordingSession.value;
    const status = s && s.id === operation?.sessionId ? s.status : undefined;
    return { enabled: enabled.value, captureKind: captureKind.value, operationId: operation?.id, sessionId: operation?.sessionId,
      phase: operation?.terminal ?? (status && ['completed', 'cancelled', 'failed', 'interrupted'].includes(status) ? status : operation?.stopped ? 'draining' : d.capturing.value && s?.status === 'listening' ? 'recording' : operation ? 'preparing' : 'idle'),
      capturing: d.capturing.value, status, error: d.error.value || error.value };
  }
  function send(body: Envelope) {
    if (documentInstanceId) window.postMessage({ direction: 'page', body: { ...body, documentInstanceId } }, location.origin);
  }
  function publish() { if (operation) send(message('STATE', { operationId: operation.id, sessionId: operation.sessionId, data: state() })); }
  function captureRequest(op: Op, data: CaptureRequest) {
    return new Promise<unknown>((resolve, reject) => {
      const body = message('CAPTURE_REQUEST', { operationId: op.id, sessionId: op.sessionId, data });
      const timer = setTimeout(() => { waiting.delete(body.requestId); reject(new Error('侧栏未确认采音操作，请检查侧栏。')); }, 15000);
      waiting.set(body.requestId, { operationId: op.id, resolve, reject, timer }); send(body);
    });
  }
  async function enable() {
    const own = ++enableGeneration; error.value = '';
    try {
      // Firefox must not request a background webpage mic. The sidebar has its own explicit grant/probe.
      if (captureKind.value !== 'sidebar') await d.probeMicrophone();
      if (own === enableGeneration) enabled.value = true;
    } catch (cause) {
      if (own !== enableGeneration) return;
      error.value = cause instanceof DOMException && cause.name === 'NotAllowedError' ? '麦克风权限未授予。请在浏览器网站权限中允许麦克风，再点击重试。' : String(cause);
    }
  }
  async function stop(cancel = false) {
    const op = operation; if (!op) return;
    op.stopped = true; op.abort.abort(); publish();
    if (op.sessionId) await (cancel ? d.cancel(op.sessionId) : d.stop(op.sessionId));
  }
  function cancelSetup() { ++enableGeneration; return d.cancelMicrophoneProbe(); }
  async function disable() { enabled.value = false; await cancelSetup(); await stop(true); }
  async function receive(event: MessageEvent) {
    if ((location.pathname !== RECORDER_PATH && !disposing) || window !== window.top || event.source !== window || event.origin !== location.origin ||
      !record(event.data) || !['extension', 'capture-reply'].includes(String(event.data.direction)) || !envelope(event.data.body)) return;
    const body = event.data.body;
    if (!body.documentInstanceId || (documentInstanceId && body.documentInstanceId !== documentInstanceId)) return;
    documentInstanceId = body.documentInstanceId; connected.value = true;
    if (event.data.direction === 'capture-reply') {
      const pending = waiting.get(body.requestId);
      if (body.type === 'CAPTURE_REQUEST' && pending && pending.operationId === body.operationId) {
        clearTimeout(pending.timer); waiting.delete(body.requestId); pending.resolve(body.data);
      }
      return;
    }
    const reply = (data: unknown) => send({ ...body, data });
    if (body.type === 'HELLO' || body.type === 'GET_OPERATION') {
      if (body.type === 'HELLO' && record(body.data) && !operation) captureKind.value = body.data.captureKind === 'sidebar' ? 'sidebar' : 'webpage';
      reply(state()); return;
    }
    if (body.type === 'AUDIO_FRAME' || body.type === 'CAPTURE_ERROR') {
      const op = operation;
      if (!op?.capture || op.id !== body.operationId) { reply({ ok: false, error: '采音操作已失效。' }); return; }
      try {
        if (body.type === 'AUDIO_FRAME') reply(op.capture.feed(body.data));
        else if (record(body.data) && typeof body.data.leaseId === 'string') {
          error.value = String(body.data.error); op.terminal = 'failed'; op.capture.fail(body.data.leaseId, error.value); reply({ ok: true }); publish();
        }
      } catch (cause) { reply({ ok: false, error: String(cause) }); }
      return;
    }
    if (!['START', 'STOP', 'CANCEL'].includes(body.type) || !body.operationId || (disposing && body.type === 'START')) return;
    if (body.type === 'START') {
      if (operation?.id === body.operationId) { reply(state()); return; }
      if (closedOperations.has(body.operationId)) { reply({ error: '该操作已结束，不能再次开始。' }); return; }
      if (!enabled.value) { reply({ error: '请先在录音页启用扩展控制并授权麦克风。' }); return; }
      if (operation && !operation.terminal && !['completed', 'cancelled', 'failed', 'interrupted'].includes(d.recordingSession.value?.status ?? '') && !operation.stopped) {
        reply({ error: '当前操作尚未结束' }); return;
      }
      const op: Op = operation = { id: body.operationId, abort: new AbortController(), stopped: false };
      if (captureKind.value === 'sidebar') op.capture = new SidebarCapture(data => captureRequest(op, data));
      error.value = ''; publish(); reply(state());
      const result = await d.start(terms(), true, '', '', '', '', { signal: op.abort.signal, capture: op.capture });
      if (operation !== op) return;
      if (result.status === 'started') { op.sessionId = result.sessionId; if (op.stopped) await d.stop(result.sessionId); }
      else { op.terminal = op.stopped ? 'cancelled' : 'failed'; if (!op.stopped) error.value = result.reason; }
      publish();
    } else {
      if (operation?.id === body.operationId && body.sessionId && operation.sessionId !== body.sessionId) {
        reply({ error: '会话已失效' }); return;
      }
      closedOperations.add(body.operationId);
      // A close receipt means this document has tombstoned the operation and handed
      // shutdown to its owner. It does not mean backend draining has already finished.
      if (operation?.id !== body.operationId) { reply({ closedOperationId: body.operationId }); return; }
      void stop(body.type === 'CANCEL').catch(cause => { error.value = String(cause); publish(); });
      reply({ ...state(), closedOperationId: body.operationId });
    }
  }
  window.addEventListener('message', receive);
  // Either the SPA or the document-idle content script may finish loading first.
  window.postMessage({ direction: 'recorder-ready', body: message('RECORDER_HELLO') }, location.origin);
  const resetDocument = (event: PageTransitionEvent) => {
    if (event.persisted) { enabled.value = false; connected.value = false; documentInstanceId = ''; operation?.abort.abort(); }
  };
  window.addEventListener('pageshow', resetDocument);
  watch([() => d.capturing.value, () => d.recordingSession.value?.status, () => d.recordingSession.value?.revision, () => d.error.value], publish);
  onBeforeUnmount(() => {
    disposing = true; window.removeEventListener('pageshow', resetDocument);
    // Keep the audio/receipt listener through STOP, including the worklet's final partial packet.
    void disable().catch(cause => { error.value = String(cause); }).finally(() => { publish(); window.removeEventListener('message', receive);
      for (const pending of waiting.values()) { clearTimeout(pending.timer); pending.reject(new Error('录音页已退出。')); } waiting.clear();
    });
  });
  return { enabled, connected, error, captureKind, enable, cancelSetup, disable, stop, state };
}
