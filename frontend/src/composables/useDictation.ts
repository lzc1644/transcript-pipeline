import { computed, inject, onBeforeUnmount, provide, ref, type InjectionKey } from "vue";
import { DictationSelection, type SelectionToken } from "./dictationSelection";
import { PcmCapture } from "./dictationCapture";
import { createLocalRecorder } from "./dictationDrafts";
import { validateTerms } from "./dictationTerms";
import { DictationPlayback } from "./dictationPlayback";

export interface Utterance {
  id: string; revision: number; status: string; text: string; raw_text: string; preview: string;
  edited: boolean; error?: string; suggestion?: string; needs_review: boolean; start_sample?: number; end_sample?: number | null;
}
export interface DictationConfig {
  max_upload_mb: number; max_upload_minutes: number; cleanup_timeout_seconds: number;
  cleanup_model: string; document_cleanup_model: string;
  cleanup_reasoning_effort: string; document_reasoning_effort: string;
  document_cleanup_timeout_seconds: number; decode_timeout_seconds: number; upload_receive_timeout_seconds: number;
}
export interface DictationSession {
  id: string; status: string; phase: string; revision: number; event_sequence: number; next_sequence: number; samples: number;
  created_at: string; mode: string; utterances: Utterance[]; draft_text: string; error?: string;
  document_text?: string; document_cleanup_error?: string; phase_key?: string;
  progress?: { total_samples?: number; transcribed_samples: number; asr_failed: number; cleanup_total: number; cleanup_done: number; cleanup_failed: number };
  document_suggestion?: { id: string; original: string; text: string; metrics?: { strategy: string; chunk_count: number } };
  config: { cleanup_model: string; document_cleanup_model?: string; reasoning_effort?: string;
    document_reasoning_effort?: string; terms: string[]; cleanup: boolean;
    cleanup_timeout_seconds?: number; document_cleanup_timeout_seconds?: number };
}
export const activeStates = ["created", "preparing", "listening", "pausing", "paused", "draining"];

