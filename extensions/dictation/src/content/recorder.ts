import browser from 'webextension-polyfill';
import { envelope, id, message, record, servicePage, captureRequest, type Envelope } from '../../../../shared/dictationExtensionProtocol';
const scope = globalThis as typeof globalThis & { __dictationRecorder?: boolean };
if (!scope.__dictationRecorder && window === window.top) {
  scope.__dictationRecorder = true;
  let documentInstanceId = id();
  let activeOperationId = '';
  const waiting = new Map<string, { body: Envelope; resolve: (value: Envelope) => void; timer: ReturnType<typeof setTimeout> }>();
  async function allowed(drain = false) {
    const config = await browser.storage.local.get('serviceOrigin');
    return typeof config.serviceOrigin === 'string' && (servicePage(location.href, config.serviceOrigin) || (drain && location.origin === config.serviceOrigin));
  }
  function pageRequest(body: Envelope): Promise<Envelope> {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { waiting.delete(body.requestId); reject(new Error('录音网页没有响应，请打开录音页检查连接。')); }, 2000);
      waiting.set(body.requestId, { body, resolve, timer });
      window.postMessage({ direction: 'extension', body: { ...body, documentInstanceId } }, location.origin);
    });
  }
  async function hello() {
    if (location.pathname !== '/dictation/extension' || !await allowed()) return;
    const status = await browser.runtime.sendMessage(message('RECORDER_HELLO', { documentInstanceId }));
    return pageRequest(message('HELLO', { data: status }));
  }
  window.addEventListener('pageshow', event => { if (event.persisted) { documentInstanceId = id(); void hello().catch(() => {}); } });
  window.addEventListener('message', event => {
    if (event.source === window && event.origin === location.origin && record(event.data) && event.data.direction === 'recorder-ready' &&
      envelope(event.data.body) && event.data.body.type === 'RECORDER_HELLO') { void hello().catch(() => {}); return; }
    if (event.source !== window || event.origin !== location.origin || !record(event.data) || event.data.direction !== 'page' || !envelope(event.data.body)) return;
    const body = event.data.body;
    if (body.documentInstanceId !== documentInstanceId) return;
    const request = waiting.get(body.requestId);
    if (request && body.type === request.body.type && body.operationId === request.body.operationId) {
      clearTimeout(request.timer); waiting.delete(body.requestId); request.resolve(body); return;
    }
    const draining = !!activeOperationId && body.operationId === activeOperationId;
    if (location.pathname !== '/dictation/extension' && !draining) return;
    if (body.type === 'CAPTURE_REQUEST') {
      if (location.pathname !== '/dictation/extension' && (!captureRequest(body.data) || body.data.action !== 'STOP')) return;
      void allowed(draining).then(async ok => {
        if (!ok) throw new Error('服务授权已失效。');
        return browser.runtime.sendMessage(body);
      }).then(data => window.postMessage({ direction: 'capture-reply', body: { ...body, data } }, location.origin),
        cause => window.postMessage({ direction: 'capture-reply', body: { ...body, data: { ok: false, error: String(cause) } } }, location.origin));
    } else if (['STATE', 'ERROR'].includes(body.type)) {
      void allowed(draining).then(ok => ok && browser.runtime.sendMessage(body)).catch(() => {});
    }
  });
  browser.runtime.onMessage.addListener((body: unknown, sender: browser.Runtime.MessageSender) => {
    if (sender.id !== browser.runtime.id || !envelope(body) ||
      !['HELLO', 'START', 'STOP', 'CANCEL', 'GET_OPERATION', 'AUDIO_FRAME', 'CAPTURE_ERROR'].includes(body.type)) return undefined;
    return (async () => {
      const draining = !!activeOperationId && body.operationId === activeOperationId && body.type !== 'START';
      if (!await allowed(draining)) throw new Error('网站权限未授予或服务地址已变更');
      if (body.type !== 'HELLO' && location.pathname !== '/dictation/extension' && !draining) throw new Error('普通听写页不接受录音控制');
      if (body.type !== 'HELLO' && body.documentInstanceId !== documentInstanceId) throw new Error('录音文档已变化');
      if (body.type === 'HELLO') {
        const state = await hello();
        return { protocolVersion: 1, requestId: body.requestId, documentInstanceId, version: browser.runtime.getManifest().version, data: state?.data };
      }
      if (body.type === 'START' && body.operationId) activeOperationId = body.operationId;
      return pageRequest(body);
    })();
  });
  window.addEventListener('message', event => {
    if (event.source !== window || event.origin !== location.origin || !record(event.data) || event.data.direction !== 'guide' || !envelope(event.data.body) || event.data.body.type !== 'HELLO') return;
    const body = event.data.body;
    void allowed().then(async ok => {
      if (!ok) return;
      const status = await browser.runtime.sendMessage(message('HELLO', { documentInstanceId }));
      window.postMessage({ direction: 'guide-status', body: { ...body, type: 'STATUS', data: status } }, location.origin);
    }).catch(() => {});
  });
  void hello().catch(() => {});
}
