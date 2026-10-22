<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { NButton, useMessage } from "naive-ui";
import { useDictation, type Utterance } from "../composables/useDictation";

const route = useRoute(), router = useRouter(), message = useMessage();
const d = useDictation();
const { session, history, error, busy, level, connection, capturing, active, partial } = d;
const tab = ref("live");
const terms = ref("");
const enableCleanup = ref(true);
const settingsOpen = ref(false);
const uploadInput = ref<HTMLInputElement | null>(null);
const editing = ref<{ id: string; revision: number; text: string } | null>(null);
const draftEditing = ref<{ revision: number; text: string } | null>(null);
const compact = computed(() => route.query.compact === "1");
watch(() => session.value?.id, id => {
  if (id && session.value) {
    terms.value = session.value.config.terms.join("\n");
    enableCleanup.value = session.value.config.cleanup;
  }
});
const completed = computed(() => session.value?.utterances.filter(u => !!u.raw_text) ?? []);
const duration = computed(() => {
  const seconds = Math.floor((session.value?.samples ?? 0) / 16000);
  return `${Math.floor(seconds / 60).toString().padStart(2, "0")}:${(seconds % 60).toString().padStart(2, "0")}`;
});
const states: Record<string, string> = { raw: "原始识别完成", cleaning: "正在整理，原始文字可用", cleaned: "整理完成",
  cleanup_failed: "整理失败，原始文字可用", asr_failed: "最终识别失败 · 预览不是最终稿", finalizing: "正在最终识别", partial: "临时识别" };
function termList() { return terms.value.split(/[\n,，]/).map(v => v.trim()).filter(Boolean); }
async function action(callback: () => Promise<unknown>) {
  try { await callback(); }
  catch (cause) { error.value = cause instanceof Error ? cause.message : String(cause); }
}
async function copy() {
  const snapshot = session.value?.draft_text ?? "";
  try { await navigator.clipboard.writeText(snapshot); message.success("已复制当前已完成文字"); }
  catch { message.error("剪贴板不可用，请选中文字复制或下载 TXT"); }
}
function edit(u: Utterance) { editing.value = { id: u.id, revision: u.revision, text: u.text }; }
async function saveEdit() {
  if (!editing.value) return;
  const buffer = editing.value;
  const u = completed.value.find(v => v.id === buffer.id);
  if (!u) return;
  await d.edit({ ...u, revision: buffer.revision }, buffer.text);
  editing.value = null;
}
async function saveDraft() {
  if (!draftEditing.value) return;
  await d.editDraft(draftEditing.value.text, draftEditing.value.revision);
  draftEditing.value = null;
}
async function uploadChanged(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0];
  if (file) {
    await d.upload(file, termList(), enableCleanup.value);
    if (!error.value) tab.value = "live";
  }
  if (uploadInput.value) uploadInput.value.value = "";
}
async function select(id: string) {
  await d.select(id);
  editing.value = null; draftEditing.value = null; tab.value = "live";
}
function removeSession(id: string) {
  if (window.confirm('删除这次会话的音频和全部文字？此操作不能撤销。')) void action(() => d.remove(id));
}
function smallWindow() {
  if (compact.value) { void router.replace({ query: { ...route.query, compact: undefined } }); return; }
  const query = new URLSearchParams({ compact: "1" });
  if (session.value) query.set("session", session.value.id);
  const opened = window.open(`/dictation?${query}`, "dictation-small", "popup,width=540,height=720");
  if (!opened) message.warning("小窗被浏览器拦截，已切换为紧凑视图");
  if (!opened) void router.replace({ query: { ...route.query, compact: "1" } });
}
onMounted(() => action(async () => {
  await d.loadHistory();
  if (typeof route.query.session === "string") await d.select(route.query.session);
}));
</script>

