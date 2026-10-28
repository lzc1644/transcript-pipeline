<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { NButton, NModal, useMessage } from "naive-ui";
import { useDictation, type Utterance } from "../composables/useDictation";
import DictationEditor from "../components/DictationEditor.vue";
import RecordingDraftPane from "../components/RecordingDraftPane.vue";
import { validateTerms } from "../composables/dictationTerms";

const route = useRoute(), router = useRouter(), message = useMessage(), d = useDictation();
const { session, recordingSession, history, error, busy, cleanupScope, connection, captureAction, active, recognizing, partial } = d;
const buffer = computed(() => d.documents.get(session.value?.id ?? ""));
const preview = computed(() => buffer.value.preview.value);
const emptyStart = computed(() => !active.value && !preview.value && !buffer.value.text.value.trim());
const startDisabled = computed(() => recognizing.value || !!captureAction.value || d.local.active.value || d.local.busy.value || d.uploadUncertain.value || busy.value);
const compact = computed(() => route.query.compact === "1");
const viewingOwnedTask = computed(() => recognizing.value && session.value?.id === recordingSession.value?.id);
const tab = computed(() => ['record', 'upload', 'history'].includes(String(route.query.mode)) ? String(route.query.mode) : 'live');
function setTab(mode: string) { return router.replace({ query: { ...route.query, mode } }); }

const SETTINGS_STORAGE_KEY = "dictation:settings";
function loadSettings() { try { return JSON.parse(localStorage.getItem(SETTINGS_STORAGE_KEY) ?? "{}"); } catch { return {}; } }
const initial = loadSettings();
const terms = ref(initial.terms ?? ""), enableCleanup = ref(initial.enableCleanup ?? true);
const cleanupModel = ref(initial.cleanupModel ?? ""), documentCleanupModel = ref(initial.documentCleanupModel ?? "");
const cleanupEffort = ref(initial.cleanupEffort ?? ""), documentEffort = ref(initial.documentEffort ?? "");
let syncingFromSession = false;

watch([terms, enableCleanup, cleanupModel, documentCleanupModel, cleanupEffort, documentEffort], () => {
  if (!syncingFromSession) localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify({
    terms: terms.value, enableCleanup: enableCleanup.value,
    cleanupModel: cleanupModel.value, documentCleanupModel: documentCleanupModel.value,
    cleanupEffort: cleanupEffort.value, documentEffort: documentEffort.value,
  }));
});

function resetSettingsToDefault() {
  localStorage.removeItem(SETTINGS_STORAGE_KEY);
  terms.value = "";
  enableCleanup.value = true;
  cleanupModel.value = documentCleanupModel.value = cleanupEffort.value = documentEffort.value = "";
  message.success("已恢复默认设置");
}

const cleanupModels = [
  { value: "gpt-6-luna", label: "GPT-6 Luna" },
  { value: "gemini-3.5-flash-lite", label: "Gemini 3.5 Flash Lite" },
  { value: "gemini-3.8-flash-high", label: "Gemini 3.8 Flash High" },
  { value: "gpt-6.1-sol", label: "GPT-6.1 Sol" },
];
const efforts = [
  { value: "low", label: "低" },
  { value: "medium", label: "中" },
  { value: "high", label: "高" },
  { value: "xhigh", label: "很高" },
  { value: "max", label: "最高" },
];

const settingsOpen = ref(false), recordsOpen = ref(false);
const savedTerms = ref<string[]>([]), chosenTerms = ref<string[]>([]), newTerm = ref("");
try { savedTerms.value = validateTerms(JSON.parse(localStorage.getItem('dictation:terms') ?? '[]')); }
catch { error.value = '常用术语库无法读取，请检查浏览器存储；当前会话词表未改动。'; }

function storeTerms(values: string[]) {
  const validated = validateTerms(values);
  localStorage.setItem('dictation:terms', JSON.stringify(validated));
  savedTerms.value = validated;
}

function addTerm(value: string) {
  if (!value.trim()) throw new Error('请输入需要保存的正确词，不能留空');
  storeTerms([...savedTerms.value, value]);
  newTerm.value = '';
}

function saveCorrectTerm(selected: string) {
  const value = window.prompt('将正确词加入本浏览器常用术语（1–80字，不自动替换正文）', selected);
  if (value !== null) void action(async () => addTerm(value));
}

function termList() {
  return terms.value.split(/[\n,，]/).map((v: string) => v.trim()).filter(Boolean);
}

const audioPlayer = ref<HTMLAudioElement | null>(null), audioURL = ref(''), playingUid = ref<string | null>(null);
let playbackGeneration = 0;

