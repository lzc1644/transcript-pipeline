<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { NButton, NModal, useMessage } from "naive-ui";
import { useDictation, type Utterance } from "../composables/useDictation";
import RecordingDraftPane from "../components/RecordingDraftPane.vue";
import { validateTerms } from "../composables/dictationTerms";

const route = useRoute(), router = useRouter(), message = useMessage();
const d = useDictation();
const { session, recordingSession, history, error, busy, cleanupScope, level, connection, captureAction, active, recognizing, partial } = d;
const viewingOwnedTask = computed(() => recognizing.value && session.value?.id === recordingSession.value?.id);
const tab = computed(() => ['record', 'upload', 'history'].includes(String(route.query.mode)) ? String(route.query.mode) : 'live');
function setTab(mode: string) { return router.replace({ query: { ...route.query, mode } }); }

const SETTINGS_STORAGE_KEY = "dictation:settings";

interface PersistedDictationSettings {
  terms?: string;
  enableCleanup?: boolean;
  cleanupModel?: string;
  cleanupEffort?: string;
  documentCleanupModel?: string;
  documentEffort?: string;
}

function loadPersistedSettings(): PersistedDictationSettings {
  try {
    const raw = localStorage.getItem(SETTINGS_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return typeof parsed === "object" && parsed !== null ? parsed : {};
  } catch {
    return {};
  }
}

const initialSettings = loadPersistedSettings();
const terms = ref(typeof initialSettings.terms === "string" ? initialSettings.terms : "");
const enableCleanup = ref(typeof initialSettings.enableCleanup === "boolean" ? initialSettings.enableCleanup : true);
const cleanupModel = ref(typeof initialSettings.cleanupModel === "string" ? initialSettings.cleanupModel : "");
const documentCleanupModel = ref(typeof initialSettings.documentCleanupModel === "string" ? initialSettings.documentCleanupModel : "");
const cleanupEffort = ref(typeof initialSettings.cleanupEffort === "string" ? initialSettings.cleanupEffort : "");
const documentEffort = ref(typeof initialSettings.documentEffort === "string" ? initialSettings.documentEffort : "");

function savePersistedSettings() {
  try {
    const data: PersistedDictationSettings = {
      terms: terms.value,
      enableCleanup: enableCleanup.value,
      cleanupModel: cleanupModel.value,
      cleanupEffort: cleanupEffort.value,
      documentCleanupModel: documentCleanupModel.value,
      documentEffort: documentEffort.value,
    };
    localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify(data));
  } catch {}
}

function resetSettingsToDefault() {
  localStorage.removeItem(SETTINGS_STORAGE_KEY);
  terms.value = "";
  enableCleanup.value = true;
  cleanupModel.value = "";
  cleanupEffort.value = "";
  documentCleanupModel.value = "";
  documentEffort.value = "";
  message.success("已恢复默认设置");
}