<template>
  <section class="workbench-page dictation-page" :class="{ 'is-compact': compact }">
    <div class="page-heading">
      <div><h2>语音输入</h2><p v-if="!compact">说出来，整理成文。临时识别即时呈现，完成的文字可编辑、复制。</p></div>
      <div class="dictation-inline"><n-button size="small" @click="smallWindow">{{ compact ? '普通视图' : '紧凑小窗' }}</n-button><n-button size="small" :aria-expanded="settingsOpen" @click="settingsOpen = !settingsOpen">设置</n-button></div>
    </div>
    <nav class="dictation-tabs" aria-label="语音输入方式">
      <button v-for="item in [{id:'live',text:'实时听写'},{id:'upload',text:'上传录音'},{id:'history',text:'历史结果'}]" :key="item.id" :aria-current="tab === item.id ? 'page' : undefined" @click="tab = item.id; if(item.id === 'history') action(d.loadHistory)">{{ item.text }}</button>
    </nav>
    <section v-if="settingsOpen" class="dictation-settings" aria-label="听写设置">
      <label for="dictation-terms">少量术语 <span>每行一个或逗号分隔，最多50项</span></label>
      <textarea id="dictation-terms" v-model="terms" :disabled="active" rows="3" placeholder="WebSocket&#10;项目专名" />
      <label class="dictation-inline"><input type="checkbox" v-model="enableCleanup" :disabled="active" />使用轻度整理（Luna · none）</label>
      <p>CPU 流式 Paraformer → GPU Qwen 0.6B → Luna。开始后配置固定；术语仅用于最终识别和整理，不强制替换。密钥沿用服务端运行设置。</p>
    </section>
    <p v-if="error" class="dictation-error" role="alert">{{ error }}</p>

    <section v-if="tab === 'upload'" class="dictation-upload">
      <h3>从一段录音开始</h3>
      <p>支持 WAV、MP3、M4A、AAC、FLAC、OGG、WebM / MP4 音轨。默认最多100 MB、30分钟。</p>
      <p>保留原始识别与整理稿。整理失败时可只重试整理，无需重新转录。</p>
      <input ref="uploadInput" aria-label="选择录音文件" type="file" accept="audio/*,.mp4,.webm" :disabled="active || busy" @change="uploadChanged" />
      <p v-if="busy" role="status">正在上传录音……</p>
    </section>

    <section v-else-if="tab === 'history'" class="dictation-history" aria-label="听写历史">
      <p v-if="!history.length">本浏览器还没有语音输入记录。</p>
      <article v-for="item in history" :key="item.id">
        <div><strong>{{ new Date(item.created_at).toLocaleString() }}</strong><p>{{ item.mode === 'upload' ? '上传录音' : '实时听写' }} · {{ item.phase }}</p><p class="dictation-excerpt">{{ item.draft_text.slice(0, 120) || '没有已完成文字' }}</p></div>
        <div class="dictation-inline"><n-button :disabled="capturing" @click="action(() => select(item.id))">打开</n-button><n-button type="error" quaternary @click="removeSession(item.id)">删除</n-button></div>
      </article>
    </section>

    <template v-else>
      <section class="dictation-paper" aria-label="已完成正文">
        <div class="dictation-paper__heading"><span>已完成正文</span><span>{{ completed.length }} 句</span></div>
        <div v-if="!completed.length && session?.document_text === undefined" class="dictation-empty">
          <div class="dictation-mic" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="9" y="2" width="6" height="13" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8"/></svg></div>
          <h3>{{ active ? '准备好后，请自然说话' : '把想说的话，留成文字' }}</h3>
          <p>{{ active ? session?.phase : '点击开始，先允许浏览器使用麦克风。' }}</p>
          <small>音频与文字保存在服务器，历史中可明确删除。</small>
        </div>
        <template v-if="session?.document_text !== undefined">
          <p class="dictation-status">全文稿已应用 · 后台逐句整理不会覆盖它</p>
          <textarea v-if="draftEditing" v-model="draftEditing.text" class="dictation-editor" aria-label="编辑全文" rows="12" />
          <p v-else class="dictation-whole">{{ session.draft_text }}</p>
          <div class="dictation-inline"><template v-if="draftEditing"><n-button @click="action(saveDraft)">保存全文</n-button><n-button quaternary @click="draftEditing = null">取消编辑</n-button></template><n-button v-else @click="draftEditing = { revision: session.revision, text: session.draft_text }">编辑全文</n-button></div>
        </template>
        <template v-else>
          <article v-for="u in completed" :key="u.id" class="dictation-utterance">
            <template v-if="editing?.id === u.id"><textarea v-model="editing.text" class="dictation-editor" :aria-label="`编辑第${u.id}句`" rows="3" /><div class="dictation-inline"><n-button @click="action(saveEdit)">保存</n-button><n-button quaternary @click="editing = null">取消编辑</n-button></div></template>
            <p v-else class="dictation-sentence">{{ u.text }}</p>
            <div class="dictation-utterance__meta"><span>{{ u.edited ? '人工已编辑 · ' : '' }}{{ states[u.status] || u.status }}{{ u.needs_review ? ' · 建议复核' : '' }}</span><n-button v-if="editing?.id !== u.id" text size="small" @click="edit(u)">编辑</n-button><n-button v-if="u.status === 'cleanup_failed'" text size="small" :disabled="active || busy" @click="action(() => d.cleanup('utterance', u.id))">重试整理</n-button></div>
            <p v-if="u.error" class="dictation-status">{{ u.error }}</p>
            <details v-if="u.raw_text !== u.text || u.suggestion" class="dictation-original"><summary>原始识别 / 整理建议</summary><p>原始识别：{{ u.raw_text }}</p><p v-if="u.suggestion">迟到建议（未覆盖编辑）：{{ u.suggestion }}</p></details>
          </article>
        </template>
      </section>
      <section v-if="partial.length" class="dictation-preview" aria-label="临时识别">
        <div v-for="u in partial" :key="u.id"><span>{{ states[u.status] }}</span><p>{{ u.preview || '已检测到语音，正在识别……' }}</p><small v-if="u.status === 'asr_failed'">{{ u.error }}；预览未进入正文和复制内容。</small></div>
      </section>
      <section class="dictation-controls" aria-label="录音控制">
        <div class="dictation-listening" role="status"><span class="dictation-indicator" :class="{ 'is-on': capturing }" aria-hidden="true" /><strong>{{ session?.phase || '准备开始' }}</strong><span class="dictation-time">{{ duration }}</span><meter min="0" max="1" :value="level" aria-label="麦克风音量" /></div>
        <p v-if="connection" class="dictation-status" role="status">{{ connection }}</p>
        <div class="dictation-actions">
          <n-button v-if="!active" type="primary" :loading="busy" @click="d.start(termList(), enableCleanup)">开始听写</n-button>
          <n-button v-else type="primary" :disabled="session?.status === 'draining'" @click="action(d.stop)">{{ session?.status === 'draining' ? '正在完成尾部' : '停止' }}</n-button>
          <n-button :disabled="!session?.draft_text" @click="copy">复制已完成文字</n-button>
          <a v-if="session?.draft_text" class="dictation-download" :href="`/api/dictation/sessions/${session.id}/draft.txt`">下载 TXT</a>
          <n-button v-if="active" quaternary @click="action(d.cancel)">取消后续处理</n-button>
          <n-button v-if="session?.draft_text && !active" quaternary :disabled="busy" @click="action(() => d.cleanup('document'))">整理全文</n-button>
        </div>
        <p v-if="session?.status === 'preparing'" class="dictation-status">模型准备期间请稍候；开始听音后再说话。</p>
        <p v-if="session?.status === 'draining'" class="dictation-status">麦克风已释放；最后几句仍在处理，原始识别可随时复制。</p>
      </section>
      <section v-if="session?.document_suggestion && session.document_text === undefined" class="dictation-diff" aria-label="全文整理差异预览">
        <h3>全文整理预览</h3><p>确认前不会修改正文。请核对数字、否定与专有名词。</p>
        <div class="dictation-diff__columns"><div><h4>当前正文</h4><pre>{{ session.document_suggestion.original }}</pre></div><div><h4>整理建议</h4><pre>{{ session.document_suggestion.text }}</pre></div></div>
        <n-button :disabled="busy" @click="action(() => d.cleanup('apply'))">确认应用全文稿</n-button>
      </section>
    </template>
  </section>