function stopPlayback() {
  ++playbackGeneration;
  d.playback.release(audioPlayer.value);
  audioPlayer.value?.pause();
  audioURL.value = '';
  playingUid.value = null;
}

function claimPlayback() {
  if (audioPlayer.value) void action(async () => d.playback.claim(audioPlayer.value!, () => { playingUid.value = null; }));
}

async function listen(u: Utterance) {
  if (d.capturing.value || d.local.capturing.value) throw new Error('请先暂停采音再回听，避免把播放声录入');
  if (playingUid.value === u.id && audioPlayer.value && !audioPlayer.value.paused) {
    audioPlayer.value.pause();
    playingUid.value = null;
    return;
  }
  stopPlayback();
  const generation = playbackGeneration;
  audioURL.value = `/api/dictation/sessions/${session.value!.id}/utterances/${encodeURIComponent(u.id)}/audio`;
  playingUid.value = u.id;
  await nextTick();
  if (generation === playbackGeneration && audioPlayer.value) await d.playback.play(audioPlayer.value, () => { playingUid.value = null; });
}

watch(() => session.value?.id, () => { stopPlayback(); recordsOpen.value = false; });
watch(tab, stopPlayback);
onBeforeUnmount(stopPlayback);

async function trial() {
  stopPlayback();
  settingsOpen.value = false;
  await setTab('record');
  await nextTick();
  await d.local.begin();
}

const uploadInput = ref<HTMLInputElement | null>(null);
const uploadLimit = computed(() => d.serverConfig.value ? `最多 ${d.serverConfig.value.max_upload_mb} MiB、${d.serverConfig.value.max_upload_minutes} 分钟` : '正在读取服务器限制');
const documentTimeout = computed(() => d.serverConfig.value?.document_cleanup_timeout_seconds ?? 3600);

const states: Record<string, string> = {
  raw: '原始识别完成',
  cleaning: '正在整理',
  cleaned: '整理完成',
  cleanup_failed: '整理失败',
  asr_failed: '最终识别失败',
  finalizing: '正在最终识别',
  partial: '临时识别',
};

const partialText = computed(() => partial.value.map(u => {
  if (u.status === 'asr_failed') return '分句识别失败，预览未写入正文；可在识别记录回听。';
  if (u.status === 'finalizing') return `${u.asr_queue_state === 'running' ? '正在最终识别' : '正在等待共享识别'}：${u.preview}`;
  return u.preview || '正在识别…';
}).join(' '));

async function action(callback: () => Promise<unknown>) {
  try { await callback(); }
  catch (cause) { error.value = cause instanceof Error ? cause.message : String(cause); }
}

async function copy() {
  try {
    await navigator.clipboard.writeText(preview.value?.text ?? buffer.value.text.value);
    message.success(preview.value ? '已复制预览' : '已复制正文');
  } catch {
    message.error('剪贴板不可用，请选中文字复制或下载 TXT');
  }
}