/** Created once by App, never by a route or by the dock. History is a separate selection. */
function createDictationController() {
  const session = ref<DictationSession | null>(null);
  const recordingSession = ref<DictationSession | null>(null);
  const selection = new DictationSelection(), recording = new DictationSelection();
  const history = ref<DictationSession[]>([]), serverConfig = ref<DictationConfig | null>(null);
  const error = ref(""), connection = ref("");
  const busy = ref(false), cleanupScope = ref<string | null>(null);
  const level = ref(0), capturing = ref(false);
  const captureAction = ref<"" | "starting" | "pausing" | "resuming" | "stopping">("");
  const active = computed(() => !!session.value && activeStates.includes(session.value.status));
  const recognizing = computed(() => !!recordingSession.value && activeStates.includes(recordingSession.value.status));
  const ownsCapture = computed(() => recognizing.value && recordingSession.value?.mode === "live");
  const partial = computed(() => session.value?.utterances.filter(u => ["partial", "finalizing", "asr_failed"].includes(u.status)) ?? []);
  const captureLabel = computed(() => {
    const labels = { starting: "正在准备麦克风与识别模型", pausing: "正在暂停 · 麦克风已关闭", resuming: "正在继续 · 等待麦克风与会话确认", stopping: "正在结束 · 麦克风已关闭" };
    if (captureAction.value) return labels[captureAction.value];
    const state = recordingSession.value;
    if (ownsCapture.value && state?.status === "listening" && !capturing.value) return "麦克风已关闭 · 暂停未确认，请检查连接或结束";
    if (state?.status === "paused") return state.mode === "live" ? "已暂停 · 麦克风已关闭" : "转写已暂停 · 识别资源保留";
    return state?.phase ?? "准备开始";
  });
  const selectedDeviceId = ref(localStorage.getItem("dictation:microphone") ?? "");
  const microphoneDevices = ref<MediaDeviceInfo[]>([]), deviceBusy = ref(false), inputWarning = ref("");
  const audio = new PcmCapture();
  const playback = new DictationPlayback(() => !audio.isOwner("live") && !audio.isOwner("local") && !audio.isOwner("probe"));
  audio.beforeCapture = () => playback.stop();
  audio.deviceId = selectedDeviceId.value;
  audio.onDevice = id => {
    selectedDeviceId.value = id;
    try { localStorage.setItem("dictation:microphone", id); }
    catch { error.value = "无法持久保存麦克风选择；本页仍可使用，请检查浏览器存储设置。"; }
  };
  const local = createLocalRecorder(audio, () => !ownsCapture.value && !captureAction.value && !deviceBusy.value);
  const uploadProgress = ref<{ loaded: number; total: number } | null>(null);
  const uploadUncertain = ref(false);
  let uploading: XMLHttpRequest | null = null;
  let socket: WebSocket | null = null;
  let sequence = 0, samples = 0, operation = 0, creation = 0, audioGeneration = 0;
  let startingCapture = -1;
  let pending: { seq: number; packet: ArrayBuffer }[] = [];
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let disposed = false, polling = false;
  let waiter: { expected: string; resolve: () => void; reject: (cause: Error) => void } | null = null;

  async function api(path: string, init: RequestInit = {}) {
    const response = await fetch(`/api/dictation${path}`, {
      ...init, headers: { ...(typeof init.body === "string" ? { "Content-Type": "application/json" } : {}), ...init.headers },
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(typeof body.detail === "string" ? body.detail : `请求失败（${response.status}）`);
    }
    return response.json();
  }
  function failWaiter(message = "采音操作已结束") { waiter?.reject(new Error(message)); waiter = null; }
  function waitForStatus(expected: string) {
    failWaiter();
    return new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => {
        waiter = null;
        reject(new Error("服务端未在期限内确认；麦克风保持关闭，请检查连接或结束听写。"));
      }, 12000);
      waiter = { expected, resolve: () => { clearTimeout(timer); resolve(); }, reject: cause => { clearTimeout(timer); reject(cause); } };
    });
  }
  async function releaseAudio(flush = true) {
    capturing.value = false; level.value = 0;
    try { await audio.stop("live", flush); }
    finally { audioGeneration++; }
  }
  function applyView(value: DictationSession, token = selection.token()) {
    if (!disposed && selection.accept(value, token)) session.value = value;
  }
  function applyRecording(value: DictationSession, token = recording.token()) {
    if (disposed || !recording.accept(value, token)) return;
    recordingSession.value = value;
    if (value.error) error.value = value.error;
    if (waiter?.expected === value.status) { const waiting = waiter; waiter = null; waiting.resolve(); }
    if (value.mode === "live" && (!activeStates.includes(value.status) || value.status === "draining")) {
      if (captureAction.value === "starting" || captureAction.value === "resuming") ++operation;
      failWaiter("会话已结束，不能继续采音");
      if (audio.isOwner("live")) void releaseAudio(false).catch(cause => { error.value = String(cause); });
      captureAction.value = "";
    } else if (value.status === "listening" && captureAction.value === "starting") {
      const generation = operation;
      void beginCapture(generation, token).catch(cause => captureFailure(cause, generation));
    }
    if (value.id === selection.token().id) applyView(value);
  }
  function apply(value: DictationSession, token: SelectionToken) {
    applyView(value, token);
    if (value.id === recording.token().id) applyRecording(value);
  }
  function choose(id: string | null) {
    session.value = null;
    return selection.select(id);
  }
  async function refresh() {
    const token = selection.token();
    if (token.id) apply(await api(`/sessions/${token.id}`), token);
  }
  async function poll() {
    if (disposed || polling) return;
    polling = true;
    try {
      const token = recording.token();
      if (token.id && recognizing.value) applyRecording(await api(`/sessions/${token.id}`), token);
      await refresh();
    } catch { /* A poll cannot claim a successful pause or reopen the microphone. WS reports connectivity. */ }
    finally { polling = false; }
  }
  async function loadHistory() { history.value = await api("/sessions"); }
  async function loadConfig() { serverConfig.value = await api("/config"); }
  async function select(id: string) {
    const token = choose(id);
    apply(await api(`/sessions/${id}`), token);
  }
  function showRecording() {
    const value = recordingSession.value;
    if (value) applyView(value, choose(value.id));
  }
  function sendPCM(pcm: ArrayBuffer) {
    const packet = new ArrayBuffer(12 + pcm.byteLength);
    const view = new DataView(packet);
    view.setUint32(0, sequence, true); view.setBigUint64(4, BigInt(samples), true);
    new Uint8Array(packet, 12).set(new Uint8Array(pcm));
    pending.push({ seq: sequence++, packet }); samples += pcm.byteLength / 2;
    if (socket?.readyState === WebSocket.OPEN) socket.send(packet);
    if (pending.length >= 62 && captureAction.value !== "stopping") {
      error.value = "未确认音频已接近5秒，已停止采音；请保留本页等待已接收内容完成。";
      if (recordingSession.value) void stop(recordingSession.value.id);
    }
  }
  async function beginCapture(generation: number, token: SelectionToken) {
    if (startingCapture === generation || !audio.isOwner("live") || !audio.prepared || !recording.current(token) || generation !== operation) return;
    startingCapture = generation;
    const epoch = ++audioGeneration;
    await audio.start("live", (pcm, volume) => {
      if (disposed || epoch !== audioGeneration || !recording.current(token)) return;
      level.value = Math.min(1, volume * 5); sendPCM(pcm);
    });
    if (generation !== operation || disposed || !recording.current(token)) return;
    capturing.value = true; captureAction.value = "";
  }
  function connect(token: SelectionToken) {
    if (disposed || !recording.current(token)) return;
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const ownedSocket = new WebSocket(`${scheme}://${location.host}/api/dictation/sessions/${token.id}/stream`);
    socket = ownedSocket;
    ownedSocket.onmessage = event => {
      if (!recording.current(token) || socket !== ownedSocket || disposed) return;
      const message = JSON.parse(event.data);
      if (message.type === "session.snapshot") {
        const next = message.snapshot.next_sequence;
        pending = pending.filter(item => item.seq >= next);
        if (next > sequence || (pending.length ? pending[0].seq !== next : next !== sequence)) {
          error.value = "音频恢复窗口已失效，不能带缺口续录。"; void stop(token.id); return;
        }
        if (activeStates.includes(message.snapshot.status)) for (const item of pending) ownedSocket.send(item.packet);
        connection.value = "";
      }
      if (message.snapshot) applyRecording(message.snapshot, token);
      if (message.type === "audio.ack") pending = pending.filter(item => item.seq >= message.next_sequence);
      if (message.type === "asr.partial" && recordingSession.value) {
        const state = recordingSession.value;
        if (message.event_sequence <= state.event_sequence) return;
        state.event_sequence = message.event_sequence;
        recording.accept(state, token);
        let utterance = state.utterances.find(u => u.id === message.utterance_id);
        if (!utterance) {
          utterance = { id: message.utterance_id, revision: message.revision, status: "partial", text: "", raw_text: "", preview: "", edited: false, needs_review: false };
          state.utterances.push(utterance);
        }
        utterance.preview = message.text;
        if (state.id === selection.token().id) applyView(state);
      }
      if (message.type === "session.error") {
        error.value = message.error; failWaiter(message.error);
        void releaseAudio(false).catch(() => {});
      }
      if (message.type === "session.warning") connection.value = message.message;
    };
    ownedSocket.onclose = () => {
      if (!disposed && recording.current(token) && socket === ownedSocket && recognizing.value && captureAction.value !== "stopping") {
        connection.value = "网络中断，正在短时重连；暂停未确认时麦克风保持关闭";
        reconnectTimer = setTimeout(() => connect(token), 700);
      }
    };
    ownedSocket.onerror = () => { if (recording.current(token)) connection.value = "网络连接异常，请检查网络；不会自动重新开启麦克风"; };
  }
  function sendControl(type: string) {
    if (socket?.readyState !== WebSocket.OPEN) throw new Error("连接中断，暂停未确认；麦克风已关闭。等待重连后重试，或结束听写。");
    socket.send(JSON.stringify({ type, next_sequence: sequence, samples }));
  }
  async function captureFailure(cause: unknown, generation: number) {
    if (generation !== operation || disposed) return;
    const detail = cause instanceof Error ? cause.message : String(cause);
    await stop(recordingSession.value?.id ?? null);
    error.value = detail;
  }
  function deviceFailure(cause: Error) {
    // Preserve the tail and the same session; a disconnected device is never silently replaced.
    error.value = cause.message;
    if (recordingSession.value?.status === "listening" && !captureAction.value) void pause().catch(() => {});
    else void captureFailure(cause, operation);
  }
  async function start(terms: string[], cleanup: boolean, cleanupModel = "", documentCleanupModel = "",
    cleanupEffort = "", documentEffort = "") {
    if (busy.value || recognizing.value || captureAction.value || local.active.value || local.busy.value || deviceBusy.value || uploadUncertain.value) return;
    const generation = ++operation, taskGeneration = ++creation;
    recording.select(null); recordingSession.value = null;
    socket?.close(); socket = null;
    choose(null); error.value = ""; connection.value = ""; busy.value = true; captureAction.value = "starting";
    try {
      terms = validateTerms(terms);
      await audio.prepare("live", deviceFailure);
      if (disposed || generation !== operation || taskGeneration !== creation) return;
      const state = await api("/sessions", { method: "POST", body: JSON.stringify({ terms, cleanup,
        cleanup_model: cleanupModel || null, document_cleanup_model: documentCleanupModel || null,
        cleanup_reasoning_effort: cleanupEffort || null, document_reasoning_effort: documentEffort || null }) });
      if (disposed || generation !== operation || taskGeneration !== creation) {
        await api(`/sessions/${state.id}/cancel`, { method: "POST" }); return;
      }
      const token = recording.select(state.id);
      sequence = state.next_sequence; samples = state.samples; pending = [];
      applyView(state, choose(state.id)); applyRecording(state, token);
      connect(token);
    } catch (cause) {
      if (generation === operation) {
        await releaseAudio(false); captureAction.value = "";
        error.value = cause instanceof Error ? cause.message : String(cause);
      }
    } finally { busy.value = false; }
  }
  async function pause() {
    if (!ownsCapture.value) throw new Error("请返回原录音窗口暂停；本窗口不拥有麦克风。");
    if (captureAction.value || recordingSession.value?.status === "paused") return;
    const generation = ++operation;
    captureAction.value = "pausing";
    try {
      await releaseAudio(true);
      if (generation !== operation || disposed) return;
      const confirmed = waitForStatus("paused");
      try { sendControl("pause"); } catch (cause) { failWaiter(String(cause)); }
      await confirmed;
    } catch (cause) {
      if (generation === operation) error.value = `暂停未确认：${cause instanceof Error ? cause.message : String(cause)}`;
    } finally { if (generation === operation) captureAction.value = ""; }
  }
  async function resume() {
    if (!ownsCapture.value) throw new Error("请返回原录音窗口继续；本窗口不能取得原窗口的采音所有权。");
    if (captureAction.value || recordingSession.value?.status !== "paused") return;
    const generation = ++operation, token = recording.token();
    captureAction.value = "resuming"; error.value = "";
    try {
      await audio.prepare("live", deviceFailure);
      if (disposed || generation !== operation) return;
      const latest: DictationSession = await api(`/sessions/${token.id}`);
      applyRecording(latest, token);
      if (generation !== operation || latest.status !== "paused") throw new Error("会话已停止或不再可恢复");
      sequence = latest.next_sequence; samples = latest.samples; pending = [];
      const confirmed = waitForStatus("listening");
      try { sendControl("resume"); } catch (cause) { failWaiter(String(cause)); }
      await confirmed;
      if (generation === operation && !disposed) await beginCapture(generation, token);
    } catch (cause) {
      if (generation === operation) {
        await releaseAudio(true).catch(() => {});
        // If resume reached the server but local startup failed, restore a real pause.
        if (["listening"].includes(recordingSession.value?.status ?? "") && socket?.readyState === WebSocket.OPEN) {
          const confirmed = waitForStatus("paused"); sendControl("pause");
          await confirmed.catch(() => {});
        }
        error.value = `未能继续：${cause instanceof Error ? cause.message : String(cause)}`;
      }
    } finally { if (generation === operation) captureAction.value = ""; }
  }
  async function stop(id: string | null) {
    // A historical/view-only stop must never target the app's different active task.
    if (id && id !== recording.token().id) {
      const state = await api(`/sessions/${id}/stop`, { method: "POST" });
      if (id === selection.token().id) applyView(state);
      return;
    }
    if (captureAction.value === "stopping") return;
    const owned = id === recording.token().id;
    const generation = ++operation;
    failWaiter(); captureAction.value = "stopping";
    try {
      if ((owned && recordingSession.value?.mode === "live") || !id) await releaseAudio(true).catch(cause => { error.value = String(cause); });
      if (reconnectTimer) clearTimeout(reconnectTimer); reconnectTimer = null;
      if (id) {
        if (owned && socket?.readyState === WebSocket.OPEN && pending.length < 62) socket.send(JSON.stringify({ type: "stop" }));
        else {
          if (owned && pending.length) error.value = "连接中断时有未确认音频，未确认部分可能未被接收；已接收内容会继续完成。";
          const state = await api(`/sessions/${id}/stop`, { method: "POST" });
          if (id === recording.token().id) applyRecording(state);
          if (id === selection.token().id) applyView(state);
        }
      }
    } catch (cause) { error.value = String(cause); }
    finally { if (generation === operation) captureAction.value = ""; }
  }
  async function cancel(id: string) {
    if (id !== recording.token().id) {
      const state = await api(`/sessions/${id}/cancel`, { method: "POST" });
      if (id === selection.token().id) applyView(state);
      return;
    }
    ++operation; failWaiter(); captureAction.value = "stopping";
    if (recordingSession.value?.mode === "live") await releaseAudio(false);
    try {
      if (id) {
        const state = await api(`/sessions/${id}/cancel`, { method: "POST" });
        if (id === recording.token().id) applyRecording(state);
        if (id === selection.token().id) applyView(state);
      }
    } finally { captureAction.value = ""; }
  }
  async function upload(file: File, terms: string[], cleanup: boolean, cleanupModel = "", documentCleanupModel = "",
    cleanupEffort = "", documentEffort = "") {
    if (busy.value || recognizing.value || captureAction.value || uploadUncertain.value) return;
    const taskGeneration = ++creation, token = choose(null);
    // A completed predecessor has no cancellation authority over this new task.
    recording.select(null); recordingSession.value = null; socket?.close(); socket = null;
    error.value = ""; busy.value = true;
    try {
      terms = validateTerms(terms);
      await loadConfig();
      const query = new URLSearchParams({ filename: file.name, terms: JSON.stringify(terms), cleanup: String(cleanup) });
      if (cleanupModel) query.set("cleanup_model", cleanupModel);
      if (documentCleanupModel) query.set("document_cleanup_model", documentCleanupModel);
      if (cleanupEffort) query.set("cleanup_reasoning_effort", cleanupEffort);
      if (documentEffort) query.set("document_reasoning_effort", documentEffort);
      const state: DictationSession = await new Promise((resolve, reject) => {
        const xhr = uploading = new XMLHttpRequest();
        uploadProgress.value = { loaded: 0, total: file.size };
        xhr.open("POST", `/api/dictation/uploads?${query}`); xhr.timeout = ((serverConfig.value?.upload_receive_timeout_seconds ?? 1800) + 10) * 1000;
        xhr.setRequestHeader("Content-Type", "application/octet-stream");
        xhr.upload.onprogress = event => { uploadProgress.value = { loaded: event.loaded, total: file.size }; };
        xhr.onload = () => {
          let body; try { body = JSON.parse(xhr.responseText); } catch { uploadUncertain.value = true; reject(new Error("上传结果不明确，请先检查历史；不会自动重复提交。")); return; }
          if (xhr.status >= 200 && xhr.status < 300) resolve(body);
          else reject(new Error(typeof body.detail === "string" ? body.detail : `上传失败（${xhr.status}），原草稿保留`));
        };
        xhr.onerror = xhr.ontimeout = xhr.onabort = () => {
          uploadUncertain.value = (uploadProgress.value?.loaded ?? 0) >= file.size;
          reject(new Error(uploadUncertain.value ? "音频已发送但提交结果不明确，请检查历史后再决定是否重试；草稿保留。" : "上传未完成，原草稿保留，可手动重试。"));
        };
        xhr.send(file);
      });
      if (!disposed && taskGeneration === creation) {
        // Task ownership is independent of what the user chose to read while uploading.
        applyRecording(state, recording.select(state.id));
        if (selection.current(token)) applyView(state, choose(state.id));
      }
      return state;
    } catch (cause) { error.value = cause instanceof Error ? cause.message : String(cause); }
    finally { busy.value = false; uploading = null; uploadProgress.value = null; }
  }
  const fileAction = ref<string | null>(null);
  async function fileControl(id: string, action: "pause" | "resume") {
    if (fileAction.value) return;
    fileAction.value = id;
    try {
      const state = await api(`/sessions/${id}/${action}`, { method: "POST" });
      if (id === recording.token().id) applyRecording(state);
      if (id === selection.token().id) applyView(state);
    } finally { fileAction.value = null; }
  }
  function cancelUpload() { uploading?.abort(); }
  function confirmUploadChecked() { uploadUncertain.value = false; }
  async function edit(u: Utterance, text: string, discardDocument = false) {
    const token = selection.token();
    if (token.id) apply(await api(`/sessions/${token.id}/utterances/${u.id}`, { method: "PATCH", body: JSON.stringify({ text, revision: u.revision, discard_document: discardDocument }) }), token);
  }
  async function editDraft(text: string, revision: number) {
    const token = selection.token();
    if (token.id) apply(await api(`/sessions/${token.id}/draft`, { method: "PATCH", body: JSON.stringify({ text, revision }) }), token);
  }
  async function cleanup(scope: string, utteranceId?: string) {
    if (!session.value) return;
    const token = selection.token();
    const body = { scope, utterance_id: utteranceId, revision: session.value.revision, suggestion_id: session.value.document_suggestion?.id };
    busy.value = true; cleanupScope.value = scope; error.value = "";
    try {
      if (scope !== "apply") await loadConfig();
      apply(await api(`/sessions/${token.id}/cleanup`, { method: "POST", body: JSON.stringify(body) }), token);
    }
    finally { busy.value = false; cleanupScope.value = null; }
  }
  async function remove(id: string) {
    if (recording.token().id === id && recognizing.value) {
      ++operation; failWaiter(); if (recordingSession.value?.mode === "live") await releaseAudio(false);
    }
    await api(`/sessions/${id}`, { method: "DELETE" });
    if (selection.token().id === id) choose(null);
    if (recording.token().id === id) { recording.select(null); recordingSession.value = null; socket?.close(); socket = null; captureAction.value = ""; }
    await loadHistory();
  }
  async function updateDevices() {
    microphoneDevices.value = (await navigator.mediaDevices.enumerateDevices()).filter(device =>
      device.kind === "audioinput" && device.deviceId && !["default", "communications"].includes(device.deviceId));
  }
  async function listMicrophones() {
    if (capturing.value || local.capturing.value || captureAction.value || local.busy.value || deviceBusy.value) throw new Error("请先暂停采音，再列出或切换设备");
    deviceBusy.value = true;
    try {
      await audio.prepare("probe", () => {}, "");
      if (!disposed) {
        await updateDevices();
        if (selectedDeviceId.value && !microphoneDevices.value.some(device => device.deviceId === selectedDeviceId.value)) error.value = "已保存的麦克风不可用，请重新选择；不会自动切换。";
      }
    } finally { await audio.stop("probe", false); deviceBusy.value = false; }
  }
  function chooseMicrophone(id: string) {
    if (capturing.value || local.capturing.value || captureAction.value || deviceBusy.value || local.busy.value) throw new Error("请先暂停采音，再切换麦克风");
    if (!microphoneDevices.value.some(device => device.deviceId === id)) throw new Error("请选择列表中仍可用的麦克风");
    audio.deviceId = id; audio.onDevice(id);
  }
  function deviceChanged() { void updateDevices().catch(() => {}); }
  navigator.mediaDevices?.addEventListener("devicechange", deviceChanged);
  function unload() { ++creation; ++operation; playback.stop(); local.interrupt(); void audio.stop("probe", false); uploading?.abort(); failWaiter(); socket?.close(); void releaseAudio(false); }
  window.addEventListener("pagehide", unload);
  const pollTimer = setInterval(() => {
    inputWarning.value = (capturing.value || local.capturing.value) && audio.silentSeconds >= 5 ? "麦克风持续5秒无明显输入，请检查设备静音、连接和输入音量。" : "";
    void poll();
  }, 1000);
  onBeforeUnmount(() => {
    disposed = true; unload(); selection.select(null); recording.select(null);
    if (reconnectTimer) clearTimeout(reconnectTimer);
    clearInterval(pollTimer); window.removeEventListener("pagehide", unload);
    navigator.mediaDevices?.removeEventListener("devicechange", deviceChanged);
  });
  return { session, recordingSession, local, playback, selectedDeviceId, microphoneDevices, deviceBusy, inputWarning, listMicrophones, chooseMicrophone, fileAction, fileControl, uploadProgress, uploadUncertain, cancelUpload, confirmUploadChecked, history, error, busy, cleanupScope, level, connection, capturing,
    captureAction, captureLabel, active, recognizing, ownsCapture, partial, start, pause, resume, stop, cancel, upload,
    select, showRecording, refresh, loadHistory, serverConfig, loadConfig, edit, editDraft, cleanup, remove };
}
export type DictationController = ReturnType<typeof createDictationController>;
const key: InjectionKey<DictationController> = Symbol("dictation-controller");
export function provideDictation() { const controller = createDictationController(); provide(key, controller); return controller; }
export function useDictation() {
  const controller = inject(key);
  if (!controller) throw new Error("Dictation controller must be provided by App");
  return controller;
}
