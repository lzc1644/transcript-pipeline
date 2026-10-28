<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { NButton } from "naive-ui";
import { Annotation, Compartment, EditorState, StateEffect, Text, Transaction, type Extension } from "@codemirror/state";
import { EditorView, keymap, placeholder } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, isolateHistory, undo, redo } from "@codemirror/commands";
import { unifiedMergeView } from "@codemirror/merge";
import type { DictationDocument } from "../composables/dictationDocument";

const props = defineProps<{
  buffer: DictationDocument;
  listening?: boolean;
  partialText: string;
  showStart?: boolean;
  startDisabled?: boolean;
  starting?: boolean;
}>();

const emit = defineEmits<{ apply: []; discard: []; term: [text: string]; start: [] }>();

const host = ref<HTMLElement>();
const mode = ref<"result" | "original" | "diff">("result");
const preview = computed(() => props.buffer.preview.value);
const emptyStart = computed(() => props.showStart && !preview.value && !props.buffer.text.value.trim());
const stale = computed(() => !!preview.value && (preview.value.id !== props.buffer.snapshot.value?.document_suggestion?.id
  || preview.value.base !== props.buffer.snapshot.value?.content_revision || props.buffer.hasPending));
const charCount = computed(() => props.buffer.text.value.length);

const statusText = computed(() => ({
  saved: props.buffer.id ? "已保存" : "开始听写时保存",
  saving: "保存中…",
  failed: "保存失败",
  conflict: "需要合并",
}[props.buffer.status.value]));

const external = Annotation.define<boolean>();
const editingMode = new Compartment();
function editable(readOnly: boolean) {
  const locked = readOnly || !!props.starting;
  return [EditorState.readOnly.of(locked), EditorView.editable.of(!locked)];
}

let view: EditorView | undefined;
let bodyState: EditorState | undefined;
let previewState: EditorState | undefined;
let unsubscribe: (() => void) | undefined;
let showing = "body";
let follow = true;

function position() {
  return Math.min(props.buffer.cursor, props.buffer.text.value.length);
}

function extensions(readOnly = false, label = "正文编辑器"): Extension[] {
  return [
    EditorView.lineWrapping,
    history(),
    keymap.of([
      {
        key: "Mod-s",
        run: () => {
          void props.buffer.flush().catch(() => {});
          return true;
        },
      },
      ...historyKeymap,
      ...defaultKeymap,
    ]),
    editingMode.of(editable(readOnly)),
    EditorView.contentAttributes.of({
      "aria-label": readOnly ? (mode.value === "diff" ? "整理修改差异" : "原稿") : label,
      "spellcheck": "false",
    }),
    placeholder("在这里输入，或开始听写。Enter 换段；听写跟随当前光标。"),
    EditorView.updateListener.of(update => {
      if (update.docChanged && !update.transactions.every(tr => tr.annotation(external))) {
        if (showing === "body") props.buffer.local(update.changes);
        else if (showing === "preview") props.buffer.editPreview(update.state.doc.toString());
      }
      if (showing === "body" && (update.selectionSet || update.docChanged)) {
        const cursor = update.state.selection.main.to;
        if (update.transactions.some(tr => !tr.annotation(external))) props.buffer.placeCursor(cursor, update.docChanged);
        else props.buffer.cursor = cursor;
      }
    }),
    EditorView.domEventHandlers({
      compositionstart: () => {
        if (showing === "body") props.buffer.composition(true);
      },
      compositionend: () => {
        queueMicrotask(() => props.buffer.composition(false));
      },
      scroll: () => {
        if (!view || showing !== "body") return;
        const rect = view.scrollDOM.getBoundingClientRect();
        const coords = view.coordsAtPos(position());
        follow = !!coords && coords.top >= rect.top && coords.bottom <= rect.bottom;
      },
    }),
    EditorView.theme({
      "&": { height: "100%", fontSize: "16px", color: "var(--text-primary)", backgroundColor: "transparent" },
      ".cm-scroller": { overflow: "auto", fontFamily: "inherit", lineHeight: "1.7", overscrollBehavior: "contain" },
      ".cm-content": { padding: "16px 18px 24px", minHeight: "100%", caretColor: "var(--primary)" },
      ".cm-line": { padding: "0" },
      "&.cm-focused": { outline: "none" },
      ".cm-selectionBackground, &.cm-focused .cm-selectionBackground": { backgroundColor: "var(--primary-alpha-20)" },
      ".cm-cursor": { borderLeftColor: "var(--primary)", borderLeftWidth: "2px" },
      ".cm-placeholder": { color: "var(--text-muted)" },
      ".cm-deletedChunk": { backgroundColor: "var(--color-error-bg)" },
      ".cm-insertedLine, .cm-changedLine": { backgroundColor: "var(--color-success-bg)" },
    }),
  ];
}

