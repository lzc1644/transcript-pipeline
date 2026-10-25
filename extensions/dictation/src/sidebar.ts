import browser from 'webextension-polyfill';
import { envelope, id, message, captureRequest, record, type Envelope } from '../../../shared/dictationExtensionProtocol';
import { SidebarHost } from './sidebarCapture';
import './sidebar.css';
const documentInstanceId = id();
let windowId = -1;
const app = document.querySelector<HTMLElement>('#app')!;
app.innerHTML = `<header><span class="eyebrow">网页语音输入 · 开发验证</span><h1>在这里采音<br>在原处继续输入。</h1></header>
<p id="phase" role="status"></p><p id="error" role="alert"></p>
<div class="actions"><button id="enable">授权麦克风并试音</button><button id="stop" disabled>结束本次听写</button></div>
<section><h2>使用前</h2><ol><li>在扩展面板连接服务，打开录音页并启用控制。</li><li>在此授权试音；结束后麦克风会关闭。</li><li>保持侧栏可见，回目标框用快捷键开始 / 结束。</li></ol>
<p>音频经录音网页发送给您配置的服务；转录文字可发送给校对供应商。此 S1 版本不回填文字。</p></section>
<footer><button id="open">打开服务录音页</button><button id="disable">停用侧栏采音</button><p id="origin"></p><p id="shortcuts"></p></footer>`;
const phase = document.querySelector<HTMLElement>('#phase')!, error = document.querySelector<HTMLElement>('#error')!;
const enable = document.querySelector<HTMLButtonElement>('#enable')!, stop = document.querySelector<HTMLButtonElement>('#stop')!;
async function rpc(body: Envelope, timeout = 1500) {
  let timer: ReturnType<typeof setTimeout>;
  try { return await Promise.race([browser.runtime.sendMessage(body), new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error('扩展或服务网页未响应，已停止新增采音。')), timeout);
  })]); } finally { clearTimeout(timer!); }
}
function publish() {
  const state = host.snapshot();
  phase.textContent = ({ unarmed: '尚未授权', probing: '正在试音…', ready: '就绪 · 麦克风已关闭',
    preparing: '正在准备麦克风…', recording: '正在听写 · 麦克风开启', failed: '采音已中断 · 请检查服务网页' } as Record<string, string>)[state.phase] ?? state.phase;
  error.textContent = state.error; enable.disabled = ['probing', 'preparing', 'recording'].includes(state.phase);
  stop.disabled = !['probing', 'preparing', 'recording'].includes(state.phase);
  if (windowId >= 0) void rpc(message('SIDEBAR_STATE', { documentInstanceId, data: { windowId, ...state } })).catch(() => {});
}
const host = new SidebarHost(
  (operationId, frame) => rpc(message('AUDIO_FRAME', { documentInstanceId, operationId, data: frame })), publish,
  (operationId, leaseId, reason) => { void rpc(message('CAPTURE_ERROR', { documentInstanceId, operationId, data: { leaseId, error: reason } })).catch(() => {}); }
);
function action(fn: () => Promise<unknown>) { return () => { void fn().catch(cause => { error.textContent = String(cause); }); }; }
enable.addEventListener('click', action(() => host.probe()));
stop.addEventListener('click', action(async () => {
  const state = host.snapshot();
  try { if (state.phase === 'probing') await host.disable(); else await host.stop(); }
  finally { if (state.operationId) await rpc(message('STOP', { documentInstanceId, operationId: state.operationId })); }
}));
document.querySelector('#disable')!.addEventListener('click', action(async () => {
  const state = host.snapshot(); await host.disable();
  if (state.operationId) await rpc(message('CANCEL', { documentInstanceId, operationId: state.operationId }));
}));
document.querySelector('#open')!.addEventListener('click', action(() => rpc(message('OPEN_RECORDER'))));
browser.runtime.onMessage.addListener((raw: unknown, sender: browser.Runtime.MessageSender) => {
  if (sender.id !== browser.runtime.id || !envelope(raw) || raw.type !== 'CAPTURE_REQUEST' ||
    raw.documentInstanceId !== documentInstanceId || !raw.operationId || !captureRequest(raw.data)) return undefined;
  return host.request(raw.operationId, raw.data).catch(cause => ({ ok: false, error: String(cause) }));
});
window.addEventListener('pagehide', () => {
  // Track stop is synchronous. The pagehide notification is advisory; tab/window handlers also recover.
  void host.disable();
  void browser.runtime.sendMessage(message('SIDEBAR_CLOSED', { documentInstanceId, data: { windowId } })).catch(() => {});
});
window.addEventListener('visibilitychange', () => { if (document.hidden) {
  const state = host.snapshot(); void host.disable();
  if (state.operationId) void rpc(message('CAPTURE_ERROR', { documentInstanceId, operationId: state.operationId,
    data: { leaseId: state.leaseId, error: '侧栏已隐藏，麦克风已关闭。请显式重新授权后再开始。' } })).catch(() => {});
} });
void (async () => {
  const win = await browser.windows.getCurrent(); if (win.incognito || win.id === undefined) throw new Error('不支持隐私窗口。');
  windowId = win.id;
  const state = await rpc(message('SIDEBAR_HELLO', { documentInstanceId, data: { windowId, enabled: false } }));
  if (record(state)) document.querySelector('#origin')!.textContent = typeof state.origin === 'string' ? state.origin : '';
  const shortcuts = await browser.commands.getAll();
  document.querySelector('#shortcuts')!.textContent = '开始 / 结束：' + (shortcuts.find(x => x.name === 'toggle-dictation')?.shortcut || '未绑定，请在扩展快捷键设置中配置');
  publish();
})().catch(cause => { error.textContent = String(cause); });
