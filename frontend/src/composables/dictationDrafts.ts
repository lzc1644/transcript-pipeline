import { computed, ref } from "vue";
import type { PcmCapture } from "./dictationCapture";

const RATE = 16000, BLOCK_SAMPLES = RATE, MAX_PENDING = RATE * 5;
const EXPORT_BATCH_BYTES = 4 * 1024 * 1024;
export interface RecordingDraft {
  id: string; createdAt: string; updatedAt: string; sampleRate: number; channels: number; format: "pcm16le";
  samples: number; status: "recording" | "paused" | "ended" | "interrupted";
  device?: { id: string; label: string };
  devices?: { startSample: number; id: string; label: string }[];
  submission?: { status: "uploading" | "submitted" | "failed" | "unknown"; samples: number; sessionId?: string };
}
function timestamp() { return new Date().toISOString(); }

/** Atomic metadata + ordered PCM blocks. No server session and no inference lifecycle. */
export class DraftStore {
  private db: Promise<IDBDatabase> | null = null;
  private exporting = new Set<string>();
  private open() {
    return this.db ??= new Promise((resolve, reject) => {
      const request = indexedDB.open("transcript-dictation-drafts", 1);
      request.onupgradeneeded = () => {
        request.result.createObjectStore("drafts", { keyPath: "id" });
        request.result.createObjectStore("chunks", { keyPath: ["id", "start"] });
      };
      request.onsuccess = () => { request.result.onversionchange = () => request.result.close(); resolve(request.result); };
      request.onerror = () => reject(request.error);
      request.onblocked = () => reject(new Error("本地录音存储被旧窗口占用，请关闭旧窗口后重试"));
    });
  }
  private async transaction<T>(stores: string[], mode: IDBTransactionMode, run: (tx: IDBTransaction, result: (value: T) => void) => void): Promise<T> {
    const db = await this.open();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(stores, mode);
      let value: T;
      tx.oncomplete = () => resolve(value);
      tx.onabort = tx.onerror = () => reject(tx.error ?? new Error("本地录音事务失败"));
      try { run(tx, result => { value = result; }); } catch (cause) { tx.abort(); reject(cause); }
    });
  }
  async list() {
    return this.transaction<RecordingDraft[]>(["drafts"], "readonly", (tx, result) => {
      tx.objectStore("drafts").getAll().onsuccess = event => result((event.target as IDBRequest<RecordingDraft[]>).result.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt)));
    });
  }
  async get(id: string) {
    const value = await this.transaction<RecordingDraft | undefined>(["drafts"], "readonly", (tx, result) => {
      const request = tx.objectStore("drafts").get(id); request.onsuccess = () => result(request.result);
    });
    if (!value) throw new Error("本浏览器没有这条录音草稿");
    return value;
  }
  async put(draft: RecordingDraft) {
    return this.transaction<RecordingDraft>(["drafts"], "readwrite", (tx, result) => {
      tx.objectStore("drafts").put(draft); result(draft);
    });
  }
  async update(id: string, change: Partial<RecordingDraft>) {
    return this.transaction<RecordingDraft>(["drafts"], "readwrite", (tx, result) => {
      const request = tx.objectStore("drafts").get(id);
      request.onsuccess = () => {
        if (!request.result) { tx.abort(); return; }
        const value = { ...request.result, ...change, updatedAt: timestamp() };
        tx.objectStore("drafts").put(value); result(value);
      };
    });
  }
  async append(id: string, start: number, pcm: ArrayBuffer) {
    return this.transaction<RecordingDraft>(["drafts", "chunks"], "readwrite", (tx, result) => {
      const drafts = tx.objectStore("drafts"), request = drafts.get(id);
      request.onsuccess = () => {
        const value: RecordingDraft = request.result;
        if (!value || value.samples !== start || pcm.byteLength % 2) { tx.abort(); return; }
        try {
          tx.objectStore("chunks").add({ id, start, pcm });
          value.samples += pcm.byteLength / 2; value.updatedAt = timestamp();
          drafts.put(value); result(value);
        } catch { tx.abort(); }
      };
    });
  }
  async wav(id: string, maximumBytes: number) {
    if (this.exporting.has(id)) throw new Error("这条草稿正在组装WAV，请等待完成后再试听、下载或提交；草稿保留。");
    this.exporting.add(id);
    // Keep only a bounded batch of PCM in JS; opaque Blob parts can be disk-backed
    // by the browser. Multiple short transactions also avoid a multi-hour read lock.
    // This does not waive the browser's Blob/temporary-disk/IndexedDB limits.
    try {
      const draft = await this.get(id);
      if (!draft.samples || draft.samples * 2 + 44 > Math.min(maximumBytes, 0xFFFFFFFF)) {
        throw new Error("音频为空或超过本次WAV大小限制");
      }
      const parts: Blob[] = [];
      let position = 0;
      while (position < draft.samples) {
        const batch = await this.transaction<{ end: number; blob: Blob }>(["drafts", "chunks"], "readonly", (tx, result) => {
          const request = tx.objectStore("drafts").get(id);
          request.onsuccess = () => {
            if (request.result?.samples !== draft.samples) { tx.abort(); return; }
            const chunks: ArrayBuffer[] = [];
            let end = position, bytes = 0;
            const cursor = tx.objectStore("chunks").openCursor(IDBKeyRange.bound([id, position], [id, draft.samples], false, true));
            cursor.onsuccess = () => {
              try {
                const row = cursor.result;
                if (!row || bytes >= EXPORT_BATCH_BYTES) {
                  if (!bytes || (!row && end !== draft.samples)) { tx.abort(); return; }
                  result({ end, blob: new Blob(chunks) }); return;
                }
                const pcm = row.value.pcm;
                if (row.value.start !== end || !(pcm instanceof ArrayBuffer) || !pcm.byteLength || pcm.byteLength % 2 || end + pcm.byteLength / 2 > draft.samples) {
                  tx.abort(); return;
                }
                chunks.push(pcm); bytes += pcm.byteLength; end += pcm.byteLength / 2;
                row.continue();
              } catch { tx.abort(); }
            };
          };
        });
        parts.push(batch.blob); position = batch.end;
      }
      if ((await this.get(id)).samples !== draft.samples) throw new Error("组装期间音频版本已变化，请结束录音后重试");
      return { draft, blob: new Blob([wavHeader(draft.samples), ...parts], { type: "audio/wav" }) };
    } catch (cause) {
      const detail = cause instanceof Error ? cause.message : String(cause);
      throw new Error(`无法组装录音WAV（${detail}）。草稿未删除；请释放浏览器内存/临时磁盘空间后重试，或重新打开草稿。`);
    } finally { this.exporting.delete(id); }
  }
  async remove(id: string) {
    await this.transaction<void>(["drafts", "chunks"], "readwrite", (tx, result) => {
      tx.objectStore("drafts").delete(id);
      tx.objectStore("chunks").delete(IDBKeyRange.bound([id, 0], [id, Number.MAX_SAFE_INTEGER])); result();
    });
  }
}
export function wavHeader(samples: number) {
  const buffer = new ArrayBuffer(44), view = new DataView(buffer);
  const text = (at: number, value: string) => { for (let i = 0; i < value.length; i++) view.setUint8(at + i, value.charCodeAt(i)); };
  text(0, "RIFF"); view.setUint32(4, 36 + samples * 2, true); text(8, "WAVE"); text(12, "fmt ");
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, RATE, true); view.setUint32(28, RATE * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  text(36, "data"); view.setUint32(40, samples * 2, true); return buffer;
}