let syncingFromSession = false;
watch(
  [terms, enableCleanup, cleanupModel, cleanupEffort, documentCleanupModel, documentEffort],
  () => {
    if (!syncingFromSession) {
      savePersistedSettings();
    }
  }
);
const cleanupModels = [
  { value: "gpt-6-luna", label: "GPT-6 Luna" },
  { value: "gemini-3.5-flash-lite", label: "Gemini 3.5 Flash Lite" },
  { value: "gemini-3.8-flash-high", label: "Gemini 3.8 Flash High" },
  { value: "gpt-6.1-sol", label: "GPT-6.1 Sol" },
];
const effortOptions = [
  { value: "low", label: "低" }, { value: "medium", label: "中" }, { value: "high", label: "高" },
  { value: "xhigh", label: "很高" }, { value: "max", label: "最高" },
];
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
async function trial() { stopPlayback(); await setTab('record'); await nextTick(); await d.local.begin(); }
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
    syncingFromSession = true;
    terms.value = session.value.config.terms.join("\n");
    enableCleanup.value = session.value.config.cleanup;
    nextTick(() => { syncingFromSession = false; });
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
function edit(u: Utterance) {
  editing.value = { id: u.id, revision: u.revision, text: u.text };
  nextTick(() => {
    const el = document.getElementById(`dictation-utterance-${u.id}`);
    if (el && paperBody.value) {
      const bodyRect = paperBody.value.getBoundingClientRect();
      const elRect = el.getBoundingClientRect();
      if (elRect.bottom > bodyRect.bottom) {
        paperBody.value.scrollTop += (elRect.bottom - bodyRect.bottom + 16);
      } else if (elRect.top < bodyRect.top) {
        paperBody.value.scrollTop -= (bodyRect.top - elRect.top + 16);
      }
    }
    const ta = el?.querySelector('textarea');
    ta?.focus();
  });
}
async function saveEdit() {
  if (!editing.value) return;
  const buffer = editing.value;
  const u = completed.value.find(v => v.id === buffer.id);
  if (!u) return;
  await d.edit({ ...u, revision: buffer.revision }, buffer.text);
  editing.value = null;
}
function startDraftEditing() {
  if (!session.value) return;
  draftEditing.value = { revision: session.value.revision, text: session.value.draft_text };
  nextTick(() => {
    const el = document.getElementById('dictation-doc-editor');
    if (paperBody.value) {
      paperBody.value.scrollTop = 0;
    }
    const ta = el?.querySelector('textarea');
    ta?.focus();
  });
}
async function saveDraft() {
  if (!draftEditing.value) return;
  await d.editDraft(draftEditing.value.text, draftEditing.value.revision);
  draftEditing.value = null;
}
function startDictation() {
  void d.start(termList(), enableCleanup.value, cleanupModel.value, documentCleanupModel.value, cleanupEffort.value, documentEffort.value);
}
async function uploadChanged(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0];
  if (file) {
    const submittedTab = tab.value;
    await d.upload(file, termList(), enableCleanup.value, cleanupModel.value, documentCleanupModel.value,
      cleanupEffort.value, documentEffort.value);
    if (!error.value && route.path === '/dictation' && tab.value === submittedTab) await setTab('live');
  }
  if (uploadInput.value) uploadInput.value.value = "";
}
async function select(id: string) {
  await d.select(id);
  editing.value = null; draftEditing.value = null;
  if (route.path === '/dictation') await setTab('live');
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
      <div>
        <h2>语音输入</h2>
        <p v-if="!compact">说出来，整理成文。临时识别即时呈现，完成的文字可编辑、复制。</p>
      </div>
      <div class="dictation-header-actions">
        <n-button size="small" class="dictation-header-btn" @click="smallWindow">{{ compact ? '普通视图' : '紧凑小窗' }}</n-button>
        <n-button size="small" class="dictation-header-btn" :type="settingsOpen ? 'primary' : 'default'" :ghost="settingsOpen" :aria-expanded="settingsOpen" @click="settingsOpen = !settingsOpen">设置</n-button>
      </div>
    </div>

    <nav class="dictation-tabs" aria-label="语音输入方式">
      <button
        v-for="item in [{id:'live',text:'实时听写'},{id:'record',text:'录音转写'},{id:'upload',text:'导入音频'},{id:'history',text:'历史结果'}]"
        :key="item.id"
        type="button"
        :aria-current="tab === item.id ? 'page' : undefined"
        @click="setTab(item.id); if(item.id === 'history') action(d.loadHistory)"
      >
        {{ item.text }}
      </button>
    </nav>

    <!-- Settings Modal -->
    <n-modal
      v-model:show="settingsOpen"
      preset="card"
      title="听写设置"
      class="dictation-settings-modal"
      :style="{ width: 'min(660px, 94vw)', maxHeight: '88vh', borderRadius: '12px' }"
      :content-style="{ overflowY: 'auto', maxHeight: 'calc(88vh - 130px)', padding: '16px 22px' }"
      closable
      :auto-focus="false"
      @close="settingsOpen = false"
    >
      <section class="dictation-settings" aria-label="听写设置">
        <div class="dictation-settings__section">
          <label for="dictation-microphone" class="dictation-settings__title">麦克风设备</label>
          <div class="dictation-settings__row">
            <div class="dictation-select-wrap">
              <select
                id="dictation-microphone"
                :value="d.selectedDeviceId.value"
                :disabled="d.capturing.value || d.local.capturing.value || !!captureAction || d.deviceBusy.value || d.local.busy.value"
                @change="action(async () => d.chooseMicrophone(($event.target as HTMLSelectElement).value))"
              >
                <option value="" disabled>请列出并选择麦克风</option>
                <option v-if="d.selectedDeviceId.value && !d.microphoneDevices.value.some(item => item.deviceId === d.selectedDeviceId.value)" :value="d.selectedDeviceId.value">已保存的麦克风（列出设备以确认）</option>
                <option v-for="device in d.microphoneDevices.value" :key="device.deviceId" :value="device.deviceId">{{ device.label || '未命名麦克风' }}</option>
              </select>
            </div>
            <div class="dictation-settings__mic-actions">
              <n-button :loading="d.deviceBusy.value" :disabled="d.capturing.value || d.local.capturing.value || !!captureAction || d.local.busy.value" @click="action(d.listMicrophones)">授权并列出设备</n-button>
              <n-button :disabled="d.ownsCapture.value || d.local.active.value || d.local.busy.value || !!captureAction || d.deviceBusy.value" @click="action(trial)">本地试音（录音后试听）</n-button>
            </div>
          </div>
          <p class="dictation-settings__hint">首次选择后保存到本浏览器；设备不可用时请重新选择，不会悄悄切换。录音中请先暂停，再切换设备。试音只保存本地草稿，不调用识别或整理。</p>
        </div>

        <div class="dictation-settings__section">
          <label for="dictation-terms" class="dictation-settings__title">少量术语 <span class="dictation-settings__subhint">每行一个或逗号分隔，最多50项</span></label>
          <textarea id="dictation-terms" v-model="terms" :disabled="active" rows="3" placeholder="WebSocket&#10;项目专名" />
          <div class="dictation-settings__toggle">
            <label class="dictation-checkbox-label">
              <input type="checkbox" v-model="enableCleanup" :disabled="active" />
              <span>使用逐句轻度整理</span>
            </label>
          </div>
        </div>

        <div class="dictation-settings__section">
          <div class="dictation-model-grid">
            <div class="dictation-model-group">
              <label for="dictation-cleanup-model">逐句快速处理模型</label>
              <div class="dictation-select-wrap">
                <select id="dictation-cleanup-model" v-model="cleanupModel" :disabled="active">
                  <option value="">服务器默认（{{ d.serverConfig.value?.cleanup_model || '读取中' }}）</option>
                  <option v-for="model in cleanupModels" :key="model.value" :value="model.value">{{ model.label }}</option>
                </select>
              </div>
            </div>
            <div class="dictation-model-group">
              <label for="dictation-cleanup-effort">逐句思考强度</label>
              <div class="dictation-select-wrap">
                <select id="dictation-cleanup-effort" v-model="cleanupEffort" :disabled="active">
                  <option value="">默认（{{ d.serverConfig.value?.cleanup_reasoning_effort || '读取中' }}）</option>
                  <option v-for="effort in effortOptions" :key="effort.value" :value="effort.value">{{ effort.label }}</option>
                </select>
              </div>
            </div>
            <div class="dictation-model-group">
              <label for="dictation-document-model">全文整理模型</label>
              <div class="dictation-select-wrap">
                <select id="dictation-document-model" v-model="documentCleanupModel" :disabled="active">
                  <option value="">服务器默认（{{ d.serverConfig.value?.document_cleanup_model || '读取中' }}）</option>
                  <option v-for="model in cleanupModels" :key="model.value" :value="model.value">{{ model.label }}</option>
                </select>
              </div>
            </div>
            <div class="dictation-model-group">
              <label for="dictation-document-effort">全文思考强度</label>
              <div class="dictation-select-wrap">
                <select id="dictation-document-effort" v-model="documentEffort" :disabled="active">
                  <option value="">默认（{{ d.serverConfig.value?.document_reasoning_effort || '读取中' }}）</option>
                  <option v-for="effort in effortOptions" :key="effort.value" :value="effort.value">{{ effort.label }}</option>
                </select>
              </div>
            </div>
          </div>
        </div>

        <details class="dictation-settings__terms-library">
          <summary>本浏览器的常用术语（主动选用，不自动改写已有会话）</summary>
          <div class="dictation-settings__terms-content">
            <div class="dictation-term-add">
              <input v-model="newTerm" aria-label="新常用术语" placeholder="一个正确词或项目名" @keydown.enter.prevent="action(async () => addTerm(newTerm))" />
              <n-button type="primary" ghost @click="action(async () => addTerm(newTerm))">保存常用术语</n-button>
            </div>
            <div v-if="savedTerms.length" class="dictation-term-chips">
              <div v-for="term in savedTerms" :key="term" class="dictation-term-chip">
                <label class="dictation-term-chip__label">
                  <input v-model="chosenTerms" type="checkbox" :value="term" />
                  <span>{{ term }}</span>
                </label>
                <n-button text size="small" :aria-label="`删除常用术语${term}`" @click="action(async () => { storeTerms(savedTerms.filter(value => value !== term)); chosenTerms = chosenTerms.filter(value => value !== term); })">移除</n-button>
              </div>
            </div>
            <p v-else class="dictation-settings__hint">暂无保存的术语，可在上方输入添加，或在已识别的句子中点“存为常用术语”。</p>
            <div class="dictation-term-apply">
              <n-button type="primary" :disabled="active || !chosenTerms.length" @click="terms = validateTerms(chosenTerms).join('\n')">用于下次识别</n-button>
            </div>
          </div>
        </details>
        <p class="dictation-settings__hint">Qwen 负责最终识别；上面两项只处理识别后的文字，并在新会话中固定。密钥沿用运行设置。</p>
      </section>

      <template #footer>
        <p v-if="error" class="dictation-error dictation-settings__error" role="alert">{{ error }}</p>
        <div class="dictation-settings__footer">
          <div class="dictation-settings__footer-left">
            <span class="dictation-settings__saved-hint">修改已自动保存至本浏览器</span>
            <n-button text size="small" class="dictation-settings__reset-btn" @click="resetSettingsToDefault">恢复默认</n-button>
          </div>
          <n-button type="primary" size="medium" @click="settingsOpen = false">完成</n-button>
        </div>
      </template>
    </n-modal>

    <!-- Error / Status Notice -->
    <p v-if="error && !settingsOpen" class="dictation-error" role="alert">{{ error }}</p>
    <p v-if="d.inputWarning.value" class="dictation-status" role="status">{{ d.inputWarning.value }}</p>

    <!-- Recording Draft Tab -->
    <RecordingDraftPane
      v-if="tab === 'record'"
      :terms="termList()"
      :cleanup="enableCleanup"
      :cleanup-model="cleanupModel"
      :document-cleanup-model="documentCleanupModel"
      :cleanup-effort="cleanupEffort"
      :document-effort="documentEffort"
      @history="setTab('history'); action(d.loadHistory)"
    />

    <!-- Upload Audio Tab -->
    <section v-else-if="tab === 'upload'" class="dictation-upload">
      <h3>从一段录音开始</h3>
      <p>支持 WAV、MP3、M4A、AAC、FLAC、OGG、WebM / MP4 音轨。{{ uploadLimit }}。</p>
      <p>保留原始识别与整理稿。整理失败时可只重试整理，无需重新转录。</p>
      <input ref="uploadInput" aria-label="选择录音文件" type="file" accept="audio/*,.mp4,.webm" :disabled="recognizing || active || busy || !!captureAction" @change="uploadChanged" />
      <div v-if="d.uploadProgress.value" role="status" class="dictation-upload__progress">
        <p>上传 {{ d.uploadProgress.value.loaded }} / {{ d.uploadProgress.value.total }} 字节</p>
        <progress :value="d.uploadProgress.value.loaded" :max="d.uploadProgress.value.total" aria-label="实际上传进度" />
        <n-button @click="d.cancelUpload">取消上传</n-button>
      </div>
      <div v-if="d.uploadUncertain.value" class="dictation-upload__uncertain">
        <p>提交结果不明确，请先检查历史，不会自动重新上传。</p>
        <n-button @click="setTab('history'); action(d.loadHistory)">检查历史</n-button>
        <n-button @click="d.confirmUploadChecked">已检查，允许手动重试</n-button>
      </div>
    </section>

    <!-- History Tab -->
    <section v-else-if="tab === 'history'" class="dictation-history" aria-label="听写历史">
      <p v-if="!history.length" class="dictation-history__empty">本浏览器还没有语音输入记录。</p>
      <article v-for="item in history" :key="item.id" class="dictation-history__card">
        <div class="dictation-history__info">
          <strong>{{ new Date(item.created_at).toLocaleString() }}</strong>
          <span class="dictation-history__tag">{{ item.mode === 'upload' ? '上传录音' : '实时听写' }} · {{ item.phase }}</span>
          <p class="dictation-excerpt">{{ item.draft_text.slice(0, 120) || '没有已完成文字' }}</p>
        </div>
        <div class="dictation-history__actions">
          <n-button type="primary" ghost size="small" @click="action(() => select(item.id))">打开</n-button>
          <n-button type="error" quaternary size="small" @click="removeSession(item.id)">删除服务器录音</n-button>
        </div>
      </article>
    </section>

    <!-- Live Dictation (Default) -->
    <template v-else>
      <section class="dictation-paper" aria-label="已完成正文">
        <div class="dictation-paper__heading">
          <span class="dictation-paper__count">已完成正文 · {{ completed.length }} 句</span>
          <n-button v-if="!followLatest" text size="small" class="dictation-follow-btn" @click="showLatest">回到最新</n-button>
          <span v-else class="dictation-follow-status">自动跟随最新</span>
        </div>
        <div ref="paperBody" class="dictation-paper__body" role="region" aria-label="听写文字" tabindex="0" @scroll="trackScroll">
          <!-- Empty State -->
          <div v-if="!completed.length && session?.document_text === undefined" class="dictation-empty">
            <button
              v-if="!active"
              type="button"
              class="dictation-mic-btn"
              :disabled="recognizing || !!captureAction || d.local.active.value || d.local.busy.value || d.uploadUncertain.value || busy"
              aria-label="开始听写"
              @click="startDictation"
            >
              <div class="dictation-mic" aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                  <rect x="9" y="2" width="6" height="13" rx="3"/>
                  <path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8"/>
                </svg>
              </div>
            </button>
            <div v-else class="dictation-mic is-active" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                <rect x="9" y="2" width="6" height="13" rx="3"/>
                <path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8"/>
              </svg>
            </div>
            <h3>{{ session?.status === 'waiting' ? '文件已保留，等待开始转写' : active && session?.mode === 'upload' ? '正在处理录音' : active ? '准备好后，请自然说话' : '把想说的话，留成文字' }}</h3>
            <div v-if="!active" class="dictation-empty__actions">
              <n-button
                type="primary"
                size="medium"
                :loading="busy"
                :disabled="recognizing || !!captureAction || d.local.active.value || d.local.busy.value || d.uploadUncertain.value"
                @click="startDictation"
              >
                开始听写
              </n-button>
            </div>
            <p v-else-if="session?.phase" class="dictation-empty-phase">{{ session.phase }}</p>
          </div>

          <!-- Whole Document Mode -->
          <template v-if="session?.document_text !== undefined">
            <div class="dictation-doc-card">
              <div class="dictation-doc-card__header">
                <span class="dictation-status-pill is-success">全文稿已应用 · 后台逐句整理不会覆盖它</span>
                <div v-if="draftEditing" class="dictation-doc-card__quick-actions">
                  <n-button type="primary" size="small" @click="action(saveDraft)">保存全文</n-button>
                  <n-button quaternary size="small" @click="draftEditing = null">取消编辑</n-button>
                </div>
                <n-button v-else size="small" @click="startDraftEditing">编辑全文</n-button>
              </div>

              <!-- Full text editor mode -->
              <div v-if="draftEditing" id="dictation-doc-editor" class="dictation-doc-editor">
                <textarea
                  v-model="draftEditing.text"
                  class="dictation-editor dictation-editor--doc"
                  aria-label="编辑全文"
                  rows="6"
                  @keydown.ctrl.enter.prevent="action(saveDraft)"
                  @keydown.meta.enter.prevent="action(saveDraft)"
                  @keydown.esc="draftEditing = null"
                />
                <div class="dictation-editor-footer">
                  <span class="dictation-edit-hint">字数：{{ draftEditing.text.length }} · 支持 Ctrl+Enter 快捷保存</span>
                  <div class="dictation-inline">
                    <n-button type="primary" size="small" @click="action(saveDraft)">保存全文</n-button>
                    <n-button quaternary size="small" @click="draftEditing = null">取消编辑</n-button>
                  </div>
                </div>
              </div>

              <!-- Full text display mode -->
              <p v-else class="dictation-whole">{{ session.draft_text }}</p>

              <details v-if="audioUtterances.length" class="dictation-original">
                <summary>原始分句音频（全文稿未逐句对齐）</summary>
                <div class="dictation-audio-chips">
                  <div v-for="u in audioUtterances" :key="u.id">
                    <n-button text size="small" @click="action(() => listen(u))">
                      {{ playingUid === u.id ? '暂停回听' : `听原始分句 ${u.id}` }}
                    </n-button>
                  </div>
                </div>
              </details>
            </div>
          </template>

          <!-- Sentence-by-sentence Mode -->
          <template v-else>
            <article
              v-for="u in completed"
              :key="u.id"
              :id="`dictation-utterance-${u.id}`"
              class="dictation-utterance"
              :class="{ 'is-editing': editing?.id === u.id }"
            >
              <!-- Sentence Editing State -->
              <template v-if="editing?.id === u.id">
                <div :id="`utterance-editor-${u.id}`" class="dictation-edit-card">
                  <div class="dictation-edit-card__header">
                    <div class="dictation-edit-card__title">
                      <span class="dictation-edit-badge">编辑第 {{ u.id }} 句</span>
                      <span class="dictation-status-pill">{{ states[u.status] || u.status }}</span>
                    </div>
                    <div class="dictation-edit-card__quick-actions">
                      <span class="dictation-edit-hint">按 Ctrl+Enter 快捷保存</span>
                      <n-button type="primary" size="tiny" class="dictation-edit-btn-top" @click="action(saveEdit)">保存</n-button>
                      <n-button quaternary size="tiny" class="dictation-edit-btn-top" @click="editing = null">取消</n-button>
                    </div>
                  </div>
                  <textarea
                    v-model="editing.text"
                    class="dictation-editor"
                    :aria-label="`编辑第${u.id}句`"
                    rows="3"
                    @keydown.ctrl.enter.prevent="action(saveEdit)"
                    @keydown.meta.enter.prevent="action(saveEdit)"
                    @keydown.esc="editing = null"
                  />
                  <div class="dictation-edit-card__footer">
                    <div v-if="u.raw_text && u.raw_text !== editing.text" class="dictation-edit-ref">
                      <small>原始识别：{{ u.raw_text }}</small>
                    </div>
                    <div class="dictation-edit-actions">
                      <n-button type="primary" size="small" @click="action(saveEdit)">保存</n-button>
                      <n-button quaternary size="small" @click="editing = null">取消编辑</n-button>
                    </div>
                  </div>
                </div>
              </template>

              <!-- Sentence Normal Display State -->
              <template v-else>
                <p class="dictation-sentence">{{ u.text }}</p>
                <div class="dictation-utterance__meta">
                  <span class="dictation-status-pill" :class="{ 'is-review': u.needs_review }">
                    {{ u.edited ? '人工已编辑 · ' : '' }}{{ states[u.status] || u.status }}{{ u.needs_review ? ' · 建议复核' : '' }}
                  </span>
                  <div class="dictation-utterance__actions">
                    <n-button text size="small" @click="edit(u)">编辑</n-button>
                    <n-button text size="small" @click="action(async () => saveCorrectTerm())">存为常用术语</n-button>
                    <n-button
                      v-if="u.end_sample != null"
                      text
                      size="small"
                      :disabled="d.capturing.value || d.local.capturing.value"
                      @click="action(() => listen(u))"
                    >
                      {{ playingUid === u.id ? '暂停回听' : '听这一句' }}
                    </n-button>
                    <n-button
                      v-if="u.status === 'cleanup_failed'"
                      text
                      size="small"
                      :disabled="active || busy"
                      @click="action(() => d.cleanup('utterance', u.id))"
                    >
                      重试整理
                    </n-button>
                  </div>
                </div>
              </template>

              <p v-if="u.error" class="dictation-status">{{ u.error }}</p>
              <details v-if="(u.raw_text !== u.text || u.suggestion) && editing?.id !== u.id" class="dictation-original">
                <summary>原始识别 / 整理建议</summary>
                <p>原始识别：{{ u.raw_text }}</p>
                <p v-if="u.suggestion">迟到建议（未覆盖编辑）：{{ u.suggestion }}</p>
              </details>
            </article>
          </template>

          <!-- Partial Real-time Preview -->
          <section v-if="partial.length" class="dictation-preview" aria-label="临时识别">
            <div v-for="u in partial" :key="u.id" class="dictation-preview__item">
              <span class="dictation-status-pill">{{ u.status === "finalizing" && u.asr_queue_state !== "running" ? "正在等待共享识别" : states[u.status] }}</span>
              <p>{{ u.preview || '已检测到语音，正在识别……' }}</p>
              <small v-if="u.status === 'asr_failed'" class="dictation-preview__error">{{ u.error }}；预览未进入正文和复制内容。</small>
              <n-button v-if="u.status === 'asr_failed' && u.end_sample != null" text size="small" :disabled="d.capturing.value || d.local.capturing.value" @click="action(() => listen(u))">听失败分句</n-button>
            </div>
          </section>
        </div>
      </section>

      <!-- Audio Player for Utterances -->
      <audio
        v-if="audioURL"
        ref="audioPlayer"
        :src="audioURL"
        controls
        class="dictation-audio-player"
        aria-label="原句回听（分句音频，非逐字对齐）"
        @play="claimPlayback"
        @pause="playingUid = null"
        @ended="playingUid = null"
        @error="error = '无法播放此句音频，请检查会话是否仍存在、音频是否保留；可从原始录音恢复。'; playingUid = null"
      />

      <!-- Controls Panel -->
      <section v-if="!viewingOwnedTask" class="dictation-controls" aria-label="录音控制">
        <div class="dictation-listening" role="status">
          <span class="dictation-indicator" :class="{ 'is-on': active }" aria-hidden="true" />
          <strong>{{ session?.phase || '准备开始' }}</strong>
          <span class="dictation-time">{{ duration }}</span>
          <meter min="0" max="1" :value="level" aria-label="麦克风音量" />
        </div>
        <p v-if="connection" class="dictation-status" role="status">{{ connection }}</p>
        <div class="dictation-actions">
          <n-button v-if="captureAction === 'starting' && !recognizing" size="medium" @click="action(() => d.stop(null))">结束准备</n-button>
          <n-button
            v-else-if="!active"
            type="primary"
            size="medium"
            :loading="busy"
            :disabled="recognizing || !!captureAction || d.local.active.value || d.local.busy.value || d.uploadUncertain.value"
            @click="startDictation"
          >
            开始听写
          </n-button>
          <n-button
            v-else
            type="primary"
            size="medium"
            :disabled="session?.status === 'draining'"
            @click="action(() => d.stop(session!.id))"
          >
            {{ session?.status === 'draining' ? '正在完成尾部' : '停止' }}
          </n-button>
          <n-button v-if="recognizing" quaternary size="medium" @click="d.showRecording">返回活动会话</n-button>
          <n-button v-if="session?.status === 'waiting'" size="medium" :loading="!!d.fileAction.value" @click="action(() => d.fileControl(session!.id, 'resume'))">重试开始转写</n-button>
          <n-button
            v-if="active && session?.mode === 'upload' && session.status !== 'waiting'"
            size="medium"
            :disabled="!!d.fileAction.value || !['listening','draining','paused'].includes(session.status)"
            @click="action(() => d.fileControl(session!.id, session!.status === 'paused' ? 'resume' : 'pause'))"
          >
            {{ session.status === 'paused' ? '继续转写' : '暂停转写' }}
          </n-button>
          <n-button :disabled="!session?.draft_text" size="medium" @click="copy">复制已完成文字</n-button>
          <a v-if="session?.draft_text" class="dictation-download" :href="`/api/dictation/sessions/${session.id}/draft.txt`">下载 TXT</a>
          <n-button v-if="active" quaternary size="medium" @click="action(() => d.cancel(session!.id))">取消后续处理</n-button>
          <n-button v-if="session?.draft_text && !active" size="medium" :loading="cleanupScope === 'document'" :disabled="busy" @click="action(() => d.cleanup('document'))">整理全文</n-button>
        </div>
        <p v-if="cleanupScope === 'document'" class="dictation-status" role="status">正在整体整理，最多等待{{ documentTimeout }}秒（总期限）；确认前不会修改正文。</p>
        <p v-if="session?.document_cleanup_error && !error && cleanupScope !== 'document'" class="dictation-error" role="alert">{{ session.document_cleanup_error }}</p>
        <p v-if="active && session?.mode === 'live'" class="dictation-status">本窗口只查看结果；暂停、继续请返回原录音窗口。这里仍可停止、复制和编辑。</p>
        <p v-if="d.serverConfig.value?.max_active_sessions" class="dictation-status">识别容量：最多 {{ d.serverConfig.value.max_active_sessions }} 个活动会话；多人可同时录音，按句轮转识别。</p>
        <p v-if="session?.status === 'preparing'" class="dictation-status">模型准备期间请稍候；开始听音后再说话。</p>
        <p v-if="session?.status === 'draining'" class="dictation-status">麦克风已释放；最后几句仍在处理，原始识别可随时复制。</p>
      </section>

      <!-- Controlled by Dock Notice -->
      <section v-else class="dictation-controls" aria-label="活动会话提示">
        <p class="dictation-status">当前会话由下方常驻面板控制，切换站内页面不会中断录音。</p>
        <a v-if="session?.draft_text" class="dictation-download" :href="`/api/dictation/sessions/${session.id}/draft.txt`">下载 TXT</a>
      </section>

      <!-- Full-text Diff Preview -->
      <section v-if="session?.document_suggestion" class="dictation-diff" aria-label="全文整理差异预览">
        <h3>全文整理预览</h3>
        <p>确认前不会修改正文。请核对数字、否定与专有名词。</p>
        <p v-if="session.document_suggestion.metrics?.strategy === 'chunked'">长文超过单次处理范围，已分 {{ session.document_suggestion.metrics.chunk_count }} 段整理，请额外核对段落衔接。</p>
        <div class="dictation-diff__columns">
          <div class="dictation-diff__box">
            <h4>当前正文</h4>
            <pre>{{ session.document_suggestion.original }}</pre>
          </div>
          <div class="dictation-diff__box">
            <h4>整理建议</h4>
            <pre>{{ session.document_suggestion.text }}</pre>
          </div>
        </div>
        <div class="dictation-diff__actions">
          <n-button type="primary" :disabled="busy" @click="action(() => d.cleanup('apply'))">确认应用全文稿</n-button>
        </div>
      </section>
    </template>
  </section>
</template>

<style scoped>
/* Page Layout */
.dictation-page {
  max-width: 960px;
  margin: 0 auto;
  gap: 20px;
}
.page-heading {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  flex-wrap: wrap;
  gap: 12px;
}
.dictation-header-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.dictation-header-btn {
  border-radius: 6px;
}

/* Tab Navigation */
.dictation-tabs {
  display: flex;
  gap: 6px;
  padding: 4px;
  background: var(--surface-subtle);
  border-radius: 8px;
  border: 1px solid var(--border-subtle);
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  scrollbar-width: none;
}
.dictation-tabs::-webkit-scrollbar {
  display: none;
}
.dictation-tabs button {
  flex: 1;
  min-width: fit-content;
  background: transparent;
  border: 0;
  border-radius: 6px;
  color: var(--text-secondary);
  padding: 8px 16px;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  white-space: nowrap;
  transition: all var(--motion-fast) var(--ease-out);
}
.dictation-tabs button:hover {
  color: var(--text-primary);
  background: var(--surface-raised);
}
.dictation-tabs button[aria-current] {
  color: var(--primary);
  background: var(--surface-canvas);
  font-weight: 600;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
}

/* Settings Modal & Content */
.dictation-settings {
  display: grid;
  gap: 18px;
  padding: 0;
  background: transparent;
  border: none;
}
.dictation-settings__footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  width: 100%;
  gap: 12px;
  flex-wrap: wrap;
}
.dictation-settings__error.dictation-error {
  margin-bottom: 12px;
}
.dictation-settings__footer-left {
  display: flex;
  align-items: center;
  gap: 12px;
}
.dictation-settings__saved-hint {
  font-size: 12px;
  color: var(--text-muted);
}
.dictation-settings__reset-btn {
  font-size: 12px;
  color: var(--text-secondary);
}
.dictation-settings__section {
  display: grid;
  gap: 8px;
}
.dictation-settings__section + .dictation-settings__section {
  padding-top: 16px;
  border-top: 1px solid var(--border-subtle);
}
.dictation-settings__title {
  font-size: 14px;
  font-weight: 650;
  color: var(--text-primary);
}
.dictation-settings__subhint {
  font-size: 12px;
  color: var(--text-secondary);
  font-weight: 400;
  margin-left: 6px;
}
.dictation-settings__row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
}
.dictation-settings__mic-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.dictation-settings__hint {
  margin: 0;
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.6;
}
.dictation-select-wrap {
  position: relative;
  display: inline-block;
  min-width: 220px;
  flex: 1;
}
.dictation-select-wrap::after {
  content: "";
  position: absolute;
  right: 12px;
  top: 50%;
  transform: translateY(-50%);
  width: 0;
  height: 0;
  border-left: 4px solid transparent;
  border-right: 4px solid transparent;
  border-top: 5px solid var(--text-secondary);
  pointer-events: none;
}
.dictation-settings select,
.dictation-settings input:not([type=checkbox]),
.dictation-settings textarea {
  width: 100%;
  height: 36px;
  padding: 0 32px 0 12px;
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  background: var(--surface-raised);
  color: var(--text-primary);
  font-family: inherit;
  font-size: 14px;
  appearance: none;
  -webkit-appearance: none;
  box-sizing: border-box;
  transition: border-color var(--motion-fast) var(--ease-out), box-shadow var(--motion-fast) var(--ease-out);
}
.dictation-settings textarea {
  height: auto;
  padding: 10px 12px;
  line-height: 1.6;
  resize: vertical;
}
.dictation-settings select:focus,
.dictation-settings input:not([type=checkbox]):focus,
.dictation-settings textarea:focus {
  outline: none;
  border-color: var(--primary);
  box-shadow: 0 0 0 2px var(--primary-alpha-20);
}
.dictation-settings select:disabled,
.dictation-settings textarea:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.dictation-checkbox-label {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 500;
  color: var(--text-primary);
  cursor: pointer;
  user-select: none;
}
.dictation-checkbox-label input[type="checkbox"] {
  width: 16px;
  height: 16px;
  accent-color: var(--primary);
  cursor: pointer;
}