function createState(doc: string, extra: Extension[] = [], readOnly = false, label = "正文编辑器") {
  return EditorState.create({ doc: Text.of(doc.split("\n")), extensions: [...extensions(readOnly, label), ...extra] });
}

function saveState() {
  if (showing === "body") bodyState = view?.state;
  else if (showing === "preview") previewState = view?.state;
}

function display() {
  if (!view) return;
  saveState();
  if (!preview.value) {
    showing = "body";
    view.setState(bodyState!);
  } else if (mode.value === "result") {
    showing = "preview";
    previewState ??= createState(preview.value.text, [], false, "整理预览编辑器");
    view.setState(previewState);
  } else {
    showing = "readonly";
    view.setState(
      mode.value === "original"
        ? createState(preview.value.original, [], true)
        : createState(preview.value.text, [
            unifiedMergeView({
              original: preview.value.original,
              mergeControls: false,
              highlightChanges: true,
              gutter: false,
              syntaxHighlightDeletions: false,
              allowInlineDiffs: true,
            }),
          ], true)
    );
  }
}

function connect() {
  unsubscribe?.();
  const previous = props.buffer.editorState;
  bodyState = previous?.doc.toString() === props.buffer.text.value
    ? previous.update({ effects: StateEffect.reconfigure.of(extensions()) }).state
    : createState(props.buffer.text.value).update({ selection: { anchor: props.buffer.cursor } }).state;
  previewState = undefined;
  showing = "body";
  view?.setState(bodyState);
  unsubscribe = props.buffer.subscribe((changes, kind, advanceCursor) => {
    const target = showing === "body" ? view!.state : bodyState!;
    const insertion = target.selection.main.to;
    // A current-turn insertion advances the one real caret, including an
    // insertion at a selection's end. Old-turn results only map the selection.
    const advance = advanceCursor && changes.touchesRange(insertion, insertion);
    const transaction = target.update({
      changes,
      selection: advance ? { anchor: changes.mapPos(insertion, 1) } : undefined,
      // A non-history replacement erases the inverse of the original speech
      // insertion. Keep cleanup as a separate undo step, followed by speech.
      annotations: [external.of(true), Transaction.addToHistory.of(["speech", "cleanup", "apply"].includes(kind)), isolateHistory.of("full")],
    });
    if (showing === "body") {
      const shouldFollow = follow && view!.state.selection.main.empty;
      view!.dispatch(transaction);
      if (shouldFollow && props.listening) {
        view!.dispatch({ effects: EditorView.scrollIntoView(position(), { y: "nearest" }) });
      }
    } else {
      bodyState = transaction.state;
    }
  });
  mode.value = "result";
  display();
}

function backToVoice() {
  follow = true;
  view?.dispatch({ effects: EditorView.scrollIntoView(position(), { y: "center" }) });
}

function addTerm() {
  if (!view) return;
  emit("term", view.state.sliceDoc(view.state.selection.main.from, view.state.selection.main.to));
}

function focusEditor() {
  view?.focus();
}

function reload() {
  if (window.confirm("放弃当前本地编辑并重新载入服务器正文？建议先下载备份。")) {
    void props.buffer.reload().catch(cause => {
      props.buffer.error.value = String(cause);
    });
  }
}

watch(() => props.buffer, connect);
watch(() => preview.value?.id, () => { previewState = undefined; mode.value = "result"; display(); });
watch(mode, display);
// A new session is created from the start-click snapshot. Do not accept edits
// into the old buffer while microphone permission/session creation is pending.
watch(() => props.starting, () => {
  view?.dispatch({ effects: editingMode.reconfigure(editable(showing === "readonly")) });
});

onMounted(() => {
  view = new EditorView({ parent: host.value, state: createState(props.buffer.text.value) });
  connect();
});

onBeforeUnmount(() => {
  saveState();
  props.buffer.editorState = bodyState;
  unsubscribe?.();
  props.buffer.composition(false);
  view?.destroy();
});
</script>