async function download() {
  if (!preview.value) {
    try { await buffer.value.flush(); }
    catch { message.warning('保存尚未成功，已下载当前本地编辑，请保留备份。'); }
  }
  const blob = new Blob([preview.value?.text ?? buffer.value.text.value], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = preview.value ? 'dictation-preview.txt' : 'dictation.txt';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function discard() {
  if (preview.value && preview.value.text !== preview.value.model && !window.confirm('预览稿已人工修改，确定放弃这些修改？')) return;
  if (session.value) await d.discardPreview(session.value.id);
}

async function startDictation() {
  if (preview.value) throw new Error('请先应用或放弃全文整理预览，再开始听写');
  await buffer.value.flush();
  const content = { text: buffer.value.text.value, position: buffer.value.cursor };
  await d.start(termList(), enableCleanup.value, cleanupModel.value, documentCleanupModel.value, cleanupEffort.value, documentEffort.value, { document: content });
}

async function uploadChanged(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0];
  if (file) {
    await buffer.value.flush();
    await d.upload(file, termList(), enableCleanup.value, cleanupModel.value, documentCleanupModel.value, cleanupEffort.value, documentEffort.value);
    if (!error.value) await setTab('live');
  }
  if (uploadInput.value) uploadInput.value.value = '';
}

async function select(id: string) {
  await d.select(id);
  await setTab('live');
}

function removeSession(id: string) {
  if (window.confirm('删除这次服务器会话的音频和全部文字？本地录音草稿不会删除。此操作不能撤销。')) {
    void action(() => d.remove(id));
  }
}

function smallWindow() {
  if (compact.value) {
    void router.replace({ query: { ...route.query, compact: undefined } });
    return;
  }
  const query = new URLSearchParams({ compact: '1' });
  if (session.value) query.set('session', session.value.id);
  if (!window.open(`/dictation?${query}`, 'dictation-small', 'popup,width=540,height=720')) {
    void router.replace({ query: { ...route.query, compact: '1' } });
  }
}

watch(() => session.value?.id, () => {
  if (session.value) {
    syncingFromSession = true;
    terms.value = session.value.config.terms.join('\n');
    enableCleanup.value = session.value.config.cleanup;
    nextTick(() => { syncingFromSession = false; });
  }
});

const notebookSlot = ref<HTMLElement>(), notebookTop = ref(230);
let observer: ResizeObserver | undefined;
function measure() {
  if (notebookSlot.value) notebookTop.value = notebookSlot.value.getBoundingClientRect().top + window.scrollY;
}

onMounted(() => {
  const pageEl = document.querySelector('.dictation-page');
  if (pageEl) {
    observer = new ResizeObserver(measure);
    observer.observe(pageEl);
  }
  window.addEventListener('resize', measure);
  void action(async () => {
    await Promise.all([d.loadHistory(), d.loadConfig()]);
    if (typeof route.query.session === 'string') await d.select(route.query.session);
    await nextTick();
    measure();
  });
});

onBeforeUnmount(() => {
  observer?.disconnect();
  window.removeEventListener('resize', measure);
});
</script>

<template>
  <section class="workbench-page dictation-page" :class="{ 'is-compact': compact }">
    <div class="page-heading">
      <div>
        <div class="heading-title-row">
          <h2>语音输入</h2>
          <span v-if="d.serverConfig.value" class="capacity-pill" :title="`当前有 ${d.serverConfig.value.active_sessions} 个正在进行的语音会话（最大支持 ${d.serverConfig.value.max_active_sessions} 个）`">
            容量 {{ d.serverConfig.value.active_sessions }}/{{ d.serverConfig.value.max_active_sessions }}
          </span>
        </div>
        <p v-if="!compact">边说边写，流式识别与模型整理结合，在同一份正文中持续完成整理。</p>
      </div>
      <div class="dictation-header-actions">
        <n-button size="small" @click="smallWindow">
          <template #icon>
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="3" y="3" width="18" height="18" rx="2" />
              <line x1="9" y1="3" x2="9" y2="21" />
            </svg>
          </template>
          {{ compact ? '普通视图' : '紧凑小窗' }}
        </n-button>
        <n-button size="small" @click="settingsOpen = true">
          <template #icon>
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83-2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
            </svg>
          </template>
          设置
        </n-button>
      </div>
    </div>

    <nav class="dictation-tabs" aria-label="语音输入方式">
      <button
        v-for="item in [
          { id: 'live', text: '实时听写' },
          { id: 'record', text: '录音转写' },
          { id: 'upload', text: '导入音频' },
          { id: 'history', text: '历史结果' }
        ]"
        :key="item.id"
        :aria-current="tab === item.id ? 'page' : undefined"
        @click="setTab(item.id); if (item.id === 'history') action(d.loadHistory)"
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
      :style="{ width: 'min(680px, 94vw)', maxHeight: '88vh', borderRadius: '12px' }"
      :content-style="{ overflowY: 'auto', maxHeight: 'calc(88vh - 130px)' }"
      :auto-focus="false"
    >
      <section class="dictation-settings" aria-label="听写设置">
        <div class="settings-section">
          <h4>麦克风与硬件设备</h4>
          <label for="dictation-microphone">麦克风输入</label>
          <select
            id="dictation-microphone"
            :value="d.selectedDeviceId.value"
            :disabled="d.capturing.value || d.local.capturing.value || !!captureAction || d.deviceBusy.value"
            @change="action(async () => d.chooseMicrophone(($event.target as HTMLSelectElement).value))"
          >
            <option value="" disabled>请列出并选择麦克风</option>
            <option
              v-if="d.selectedDeviceId.value && !d.microphoneDevices.value.some(item => item.deviceId === d.selectedDeviceId.value)"
              :value="d.selectedDeviceId.value"
            >
              已保存的麦克风（列出设备以确认）
            </option>
            <option v-for="device in d.microphoneDevices.value" :key="device.deviceId" :value="device.deviceId">
              {{ device.label || '未命名麦克风' }}
            </option>
          </select>
          <div class="dictation-actions" style="margin-top: 8px;">
            <n-button :loading="d.deviceBusy.value" :disabled="d.capturing.value || d.local.capturing.value || !!captureAction" @click="action(d.listMicrophones)">
              授权并列出设备
            </n-button>
            <n-button :disabled="d.ownsCapture.value || d.local.active.value || !!captureAction" @click="action(trial)">
              本地试音（录音后试听）
            </n-button>
          </div>
        </div>

        <div class="settings-section">
          <h4>识别词表与术语</h4>
          <label for="dictation-terms">少量术语（每行一个或逗号分隔，最多50项）</label>
          <textarea
            id="dictation-terms"
            v-model="terms"
            :disabled="active"
            rows="3"
            placeholder="例如：量子纠缠、反重力、梯度下降"
          />
          <details class="dictation-settings__terms-library">
            <summary>本浏览器的常用术语库（主动选用，不自动改写已有会话）</summary>
            <div class="terms-lib-add">
              <input
                v-model="newTerm"
                aria-label="新常用术语"
                placeholder="输入一个正确词或专有名词"
                @keydown.enter.prevent="action(async () => addTerm(newTerm))"
              />
              <n-button @click="action(async () => addTerm(newTerm))">保存常用术语</n-button>
            </div>
            <div v-if="savedTerms.length" class="terms-lib-list">
              <div v-for="term in savedTerms" :key="term" class="terms-lib-item">
                <label><input v-model="chosenTerms" type="checkbox" :value="term" />{{ term }}</label>
                <n-button text size="tiny" type="error" @click="action(async () => storeTerms(savedTerms.filter(v => v !== term)))">移除</n-button>
              </div>
            </div>
            <p v-else class="terms-lib-empty">尚未保存常用术语。可从正文选中后存入，或在此输入。</p>
            <n-button :disabled="active || !chosenTerms.length" size="small" @click="terms = validateTerms(chosenTerms).join('\n')">
              将选中项填入下次识别
            </n-button>
          </details>
        </div>

        <div class="settings-section">
          <h4>AI 模型与整理策略</h4>
          <label class="checkbox-label">
            <input v-model="enableCleanup" type="checkbox" :disabled="active" />
            使用逐句轻度整理
          </label>
          <div class="dictation-model-grid">
            <label for="dictation-cleanup-model">
              逐句快速处理模型
              <select id="dictation-cleanup-model" v-model="cleanupModel" :disabled="active">
                <option value="">服务器默认（{{ d.serverConfig.value?.cleanup_model || '读取中' }}）</option>
                <option v-for="m in cleanupModels" :key="m.value" :value="m.value">{{ m.label }}</option>
              </select>
            </label>
            <label for="dictation-cleanup-effort">
              逐句思考强度
              <select id="dictation-cleanup-effort" v-model="cleanupEffort" :disabled="active">
                <option value="">默认（{{ d.serverConfig.value?.cleanup_reasoning_effort || '读取中' }}）</option>
                <option v-for="e in efforts" :key="e.value" :value="e.value">{{ e.label }}</option>
              </select>
            </label>
            <label for="dictation-document-model">
              全文整理模型
              <select id="dictation-document-model" v-model="documentCleanupModel" :disabled="active">
                <option value="">服务器默认（{{ d.serverConfig.value?.document_cleanup_model || '读取中' }}）</option>
                <option v-for="m in cleanupModels" :key="m.value" :value="m.value">{{ m.label }}</option>
              </select>
            </label>
            <label for="dictation-document-effort">
              全文思考强度
              <select id="dictation-document-effort" v-model="documentEffort" :disabled="active">
                <option value="">默认（{{ d.serverConfig.value?.document_reasoning_effort || '读取中' }}）</option>
                <option v-for="e in efforts" :key="e.value" :value="e.value">{{ e.label }}</option>
              </select>
            </label>
          </div>
        </div>

        <p class="dictation-status">
          当前识别容量 {{ d.serverConfig.value?.active_sessions ?? '—' }}/{{ d.serverConfig.value?.max_active_sessions ?? '—' }}。模型与词表在新会话开始时冻结；API 密钥沿用系统设置。
        </p>
      </section>

      <template #footer>
        <p v-if="error" class="dictation-error" role="alert">{{ error }}</p>
        <div class="settings-footer-actions">
          <span class="dictation-status">设置自动保存到本浏览器</span>
          <div class="dictation-actions">
            <n-button text size="small" @click="resetSettingsToDefault">恢复默认</n-button>
            <n-button type="primary" size="small" @click="settingsOpen = false">完成</n-button>
          </div>
        </div>
      </template>
    </n-modal>

    <!-- Global alert messages -->
    <p v-if="error && !settingsOpen" class="dictation-error" role="alert">{{ error }}</p>
    <p v-if="d.inputWarning.value" class="dictation-status-banner" role="status">{{ d.inputWarning.value }}</p>

    <!-- Tab 2: Local recording drafts -->
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

    <!-- Tab 3: Upload audio file -->
    <section v-else-if="tab === 'upload'" class="dictation-upload">
      <div class="upload-dropzone">
        <div class="upload-icon-circle">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
            <polyline points="17 8 12 3 7 8" />
            <line x1="12" y1="3" x2="12" y2="15" />
          </svg>
        </div>
        <h3>上传音频进行双遍转写</h3>
        <p class="upload-hint">支持 WAV、MP3、M4A、AAC、FLAC、OGG、WebM / MP4 音轨。{{ uploadLimit }}。</p>
        <label class="upload-file-button" :class="{ 'is-disabled': recognizing || active || busy || !!captureAction || !!preview }">
          <input
            ref="uploadInput"
            aria-label="选择录音文件"
            type="file"
            accept="audio/*,.mp4,.webm"
            :disabled="recognizing || active || busy || !!captureAction || !!preview"
            @change="action(async () => uploadChanged($event))"
          />
          <span>选择录音文件</span>
        </label>
      </div>

      <div v-if="d.uploadProgress.value" class="upload-progress-card" role="status">
        <div class="progress-info">
          <span>正在上传文件…</span>
          <span>{{ Math.round((d.uploadProgress.value.loaded / d.uploadProgress.value.total) * 100) }}% ({{ (d.uploadProgress.value.loaded / 1024 / 1024).toFixed(1) }} / {{ (d.uploadProgress.value.total / 1024 / 1024).toFixed(1) }} MB)</span>
        </div>
        <progress :value="d.uploadProgress.value.loaded" :max="d.uploadProgress.value.total" />
        <n-button size="small" @click="d.cancelUpload">取消上传</n-button>
      </div>

      <div v-if="d.uploadUncertain.value" class="upload-uncertain-card">
        <p>提交结果不明确，请先检查历史中是否已生成任务。</p>
        <div class="dictation-actions">
          <n-button size="small" @click="setTab('history'); action(d.loadHistory)">检查历史</n-button>
          <n-button size="small" type="primary" @click="d.confirmUploadChecked">已检查，允许手动重试</n-button>
        </div>
      </div>
    </section>

    <!-- Tab 4: History list -->
    <section v-else-if="tab === 'history'" class="dictation-history" aria-label="听写历史">
      <div v-if="!history.length" class="history-empty">
        <svg viewBox="0 0 24 24" width="48" height="48" fill="none" stroke="currentColor" stroke-width="1.2">
          <circle cx="12" cy="12" r="10" />
          <polyline points="12 6 12 12 16 14" />
        </svg>
        <p>本浏览器还没有语音输入记录。</p>
        <n-button size="small" type="primary" @click="setTab('live')">前往听写</n-button>
      </div>

      <article v-for="item in history" :key="item.id" class="history-card">
        <div class="history-content">
          <div class="history-header">
            <strong>{{ new Date(item.created_at).toLocaleString() }}</strong>
            <span class="phase-badge">{{ item.phase }}</span>
          </div>
          <p class="history-text">{{ item.draft_text.slice(0, 140) || '没有已完成文字' }}</p>
        </div>
        <div class="history-actions">
          <n-button size="small" @click="action(() => select(item.id))">打开</n-button>
          <n-button type="error" quaternary size="small" @click="removeSession(item.id)">删除</n-button>
        </div>
      </article>
    </section>

    <!-- Tab 1: Live dictation notebook -->
    <template v-else>
      <div ref="notebookSlot" :style="{ '--notebook-height': `max(320px, calc(100dvh - ${notebookTop}px - 60px))` }">
        <DictationEditor
          v-if="!session || session.editor_version === 1"
          :key="buffer.id"
          :buffer="buffer"
          :listening="active"
          :partial-text="partialText"
          :show-start="!active"
          :start-disabled="startDisabled"
          :starting="captureAction === 'starting'"
          @start="action(startDictation)"
          @term="saveCorrectTerm"
          @apply="action(() => d.cleanup('apply'))"
          @discard="action(discard)"
        >
          <n-button size="small" @click="copy">{{ preview ? '复制预览' : '复制正文' }}</n-button>
          <n-button size="small" @click="action(download)">{{ preview ? '下载预览' : '下载 TXT' }}</n-button>
          <n-button
            v-if="!preview"
            size="small"
            :loading="cleanupScope === 'document'"
            :disabled="active || busy || !buffer.text.value.trim() || !session"
            @click="action(() => d.cleanup('document'))"
          >
            整理全文
          </n-button>
        </DictationEditor>
        <p v-else role="status" class="dictation-status">正在打开连续正文…</p>
      </div>

      <p v-if="cleanupScope === 'document'" class="dictation-status-banner" role="status">
        正在整理全文，最多等待 {{ documentTimeout }} 秒；仍可编辑正文，确认前不替换。
      </p>
      <p v-if="session?.document_suggestion?.metrics?.strategy === 'chunked' && preview" class="dictation-status-banner">
        长文已分 {{ session.document_suggestion.metrics.chunk_count }} 段整理，请核对衔接处。
      </p>

      <section class="dictation-controls" aria-label="录音控制">
        <template v-if="!viewingOwnedTask">
          <div class="control-state-group">
            <span class="control-phase">{{ session?.phase || '准备开始' }}</span>
            <n-button v-if="captureAction === 'starting' && !recognizing" size="small" @click="action(() => d.stop(null))">
              结束准备
            </n-button>
            <n-button
              v-else-if="!active"
              v-show="!emptyStart"
              type="primary"
              size="small"
              :loading="busy && cleanupScope !== 'document'"
              :disabled="startDisabled || !!preview"
              @click="action(startDictation)"
            >
              开始听写
            </n-button>
            <n-button v-else size="small" :disabled="session?.status === 'draining'" @click="action(() => d.stop(session!.id))">
              停止
            </n-button>
            <n-button v-if="recognizing" size="small" @click="d.showRecording">返回活动会话</n-button>
            <n-button v-if="session?.status === 'waiting'" size="small" :loading="!!d.fileAction.value" @click="action(() => d.fileControl(session!.id, 'resume'))">
              重试开始转写
            </n-button>
            <n-button
              v-if="active && session?.mode === 'upload' && session.status !== 'waiting'"
              size="small"
              :disabled="!!d.fileAction.value || !['listening','draining','paused'].includes(session.status)"
              @click="action(() => d.fileControl(session!.id, session!.status === 'paused' ? 'resume' : 'pause'))"
            >
              {{ session.status === 'paused' ? '继续转写' : '暂停转写' }}
            </n-button>
            <n-button v-if="active" size="small" quaternary @click="action(() => d.cancel(session!.id))">
              取消后续处理
            </n-button>
          </div>
        </template>
        <span v-else class="control-phase">听写跟随当前光标；切换位置时稍候，显示「正在听音」后再说。</span>

        <n-button v-if="session" text size="small" :aria-expanded="recordsOpen" @click="recordsOpen = !recordsOpen">
          识别记录（{{ session.utterances.length }}）
          <template #icon>
            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" :style="{ transform: recordsOpen ? 'rotate(180deg)' : 'none', transition: 'transform 0.2s' }">
              <polyline points="6 9 12 15 18 9" />
            </svg>
          </template>
        </n-button>
      </section>

      <p v-if="active && session?.mode === 'live' && !viewingOwnedTask" class="dictation-status">
        本窗口可编辑正文；暂停、继续请返回原录音窗口。
      </p>
      <p v-if="connection" class="dictation-status" role="status">{{ connection }}</p>
      <p v-if="session?.document_cleanup_error && !error" class="dictation-error" role="alert">{{ session.document_cleanup_error }}</p>

      <!-- Sentence records drawer / section -->
      <section v-if="recordsOpen && session" class="dictation-records" aria-label="识别记录">
        <div class="records-header">
          <strong>识别记录与分句回听</strong>
          <small>原始分句录音保留；当前正文可连续跨句编辑，不提供逐字对齐。</small>
        </div>
        <article v-for="u in session.utterances" :key="u.id" class="record-item">
          <header class="record-item-header">
            <span class="record-id">分句 #{{ u.id }}</span>
            <span class="record-status-badge" :class="u.status">{{ states[u.status] || u.status }}</span>
            <div class="record-item-actions">
              <n-button v-if="u.end_sample != null" text size="tiny" :disabled="d.capturing.value || d.local.capturing.value" @click="action(() => listen(u))">
                {{ playingUid === u.id ? '暂停回听' : '听原始分句' }}
              </n-button>
              <n-button v-if="u.status === 'cleanup_failed'" text size="tiny" :disabled="active || busy" @click="action(() => d.cleanup('utterance', u.id))">
                重试整理
              </n-button>
            </div>
          </header>
          <p class="record-raw"><span>原始识别：</span>{{ u.raw_text || u.preview || '等待识别' }}</p>
          <p v-if="u.cleaned_text || u.suggestion" class="record-cleaned"><span>整理建议：</span>{{ u.suggestion || u.cleaned_text }}</p>
          <p v-if="u.error" class="dictation-error">{{ u.error }}</p>
        </article>
      </section>

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
        @error="error = '无法播放此句音频，请检查音频是否保留。'; playingUid = null"
      />
    </template>
  </section>