/* Model Grid in Settings */
.dictation-model-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}
.dictation-model-group {
  display: grid;
  gap: 6px;
  min-width: 0;
}
.dictation-model-group label {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

/* Saved Terms Library */
.dictation-settings__terms-library {
  border-top: 1px solid var(--border-subtle);
  padding-top: 14px;
}
.dictation-settings__terms-library summary {
  cursor: pointer;
  font-weight: 600;
  color: var(--text-primary);
  padding: 4px 0;
}
.dictation-settings__terms-library summary:hover {
  color: var(--primary);
}
.dictation-settings__terms-content {
  display: grid;
  gap: 12px;
  padding-top: 12px;
}
.dictation-term-add {
  display: flex;
  align-items: center;
  gap: 8px;
}
.dictation-term-add input {
  flex: 1;
}
.dictation-term-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  max-height: 160px;
  overflow-y: auto;
  padding: 4px 0;
}
.dictation-term-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  background: var(--surface-raised);
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  font-size: 13px;
}
.dictation-term-chip__label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
}
.dictation-term-apply {
  padding-top: 4px;
}

/* Alerts */
.dictation-error {
  padding: 12px 16px;
  border-left: 3px solid var(--color-error);
  background: var(--color-error-bg);
  color: var(--color-error);
  margin: 0;
  border-radius: 0 6px 6px 0;
  font-size: 13px;
  overflow-wrap: anywhere;
}
.dictation-status {
  font-size: 12px;
  color: var(--text-secondary);
  margin: 8px 0 0;
  overflow-wrap: anywhere;
}

