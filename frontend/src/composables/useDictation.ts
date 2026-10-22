import { computed, onBeforeUnmount, ref } from "vue";
import { DictationSelection, type SelectionToken } from "./dictationSelection";

export interface Utterance {
  id: string; revision: number; status: string; text: string; raw_text: string; preview: string;
  edited: boolean; error?: string; suggestion?: string; needs_review: boolean;
}
export interface DictationSession {
  id: string; status: string; phase: string; revision: number; event_sequence: number; next_sequence: number; samples: number;
  created_at: string; mode: string; utterances: Utterance[]; draft_text: string; error?: string;
  document_text?: string; document_suggestion?: { id: string; original: string; text: string };
  config: { cleanup_model: string; terms: string[]; cleanup: boolean };
}
const activeStates = ["created", "preparing", "listening", "draining"];

export function useDictation() {
  const session = ref<DictationSession | null>(null);
  const selection = new DictationSelection();
  const history = ref<DictationSession[]>([]);
  const error = ref("");
  const busy = ref(false);
  const level = ref(0);
  const connection = ref("");
  const capturing = ref(false);
  const active = computed(() => !!session.value && activeStates.includes(session.value.status));
  const partial = computed(() => session.value?.utterances.filter(u => ["partial", "finalizing", "asr_failed"].includes(u.status)) ?? []);
  let media: MediaStream | null = null;
  let context: AudioContext | null = null;
  let worklet: AudioWorkletNode | null = null;
  let socket: WebSocket | null = null;
  let sequence = 0, samples = 0;
  let pending: { seq: number; packet: ArrayBuffer }[] = [];
  let flushResolve: (() => void) | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let pollTimer: ReturnType<typeof setInterval> | null = null;
  let disposed = false, stopping = false;

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
  function apply(value: DictationSession, token: SelectionToken) {
    if (disposed || !selection.accept(value, token)) return;
    session.value = value;
    if (value.error) error.value = value.error;
    if (value.status !== "listening" && value.status !== "preparing" && capturing.value) void releaseAudio();
  }
  function choose(id: string | null) {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = null;
    const token = selection.select(id);
    socket?.close(); socket = null;
    pending = [];
    session.value = null;
    return token;
  }
  async function refresh() {
    const token = selection.token();
    if (token.id) apply(await api(`/sessions/${token.id}`), token);
  }
  async function loadHistory() { history.value = await api("/sessions"); }
  async function select(id: string) {
    if (capturing.value || media || busy.value) throw new Error("请先停止当前录音或等待当前操作完成");
    const token = choose(id);
    apply(await api(`/sessions/${id}`), token);
    if (selection.current(token)) connection.value = active.value ? "在录音窗口监听；本窗口同步显示结果" : "";
  }
  async function releaseAudio(flush = false) {
    // Stop hardware tracks first, even when final ASR/LLM still has work.
    media?.getTracks().forEach(track => track.stop());
    media = null;
    capturing.value = false;
    level.value = 0;
    if (flush && worklet) {
      await new Promise<void>(resolve => {
        flushResolve = resolve;
        worklet!.port.postMessage("flush");
        setTimeout(resolve, 300);
      });
    }
    worklet?.disconnect();
    worklet = null;
    const old = context;
    context = null;
    if (old && old.state !== "closed") await old.close();
  }
  function sendPCM(pcm: ArrayBuffer) {
    const packet = new ArrayBuffer(12 + pcm.byteLength);
    const view = new DataView(packet);
    view.setUint32(0, sequence, true);
    view.setBigUint64(4, BigInt(samples), true);
    new Uint8Array(packet, 12).set(new Uint8Array(pcm));
    pending.push({ seq: sequence++, packet });
    samples += pcm.byteLength / 2;
    if (socket?.readyState === WebSocket.OPEN) socket.send(packet);
    if (pending.length >= 62) {
      error.value = "未确认音频已接近5秒，已停止采音；请保留本页等待已接收内容完成。";
      void stop();
    }
  }
  async function beginCapture(token: SelectionToken) {
    if (context || !media || stopping || disposed || !selection.current(token)) return;
    context = new AudioContext();
    const ownedContext = context;
    await ownedContext.audioWorklet.addModule("/dictation-worklet.js");
    if (stopping || !media || !selection.current(token) || context !== ownedContext) {
      if (ownedContext.state !== "closed") await ownedContext.close();
      return;
    }
    worklet = new AudioWorkletNode(context, "dictation-capture");
    worklet.port.onmessage = event => {
      if (event.data.flushed) { flushResolve?.(); flushResolve = null; return; }
      if (event.data.pcm) {
        level.value = Math.min(1, event.data.level * 5);
        sendPCM(event.data.pcm);
      }
    };
    const source = context.createMediaStreamSource(media);
    const mute = context.createGain(); mute.gain.value = 0;
    source.connect(worklet); worklet.connect(mute); mute.connect(context.destination);
    await context.resume();
    capturing.value = true;
  }
  function connect(token = selection.token()) {
    if (!session.value || disposed || !selection.current(token)) return;
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const id = token.id;
    const ownedSocket = new WebSocket(`${scheme}://${location.host}/api/dictation/sessions/${id}/stream`);
    socket = ownedSocket;
    ownedSocket.onmessage = event => {
      if (!selection.current(token) || socket !== ownedSocket) return;
      const message = JSON.parse(event.data);
      if (message.snapshot) {
        apply(message.snapshot, token);
        // Resume only an owned capture; history/small-window viewers never acquire the mic.
        if (message.snapshot.status === "listening" && media) void beginCapture(token).catch(cause => failCapture(cause, token));
      }
      if (message.type === "audio.ack") pending = pending.filter(item => item.seq >= message.next_sequence);
      if (message.type === "session.snapshot") {
        const next = message.snapshot.next_sequence;
        pending = pending.filter(item => item.seq >= next);
        if (pending.length && pending[0].seq !== next) {
          error.value = "音频恢复窗口已失效，不能带缺口续录。"; void stop(); return;
        }
        for (const item of pending) socket?.send(item.packet);
        connection.value = "";
      }
      if (message.type === "asr.partial" && session.value) {
        if (message.event_sequence <= session.value.event_sequence) return;
        session.value.event_sequence = message.event_sequence;
        selection.accept(session.value, token);
        let utterance = session.value.utterances.find(u => u.id === message.utterance_id);
        if (!utterance) {
          utterance = { id: message.utterance_id, revision: message.revision, status: "partial", text: "", raw_text: "", preview: "", edited: false, needs_review: false };
          session.value.utterances.push(utterance);
        }
        utterance.preview = message.text;
      }
      if (message.type === "session.error") { error.value = message.error; void releaseAudio(); }
      if (message.type === "session.warning") connection.value = message.message;
    };
    ownedSocket.onclose = () => {
      if (!disposed && selection.current(token) && socket === ownedSocket && active.value && !stopping) {
        connection.value = "网络中断，正在短时重连；未确认音频有界保留";
        reconnectTimer = setTimeout(() => connect(token), 700);
      }
    };
    ownedSocket.onerror = () => { if (selection.current(token)) connection.value = "网络连接异常"; };
  }
  async function failCapture(cause: unknown, token: SelectionToken) {
    if (!selection.current(token)) return;
    error.value = String(cause);
    await releaseAudio();
    if (token.id) await api(`/sessions/${token.id}/cancel`, { method: "POST" }).catch(() => {});
  }
  async function start(terms: string[], cleanup: boolean) {
    if (busy.value || active.value) return;
    const pendingSelection = choose(null);
    error.value = ""; busy.value = true; stopping = false;
    try {
      if (!navigator.mediaDevices?.getUserMedia || !window.isSecureContext) throw new Error("麦克风需要 HTTPS 或本机 localhost；也可上传录音。");
      // No server-side phantom recording on permission denial.
      media = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
      const state = await api("/sessions", { method: "POST", body: JSON.stringify({ terms, cleanup }) });
      if (disposed || !selection.current(pendingSelection)) {
        await api(`/sessions/${state.id}/cancel`, { method: "POST" });
        await releaseAudio(); return;
      }
      const token = choose(state.id);
      apply(state, token); sequence = state.next_sequence; samples = state.samples; pending = [];
      connect(token);
    } catch (cause) {
      await releaseAudio(); error.value = cause instanceof Error ? cause.message : String(cause);
    } finally { busy.value = false; }
  }
  async function stop() {
    if (stopping) return;
    stopping = true;
    await releaseAudio(true);
    const token = selection.token();
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = null;
    if (token.id) {
      if (socket?.readyState === WebSocket.OPEN && pending.length < 62) socket.send(JSON.stringify({ type: "stop" }));
      else {
        if (pending.length) error.value = "连接中断时有未确认音频，未确认部分可能未被接收；已接收内容会继续完成。";
        await api(`/sessions/${token.id}/stop`, { method: "POST" }).then(value => apply(value, token)).catch(cause => { error.value = String(cause); });
      }
    }
    stopping = false;
  }
  async function cancel() {
    const token = selection.token();
    await releaseAudio();
    if (token.id) apply(await api(`/sessions/${token.id}/cancel`, { method: "POST" }), token);
  }
  async function upload(file: File, terms: string[], cleanup: boolean) {
    if (busy.value || active.value) return;
    const pendingSelection = choose(null);
    error.value = ""; busy.value = true;
    try {
      const query = new URLSearchParams({ filename: file.name, terms: JSON.stringify(terms), cleanup: String(cleanup) });
      const state = await api(`/uploads?${query}`, { method: "POST", body: file, headers: { "Content-Type": "application/octet-stream" } });
      if (!disposed && selection.current(pendingSelection)) apply(state, choose(state.id));
    } catch (cause) { error.value = cause instanceof Error ? cause.message : String(cause); }
    finally { busy.value = false; }
  }
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
    busy.value = true;
    try {
      apply(await api(`/sessions/${token.id}/cleanup`, { method: "POST", body: JSON.stringify(body) }), token);
    } finally { busy.value = false; }
  }
  async function remove(id: string) {
    if (session.value?.id === id) await releaseAudio();
    await api(`/sessions/${id}`, { method: "DELETE" });
    if (selection.token().id === id) choose(null);
    await loadHistory();
  }
  function startPolling() {
    pollTimer = setInterval(() => { if (session.value && (active.value || !capturing.value)) void refresh().catch(() => {}); }, 1000);
  }
  function unload() { media?.getTracks().forEach(track => track.stop()); }
  window.addEventListener("pagehide", unload);
  startPolling();
  onBeforeUnmount(() => {
    disposed = true;
    selection.select(null);
    if (reconnectTimer) clearTimeout(reconnectTimer);
    if (pollTimer) clearInterval(pollTimer);
    window.removeEventListener("pagehide", unload);
    void releaseAudio();
    socket?.close();
  });
  return { session, history, error, busy, level, connection, capturing, active, partial, start, stop, cancel, upload,
    select, refresh, loadHistory, edit, editDraft, cleanup, remove };
}