</template>

<style scoped>
.dictation-page {
  max-width: 1080px;
  margin: 0 auto;
  display: grid;
  gap: 14px;
  grid-template-columns: minmax(0, 1fr);
}

.page-heading {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  flex-wrap: wrap;
}

.heading-title-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.heading-title-row h2 {
  margin: 0;
  font-size: 22px;
  font-weight: 600;
  color: var(--text-primary);
}

.capacity-pill {
  font-size: 11px;
  color: var(--text-secondary);
  background: var(--surface-subtle);
  border: 1px solid var(--border-subtle);
  padding: 2px 8px;
  border-radius: 12px;
}

.page-heading p {
  margin: 4px 0 0;
  color: var(--text-secondary);
  font-size: 13px;
}

.dictation-header-actions,
.dictation-actions,
.dictation-controls {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.control-state-group {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.control-phase {
  font-size: 13px;
  color: var(--text-secondary);
  font-weight: 500;
}

.dictation-tabs {
  display: flex;
  gap: 4px;
  padding: 4px;
  border: 1px solid var(--border-subtle);
  background: var(--surface-subtle);
  border-radius: 9px;
}

.dictation-tabs button {
  flex: 1;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--text-secondary);
  padding: 8px 12px;
  white-space: nowrap;
  cursor: pointer;
  font: inherit;
  font-size: 14px;
  font-weight: 500;
  transition: all 0.15s ease;
}

.dictation-tabs button:hover:not([aria-current]) {
  color: var(--text-primary);
  background: var(--surface-raised);
}

.dictation-tabs button[aria-current] {
  background: var(--surface-canvas);
  color: var(--primary);
  font-weight: 600;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
}

/* Settings Modal */
.dictation-settings {
  display: grid;
  gap: 18px;
}

.settings-section {
  display: grid;
  gap: 8px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--border-subtle);
}