/* Status Pill */
.dictation-status-pill {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: 4px;
  background: var(--surface-subtle);
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 500;
}
.dictation-status-pill.is-success {
  background: var(--color-success-bg);
  color: var(--color-success);
}
.dictation-status-pill.is-review {
  background: var(--color-warning-bg);
  color: var(--color-warning);
}

/* Main Paper Container */
.dictation-paper {
  background: var(--surface-raised);
  border: 1px solid var(--border-strong);
  border-radius: 8px;
  padding: 20px 24px;
  height: clamp(240px, 45dvh, 440px);
  display: flex;
  flex-direction: column;
  min-height: 0;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
}
.dictation-paper__heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-shrink: 0;
  color: var(--text-muted);
  font-size: 12px;
  margin-bottom: 16px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border-subtle);
}
.dictation-paper__count {
  font-weight: 600;
  color: var(--text-secondary);
}
.dictation-follow-btn {
  color: var(--primary);
  font-weight: 600;
}
.dictation-follow-status {
  color: var(--text-muted);
}
.dictation-paper__body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior-y: contain;
  scrollbar-gutter: stable;
  padding-right: 8px;
}

/* Empty State */
.dictation-empty {
  display: grid;
  place-items: center;
  text-align: center;
  padding: 28px 0 32px;
  gap: 12px;
}
.dictation-mic-btn {
  background: transparent;
  border: none;
  padding: 0;
  cursor: pointer;
  border-radius: 50%;
  display: inline-flex;
  transition: transform var(--motion-fast) var(--ease-out);
}
.dictation-mic-btn:hover:not(:disabled) {
  transform: scale(1.08);
}
.dictation-mic-btn:active:not(:disabled) {
  transform: scale(0.96);
}
.dictation-mic-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.dictation-mic {
  width: 52px;
  height: 52px;
  padding: 14px;
  border-radius: 50%;
  color: var(--primary);
  background: var(--primary-alpha-10);
  transition: all var(--motion-fast) var(--ease-out);
}
.dictation-mic svg {
  width: 24px;
  height: 24px;
}
.dictation-mic.is-active {
  color: #fff;
  background: var(--primary);
  box-shadow: 0 0 0 6px var(--primary-alpha-20);
  animation: dictation-pulse 1.8s infinite;
}
@keyframes dictation-pulse {
  0% { box-shadow: 0 0 0 0 var(--primary-alpha-30); }
  70% { box-shadow: 0 0 0 10px transparent; }
  100% { box-shadow: 0 0 0 0 transparent; }
}
.dictation-empty h3 {
  font-size: 17px;
  font-weight: 600;
  color: var(--text-primary);
  margin: 0;
}
.dictation-empty__actions {
  margin-top: 4px;
}
.dictation-empty__actions .n-button {
  min-width: 120px;
}
.dictation-empty-phase {
  margin: 0;
  font-size: 13px;
  color: var(--text-secondary);
}