<template>
  <section class="dictation-notebook" :class="{ 'has-empty-start': emptyStart, 'is-listening': listening }" :aria-label="preview ? '全文整理预览' : '正文'">
    <header class="notebook-heading">
      <strong>{{ preview ? '全文整理预览 · 尚未应用' : '正文' }}</strong>
      <span class="notebook-save" :class="buffer.status.value" role="status">{{ preview ? '预览草稿' : statusText }}</span>
      <div class="notebook-tools">
        <n-button size="tiny" quaternary :disabled="starting || (!!preview && mode !== 'result')" @click="view && undo(view)">撤销</n-button>
        <n-button size="tiny" quaternary :disabled="starting || (!!preview && mode !== 'result')" @click="view && redo(view)">重做</n-button>
        <n-button size="tiny" quaternary title="将选中文本加入常用术语库" @click="addTerm">存为术语</n-button>
        <n-button v-if="listening && !preview" size="tiny" quaternary aria-label="回到光标" @click="backToVoice">
          <span class="voice-return-long">回到光标</span>
          <span class="voice-return-short">光标</span>
        </n-button>
      </div>
    </header>

    <nav v-if="preview" class="notebook-preview-tabs" aria-label="整理预览方式">
      <button
        v-for="item in [{ id: 'result', label: '整理结果' }, { id: 'original', label: '原稿' }, { id: 'diff', label: '显示修改' }]"
        :key="item.id"
        :aria-pressed="mode === item.id"
        @click="mode = item.id as typeof mode"
      >
        {{ item.label }}
      </button>
      <span v-if="mode !== 'result'" class="notebook-preview-readonly">只读</span>
    </nav>

    <div class="notebook-editor">
      <div ref="host" class="notebook-codemirror" />
      <div v-if="emptyStart" class="notebook-empty">
        <button
          type="button"
          class="notebook-start"
          :disabled="startDisabled || starting"
          :aria-busy="starting"
          aria-label="开始听写"
          @click="emit('start')"
        >
          <span class="notebook-start__icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
              <rect x="9" y="2" width="6" height="12" rx="3" />
              <path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8" />
            </svg>
          </span>
          <strong>{{ starting ? '正在启动…' : '开始听写' }}</strong>
        </button>
        <p class="notebook-empty__hint" role="button" tabindex="0" title="点击开始编辑" @click="focusEditor" @keydown.enter="focusEditor">
          也可以直接在这里输入文字
        </p>
      </div>
    </div>

    <div v-if="partialText && !preview" class="notebook-partial" role="status">
      <span class="notebook-partial__badge">
        <span class="pulse-indicator" aria-hidden="true" />
        正在识别
      </span>
      <p :title="partialText">{{ partialText }}</p>
    </div>

    <p v-if="buffer.error.value" class="notebook-error" role="alert">
      {{ buffer.error.value }}
      <n-button v-if="buffer.status.value === 'failed'" size="tiny" @click="buffer.retry().catch(() => {})">重试保存</n-button>
      <n-button v-if="buffer.status.value === 'conflict'" size="tiny" @click="reload">重新载入服务器稿</n-button>
    </p>

    <p v-if="stale" class="notebook-error" role="alert">
      正文在整理期间或另一窗口中已变化，此预览不能直接应用；可复制或下载预览后放弃，重新整理。
    </p>

    <footer class="notebook-footer">
      <div class="notebook-footer-left">
        <span v-if="!preview" class="notebook-count" :title="`当前正文共 ${charCount} 个字符`">{{ charCount.toLocaleString() }} 字</span>
        <span v-else class="notebook-count" :title="`原稿 ${preview.original.length} 字，整理后 ${preview.text.length} 字`">原 {{ preview.original.length }} 字 → 现 {{ preview.text.length }} 字</span>
      </div>
      <div class="notebook-footer-actions">
        <slot />
        <template v-if="preview">
          <n-button size="small" @click="emit('discard')">放弃</n-button>
          <n-button type="primary" size="small" :disabled="stale" @click="emit('apply')">应用到正文</n-button>
        </template>
      </div>
    </footer>
  </section>
</template>

<style scoped>
.dictation-notebook {
  min-width: 0;
  min-height: 220px;
  height: var(--notebook-height, 56dvh);
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border-strong);
  border-radius: 10px;
  background: var(--surface-raised);
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
  transition: border-color 0.2s ease, box-shadow 0.2s ease;
}

.dictation-notebook:focus-within {
  border-color: var(--primary);
  box-shadow: 0 0 0 1px var(--primary-alpha-20);
}

.notebook-heading {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--border-subtle);
  background: var(--surface-raised);
  border-top-left-radius: 9px;
  border-top-right-radius: 9px;
  flex-wrap: wrap;
}

.notebook-heading strong {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}

.notebook-save {
  font-size: 12px;
  color: var(--text-muted);
}

.notebook-save.failed,
.notebook-save.conflict {
  color: var(--color-error);
  font-weight: 500;
}

.notebook-tools {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;
}