.settings-section:last-of-type {
  border-bottom: 0;
  padding-bottom: 0;
}

.settings-section h4 {
  margin: 0 0 4px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.settings-section label {
  font-size: 13px;
  color: var(--text-primary);
}

.checkbox-label {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
}

.dictation-settings select,
.dictation-settings input:not([type=checkbox]),
.dictation-settings textarea {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid var(--border-strong);
  border-radius: 7px;
  background: var(--surface-raised);
  color: var(--text-primary);
  padding: 8px 10px;
  font: inherit;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}

.dictation-settings select:focus,
.dictation-settings input:not([type=checkbox]):focus,
.dictation-settings textarea:focus {
  outline: none;
  border-color: var(--primary);
  box-shadow: 0 0 0 2px var(--primary-alpha-20);
}

.dictation-settings select {
  display: block;
  margin-top: 4px;
}

.dictation-model-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-top: 4px;
}

.dictation-settings details {
  border-top: 1px solid var(--border-subtle);
  padding-top: 10px;
  margin-top: 6px;
}

.dictation-settings summary {
  cursor: pointer;
  margin-bottom: 10px;
  font-size: 13px;
  color: var(--primary);
  font-weight: 500;
}

.terms-lib-add {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
}

.terms-lib-list {
  display: grid;
  gap: 6px;
  max-height: 140px;
  overflow-y: auto;
  padding: 6px;
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  background: var(--surface-subtle);
  margin-bottom: 10px;
}