/* Utterance Item */
.dictation-utterance {
  position: relative;
  transition: background-color var(--motion-fast) var(--ease-out);
}
.dictation-utterance + .dictation-utterance {
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid var(--border-subtle);
}
.dictation-sentence, .dictation-whole {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 17px;
  line-height: 1.8;
  color: var(--text-primary);
  margin: 0 0 10px;
}
.dictation-utterance__meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  font-size: 12px;
  color: var(--text-muted);
}
.dictation-utterance__actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

/* Edit Card inside Utterance */
.dictation-edit-card {
  display: grid;
  gap: 10px;
  padding: 12px 14px;
  background: var(--surface-canvas);
  border: 1px solid var(--primary);
  border-radius: 8px;
  box-shadow: 0 0 0 1px var(--primary-alpha-20);
  animation: reveal var(--motion-fast) var(--ease-out);
}
.dictation-edit-card__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.dictation-edit-card__title {
  display: flex;
  align-items: center;
  gap: 8px;
}
.dictation-edit-card__quick-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.dictation-edit-badge {
  font-size: 12px;
  font-weight: 650;
  color: var(--primary);
}
.dictation-edit-hint {
  font-size: 11px;
  color: var(--text-muted);
}
.dictation-editor {
  width: 100%;
  padding: 10px 12px;
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  background: var(--surface-raised);
  color: var(--text-primary);
  font-family: inherit;
  font-size: 16px;
  line-height: 1.7;
  resize: vertical;
  box-sizing: border-box;
  transition: border-color var(--motion-fast) var(--ease-out), box-shadow var(--motion-fast) var(--ease-out);
}
.dictation-editor:focus {
  outline: none;
  border-color: var(--primary);
  box-shadow: 0 0 0 2px var(--primary-alpha-20);
}
.dictation-edit-card__footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
}
.dictation-edit-ref {
  color: var(--text-secondary);
  font-size: 12px;
}
.dictation-edit-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
}