export function createLocalRecorder(audio: PcmCapture, canCapture: () => boolean) {
  const store = new DraftStore();
  const draft = ref<RecordingDraft | null>(null), drafts = ref<RecordingDraft[]>([]);
  const error = ref(""), busy = ref(false), capturing = ref(false), level = ref(0), recordedSamples = ref(0), submittingId = ref<string | null>(null);
  const active = computed(() => !!draft.value && ["recording", "paused"].includes(draft.value.status));
  let operation = 0, buffer: Uint8Array[] = [], bufferedSamples = 0, queuedSamples = 0;
  let writing: Promise<void> = Promise.resolve(), writeError: unknown = null;
  let maxSamples = 4 * 60 * 60 * RATE, maxBytes = 512 * 1024 * 1024;
  let url = "";
  const playbackURL = ref("");
  function clearPlayback() { if (url) URL.revokeObjectURL(url); url = ""; playbackURL.value = ""; }
  async function load() { drafts.value = await store.list(); }
  function publish(value: RecordingDraft) { if (draft.value?.id === value.id) draft.value = value; }
  function queueBlock() {
    if (!bufferedSamples || !draft.value || writeError) return;
    const raw = new Uint8Array(bufferedSamples * 2), id = draft.value.id, start = queuedSamples;
    let at = 0; for (const part of buffer) { raw.set(part, at); at += part.length; }
    buffer = []; bufferedSamples = 0; queuedSamples += raw.byteLength / 2;
    writing = writing.then(async () => { publish(await store.append(id, start, raw.buffer)); });
    void writing.catch(cause => {
      if (!writeError) {
        writeError = cause; error.value = "录音保存失败（可能空间不足或存储不可用），已停止采音；可试听、下载已成功保存的部分。";
        void finish("interrupted").catch(() => {});
      }
    });
  }
  async function select(id: string) {
    if (active.value || busy.value || submittingId.value) throw new Error("请先结束当前本地录音或等待上传，再查看其他草稿");
    clearPlayback();
    let value = await store.get(id);
    if (value.status === "recording") value = await store.update(id, { status: "interrupted" }).catch(() => ({ ...value, status: "interrupted" }));
    if (value.submission?.status === "uploading") value = await store.update(id, { submission: { ...value.submission, status: "unknown" } });
    draft.value = value; recordedSamples.value = value.samples;
  }
  async function begin(id?: string) {
    if (busy.value || capturing.value || submittingId.value || !canCapture()) throw new Error("请先结束另一项网页采音或等待提交完成");
    busy.value = true; const generation = ++operation; error.value = ""; clearPlayback();
    try {
      const config = await fetch("/api/dictation/config").then(r => { if (!r.ok) throw new Error("无法读取录音限制，请检查连接后重试"); return r.json(); });
      maxBytes = config.max_upload_mb * 1024 * 1024;
      maxSamples = Math.min(config.max_upload_minutes * 60 * RATE, Math.floor((maxBytes - 44) / 2));
      const value: RecordingDraft = id ? await store.get(id) : await store.put({ id: crypto.randomUUID(), createdAt: timestamp(), updatedAt: timestamp(),
        sampleRate: RATE, channels: 1, format: "pcm16le", samples: 0, status: "paused" });
      if (generation !== operation) return;
      if (value.samples >= maxSamples) throw new Error("草稿已达到当前上传时长或大小上限，请先下载或转写");
      if (value.submission?.status === "uploading") throw new Error("此音频版本正在提交转写，请等待结果再继续录音");
      draft.value = value; recordedSamples.value = queuedSamples = value.samples;
      writeError = null; buffer = []; bufferedSamples = 0; writing = Promise.resolve();
      await audio.prepare("local", cause => { error.value = cause.message; void finish("interrupted"); });
      if (generation !== operation) return;
      const device = audio.deviceInfo;
      const updated = await store.update(value.id, { status: "recording", device,
        devices: [...(value.devices ?? []), { startSample: value.samples, ...device }] });
      if (generation !== operation) return;
      draft.value = updated;
      await audio.start("local", (pcm, volume) => {
        if (generation !== operation || writeError) return;
        const available = maxSamples - recordedSamples.value;
        const count = Math.min(pcm.byteLength / 2, available);
        if (count) {
          if (recordedSamples.value - (draft.value?.samples ?? 0) + count > MAX_PENDING) {
            error.value = "本地写入积压超过5秒，已停止采音；等待已接收内容保存后可下载。";
            void finish("interrupted"); return;
          }
          buffer.push(new Uint8Array(pcm.slice(0, count * 2))); bufferedSamples += count; recordedSamples.value += count;
          level.value = Math.min(1, volume * 5);
          if (bufferedSamples >= BLOCK_SAMPLES) queueBlock();
        }
        if (recordedSamples.value >= maxSamples) { error.value = "已达到录音上限，采音已停止；请试听、下载或开始转写。"; void finish("ended"); }
      });
      if (generation === operation) capturing.value = true;
    } catch (cause) {
      if (generation === operation) {
        await audio.stop("local", false); error.value = cause instanceof Error ? cause.message : String(cause);
        if (draft.value) publish(await store.update(draft.value.id, { status: "interrupted" }).catch(() => draft.value!));
      }
    } finally { busy.value = false; await load().catch(() => {}); }
  }
  let finishing: Promise<void> | null = null;
  function finish(state: RecordingDraft["status"]) {
    if (finishing) return finishing;
    capturing.value = false; level.value = 0; busy.value = true;
    finishing = (async () => {
      try {
        await audio.stop("local", true).catch(cause => { error.value = String(cause); state = "interrupted"; });
        ++operation; queueBlock(); await writing.catch(() => { state = "interrupted"; });
        if (draft.value) {
          const id = draft.value.id, status = writeError ? "interrupted" : state;
          try { publish(await store.update(id, { status })); }
          catch {
            publish({ ...draft.value, status: "interrupted" });
            error.value = "草稿状态无法写入，已停止采音；已保存音频仍可试听和下载。";
          }
        }
      } finally { finishing = null; busy.value = false; await load().catch(() => {}); }
    })();
    return finishing;
  }
  async function preview() {
    if (!draft.value || capturing.value || draft.value.status === "recording" || busy.value) throw new Error("请先暂停或结束录音并等待保存完成");
    // Download is a recovery path even if the server's upload limit was lowered.
    busy.value = true; const generation = operation;
    try {
      clearPlayback();
      const result = await store.wav(draft.value.id, 0xFFFFFFFF);
      if (generation !== operation) throw new DOMException("试听操作已结束，草稿保留", "AbortError");
      url = URL.createObjectURL(result.blob); playbackURL.value = url; return result;
    } finally { busy.value = false; }
  }
  async function remove(id: string) {
    if ((draft.value?.id === id && active.value) || busy.value || submittingId.value === id) throw new Error("请先结束录音并等待保存/提交完成");
    await store.remove(id); if (draft.value?.id === id) { draft.value = null; clearPlayback(); } await load();
  }
  async function freezeForSubmission() {
    if (!draft.value?.samples || active.value || busy.value || submittingId.value) throw new Error("请先结束录音并等待保存/提交完成");
    if (draft.value.submission?.status === "unknown") throw new Error("上次提交结果不明确，请先检查历史再确认重试");
    const id = draft.value.id;
    submittingId.value = id; // Claim before the first await, including Blob assembly.
    clearPlayback();
    try {
      const config = await fetch("/api/dictation/config").then(r => { if (!r.ok) throw new Error("无法确认当前转写限制，草稿仍可下载"); return r.json(); });
      const current = await store.get(id);
      if (current.samples > config.max_upload_minutes * 60 * RATE || current.samples * 2 + 44 > config.max_upload_mb * 1024 * 1024) {
        throw new Error("录音超过当前服务器的时长或大小限制，请先下载备份；不会裁剪或自动重复提交");
      }
      const frozen = await store.wav(id, config.max_upload_mb * 1024 * 1024);
      publish(await store.update(id, { submission: { status: "uploading", samples: frozen.draft.samples } }));
      return frozen;
    } catch (cause) { submittingId.value = null; throw cause; }
  }
  async function submission(id: string, status: NonNullable<RecordingDraft["submission"]>["status"], samples: number, sessionId?: string) {
    submittingId.value = status === "uploading" ? id : null;
    publish(await store.update(id, { submission: { status, samples, sessionId } }));
  }
  function interrupt() { ++operation; capturing.value = false; void audio.stop("local", false).catch(() => {}); clearPlayback(); /* Last committed chunks survive. Never reopen the mic on load. */ }
  return { draft, drafts, store, error, busy, capturing, level, recordedSamples, submittingId, active, playbackURL, load, select, begin,
    pause: () => finish("paused"), end: () => finish("ended"), preview, remove, freezeForSubmission, submission, clearPlayback, interrupt };
}