.terms-lib-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 13px;
}

.terms-lib-item label {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
}

.terms-lib-empty {
  font-size: 12px;
  color: var(--text-muted);
  margin: 6px 0 10px;
}

.settings-footer-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
  width: 100%;
}

.dictation-status {
  margin: 0;
  color: var(--text-secondary);
  font-size: 12px;
  overflow-wrap: anywhere;
}

.dictation-status-banner {
  margin: 0;
  padding: 8px 12px;
  border-radius: 6px;
  background: var(--surface-subtle);
  border-left: 3px solid var(--primary);
  color: var(--text-secondary);
  font-size: 13px;
}

.dictation-error {
  margin: 0;
  color: var(--color-error);
  padding: 8px 12px;
  border-left: 3px solid var(--color-error);
  border-radius: 6px;
  background: var(--color-error-bg);
  font-size: 13px;
  overflow-wrap: anywhere;
}

.dictation-controls {
  justify-content: space-between;
  padding: 2px 0;
}

/* Upload Tab */
.dictation-upload {
  display: grid;
  gap: 16px;
  padding: 10px 0;
}

.upload-dropzone {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 36px 20px;
  border: 2px dashed var(--border-strong);
  border-radius: 12px;
  background: var(--surface-subtle);
  text-align: center;
  gap: 12px;
  transition: border-color 0.15s ease, background-color 0.15s ease;
}

