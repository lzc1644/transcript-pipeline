import browser from "webextension-polyfill";
import { envelope, identifier, id, message, record, serviceOrigin, hostPermission, servicePage, RECORDER_PATH, captureRequest, audioFrame, captureBinding, type CaptureBinding, type Envelope, type CaptureRequest, type MessageType } from "../../../shared/dictationExtensionProtocol";

// S1 owns recording intents only. No target values, text delivery or backend network requests.
type Recorder = { tabId: number; documentInstanceId: string; exactOrigin: string };
type CloseIntent = { kind: "stop" | "cancel" | "fault"; pagePending: boolean; capturePending: boolean; discard: boolean };
type Operation = { operationId: string; recorder: Recorder; sessionId?: string; phase: string; error?: string; faulted?: boolean;
  capture?: CaptureBinding & { leaseId?: string }; closeIntent?: CloseIntent };
const phases = ["preparing", "recording", "draining", "completed", "cancelled", "failed", "interrupted"];
let origin = "", recorder: Recorder | null = null, operation: Operation | null = null, connecting = false;
const sidebarMode = !!browser.runtime.getManifest().sidebar_action;
let sidebars: Record<string, CaptureBinding> = {};
let queue = Promise.resolve();
function serial<T>(fn: () => Promise<T>): Promise<T> {
  const result = queue.then(fn); queue = result.then(() => {}, () => {}); return result;
}
async function persist() { await browser.storage.session.set({ recorder, operation, sidebars }); }
function validRecorder(value: unknown): value is Recorder {
  return record(value) && Number.isInteger(value.tabId) && typeof value.documentInstanceId === "string" && value.exactOrigin === origin;
}
function validOperation(value: unknown): value is Operation {
  const close = record(value) ? value.closeIntent : undefined;
  return record(value) && identifier(value.operationId) && validRecorder(value.recorder) &&
    phases.includes(String(value.phase)) && (value.sessionId === undefined || identifier(value.sessionId)) &&
    (close === undefined || (record(close) && ['stop', 'cancel', 'fault'].includes(String(close.kind)) &&
      ['pagePending', 'capturePending', 'discard'].every(key => typeof close[key] === 'boolean'))) &&
    (value.capture === undefined || (captureBinding(value.capture) && record(value.capture) && (value.capture.leaseId === undefined || identifier(value.capture.leaseId))));
}
async function command(op: Operation, type: "START" | "STOP" | "CANCEL" | "GET_OPERATION") {
  const body = message(type, {
    operationId: op.operationId, sessionId: op.sessionId, documentInstanceId: op.recorder.documentInstanceId,
  });
  const reply = await browser.tabs.sendMessage(op.recorder.tabId, body, { frameId: 0 });
  if (!envelope(reply) || reply.type !== type || reply.requestId !== body.requestId || reply.operationId !== op.operationId ||
    reply.documentInstanceId !== op.recorder.documentInstanceId || (body.sessionId && reply.sessionId !== body.sessionId))
    throw new Error("录音页绑定已失效，请打开原录音页检查；不要重复启动。");
  return reply;
}
async function failure(detail: string) {
  await browser.action.setBadgeText({ text: "!" });
  await browser.action.setTitle({ title: detail });
}
// Loading bindings and reconciling external state are separate: STOP may flush audio back
// through this worker, so original-document receipts/tail packets cannot wait for recovery.
const loaded = (async () => {
  const config = await browser.storage.local.get("serviceOrigin");
  if (typeof config.serviceOrigin === "string") { try { origin = serviceOrigin(config.serviceOrigin); } catch { /* Invalid configuration stays disconnected. */ } }
  const saved = await browser.storage.session.get(["recorder", "operation", "sidebars"]);
  if (record(saved.sidebars)) for (const [key, value] of Object.entries(saved.sidebars)) if (captureBinding(value) && String(value.windowId) === key) sidebars[key] = value;
  recorder = validRecorder(saved.recorder) ? saved.recorder : null;
  operation = validOperation(saved.operation) ? saved.operation : null;
  // Upgrade pre-fix records in place. Their phase/fault flag already expresses a close intent.
  if (operation && !operation.closeIntent && (operation.faulted || operation.phase === 'draining')) {
    markClose(operation, operation.faulted ? 'fault' : 'stop'); await persist();
  }
})();
const ready = loaded.then(async () => {
  // Rebuild by probing the ORIGINAL document, never by opening or starting another recorder.
  const op = operation;
  if (op) {
    // Replay BEFORE consulting observations. A still-recording page cannot retract a user stop.
    if (op.closeIntent) await deliverClose(op);
    if (operation !== op) return;
    try {
      const reply = await command(op, "GET_OPERATION");
      if (!record(reply.data) || reply.data.operationId !== op.operationId ||
        (op.sessionId && reply.data.sessionId !== op.sessionId) || !phases.includes(String(reply.data.phase))) throw new Error();
      const data = reply.data;
      await serial(async () => {
        if (operation !== op) return;
        observe(op, String(data.phase));
        if (identifier(data.sessionId)) op.sessionId = data.sessionId;
        await persist();
      });
      if (!op.closeIntent && op.capture?.leaseId && ['preparing', 'recording'].includes(op.phase)) {
        const state = await captureCommand(op, { action: 'INSPECT', leaseId: op.capture.leaseId });
        if (!record(state) || state.ok !== true || state.operationId !== op.operationId || state.leaseId !== op.capture.leaseId ||
          !['preparing', 'recording'].includes(String(state.phase))) throw new Error('原侧栏已失效');
      }
    } catch { await emergency(op, '无法核对原录音页或采音侧栏。麦克风已请求关闭；请打开原页检查已接收内容。'); }
  }
});