</template>

<style scoped>
.dictation-page { max-width: 960px; margin: 0 auto; gap: 20px; }
.dictation-inline { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
.dictation-tabs { display: flex; gap: 24px; border-bottom: 1px solid var(--border-strong); }
.dictation-tabs button { background: none; border: 0; border-bottom: 2px solid transparent; color: var(--text-secondary); padding: 8px 0 12px; cursor: pointer; }
.dictation-tabs button[aria-current] { color: var(--primary); border-bottom-color: var(--primary); font-weight: 600; }
.dictation-settings { display: grid; gap: 12px; padding: 20px; background: var(--surface-subtle); border-radius: 8px; }
.dictation-settings label { font-weight: 600; }.dictation-settings label span { font-size: 12px; color: var(--text-secondary); font-weight: 400; }
.dictation-settings p, .dictation-upload p { margin: 0; color: var(--text-secondary); font-size: 13px; }
.dictation-settings textarea, .dictation-editor { width: 100%; padding: 12px; border: 1px solid var(--border-strong); border-radius: 8px; background: var(--surface-raised); color: var(--text-primary); resize: vertical; }
.dictation-error { padding: 12px 16px; border-left: 3px solid var(--color-error); background: var(--color-error-bg); color: var(--color-error); margin: 0; overflow-wrap: anywhere; }
.dictation-paper { background: var(--surface-raised); border: 1px solid var(--border-strong); border-radius: 8px; padding: 24px 28px; min-height: 280px; }
.dictation-paper__heading { display: flex; justify-content: space-between; color: var(--text-muted); font-size: 12px; margin-bottom: 20px; }
.dictation-empty { display: grid; place-items: center; text-align: center; padding: 20px 0 36px; }
.dictation-empty h3 { font-size: 20px; font-weight: 550; margin: 16px 0 4px; }.dictation-empty p { color: var(--text-secondary); margin: 0 0 16px; }.dictation-empty small { color: var(--text-muted); }
.dictation-mic { width: 48px; height: 48px; padding: 12px; border-radius: 50%; color: var(--primary); background: var(--primary-alpha-10); }.dictation-mic svg { width: 24px; height: 24px; }
.dictation-utterance + .dictation-utterance { margin-top: 20px; padding-top: 20px; border-top: 1px solid var(--border-subtle); }
.dictation-sentence, .dictation-whole { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 18px; line-height: 1.9; margin: 0 0 12px; }
.dictation-utterance__meta { display: flex; flex-wrap: wrap; align-items: center; gap: 16px; color: var(--text-muted); font-size: 12px; }
.dictation-original { margin-top: 12px; font-size: 13px; color: var(--text-secondary); }.dictation-original summary { cursor: pointer; }.dictation-original p { white-space: pre-wrap; }
.dictation-preview { padding: 0 4px; color: var(--text-secondary); }.dictation-preview span { font-size: 12px; color: var(--text-muted); }.dictation-preview p { margin: 4px 0; font-size: 17px; overflow-wrap: anywhere; }
.dictation-controls { padding: 20px 0; border-top: 1px solid var(--border-strong); }.dictation-listening { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }.dictation-listening strong { font-size: 13px; font-weight: 500; }.dictation-time { font-variant-numeric: tabular-nums; color: var(--text-muted); margin-left: auto; }.dictation-listening meter { width: 72px; }
.dictation-indicator { width: 8px; height: 8px; border-radius: 50%; background: var(--text-muted); }.dictation-indicator.is-on { background: var(--color-success); }
.dictation-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; margin-top: 20px; }.dictation-download { padding: 8px 0; text-underline-offset: 4px; }
.dictation-status { font-size: 12px; color: var(--text-secondary); margin: 12px 0 0; overflow-wrap: anywhere; }.dictation-upload { display: grid; gap: 16px; padding: 24px 0; }.dictation-upload h3 { margin: 0; }
.dictation-history article { display: flex; gap: 20px; justify-content: space-between; padding: 20px 0; border-bottom: 1px solid var(--border-subtle); }.dictation-history p { color: var(--text-secondary); margin: 4px 0; }.dictation-excerpt { font-size: 13px; overflow-wrap: anywhere; }
.dictation-diff { padding-top: 20px; border-top: 1px solid var(--border-strong); }.dictation-diff__columns { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }.dictation-diff pre { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; padding: 16px; background: var(--surface-subtle); }.dictation-diff h4 { margin-bottom: 8px; }
.is-compact .dictation-paper { padding: 20px; min-height: 180px; }.is-compact .page-heading h2 { font-size: 20px; }
@media(max-width:600px) { .dictation-paper { padding: 20px; }.dictation-diff__columns { grid-template-columns: 1fr; }.dictation-history article { flex-direction: column; }.dictation-sentence { font-size: 17px; } }
</style>