.notebook-editor {
  position: relative;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.notebook-codemirror {
  height: 100%;
}

:deep(.cm-scroller::-webkit-scrollbar) {
  width: 6px;
  height: 6px;
}

:deep(.cm-scroller::-webkit-scrollbar-thumb) {
  background: var(--border-subtle);
  border-radius: 3px;
}

:deep(.cm-scroller::-webkit-scrollbar-thumb:hover) {
  background: var(--border-strong);
}

.notebook-empty {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  pointer-events: none;
}

.notebook-empty__hint {
  margin: 0;
  color: var(--text-muted);
  font-size: 13px;
  pointer-events: auto;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 4px;
  transition: color 0.15s ease, background 0.15s ease;
}

.notebook-empty__hint:hover {
  color: var(--text-primary);
  background: var(--surface-subtle);
}

.notebook-start {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  border: 0;
  background: transparent;
  color: var(--text-primary);
  cursor: pointer;
  pointer-events: auto;
  padding: 12px 28px;
  border-radius: 14px;
  transition: transform 0.15s cubic-bezier(0.2, 0, 0, 1);
}

.notebook-start:hover:not(:disabled) {
  transform: translateY(-2px);
}

.notebook-start:active:not(:disabled) {
  transform: translateY(0);
}

.notebook-start__icon {
  display: grid;
  place-items: center;
  width: 68px;
  height: 68px;
  border-radius: 50%;
  background: var(--primary);
  color: var(--surface-canvas);
  box-shadow: 0 6px 20px var(--primary-alpha-20);
  transition: background 0.15s ease, box-shadow 0.15s ease;
}

.notebook-start__icon svg {
  width: 32px;
  height: 32px;
}

.notebook-start strong {
  font-size: 16px;
  font-weight: 600;
  letter-spacing: 0.5px;
}

.notebook-start:hover:not(:disabled) .notebook-start__icon {
  background: var(--primary-hover);
  box-shadow: 0 8px 24px var(--primary-alpha-20);
}

.notebook-start:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.has-empty-start :deep(.cm-placeholder) {
  visibility: hidden;
}

.voice-return-short {
  display: none;
}

.notebook-partial {
  display: flex;
  min-width: 0;
  align-items: baseline;
  gap: 10px;
  padding: 8px 16px;
  background: var(--surface-subtle);
  font-size: 13px;
  color: var(--text-secondary);
}

.notebook-partial__badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex: none;
  white-space: nowrap;
  color: var(--primary);
  font-weight: 500;
}

.pulse-indicator {
  display: inline-block;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--primary);
  box-shadow: 0 0 0 0 var(--primary-alpha-20);
  animation: badge-pulse 1.8s infinite ease-out;
}

@keyframes badge-pulse {
  0% { transform: scale(0.95); box-shadow: 0 0 0 0 var(--primary); }
  70% { transform: scale(1); box-shadow: 0 0 0 6px transparent; }
  100% { transform: scale(0.95); box-shadow: 0 0 0 0 transparent; }
}

.notebook-partial p {
  margin: 0;
  min-width: 0;
  flex: 1;
  max-height: 3.2em;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.is-listening .notebook-partial {
  padding: 6px 12px;
}

.is-listening .notebook-footer {
  padding: 6px 12px;
}

.notebook-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  padding: 10px 16px;
  border-top: 1px solid var(--border-subtle);
  background: var(--surface-raised);
  border-bottom-left-radius: 9px;
  border-bottom-right-radius: 9px;
}

.notebook-footer-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.notebook-count {
  font-size: 12px;
  color: var(--text-muted);
  background: var(--surface-subtle);
  padding: 2px 7px;
  border-radius: 4px;
}

.notebook-footer-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.notebook-error {
  font-size: 13px;
  color: var(--color-error);
  margin: 0;
  padding: 8px 16px;
  overflow-wrap: anywhere;
  background: var(--color-error-bg);
  border-top: 1px solid var(--border-subtle);
  display: flex;
  align-items: center;
  gap: 10px;
}

.notebook-preview-tabs {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 16px;
  font-size: 12px;
  color: var(--text-muted);
  border-bottom: 1px solid var(--border-subtle);
  background: var(--surface-subtle);
}

.notebook-preview-tabs button {
  border: 0;
  border-radius: 5px;
  padding: 5px 10px;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  font: inherit;
  transition: all 0.15s ease;
}

.notebook-preview-tabs button:hover:not([aria-pressed=true]) {
  color: var(--text-primary);
  background: var(--surface-raised);
}

.notebook-preview-tabs button[aria-pressed=true] {
  color: var(--primary);
  background: var(--primary-alpha-10);
  font-weight: 500;
}

.notebook-preview-readonly {
  margin-left: 4px;
  font-size: 11px;
  color: var(--text-muted);
  background: var(--surface-raised);
  padding: 2px 6px;
  border-radius: 4px;
}

@media (max-width: 600px) {
  .notebook-heading { padding: 8px 10px; gap: 6px; flex-wrap: nowrap; }
  .notebook-tools { gap: 2px; }
  .notebook-footer { padding: 8px 12px; }
  .voice-return-long { display: none; }
  .voice-return-short { display: inline; }
  .notebook-start__icon { width: 60px; height: 60px; }
  .notebook-start strong { font-size: 15px; }
}
</style>