/* Full Document Card & Editor */
.dictation-doc-card {
  display: grid;
  gap: 12px;
}
.dictation-doc-card__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border-subtle);
}
.dictation-doc-card__quick-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.dictation-doc-editor {
  display: grid;
  gap: 10px;
  padding: 12px 14px;
  background: var(--surface-canvas);
  border: 1px solid var(--primary);
  border-radius: 8px;
  animation: reveal var(--motion-fast) var(--ease-out);
}
.dictation-editor--doc {
  min-height: 120px;
  max-height: 200px;
}
.dictation-editor-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
}
.dictation-audio-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
}

/* Collapsible Original / Suggestion */
.dictation-original {
  margin-top: 10px;
  font-size: 13px;
  color: var(--text-secondary);
}
.dictation-original summary {
  cursor: pointer;
  color: var(--text-muted);
}
.dictation-original summary:hover {
  color: var(--primary);
}
.dictation-original p {
  white-space: pre-wrap;
  margin: 6px 0 0;
  padding: 8px 12px;
  background: var(--surface-subtle);
  border-radius: 6px;
}

/* Partial Recognition Preview */
.dictation-preview {
  padding: 16px 0 0;
  margin-top: 16px;
  border-top: 1px dashed var(--border-subtle);
  color: var(--text-secondary);
}
.dictation-preview__item {
  display: grid;
  gap: 4px;
}
.dictation-preview p {
  margin: 6px 0;
  font-size: 16px;
  line-height: 1.7;
  color: var(--text-primary);
  overflow-wrap: anywhere;
}
.dictation-preview__error {
  color: var(--color-error);
  font-size: 12px;
}

