<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { NButton, useMessage } from "naive-ui";
import { useDictation, type Utterance } from "../composables/useDictation";
import RecordingDraftPane from "../components/RecordingDraftPane.vue";
import { validateTerms } from "../composables/dictationTerms";

const route = useRoute(), router = useRouter(), message = useMessage();
const d = useDictation();
const { session, recordingSession, history, error, busy, cleanupScope, level, connection, captureAction, active, recognizing, partial } = d;
const viewingOwnedTask = computed(() => recognizing.value && session.value?.id === recordingSession.value?.id);
const tab = ref("live");
watch(() => route.query.mode, value => { if (value === 'record' || value === 'live') tab.value = value; }, { immediate: true });
const terms = ref("");
const enableCleanup = ref(true);
const settingsOpen = ref(false);
const savedTerms = ref<string[]>([]), chosenTerms = ref<string[]>([]), newTerm = ref("");
try {
  const stored = JSON.parse(localStorage.getItem('dictation:terms') ?? '[]');
  if (!Array.isArray(stored) || !stored.every(value => typeof value === 'string')) throw new Error('invalid terms');
  savedTerms.value = validateTerms(stored);
} catch { error.value = '常用术语库无法读取，请检查浏览器存储；当前会话词表未改动。'; }
function storeTerms(values: string[]) {
  const validated = validateTerms(values);
  localStorage.setItem('dictation:terms', JSON.stringify(validated)); savedTerms.value = validated;
}
function addTerm(value: string) {
  if (!value.trim()) throw new Error('请输入需要保存的正确词，不能留空');
  storeTerms([...savedTerms.value, value]); newTerm.value = '';
}
function saveCorrectTerm() {
  const value = window.prompt('将正确词加入本浏览器常用术语（1–80字，不自动替换正文）', window.getSelection()?.toString() ?? '');
  if (value !== null) addTerm(value);
}
const audioPlayer = ref<HTMLAudioElement | null>(null), audioURL = ref(''), playingUid = ref<string | null>(null);
let playbackGeneration = 0;
function stopPlayback() { ++playbackGeneration; d.playback.release(audioPlayer.value); audioPlayer.value?.pause(); audioURL.value = ''; playingUid.value = null; }
function claimPlayback() { if (audioPlayer.value) void action(async () => d.playback.claim(audioPlayer.value!, () => { playingUid.value = null; })); }
const audioUtterances = computed(() => session.value?.utterances.filter(u => u.end_sample !== undefined && u.end_sample !== null) ?? []);
async function listen(u: Utterance) {
  if (d.capturing.value || d.local.capturing.value) throw new Error('请先暂停采音再回听，避免把播放声录入');
  if (playingUid.value === u.id && audioPlayer.value && !audioPlayer.value.paused) { audioPlayer.value.pause(); playingUid.value = null; return; }
  stopPlayback(); const generation = playbackGeneration;
  audioURL.value = `/api/dictation/sessions/${session.value!.id}/utterances/${encodeURIComponent(u.id)}/audio`;
  playingUid.value = u.id; await nextTick();
  if (generation === playbackGeneration && audioPlayer.value) await d.playback.play(audioPlayer.value, () => { playingUid.value = null; });
}
watch(() => session.value?.id, stopPlayback);
watch(tab, stopPlayback);
onBeforeUnmount(stopPlayback);
async function trial() { stopPlayback(); tab.value = 'record'; await nextTick(); await d.local.begin(); }
const uploadInput = ref<HTMLInputElement | null>(null);
const uploadLimit = computed(() => {
  const config = d.serverConfig.value;
  if (!config?.max_upload_mb || !config.max_upload_minutes) return '正在读取服务器限制';
  const minutes = config.max_upload_minutes;
  return `最多 ${config.max_upload_mb} MiB、${minutes % 60 ? `${minutes}分钟` : `${minutes / 60}小时`}`;
});
const documentTimeout = computed(() => d.serverConfig.value?.document_cleanup_timeout_seconds ?? session.value?.config.document_cleanup_timeout_seconds ?? 3600);
const editing = ref<{ id: string; revision: number; text: string } | null>(null);
const draftEditing = ref<{ revision: number; text: string } | null>(null);
const compact = computed(() => route.query.compact === "1");
const paperBody = ref<HTMLElement | null>(null);
const followLatest = ref(true);
function trackScroll() {
  const body = paperBody.value;
  if (body) followLatest.value = body.scrollHeight - body.scrollTop - body.clientHeight < 48;
}
function showLatest() {
  followLatest.value = true;
  if (paperBody.value) paperBody.value.scrollTop = paperBody.value.scrollHeight;
}
watch(() => [session.value?.id, session.value?.revision, partial.value.map(u => u.preview).join("\n")], (value, old) => {
  if (value[0] !== old?.[0]) followLatest.value = true;
  if (followLatest.value && !editing.value && !draftEditing.value) showLatest();
}, { flush: "post" });
watch(() => session.value?.id, id => {
  if (id && session.value) {
    terms.value = session.value.config.terms.join("\n");
    enableCleanup.value = session.value.config.cleanup;
  }
}, { immediate: true });
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
    const submittedTab = tab.value;
    await d.upload(file, termList(), enableCleanup.value);
    if (!error.value && tab.value === submittedTab) tab.value = "live";
  }
  if (uploadInput.value) uploadInput.value.value = "";
}
async function select(id: string) {
  await d.select(id);
  editing.value = null; draftEditing.value = null; tab.value = "live";
}
function removeSession(id: string) {
  if (window.confirm('删除这次服务器会话的音频和全部文字？本地录音草稿不会删除。此操作不能撤销。')) void action(() => d.remove(id));
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
  await Promise.all([d.loadHistory(), d.loadConfig()]);
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
      <button v-for="item in [{id:'live',text:'实时听写'},{id:'record',text:'录音转写'},{id:'upload',text:'导入音频'},{id:'history',text:'历史结果'}]" :key="item.id" :aria-current="tab === item.id ? 'page' : undefined" @click="tab = item.id; if(item.id === 'history') action(d.loadHistory)">{{ item.text }}</button>
    </nav>
    <section v-if="settingsOpen" class="dictation-settings" aria-label="听写设置">
      <label for="dictation-microphone">麦克风</label>
      <div class="dictation-inline">
        <select id="dictation-microphone" :value="d.selectedDeviceId.value" :disabled="d.capturing.value || d.local.capturing.value || !!captureAction || d.deviceBusy.value || d.local.busy.value" @change="action(async () => d.chooseMicrophone(($event.target as HTMLSelectElement).value))">
          <option value="" disabled>请列出并选择麦克风</option>
          <option v-if="d.selectedDeviceId.value && !d.microphoneDevices.value.some(item => item.deviceId === d.selectedDeviceId.value)" :value="d.selectedDeviceId.value">已保存的麦克风（列出设备以确认）</option>
          <option v-for="device in d.microphoneDevices.value" :key="device.deviceId" :value="device.deviceId">{{ device.label || '未命名麦克风' }}</option>
        </select>
        <n-button :loading="d.deviceBusy.value" :disabled="d.capturing.value || d.local.capturing.value || !!captureAction || d.local.busy.value" @click="action(d.listMicrophones)">授权并列出设备</n-button>
        <n-button :disabled="d.ownsCapture.value || d.local.active.value || d.local.busy.value || !!captureAction || d.deviceBusy.value" @click="action(trial)">本地试音（录音后试听）</n-button>
      </div>
      <p>首次选择后保存到本浏览器；设备不可用时请重新选择，不会悄悄切换。录音中请先暂停，再切换设备。试音只保存本地草稿，不调用识别或整理。</p>
      <label for="dictation-terms">少量术语 <span>每行一个或逗号分隔，最多50项</span></label>
      <textarea id="dictation-terms" v-model="terms" :disabled="active" rows="3" placeholder="WebSocket&#10;项目专名" />
      <label class="dictation-inline"><input type="checkbox" v-model="enableCleanup" :disabled="active" />使用逐句轻度整理（Luna · low）</label>
      <details><summary>本浏览器的常用术语（主动选用，不自动改写已有会话）</summary>
        <div class="dictation-inline"><input v-model="newTerm" aria-label="新常用术语" placeholder="一个正确词或项目名" /><n-button @click="action(async () => addTerm(newTerm))">保存常用术语</n-button></div>
        <div v-for="term in savedTerms" :key="term" class="dictation-inline"><label><input v-model="chosenTerms" type="checkbox" :value="term" />{{ term }}</label><n-button text size="small" :aria-label="`删除常用术语${term}`" @click="action(async () => { storeTerms(savedTerms.filter(value => value !== term)); chosenTerms = chosenTerms.filter(value => value !== term); })">移除</n-button></div>
        <n-button :disabled="active || !chosenTerms.length" @click="terms = validateTerms(chosenTerms).join('\n')">用于下次识别</n-button>
      </details>
      <p>CPU 流式 Paraformer → GPU Qwen 0.6B → Luna low。停止后可用 Luna high 整体整理全文，再预览确认。术语仅作参考，不强制替换。密钥沿用服务端运行设置。</p>
    </section>
    <p v-if="error" class="dictation-error" role="alert">{{ error }}</p>
    <p v-if="d.inputWarning.value" class="dictation-status" role="status">{{ d.inputWarning.value }}</p>

    <RecordingDraftPane v-if="tab === 'record'" :terms="termList()" :cleanup="enableCleanup" @history="tab = 'history'; action(d.loadHistory)" />
    <section v-else-if="tab === 'upload'" class="dictation-upload">
      <h3>从一段录音开始</h3>
      <p>支持 WAV、MP3、M4A、AAC、FLAC、OGG、WebM / MP4 音轨。{{ uploadLimit }}。</p>
      <p>保留原始识别与整理稿。整理失败时可只重试整理，无需重新转录。</p>
      <input ref="uploadInput" aria-label="选择录音文件" type="file" accept="audio/*,.mp4,.webm" :disabled="recognizing || active || busy || !!captureAction" @change="uploadChanged" />
      <div v-if="d.uploadProgress.value" role="status">
        <p>上传 {{ d.uploadProgress.value.loaded }} / {{ d.uploadProgress.value.total }} 字节</p>
        <progress :value="d.uploadProgress.value.loaded" :max="d.uploadProgress.value.total" aria-label="实际上传进度" />
        <n-button @click="d.cancelUpload">取消上传</n-button>
      </div>
      <div v-if="d.uploadUncertain.value"><p>提交结果不明确，请先检查历史，不会自动重新上传。</p><n-button @click="tab = 'history'; action(d.loadHistory)">检查历史</n-button><n-button @click="d.confirmUploadChecked">已检查，允许手动重试</n-button></div>
    </section>

    <section v-else-if="tab === 'history'" class="dictation-history" aria-label="听写历史">
      <p v-if="!history.length">本浏览器还没有语音输入记录。</p>
      <article v-for="item in history" :key="item.id">
        <div><strong>{{ new Date(item.created_at).toLocaleString() }}</strong><p>{{ item.mode === 'upload' ? '上传录音' : '实时听写' }} · {{ item.phase }}</p><p class="dictation-excerpt">{{ item.draft_text.slice(0, 120) || '没有已完成文字' }}</p></div>
        <div class="dictation-inline"><n-button @click="action(() => select(item.id))">打开</n-button><n-button type="error" quaternary @click="removeSession(item.id)">删除服务器录音</n-button></div>
      </article>
    </section>

    <template v-else>
      <section class="dictation-paper" aria-label="已完成正文">
        <div class="dictation-paper__heading"><span>已完成正文 · {{ completed.length }} 句</span><n-button v-if="!followLatest" text size="small" @click="showLatest">回到最新</n-button><span v-else>自动跟随最新</span></div>
        <div ref="paperBody" class="dictation-paper__body" role="region" aria-label="听写文字" tabindex="0" @scroll="trackScroll">
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
          <details v-if="audioUtterances.length" class="dictation-original"><summary>原始分句音频（全文稿未逐句对齐）</summary><div v-for="u in audioUtterances" :key="u.id"><n-button text size="small" @click="action(() => listen(u))">{{ playingUid === u.id ? '暂停回听' : `听原始分句 ${u.id}` }}</n-button></div></details>
          <div class="dictation-inline"><template v-if="draftEditing"><n-button @click="action(saveDraft)">保存全文</n-button><n-button quaternary @click="draftEditing = null">取消编辑</n-button></template><n-button v-else @click="draftEditing = { revision: session.revision, text: session.draft_text }">编辑全文</n-button></div>
        </template>
        <template v-else>
          <article v-for="u in completed" :key="u.id" class="dictation-utterance">
            <template v-if="editing?.id === u.id"><textarea v-model="editing.text" class="dictation-editor" :aria-label="`编辑第${u.id}句`" rows="3" /><div class="dictation-inline"><n-button @click="action(saveEdit)">保存</n-button><n-button quaternary @click="editing = null">取消编辑</n-button></div></template>
            <p v-else class="dictation-sentence">{{ u.text }}</p>
            <div class="dictation-utterance__meta"><span>{{ u.edited ? '人工已编辑 · ' : '' }}{{ states[u.status] || u.status }}{{ u.needs_review ? ' · 建议复核' : '' }}</span><n-button v-if="editing?.id !== u.id" text size="small" @click="edit(u)">编辑</n-button><n-button text size="small" @click="action(async () => saveCorrectTerm())">存为常用术语</n-button><n-button v-if="u.end_sample != null" text size="small" :disabled="d.capturing.value || d.local.capturing.value" @click="action(() => listen(u))">{{ playingUid === u.id ? '暂停回听' : '听这一句' }}</n-button><n-button v-if="u.status === 'cleanup_failed'" text size="small" :disabled="active || busy" @click="action(() => d.cleanup('utterance', u.id))">重试整理</n-button></div>
            <p v-if="u.error" class="dictation-status">{{ u.error }}</p>
            <details v-if="u.raw_text !== u.text || u.suggestion" class="dictation-original"><summary>原始识别 / 整理建议</summary><p>原始识别：{{ u.raw_text }}</p><p v-if="u.suggestion">迟到建议（未覆盖编辑）：{{ u.suggestion }}</p></details>
          </article>
        </template>
        <section v-if="partial.length" class="dictation-preview" aria-label="临时识别">
          <div v-for="u in partial" :key="u.id"><span>{{ states[u.status] }}</span><p>{{ u.preview || '已检测到语音，正在识别……' }}</p><small v-if="u.status === 'asr_failed'">{{ u.error }}；预览未进入正文和复制内容。</small><n-button v-if="u.status === 'asr_failed' && u.end_sample != null" text size="small" :disabled="d.capturing.value || d.local.capturing.value" @click="action(() => listen(u))">听失败分句</n-button></div>
        </section>
        </div>
      </section>
      <audio v-if="audioURL" ref="audioPlayer" :src="audioURL" controls aria-label="原句回听（分句音频，非逐字对齐）" @play="claimPlayback" @pause="playingUid = null" @ended="playingUid = null" @error="error = '无法播放此句音频，请检查会话是否仍存在、音频是否保留；可从原始录音恢复。'; playingUid = null" />
      <section v-if="!viewingOwnedTask" class="dictation-controls" aria-label="录音控制">
        <div class="dictation-listening" role="status"><span class="dictation-indicator" aria-hidden="true" /><strong>{{ session?.phase || '准备开始' }}</strong><span class="dictation-time">{{ duration }}</span><meter min="0" max="1" :value="level" aria-label="麦克风音量" /></div>
        <p v-if="connection" class="dictation-status" role="status">{{ connection }}</p>
        <div class="dictation-actions">
          <n-button v-if="captureAction === 'starting' && !recognizing" @click="action(() => d.stop(null))">结束准备</n-button>
          <n-button v-else-if="!active" type="primary" :loading="busy" :disabled="recognizing || !!captureAction || d.local.active.value || d.local.busy.value || d.uploadUncertain.value" @click="d.start(termList(), enableCleanup)">开始听写</n-button>
          <n-button v-else type="primary" :disabled="session?.status === 'draining'" @click="action(() => d.stop(session!.id))">{{ session?.status === 'draining' ? '正在完成尾部' : '停止' }}</n-button>
          <n-button v-if="recognizing" quaternary @click="d.showRecording">返回活动会话</n-button>
          <n-button v-if="active && session?.mode === 'upload'" :disabled="!!d.fileAction.value || !['listening','draining','paused'].includes(session.status)" @click="action(() => d.fileControl(session!.id, session!.status === 'paused' ? 'resume' : 'pause'))">{{ session.status === 'paused' ? '继续转写' : '暂停转写' }}</n-button>
          <n-button :disabled="!session?.draft_text" @click="copy">复制已完成文字</n-button>
          <a v-if="session?.draft_text" class="dictation-download" :href="`/api/dictation/sessions/${session.id}/draft.txt`">下载 TXT</a>
          <n-button v-if="active" quaternary @click="action(() => d.cancel(session!.id))">取消后续处理</n-button>
          <n-button v-if="session?.draft_text && !active" quaternary :loading="cleanupScope === 'document'" :disabled="busy" @click="action(() => d.cleanup('document'))">整理全文</n-button>
        </div>
        <p v-if="cleanupScope === 'document'" class="dictation-status" role="status">正在整体整理，最多等待{{ documentTimeout }}秒（总期限）；确认前不会修改正文。</p>
        <p v-if="session?.document_cleanup_error && !error && cleanupScope !== 'document'" class="dictation-error" role="alert">{{ session.document_cleanup_error }}</p>
        <p v-if="active && session?.mode === 'live'" class="dictation-status">本窗口只查看结果；暂停、继续请返回原录音窗口。这里仍可停止、复制和编辑。</p>
        <p v-if="session?.status === 'preparing'" class="dictation-status">模型准备期间请稍候；开始听音后再说话。</p>
        <p v-if="session?.status === 'draining'" class="dictation-status">麦克风已释放；最后几句仍在处理，原始识别可随时复制。</p>
      </section>
      <section v-else class="dictation-controls" aria-label="活动会话提示">
        <p class="dictation-status">当前会话由下方常驻面板控制，切换站内页面不会中断录音。</p>
        <a v-if="session?.draft_text" class="dictation-download" :href="`/api/dictation/sessions/${session.id}/draft.txt`">下载 TXT</a>
      </section>
      <section v-if="session?.document_suggestion" class="dictation-diff" aria-label="全文整理差异预览">
        <h3>全文整理预览</h3><p>确认前不会修改正文。请核对数字、否定与专有名词。</p>
        <p v-if="session.document_suggestion.metrics?.strategy === 'chunked'">长文超过单次处理范围，已分 {{ session.document_suggestion.metrics.chunk_count }} 段整理，请额外核对段落衔接。</p>
        <div class="dictation-diff__columns"><div><h4>当前正文</h4><pre>{{ session.document_suggestion.original }}</pre></div><div><h4>整理建议</h4><pre>{{ session.document_suggestion.text }}</pre></div></div>
        <n-button :disabled="busy" @click="action(() => d.cleanup('apply'))">确认应用全文稿</n-button>
      </section>
    </template>
  </section>
</template>

<style scoped>
.dictation-page { max-width: 960px; margin: 0 auto; gap: 20px; }
.dictation-page > audio { width: 100%; max-width: 560px; }
.dictation-settings select, .dictation-settings input:not([type=checkbox]) { padding: 8px; max-width: 100%; border: 1px solid var(--border-strong); border-radius: 6px; background: var(--surface-raised); color: var(--text-primary); }
.dictation-inline { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
.dictation-tabs { display: flex; gap: 24px; border-bottom: 1px solid var(--border-strong); }
.dictation-tabs button { background: none; border: 0; border-bottom: 2px solid transparent; color: var(--text-secondary); padding: 8px 0 12px; cursor: pointer; }
.dictation-tabs button[aria-current] { color: var(--primary); border-bottom-color: var(--primary); font-weight: 600; }
.dictation-settings { display: grid; gap: 12px; padding: 20px; background: var(--surface-subtle); border-radius: 8px; }
.dictation-settings label { font-weight: 600; }.dictation-settings label span { font-size: 12px; color: var(--text-secondary); font-weight: 400; }
.dictation-settings p, .dictation-upload p { margin: 0; color: var(--text-secondary); font-size: 13px; }
.dictation-settings textarea, .dictation-editor { width: 100%; padding: 12px; border: 1px solid var(--border-strong); border-radius: 8px; background: var(--surface-raised); color: var(--text-primary); resize: vertical; }
.dictation-error { padding: 12px 16px; border-left: 3px solid var(--color-error); background: var(--color-error-bg); color: var(--color-error); margin: 0; overflow-wrap: anywhere; }
.dictation-paper { background: var(--surface-raised); border: 1px solid var(--border-strong); border-radius: 8px; padding: 20px 24px; height: clamp(240px, 45dvh, 440px); display: flex; flex-direction: column; min-height: 0; }
.dictation-paper__heading { display: flex; justify-content: space-between; align-items: center; flex-shrink: 0; color: var(--text-muted); font-size: 12px; margin-bottom: 16px; }
.dictation-paper__body { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior-y: contain; scrollbar-gutter: stable; padding-right: 8px; }
.dictation-empty { display: grid; place-items: center; text-align: center; padding: 20px 0 36px; }
.dictation-empty h3 { font-size: 20px; font-weight: 550; margin: 16px 0 4px; }.dictation-empty p { color: var(--text-secondary); margin: 0 0 16px; }.dictation-empty small { color: var(--text-muted); }
.dictation-mic { width: 48px; height: 48px; padding: 12px; border-radius: 50%; color: var(--primary); background: var(--primary-alpha-10); }.dictation-mic svg { width: 24px; height: 24px; }
.dictation-utterance + .dictation-utterance { margin-top: 20px; padding-top: 20px; border-top: 1px solid var(--border-subtle); }
.dictation-sentence, .dictation-whole { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 18px; line-height: 1.9; margin: 0 0 12px; }
.dictation-utterance__meta { display: flex; flex-wrap: wrap; align-items: center; gap: 16px; color: var(--text-muted); font-size: 12px; }
.dictation-original { margin-top: 12px; font-size: 13px; color: var(--text-secondary); }.dictation-original summary { cursor: pointer; }.dictation-original p { white-space: pre-wrap; }
.dictation-preview { padding: 16px 0 0; border-top: 1px dashed var(--border-subtle); color: var(--text-secondary); }.dictation-preview span { font-size: 12px; color: var(--text-muted); }.dictation-preview p { margin: 4px 0; font-size: 17px; overflow-wrap: anywhere; }
.dictation-controls { padding: 20px 0; border-top: 1px solid var(--border-strong); }.dictation-listening { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }.dictation-listening strong { font-size: 13px; font-weight: 500; }.dictation-time { font-variant-numeric: tabular-nums; color: var(--text-muted); margin-left: auto; }.dictation-listening meter { width: 72px; }
.dictation-indicator { width: 8px; height: 8px; border-radius: 50%; background: var(--text-muted); }.dictation-indicator.is-on { background: var(--color-success); }
.dictation-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; margin-top: 20px; }.dictation-download { padding: 8px 0; text-underline-offset: 4px; }
.dictation-status { font-size: 12px; color: var(--text-secondary); margin: 12px 0 0; overflow-wrap: anywhere; }.dictation-upload { display: grid; gap: 16px; padding: 24px 0; }.dictation-upload h3 { margin: 0; }
.dictation-history article { display: flex; gap: 20px; justify-content: space-between; padding: 20px 0; border-bottom: 1px solid var(--border-subtle); }.dictation-history p { color: var(--text-secondary); margin: 4px 0; }.dictation-excerpt { font-size: 13px; overflow-wrap: anywhere; }
.dictation-diff { padding-top: 20px; border-top: 1px solid var(--border-strong); }.dictation-diff__columns { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }.dictation-diff pre { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; padding: 16px; background: var(--surface-subtle); max-height: 30dvh; overflow-y: auto; overscroll-behavior-y: contain; }.dictation-diff h4 { margin-bottom: 8px; }
.is-compact .dictation-paper { padding: 16px; height: clamp(200px, 45dvh, 380px); }.is-compact .page-heading h2 { font-size: 20px; }
@media(max-width:600px) { .dictation-paper { padding: 20px; }.dictation-diff__columns { grid-template-columns: 1fr; }.dictation-history article { flex-direction: column; }.dictation-sentence { font-size: 17px; } }
</style>
