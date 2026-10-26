import { ChangeSet, Text, type EditorState } from "@codemirror/state";
import { ref, shallowRef } from "vue";
import type { DictationSession } from "./useDictation";

export interface TextChange { from: number; to: number; insert: string }
export interface DocumentOperation { id: string; version: number; changes: TextChange[]; kind: string }
type Pending = { id: string; changes: ChangeSet; payload?: object };
type API = (path: string, init?: RequestInit) => Promise<any>;
// Window names survive refresh; unlike sessionStorage, a new popup doesn't
// inherit its opener's name. Each editor owns its pending-edit recovery copy.
const writer = window.name || (window.name = `dictation-editor-${crypto.randomUUID()}`);
const asText = (value: string) => Text.of(value.split("\n"));
export function changesJSON(changes: ChangeSet): TextChange[] {
  const result: TextChange[] = [];
  changes.iterChanges((from, to, _a, _b, insert) => result.push({ from, to, insert: insert.toString() }));
  return result;
}
function overlap(a: TextChange, b: TextChange) {
  if (a.from === a.to) return b.from < a.from && a.from < b.to;
  if (b.from === b.to) return a.from < b.from && b.from < a.to;
  return Math.max(a.from, b.from) < Math.min(a.to, b.to);
}

/** One buffer per session, shared by the route and the dock, retained across navigation. */
export class DictationDocument {
  readonly text = ref("");
  readonly status = ref<"saved" | "saving" | "failed" | "conflict">("saved");
  readonly error = ref("");
  readonly snapshot = shallowRef<DictationSession>();
  readonly preview = ref<{ id: string; original: string; model: string; text: string; base: number } | null>(null);
  editorState?: EditorState;
  version = 0;
  cursor = 0;
  composing = false;
  private base = "";
  private pending: Pending[] = [];
  private listeners = new Set<(changes: ChangeSet, kind: string) => void>();
  private flight: Promise<void> | null = null;
  private timer?: ReturnType<typeof setTimeout>;
  private deferred?: DictationSession;
  private initialized = false;
  constructor(readonly id: string, private api: API) {
    if (!id) {
      try { this.text.value = JSON.parse(localStorage.getItem(this.key) ?? "null")?.text ?? ""; }
      catch {}
      this.cursor = this.text.value.length;
    }
  }
  private get key() { return `dictation:document:${this.id || "scratch"}:${writer}`; }
  private remember() {
    try {
      if (this.pending.length || !this.id) localStorage.setItem(this.key, JSON.stringify({
        version: this.version, base: this.base, pending: this.pending.map(p => ({ id: p.id, changes: p.changes.toJSON(), payload: p.payload })),
        text: this.text.value,
      }));
      else localStorage.removeItem(this.key);
    } catch { this.error.value = "本浏览器无法保存恢复副本，请保持页面打开并导出当前文本。"; }
  }
  subscribe(listener: (changes: ChangeSet, kind: string) => void) { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; }
  private publish(changes: ChangeSet, kind: string) {
    this.text.value = changes.apply(asText(this.text.value)).toString();
    this.cursor = changes.mapPos(this.cursor, 1);
    for (const listener of this.listeners) listener(changes, kind);
  }
  private initialize(state: DictationSession) {
    this.initialized = true;
    this.version = state.document_version ?? 0;
    this.base = state.draft_text;
    this.text.value = this.base;
    try {
      const recovery = JSON.parse(localStorage.getItem(this.key) ?? "null");
      if (recovery) {
        this.version = recovery.version; this.base = recovery.base;
        this.pending = recovery.pending.map((p: any) => ({ id: p.id, changes: ChangeSet.fromJSON(p.changes), payload: p.payload }));
        this.text.value = recovery.text;
        if (this.pending.length) { this.status.value = "failed"; this.error.value = "已恢复尚未提交的本地编辑，请重试保存或下载备份。"; }
      }
    } catch { this.error.value = "无法读取本地恢复副本，请检查浏览器存储。"; }
    this.cursor = this.text.value.length;
  }
  receive(state: DictationSession) {
    if (this.initialized && (state.document_version ?? 0) < this.version) return;
    // Save responses bypass the controller's snapshot gate. A late response
    // must not restore a discarded suggestion or an older capture state.
    const latest = this.deferred ?? this.snapshot.value;
    if (latest && (state.revision < latest.revision
        || (state.revision === latest.revision && state.event_sequence < latest.event_sequence))) return;
    if (this.composing) { this.deferred = state; return; }
    if (!this.initialized) this.initialize(state);
    this.snapshot.value = state;
    if (this.status.value === "conflict") return;
    try {
      const operations = (state.document_operations ?? []).filter(op => op.version > this.version);
      if ((state.document_version ?? 0) > this.version && operations[0]?.version !== this.version + 1)
        throw new Error("同步记录已过期；本地编辑保留，请导出或重新载入服务器稿。");
      for (const op of operations) {
        let remote = ChangeSet.of(op.changes, this.base.length);
        const own = op.id === this.pending[0]?.id;
        const nextPending: Pending[] = own ? this.pending.slice(1) : [];
        if (!own) {
          for (const pending of this.pending) {
            if (changesJSON(pending.changes).some(a => changesJSON(remote).some(b => overlap(a, b))))
              throw new Error("后台或另一窗口修改了相同范围；本地稿已保留，请导出后合并，或重新载入服务器稿。");
            const mapped = pending.changes.map(remote);
            remote = remote.map(pending.changes, true);
            nextPending.push({ ...pending, changes: mapped });
          }
        }
        const nextBase = ChangeSet.of(op.changes, this.base.length).apply(asText(this.base)).toString();
        const nextText = own ? this.text.value : remote.apply(asText(this.text.value)).toString();
        // Publish only after the entire remote operation has a valid plan.
        this.pending = nextPending;
        this.base = nextBase;
        this.version = op.version;
        if (!own) {
          this.text.value = nextText;
          this.cursor = remote.mapPos(this.cursor, 1);
          for (const listener of this.listeners) listener(remote, op.kind);
        }
      }
      if (!this.pending.length && this.text.value !== state.draft_text)
        throw new Error("正文同步不一致；本地稿已保留，请导出后重新载入。");
      if (!this.pending.length) { this.status.value = "saved"; this.error.value = ""; }
      this.remember();
      const suggestion = state.document_suggestion;
      if (!suggestion && this.preview.value && this.preview.value.text === this.preview.value.model
          && (operations.some(op => op.kind === "apply") || state.content_revision === this.preview.value.base)) this.clearPreview();
      if (suggestion && !this.preview.value && !this.pending.length) {
        let text = suggestion.text;
        try {
          const saved = JSON.parse(localStorage.getItem(`${this.key}:preview`) ?? "null");
          if (saved?.id === suggestion.id) text = saved.text;
        } catch {}
        this.preview.value = { id: suggestion.id, original: suggestion.original, model: suggestion.text, text,
          base: suggestion.base_content_revision };
      }
    } catch (cause) { this.status.value = "conflict"; this.error.value = String(cause instanceof Error ? cause.message : cause); this.remember(); }
  }
  local(changes: ChangeSet) {
    if (changes.empty) return;
    this.text.value = changes.apply(asText(this.text.value)).toString();
    const last = this.pending.at(-1);
    if (last && !last.payload) last.changes = last.changes.compose(changes);
    else this.pending.push({ id: crypto.randomUUID(), changes });
    this.remember();
    if (this.id && !["failed", "conflict"].includes(this.status.value)) {
      this.status.value = "saving";
      clearTimeout(this.timer); this.timer = setTimeout(() => { void this.flush().catch(() => {}); }, 450);
    }
  }
  composition(value: boolean) {
    this.composing = value;
    if (!value) {
      const state = this.deferred; this.deferred = undefined;
      if (state) this.receive(state);
      if (this.pending.length) void this.flush().catch(() => {});
    }
  }
  async flush(): Promise<void> {
    clearTimeout(this.timer);
    if (!this.id) return;
    if (this.composing) throw new Error("请先完成输入法组词");
    if (this.status.value === "conflict" || this.status.value === "failed") throw new Error(this.error.value);
    if (this.flight) { await this.flight; if (this.pending.length) return this.flush(); return; }
    this.flight = (async () => {
      try {
        while (this.pending.length) {
          this.status.value = "saving";
          const first = this.pending[0];
          first.payload ??= { operation_id: first.id, base_version: this.version, changes: changesJSON(first.changes) };
          this.remember();
          const result = await this.api(`/sessions/${this.id}/document`, { method: "PATCH", body: JSON.stringify(first.payload) });
          this.receive(result);
          if (this.failed()) throw new Error(this.error.value);
          if (this.composing) return; // The response will be consumed after compositionend.
        }
        this.status.value = "saved";
      } catch (cause) {
        if (this.status.value !== "conflict") this.status.value = "failed";
        this.error.value = cause instanceof Error ? cause.message : String(cause);
        this.remember(); throw cause;
      }
    })();
    try { await this.flight; } finally { this.flight = null; }
  }
  private failed() { return this.status.value === "conflict"; }
  async retry() {
    if (this.status.value === "conflict") return;
    this.status.value = "saving"; this.error.value = "";
    await this.flush();
  }
  async reload() {
    if (this.flight) await this.flight.catch(() => {});
    const state = await this.api(`/sessions/${this.id}`);
    const changes = ChangeSet.of({ from: 0, to: this.text.value.length, insert: state.draft_text }, this.text.value.length);
    this.pending = []; this.base = state.draft_text; this.version = state.document_version;
    this.publish(changes, "reload"); this.status.value = "saved"; this.error.value = "";
    this.receive(state);
  }
  editPreview(text: string) {
    if (!this.preview.value) return;
    this.preview.value.text = text;
    try { localStorage.setItem(`${this.key}:preview`, JSON.stringify({ id: this.preview.value.id, text })); }
    catch { this.error.value = "预览恢复副本保存失败，请先下载预览。"; }
  }
  clearPreview() { this.preview.value = null; localStorage.removeItem(`${this.key}:preview`); }
  get hasPending() { return this.pending.length > 0; }
}

export function createDictationDocuments(api: API) {
  const buffers = new Map<string, DictationDocument>();
  function get(id: string) {
    if (!buffers.has(id)) buffers.set(id, new DictationDocument(id, api));
    return buffers.get(id)!;
  }
  return { get, receive(state: DictationSession) { if (state.editor_version === 1) get(state.id).receive(state); } };
}