/* Audio Player */
.dictation-audio-player {
  width: 100%;
  max-width: 560px;
  height: 40px;
  margin-top: 8px;
}

/* Controls Bar */
.dictation-controls {
  padding: 16px 0 0;
  border-top: 1px solid var(--border-subtle);
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.dictation-listening {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 28px;
}
.dictation-listening strong {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}
.dictation-time {
  font-variant-numeric: tabular-nums;
  font-family: ui-monospace, monospace;
  font-size: 13px;
  font-weight: 500;
  color: var(--text-secondary);
  margin-left: auto;
  padding: 2px 8px;
  background: var(--surface-subtle);
  border-radius: 4px;
}
.dictation-listening meter {
  width: 80px;
  height: 8px;
}
.dictation-indicator {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--text-muted);
  transition: background-color var(--motion-fast) var(--ease-out);
}
.dictation-indicator.is-on {
  background: var(--color-success);
  box-shadow: 0 0 6px var(--color-success);
}
.dictation-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}
.dictation-download {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 34px;
  padding: 0 14px;
  font-size: 14px;
  font-weight: 500;
  border-radius: 6px;
  border: 1px solid var(--border-subtle);
  background: var(--surface-canvas);
  color: var(--text-primary);
  text-decoration: none;
  cursor: pointer;
  box-sizing: border-box;
  transition: all var(--motion-fast) var(--ease-out);
}
.dictation-download:hover {
  border-color: var(--primary);
  color: var(--primary);
  background: var(--primary-alpha-10);
}