.upload-icon-circle {
  display: grid;
  place-items: center;
  width: 52px;
  height: 52px;
  border-radius: 50%;
  background: var(--surface-canvas);
  color: var(--primary);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
}

.upload-icon-circle svg {
  width: 26px;
  height: 26px;
}

.upload-dropzone h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary);
}

.upload-hint {
  margin: 0;
  color: var(--text-secondary);
  font-size: 13px;
  max-width: 480px;
}

.upload-file-button {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 8px 18px;
  border-radius: 7px;
  background: var(--primary);
  color: var(--surface-canvas);
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  overflow: hidden;
  box-shadow: 0 2px 6px var(--primary-alpha-20);
  transition: background 0.15s ease;
}

.upload-file-button:hover:not(.is-disabled) {
  background: var(--primary-hover);
}

.upload-file-button.is-disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.upload-file-button input[type=file] {
  position: absolute;
  inset: 0;
  opacity: 0;
  cursor: pointer;
}

.upload-progress-card {
  display: grid;
  gap: 8px;
  padding: 16px;
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  background: var(--surface-raised);
}

.progress-info {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  color: var(--text-secondary);
}

.upload-progress-card progress {
  width: 100%;
  height: 8px;
  border-radius: 4px;
}

.upload-uncertain-card {
  padding: 14px;
  border: 1px solid var(--border-subtle);
  border-left: 3px solid #eab308;
  border-radius: 8px;
  background: var(--surface-subtle);
}