function observe(op: Operation, phase: string) {
  if (op.faulted || op.closeIntent?.kind === 'cancel') return;
  if ((op.closeIntent || op.phase === 'draining') && ['preparing', 'recording'].includes(phase)) return;
  op.phase = phase;
}
// Call only under serial(), or during initial loading before handlers can use bindings.
function markClose(op: Operation, kind: CloseIntent['kind'], discard = false) {
  const previous = op.closeIntent;
  if (!previous || (kind === 'cancel' && previous.kind !== 'cancel') || (kind === 'fault' && !op.faulted)) {
    op.closeIntent = { kind: previous?.kind === 'cancel' ? 'cancel' : kind,
      pagePending: !!op.recorder.documentInstanceId, capturePending: !!op.capture?.leaseId,
      discard: discard || !!previous?.discard };
  } else if (discard) previous.discard = true;
  if (kind === 'fault') { op.faulted = true; op.phase = 'failed'; }
  else if (!op.faulted) {
    if (op.closeIntent!.kind === 'cancel') op.phase = 'cancelled';
    else if (['preparing', 'recording'].includes(op.phase)) op.phase = 'draining';
  }
}
const deliveries = new WeakMap<CloseIntent, Promise<void>>();
function deliverClose(op: Operation): Promise<void> {
  const intent = op.closeIntent;
  if (!intent || operation !== op) return Promise.resolve();
  const existing = deliveries.get(intent); if (existing) return existing;
  const delivery = (async () => {
    const acknowledge = (key: 'pagePending' | 'capturePending') => serial(async () => {
      if (operation !== op || op.closeIntent !== intent) return;
      intent[key] = false; await persist();
    });
    await Promise.all([
      (async () => {
        if (!intent.capturePending) return;
        try {
          const reply = await captureCommand(op, { action: 'STOP', leaseId: op.capture!.leaseId!,
            flush: intent.kind === 'stop', disable: !!op.faulted });
          if (!record(reply) || reply.ok !== true) throw new Error('原采音侧栏未确认关闭。');
          await acknowledge('capturePending');
        } catch { /* Keep the original lease pending; no acknowledgement was received. */ }
      })(),
      (async () => {
        if (!intent.pagePending) return;
        try {
          const reply = await command(op, intent.kind === 'cancel' ? 'CANCEL' : 'STOP');
          if (!record(reply.data) || reply.data.closedOperationId !== op.operationId) throw new Error('原录音页未确认关闭操作。');
          await acknowledge('pagePending');
        } catch {
          // A removed tab is conclusive; a timeout, navigation or mismatched receipt is not.
          const tab = await browser.tabs.get(op.recorder.tabId).catch(() => null);
          if (!tab) await acknowledge('pagePending');
        }
      })(),
    ]);
    await serial(async () => {
      if (operation !== op || op.closeIntent !== intent) return;
      if (!intent.pagePending && !intent.capturePending && (intent.kind === 'cancel' || intent.discard)) operation = null;
      await persist();
    });
  })().finally(() => { deliveries.delete(intent); });
  deliveries.set(intent, delivery); return delivery;
}
async function captureCommand(op: Operation, data: CaptureRequest) {
  if (!op.capture) throw new Error('此操作未绑定侧栏。');
  const body = message('CAPTURE_REQUEST', { operationId: op.operationId,
    documentInstanceId: op.capture.documentInstanceId, data });
  let timer: ReturnType<typeof setTimeout>;
  try { return await Promise.race([browser.runtime.sendMessage(body), new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error('原采音侧栏未响应。')), data.action === 'PREPARE' ? 15000 : 4000);
  })]); } finally { clearTimeout(timer!); }
}
async function pageAudio(op: Operation, body: Envelope) {
  const request = { ...body, documentInstanceId: op.recorder.documentInstanceId, sessionId: op.sessionId };
  const reply = await browser.tabs.sendMessage(op.recorder.tabId, request, { frameId: 0 });
  if (!envelope(reply) || reply.requestId !== request.requestId || reply.type !== request.type ||
    reply.documentInstanceId !== request.documentInstanceId || reply.operationId !== op.operationId)
    throw new Error('原录音网页未确认音频。');
  return reply.data;
}
async function emergency(op: Operation, reason: string) {
  const applied = await serial(async () => {
    if (operation !== op) return false;
    // Persist fault AND both original endpoints before the first external shutdown RPC.
    markClose(op, 'fault'); op.error = reason;
    const binding = op.capture && sidebars[String(op.capture.windowId)];
    if (binding && binding.documentInstanceId === op.capture?.documentInstanceId) binding.enabled = false;
    await persist(); return true;
  });
  if (!applied) return;
  // Hardware release and page handoff are independent: a missing page cannot block the sidebar.
  const closing = deliverClose(op);
  await browser.action.setBadgeText({ text: '!' }); await browser.action.setTitle({ title: reason });
  await closing;
}
function sidebarSender(sender: browser.Runtime.MessageSender) {
  return sidebarMode && sender.id === browser.runtime.id && !sender.tab?.incognito &&
    (sender.frameId === undefined || sender.frameId === 0) && sender.url === browser.runtime.getURL('sidebar.html');
}
function boundSidebar(raw: Envelope, sender: browser.Runtime.MessageSender) {
  const op = operation;
  if (!sidebarSender(sender) || !op?.capture || raw.operationId !== op.operationId ||
    raw.documentInstanceId !== op.capture.documentInstanceId) throw new Error('采音侧栏绑定已失效。');
  return op;
}
async function sidebarMessage(raw: Envelope, sender: browser.Runtime.MessageSender) {
  if (['SIDEBAR_HELLO', 'SIDEBAR_STATE', 'SIDEBAR_CLOSED'].includes(raw.type)) {
    if (!record(raw.data) || !Number.isInteger(raw.data.windowId) || !identifier(raw.documentInstanceId)) throw new Error('侧栏身份无效。');
    const windowId = Number(raw.data.windowId), win = await browser.windows.get(windowId);
    if (win.incognito || (sender.tab && sender.tab.windowId !== windowId)) throw new Error('侧栏窗口无效。');
    const binding: CaptureBinding = { windowId, documentInstanceId: raw.documentInstanceId, enabled: raw.data.enabled === true };
    const op = operation;
    if (op?.capture?.windowId === windowId && op.capture.documentInstanceId !== binding.documentInstanceId && raw.type === 'SIDEBAR_HELLO')
      await emergency(op, '采音侧栏已重建；原操作不会自动重新开麦，请检查录音页。');
    await serial(async () => {
      if (raw.type === 'SIDEBAR_CLOSED') {
        if (sidebars[String(windowId)]?.documentInstanceId === binding.documentInstanceId) delete sidebars[String(windowId)];
      } else if (raw.type === 'SIDEBAR_HELLO' || sidebars[String(windowId)]?.documentInstanceId === binding.documentInstanceId) sidebars[String(windowId)] = binding;
      await persist();
    });
    if (raw.type === 'SIDEBAR_CLOSED' && op?.capture?.documentInstanceId === binding.documentInstanceId)
      await emergency(op, '采音侧栏已关闭；已接收内容仍在服务历史中，请打开录音页检查。');
    return { origin, captureKind: 'sidebar' };
  }
  const op = boundSidebar(raw, sender);
  if (raw.type === 'AUDIO_FRAME') {
    if (!audioFrame(raw.data) || raw.data.leaseId !== op.capture!.leaseId || op.faulted ||
      (op.closeIntent && op.closeIntent.kind !== 'stop') || !await permitted()) throw new Error('音频 lease 或服务权限失效。');
    return pageAudio(op, raw);
  }
  if (raw.type === 'CAPTURE_ERROR') {
    if (!record(raw.data) || raw.data.leaseId !== op.capture!.leaseId) throw new Error('音频 lease 失效。');
    const reason = typeof raw.data.error === 'string' ? raw.data.error : '侧栏采音已中断。';
    await pageAudio(op, raw).catch(() => {}); await emergency(op, reason); return { ok: true };
  }
  if (raw.type === 'STOP') {
    await serial(async () => { if (operation === op) { markClose(op, 'stop'); await persist(); } });
    await deliverClose(op); return { ok: true };
  }
  if (raw.type === 'CANCEL') { await cancel(); return { ok: true }; }
  throw new Error('侧栏消息无效。');
}
async function permitted() { return !!origin && await browser.permissions.contains({ origins: [hostPermission(origin)] }); }
async function openRecorder() {
  if (!await permitted()) throw new Error("网站权限未授予，请重新连接此服务。");
  if (recorder) {
    const tab = await browser.tabs.get(recorder.tabId).catch(() => null);
    if (tab && servicePage(tab.url, origin, true)) { await browser.tabs.update(recorder.tabId, { active: true }); return; }
  }
  if (operation) throw new Error("原录音页已关闭，请先放弃旧操作；原稿仍在历史中。");
  const tab = await browser.tabs.create({ url: origin + RECORDER_PATH });
  await serial(async () => { recorder = { tabId: tab.id!, exactOrigin: origin, documentInstanceId: "" }; await persist(); });
}
async function connect(value: string) {
  const next = serviceOrigin(value);
  await serial(async () => {
    if (operation || connecting) throw new Error("请先结束并放弃当前操作。");
    connecting = true;
  });
  try {
    if (!await browser.permissions.contains({ origins: [hostPermission(next)] })) throw new Error("网站权限未授予。");
    await browser.scripting.unregisterContentScripts({ ids: ["dictation-recorder"] }).catch(() => {});
    const host = hostPermission(next).slice(0, -2);
    await browser.scripting.registerContentScripts([{ id: "dictation-recorder", js: ["recorder.js"],
      matches: ["/dictation", "/dictation?*", "/dictation/extension", "/dictation/extension?*"].map(path => host + path), runAt: "document_idle", allFrames: false }]);
    const old = origin;
    await serial(async () => { origin = next; recorder = null; await browser.storage.local.set({ serviceOrigin: origin }); await persist(); });
    if (old && old !== next && hostPermission(old) !== hostPermission(next)) await browser.permissions.remove({ origins: [hostPermission(old)] });
    await openRecorder();
  } finally { connecting = false; }
}
async function toggle() {
  // Sidebar must belong to the currently focused ordinary browser window.
  const win = sidebarMode && !operation ? await browser.windows.getLastFocused() : null;
  // Claim an intent under a short queue; no browser RPC waits inside the state queue.
  const intent = await serial(async () => {
    if (connecting) throw new Error("正在更换服务，请稍候。");
    if (operation) {
      if (!operation.closeIntent && !["preparing", "recording"].includes(operation.phase)) return null;
      markClose(operation, 'stop'); await persist(); return { type: "STOP" as const, op: operation };
    }
    if (!recorder) throw new Error("请先打开录音页并启用扩展控制。");
    const capture = win ? sidebars[String(win.id)] : undefined;
    if (sidebarMode && (!win?.focused || win.incognito || !capture?.enabled)) throw new Error('请在当前窗口打开采音侧栏并授权试音。');
    operation = { operationId: id(), recorder: { ...recorder, documentInstanceId: '' }, phase: "preparing", ...(capture ? { capture: { ...capture } } : {}) };
    await persist(); return { type: "START" as const, op: operation };
  });
  if (!intent) return;
  const { op } = intent;
  // Shutdown remains allowed after host permission withdrawal; never gate it on a new grant.
  if (intent.type === 'STOP') { await deliverClose(op); return; }
  try {
    if (!await permitted()) throw new Error("网站权限未授予，请重新连接；原录音页可独立停止。");
    const request = message("HELLO");
    const hello = await browser.tabs.sendMessage(op.recorder.tabId, request, { frameId: 0 });
    if (!record(hello) || hello.requestId !== request.requestId || hello.protocolVersion !== 1 || !identifier(hello.documentInstanceId) || !record(hello.data) || !hello.data.enabled)
      throw new Error("请在录音页启用扩展控制并试音。");
    const send = await serial(async () => {
      if (operation !== op || op.closeIntent || op.phase !== "preparing") return false;
      // Only an explicit NEW user operation may bind a new page incarnation.
      op.recorder.documentInstanceId = hello.documentInstanceId as string;
      recorder = { ...op.recorder }; await persist(); return true;
    });
    if (send) {
      const reply = await command(op, "START");
      if (record(reply.data) && typeof reply.data.error === "string" && reply.data.error) await emergency(op, reply.data.error);
    }
  } catch (cause) { await emergency(op, cause instanceof Error ? cause.message : "录音连接失败，请打开录音页检查。"); }
}
async function cancel(discard = false) {
  const op = await serial(async () => {
    const current = operation; if (!current) return null;
    // Discarding terminal history must not turn it into a backend cancellation. Any
    // unfinished fault/stop handoff still has to complete before its bindings can go.
    if (discard && ['completed', 'cancelled', 'failed', 'interrupted'].includes(current.phase)) {
      if (current.closeIntent) current.closeIntent.discard = true;
      else current.closeIntent = { kind: 'stop', pagePending: !!current.recorder.documentInstanceId,
        capturePending: !!current.capture?.leaseId, discard: true };
    } else markClose(current, 'cancel', true);
    await persist(); return current;
  });
  if (op) await deliverClose(op);
}
async function disconnect() {
  if (operation) throw new Error("请先取消当前操作，并在录音页确认麦克风已停止。");
  const old = origin;
  await browser.scripting.unregisterContentScripts({ ids: ["dictation-recorder"] }).catch(() => {});
  await serial(async () => { origin = ""; recorder = null; await browser.storage.local.remove("serviceOrigin"); await persist(); });
  if (old) await browser.permissions.remove({ origins: [hostPermission(old)] });
}
function uiSender(sender: browser.Runtime.MessageSender) {
  return sender.id === browser.runtime.id && !sender.tab?.incognito &&
    ["popup.html", "options.html"].some(path => sender.url?.split("?")[0] === browser.runtime.getURL(path));
}
const uiTypes: MessageType[] = ["CONNECT", "DISCONNECT", "OPEN_RECORDER", "TOGGLE", "CANCEL", "DISCARD", "SHORTCUTS", "UI_STATE"];
browser.runtime.onMessage.addListener((raw: unknown, sender: browser.Runtime.MessageSender) => {
  if (sender.id !== browser.runtime.id || !envelope(raw)) return undefined;
  return (async () => {
    await loaded;
    if (sidebarSender(sender) && !['OPEN_RECORDER'].includes(raw.type)) return sidebarMessage(raw, sender);
    if (uiSender(sender) || (sidebarSender(sender) && raw.type === 'OPEN_RECORDER')) {
      await ready;
      if (!uiTypes.includes(raw.type)) throw new Error("扩展界面消息不受支持。");
      if (raw.type === "CONNECT") {
        if (!record(raw.data) || typeof raw.data.origin !== "string") throw new Error("服务地址无效。");
        await connect(raw.data.origin);
      }
      if (raw.type === "DISCONNECT") await disconnect();
      if (raw.type === "OPEN_RECORDER") await openRecorder();
      if (raw.type === "TOGGLE") await toggle();
      if (["CANCEL", "DISCARD"].includes(raw.type)) await cancel(raw.type === "DISCARD");
      if (raw.type === "SHORTCUTS") return browser.commands.getAll();
      return { origin, recorder, operation, sidebars, captureKind: sidebarMode ? "sidebar" : "webpage", permitted: await permitted(), protocolVersion: 1, version: browser.runtime.getManifest().version };
    }
    // After an SPA route change, the originally bound document may only close its existing lease.
    // It cannot PREPARE/START from the normal page or another route.
    const draining = operation && sender.tab?.id === operation.recorder.tabId && raw.operationId === operation.operationId &&
      raw.documentInstanceId === operation.recorder.documentInstanceId && !!sender.url && new URL(sender.url).origin === origin &&
      ((raw.type === 'CAPTURE_REQUEST' && captureRequest(raw.data) && raw.data.action === 'STOP') ||
        (raw.type === 'STATE' && record(raw.data) && ['draining', 'completed', 'cancelled', 'failed', 'interrupted'].includes(String(raw.data.phase))));
    if (!sender.tab || sender.tab.incognito || sender.frameId !== 0 || (!servicePage(sender.url, origin) && !draining)) throw new Error("未授权来源。");
    if (raw.type === 'RECORDER_HELLO' && servicePage(sender.url, origin, true)) {
      if (!identifier(raw.documentInstanceId) || !await permitted()) throw new Error('服务页身份或权限无效。');
      const op = operation;
      if (op && op.recorder.tabId === sender.tab.id && op.recorder.documentInstanceId && op.recorder.documentInstanceId !== raw.documentInstanceId)
        await emergency(op, '录音网页已刷新，原采音已请求停止；请重新启用控制。');
      return { captureKind: sidebarMode ? 'sidebar' : 'webpage' };
    }
    if (raw.type === "HELLO") return { protocolVersion: 1, connected: await permitted(), version: browser.runtime.getManifest().version,
      browser: navigator.userAgent.includes("Firefox/") ? "Firefox" : "Chrome" };
    if (raw.type === 'CAPTURE_REQUEST') {
      const op = operation;
      if (!op?.capture || sender.tab.id !== op.recorder.tabId || raw.documentInstanceId !== op.recorder.documentInstanceId ||
        raw.operationId !== op.operationId || (!servicePage(sender.url, origin, true) && !draining) || !captureRequest(raw.data)) throw new Error('侧栏请求绑定无效。');
      const data = raw.data;
      if (data.action !== 'STOP') {
        await ready;
        if (!await permitted() || op.faulted || op.closeIntent) throw new Error('服务权限或操作已停止，不能重新开麦。');
      }
      await serial(async () => {
        if (operation !== op) throw new Error('操作已结束。');
        if (['PREPARE', 'START'].includes(data.action) && (op.closeIntent || !['preparing', 'recording'].includes(op.phase))) throw new Error('操作已停止，不能重新开麦。');
        if (data.action === 'PREPARE' && !op.capture!.leaseId) op.capture!.leaseId = data.leaseId;
        if (op.capture!.leaseId !== data.leaseId) throw new Error('音频 lease 不匹配。');
        await persist();
      });
      return captureCommand(op, data);
    }
    return serial(async () => {
      const op = operation;
      if (!op || sender.tab!.id !== op.recorder.tabId || raw.documentInstanceId !== op.recorder.documentInstanceId ||
        raw.operationId !== op.operationId || (!servicePage(sender.url, origin, true) && !draining)) throw new Error("录音操作绑定已失效。");
      if (raw.type !== "STATE" || !record(raw.data) || !phases.includes(String(raw.data.phase))) throw new Error("录音状态消息无效。");
      if (op.sessionId && raw.sessionId !== op.sessionId) throw new Error("会话绑定不匹配。");
      op.sessionId ??= raw.sessionId;
      // A late preparing/recording update cannot revoke an already recorded stop intent.
      observe(op, String(raw.data.phase));
      if (!op.faulted && !op.closeIntent && typeof raw.data.error === "string") op.error = raw.data.error;
      await persist(); return { accepted: true };
    });
  })();
});
browser.commands.onCommand.addListener(name => {
  // insert-pending remains declared for future S2; it cannot accidentally start recording.
  if (name !== "toggle-dictation") return;
  void ready.then(toggle).catch(cause => failure(cause instanceof Error ? cause.message : "请打开扩展检查连接。"));
});
browser.tabs.onRemoved.addListener(tabId => {
  void ready.then(async () => {
    if (operation?.recorder.tabId === tabId) await emergency(operation, "录音页已关闭，请重新打开；已接收结果可在历史查看。");
    if (recorder?.tabId === tabId && !operation) await serial(async () => { recorder = null; await persist(); });
  });
});
browser.permissions.onRemoved.addListener(() => {
  void ready.then(async () => {
    if (origin && !await permitted()) {
      await browser.scripting.unregisterContentScripts({ ids: ["dictation-recorder"] }).catch(() => {});
      if (operation) await emergency(operation, "网站权限已撤回，采音已请求停止；请回原录音页检查并独立停止麦克风。");
    }
  });
});

browser.tabs.onUpdated?.addListener((tabId, change) => {
  if (!change.url && change.status !== 'loading') return;
  void ready.then(async () => {
    const op = operation;
    if (op?.recorder.tabId === tabId) await emergency(op, '录音网页已导航或刷新；采音已请求停止，已接收内容仍可在历史查看。');
  });
});
browser.windows?.onRemoved?.addListener(windowId => {
  void ready.then(async () => {
    const op = operation;
    if (op?.capture?.windowId === windowId) await emergency(op, '采音窗口已关闭；请打开服务录音页检查已接收内容。');
    await serial(async () => { delete sidebars[String(windowId)]; await persist(); });
  });
});