/* Upload Tab */
.dictation-upload {
  display: grid;
  gap: 16px;
  padding: 24px 0;
}
.dictation-upload h3 {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
}
.dictation-upload p {
  margin: 0;
  color: var(--text-secondary);
  font-size: 13px;
}
.dictation-upload__progress,
.dictation-upload__uncertain {
  display: grid;
  gap: 8px;
  padding: 16px;
  background: var(--surface-subtle);
  border-radius: 8px;
}

/* History Tab */
.dictation-history {
  display: grid;
  gap: 12px;
}
.dictation-history__empty {
  color: var(--text-secondary);
  padding: 20px 0;
}
.dictation-history__card {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  padding: 16px;
  background: var(--surface-raised);
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  transition: border-color var(--motion-fast) var(--ease-out);
}
.dictation-history__card:hover {
  border-color: var(--border-strong);
}
.dictation-history__info {
  display: grid;
  gap: 4px;
  min-width: 0;
}
.dictation-history__info strong {
  font-size: 14px;
  color: var(--text-primary);
}
.dictation-history__tag {
  font-size: 12px;
  color: var(--text-muted);
}
.dictation-excerpt {
  font-size: 13px;
  color: var(--text-secondary);
  margin: 4px 0 0;
  overflow-wrap: anywhere;
}
.dictation-history__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

/* Diff Preview */
.dictation-diff {
  padding-top: 20px;
  border-top: 1px solid var(--border-strong);
  display: grid;
  gap: 14px;
}
.dictation-diff h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
}
.dictation-diff p {
  margin: 0;
  font-size: 13px;
  color: var(--text-secondary);
}
.dictation-diff__columns {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
}
.dictation-diff__box h4 {
  margin: 0 0 8px;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}
.dictation-diff pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font: inherit;
  font-size: 13px;
  line-height: 1.7;
  padding: 14px;
  background: var(--surface-subtle);
  border-radius: 6px;
  border: 1px solid var(--border-subtle);
  max-height: 30dvh;
  overflow-y: auto;
  overscroll-behavior-y: contain;
  margin: 0;
}
.dictation-diff__actions {
  padding-top: 4px;
}

/* Compact Mode */
.is-compact .dictation-paper {
  padding: 16px;
  height: clamp(200px, 45dvh, 380px);
}
.is-compact .page-heading h2 {
  font-size: 20px;
}

/* Mobile Responsive Optimization */
@media (max-width: 600px) {
  .dictation-page {
    gap: 16px;
  }
  .page-heading {
    gap: 8px;
  }
  .page-heading .dictation-header-actions {
    width: 100%;
    justify-content: flex-start;
  }
  .dictation-tabs {
    gap: 4px;
    padding: 3px;
  }
  .dictation-tabs button {
    padding: 7px 12px;
    font-size: 13px;
  }
  .dictation-settings {
    padding: 0;
    gap: 14px;
  }
  .dictation-settings select,
  .dictation-settings input:not([type=checkbox]),
  .dictation-settings textarea {
    font-size: 16px; /* Avoid iOS auto-zoom */
  }
  .dictation-settings__row {
    flex-direction: column;
    align-items: stretch;
  }
  .dictation-select-wrap {
    min-width: 100%;
    width: 100%;
  }
  .dictation-settings__mic-actions {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
    width: 100%;
  }
  .dictation-settings__mic-actions > .n-button {
    width: 100%;
  }
  .dictation-model-grid {
    grid-template-columns: 1fr;
  }
  .dictation-term-add {
    flex-direction: column;
    align-items: stretch;
  }
  .dictation-paper {
    padding: 14px 16px;
  }
  .dictation-sentence, .dictation-whole {
    font-size: 16px;
  }
  .dictation-editor {
    font-size: 16px;
  }
  .dictation-utterance__meta {
    gap: 8px;
  }
  .dictation-edit-card__header {
    flex-direction: column;
    align-items: flex-start;
    gap: 6px;
  }
  .dictation-edit-card__quick-actions {
    width: 100%;
    justify-content: space-between;
  }
  .dictation-edit-card__footer {
    flex-direction: row;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }
  .dictation-edit-actions {
    width: auto;
    margin-left: auto;
  }
  .dictation-actions {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 8px;
  }
  .dictation-actions > .n-button {
    width: 100%;
  }
  .dictation-actions > .n-button--primary-type,
  .dictation-actions > .n-button:first-child {
    grid-column: 1 / -1;
  }
  .dictation-actions .dictation-download {
    width: 100%;
  }
  .dictation-diff__columns {
    grid-template-columns: 1fr;
  }
  .dictation-history__card {
    flex-direction: column;
    align-items: flex-start;
  }
  .dictation-history__actions {
    width: 100%;
    justify-content: flex-end;
  }
}
</style>