/* History Tab */
.dictation-history {
  display: grid;
  gap: 12px;
}

.history-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 48px 20px;
  gap: 12px;
  color: var(--text-muted);
}

.history-card {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  padding: 14px 18px;
  border: 1px solid var(--border-subtle);
  border-radius: 10px;
  background: var(--surface-raised);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}

.history-card:hover {
  border-color: var(--border-strong);
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.06);
}

.history-content {
  min-width: 0;
  flex: 1;
}

.history-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 6px;
}

.history-header strong {
  font-size: 14px;
  color: var(--text-primary);
}

.phase-badge {
  font-size: 11px;
  padding: 2px 7px;
  border-radius: 10px;
  background: var(--surface-subtle);
  color: var(--text-secondary);
  border: 1px solid var(--border-subtle);
}

.history-text {
  margin: 0;
  font-size: 13px;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  line-height: 1.5;
}

.history-actions {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-shrink: 0;
}

/* Records List */
.dictation-records {
  border-top: 1px solid var(--border-subtle);
  max-height: 45dvh;
  overflow-y: auto;
  padding: 12px 0;
  display: grid;
  gap: 10px;
}

.records-header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 4px;
}

.records-header strong {
  font-size: 14px;
}

.records-header small {
  color: var(--text-muted);
  font-size: 12px;
}

.record-item {
  padding: 10px 12px;
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  background: var(--surface-subtle);
  font-size: 13px;
  display: grid;
  gap: 6px;
}

.record-item-header {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}

.record-id {
  font-weight: 600;
  color: var(--text-primary);
}

.record-status-badge {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--surface-raised);
  color: var(--text-secondary);
}

.record-status-badge.cleaned { color: #10b981; }
.record-status-badge.cleaning, .record-status-badge.finalizing { color: #f59e0b; }
.record-status-badge.asr_failed, .record-status-badge.cleanup_failed { color: var(--color-error); }

.record-item-actions {
  margin-left: auto;
  display: flex;
  gap: 6px;
}

.record-raw, .record-cleaned {
  margin: 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.5;
}

.record-raw span, .record-cleaned span {
  color: var(--text-muted);
  font-weight: 500;
}

.dictation-audio-player {
  width: 100%;
  height: 40px;
  margin-top: 6px;
}

@media (max-width: 600px) {
  .dictation-model-grid { grid-template-columns: 1fr; }
  .history-card { flex-direction: column; align-items: flex-start; }
  .history-actions { width: 100%; justify-content: flex-end; }
  .dictation-tabs button { font-size: 13px; padding: 7px 4px; }
  .dictation-page { gap: 10px; }
}
</style>
