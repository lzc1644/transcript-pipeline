<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { useMessage } from "naive-ui";
import { useDictation } from "../composables/useDictation";

interface Position {
  x: number;
  y: number;
}

const emit = defineEmits<{ height: [value: number] }>();
const router = useRouter(), message = useMessage(), d = useDictation();
const { recordingSession, recognizing, capturing, captureAction, captureLabel, level, connection, error } = d;
const collapsed = ref(false), panel = ref<HTMLElement | null>(null);
const showDetails = ref(false);

const compactStatus = computed(() => {
  if (captureAction.value) return { starting: "准备中", pausing: "暂停中", resuming: "继续中", stopping: "结束中" }[captureAction.value];
  if (recordingSession.value?.status === "paused") return "已暂停";
  if (recordingSession.value?.status === "draining") return "收尾中";
  return capturing.value ? "听写中" : "采音已停止";
});

const local = d.local;
const { active: localActive, draft: localDraft, capturing: localCapturing, recordedSamples, busy: localBusy } = local;

function clock(samples: number) {
  const s = Math.floor(samples / 16000);
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

const duration = computed(() => {
  const seconds = Math.floor((recordingSession.value?.samples ?? 0) / 16000);
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
});

let observer: ResizeObserver | null = null;
watch(panel, element => {
  observer?.disconnect();
  if (!element) { emit("height", 0); return; }
  observer = new ResizeObserver(() => emit("height", Math.ceil(element.getBoundingClientRect().height) + 16));
  observer.observe(element);
}, { flush: "post" });

// Draggable logic
const customPosition = ref<Position | null>(null);
const isDragging = ref(false);
let dragStartPointer = { x: 0, y: 0 };
let dragStartPos = { x: 0, y: 0 };

function onPointerDown(e: PointerEvent) {
  if (e.button !== 0) return;
  const target = e.target as HTMLElement | null;
  if (target?.closest("button, a, input, select, textarea, meter")) return;
  if (!panel.value) return;

  const rect = panel.value.getBoundingClientRect();
  dragStartPointer = { x: e.clientX, y: e.clientY };
  dragStartPos = { x: rect.left, y: rect.top };
  isDragging.value = true;
  try {
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
  } catch {}
}

function onPointerMove(e: PointerEvent) {
  if (!isDragging.value || !panel.value) return;
  const dx = e.clientX - dragStartPointer.x;
  const dy = e.clientY - dragStartPointer.y;
  const rect = panel.value.getBoundingClientRect();

  const minX = 8;
  const maxX = Math.max(8, window.innerWidth - rect.width - 8);
  const minY = 8;
  const maxY = Math.max(8, window.innerHeight - rect.height - 8);

  const newX = Math.round(Math.min(Math.max(dragStartPos.x + dx, minX), maxX));
  const newY = Math.round(Math.min(Math.max(dragStartPos.y + dy, minY), maxY));
  customPosition.value = { x: newX, y: newY };
}

function onPointerUp(e: PointerEvent) {
  if (isDragging.value) {
    isDragging.value = false;
    try {
      (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
    } catch {}
    if (customPosition.value) {
      sessionStorage.setItem("transcript-pipeline:dock-pos", JSON.stringify(customPosition.value));
    }
  }
}

function resetPosition() {
  customPosition.value = null;
  sessionStorage.removeItem("transcript-pipeline:dock-pos");
}

function onWindowResize() {
  if (customPosition.value && panel.value) {
    const rect = panel.value.getBoundingClientRect();
    const maxX = Math.max(8, window.innerWidth - rect.width - 8);
    const maxY = Math.max(8, window.innerHeight - rect.height - 8);
    customPosition.value = {
      x: Math.round(Math.min(Math.max(customPosition.value.x, 8), maxX)),
      y: Math.round(Math.min(Math.max(customPosition.value.y, 8), maxY)),
    };
  }
}

onMounted(() => {
  window.addEventListener("resize", onWindowResize);
  try {
    const saved = sessionStorage.getItem("transcript-pipeline:dock-pos");
    if (saved) {
      const pos = JSON.parse(saved);
      if (typeof pos.x === "number" && typeof pos.y === "number") {
        customPosition.value = pos;
      }
    }
  } catch {}
});

onBeforeUnmount(() => {
  window.removeEventListener("resize", onWindowResize);
  observer?.disconnect();
  emit("height", 0);
});

async function action(callback: () => Promise<unknown>) {
  try { await callback(); } catch (cause) { error.value = cause instanceof Error ? cause.message : String(cause); }
}

async function copy() {
  const snapshot = recordingSession.value?.draft_text ?? "";
  try {
    await navigator.clipboard.writeText(snapshot);
    message.success("已复制活动会话的已完成正文");
  } catch {
    message.error("剪贴板不可用，请打开正文下载 TXT 或选中文字复制");
  }
}

async function openText() {
  d.showRecording();
  await router.push("/dictation?mode=live");
}
</script>

<template>
  <aside
    v-if="(recognizing && recordingSession) || localActive"
    ref="panel"
    class="dictation-dock"
    :class="{ 'is-collapsed': collapsed, 'is-dragging': isDragging }"
    :style="customPosition ? {
      left: `${customPosition.x}px`,
      top: `${customPosition.y}px`,
      right: 'auto',
      bottom: 'auto',
      transform: 'none'
    } : undefined"
    aria-label="常驻语音控制"
  >
    <!-- Top Drag Handle Bar -->
    <div
      class="dictation-dock__drag-bar"
      title="按住拖动面板 · 双击复位位置"
      @pointerdown="onPointerDown"
      @pointermove="onPointerMove"
      @pointerup="onPointerUp"
      @pointercancel="onPointerUp"
      @dblclick="resetPosition"
    >
      <div class="dictation-dock__handle" aria-hidden="true" />
    </div>

    <!-- Local Recording Mode -->
    <section v-if="localActive && localDraft" class="dictation-dock__local" aria-label="本地录音控制">
      <div class="dictation-dock__header" role="status">
        <div class="dictation-dock__title-group">
          <span class="dictation-dock__signal" :class="{ 'is-on': localCapturing }" aria-hidden="true">{{ localCapturing ? '●' : 'Ⅱ' }}</span>
          <strong>{{ localCapturing ? '本地录音中' : '本地录音已暂停' }}</strong>
          <span class="dictation-dock__time">{{ clock(recordedSamples) }}</span>
        </div>
        <button
          type="button"
          class="win-header-btn"
          :aria-expanded="!collapsed"
          :title="collapsed ? '展开面板' : '收起面板'"
          @click="collapsed = !collapsed"
        >
          <span class="win-header-btn-text">{{ collapsed ? '展开' : '收起' }}</span>
        </button>
      </div>

      <!-- Expanded Content -->
      <template v-if="!collapsed">
        <div class="dictation-dock__hero">
          <!-- Left: Secondary/Settings Button -->
          <button
            type="button"
            class="win-side-btn"
            :class="{ 'is-active': showDetails }"
            title="录音详情与设置"
            aria-label="更多设置"
            @click="showDetails = !showDetails"
          >
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83-2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
            </svg>
            <span class="sr-only">更多设置</span>
          </button>

          <!-- Center: Hero Mic Button (Toggle Pause / Resume) -->
          <button
            type="button"
            class="win-mic-btn"
            :class="{ 'is-listening': localCapturing, 'is-paused': !localCapturing }"
            :disabled="localBusy"
            :aria-label="localCapturing ? '暂停录音' : '继续录音'"
            :title="localCapturing ? '暂停录音' : '继续录音'"
            @click="action(localCapturing ? local.pause : () => local.begin(localDraft!.id))"
          >
            <span
              class="win-mic-pulse"
              aria-hidden="true"
              :style="{
                transform: `scale(${1 + (localCapturing ? 0.15 + level * 0.45 : 0)})`,
                opacity: localCapturing ? 0.45 + level * 0.55 : 0
              }"
            />
            <svg class="win-mic-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect x="9" y="2" width="6" height="12" rx="3" />
              <path d="M5 10a7 7 0 0 0 14 0" />
              <line x1="12" y1="19" x2="12" y2="22" />
            </svg>
            <span class="win-mic-label">{{ localCapturing ? '暂停录音' : '继续录音' }}</span>
          </button>

          <!-- Right: Stop Button -->
          <button
            type="button"
            class="win-side-btn win-side-btn--stop"
            :disabled="localBusy"
            aria-label="结束录音"
            title="结束录音"
            @click="action(local.end)"
          >
            <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
              <rect x="5" y="5" width="14" height="14" rx="2" />
            </svg>
            <span class="sr-only">结束录音</span>
          </button>
        </div>

        <!-- Pill Actions Row -->
        <div class="dictation-dock__actions">
          <button
            type="button"
            class="win-chip-btn"
            @click="router.push('/dictation?mode=record')"
          >
            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
              <polyline points="14 2 14 8 20 8"/>
            </svg>
            <span>录音草稿</span>
          </button>
        </div>

        <p class="dictation-dock__note">{{ localBusy ? '正在保存' : '已保存' }} {{ clock(localDraft.samples) }} · 本地录音不占识别资源</p>
        <p v-if="d.inputWarning.value" class="dictation-dock__note" role="status">{{ d.inputWarning.value }}</p>
        <p v-if="local.error.value" class="dictation-dock__error" role="alert">{{ local.error.value }}</p>
      </template>

      <!-- Collapsed Quick Controls -->
      <div v-else class="dictation-dock__collapsed-bar">
        <button
          type="button"
          class="win-collapsed-toggle"
          :class="{ 'is-listening': localCapturing }"
          :disabled="localBusy"
          :aria-label="localCapturing ? '暂停录音' : '继续录音'"
          @click="action(localCapturing ? local.pause : () => local.begin(localDraft!.id))"
        >
          <svg v-if="!localCapturing" viewBox="0 0 24 24" width="12" height="12" fill="currentColor">
            <polygon points="6,4 20,12 6,20" />
          </svg>
          <svg v-else viewBox="0 0 24 24" width="12" height="12" fill="currentColor">
            <rect x="6" y="4" width="4" height="16" rx="1" />
            <rect x="14" y="4" width="4" height="16" rx="1" />
          </svg>
          <span>{{ localCapturing ? '暂停' : '继续' }}</span>
        </button>
      </div>
    </section>

    <!-- Active Live / Upload Recognition Mode -->
    <section v-if="recognizing && recordingSession" class="dictation-dock__live" aria-label="活动识别控制">
      <div class="dictation-dock__header" role="status">
        <div class="dictation-dock__title-group">
          <span class="dictation-dock__signal" :class="{ 'is-on': capturing }" aria-hidden="true">{{ recordingSession.status === 'paused' ? 'Ⅱ' : '●' }}</span>
          <strong>{{ recordingSession.mode === 'upload' ? recordingSession.phase : collapsed ? compactStatus : captureLabel }}</strong>
          <span class="dictation-dock__time">{{ duration }}</span>
        </div>
        <button
          type="button"
          class="win-header-btn"
          :aria-expanded="!collapsed"
          :title="collapsed ? '展开面板' : '收起面板'"
          @click="collapsed = !collapsed"
        >
          <span class="win-header-btn-text">{{ collapsed ? '展开' : '收起' }}</span>
        </button>
      </div>

      <p v-if="recordingSession.status === 'waiting'" class="dictation-dock__note">文件已保留，无需重新上传。<button type="button" :disabled="!!d.fileAction.value" @click="action(() => d.fileControl(recordingSession!.id, 'resume'))">重试开始转写</button></p>
      <p v-if="recordingSession.utterances.some(u => u.status === 'finalizing' && u.asr_queue_state !== 'running')" class="dictation-dock__note" role="status">正在等待共享识别 · 按会话轮转</p>
      <!-- Expanded Content -->
      <template v-if="!collapsed">
        <!-- Hero Row: [⚙ Settings] [🎙 Hero Mic Toggle] [⏹ Stop] -->
        <div class="dictation-dock__hero">
          <!-- Left: More Details / Settings Button -->
          <button
            type="button"
            class="win-side-btn"
            :class="{ 'is-active': showDetails }"
            title="更多信息与设置"
            aria-label="更多设置"
            @click="showDetails = !showDetails"
          >
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83-2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
            </svg>
            <span class="sr-only">更多设置</span>
          </button>

          <!-- Center: Large Circular Hero Mic Button (Toggle Pause / Resume) -->
          <button
            v-if="recordingSession.mode === 'live'"
            type="button"
            class="win-mic-btn"
            :class="{ 'is-listening': capturing, 'is-paused': recordingSession.status === 'paused' }"
            :disabled="!!captureAction || !['listening','paused'].includes(recordingSession.status)"
            :aria-label="recordingSession.status === 'paused' ? '继续听写' : '暂停听写'"
            :title="recordingSession.status === 'paused' ? '继续听写' : '暂停听写'"
            @click="action(recordingSession.status === 'paused' ? d.resume : d.pause)"
          >
            <span
              class="win-mic-pulse"
              aria-hidden="true"
              :style="{
                transform: `scale(${1 + (capturing ? 0.15 + level * 0.45 : 0)})`,
                opacity: capturing ? 0.45 + level * 0.55 : 0
              }"
            />
            <svg class="win-mic-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect x="9" y="2" width="6" height="12" rx="3" />
              <path d="M5 10a7 7 0 0 0 14 0" />
              <line x1="12" y1="19" x2="12" y2="22" />
            </svg>
            <span class="win-mic-label">{{ recordingSession.status === 'paused' ? '继续听写' : '暂停听写' }}</span>
          </button>

          <button
            v-else
            type="button"
            class="win-mic-btn"
            :class="{ 'is-listening': capturing, 'is-paused': recordingSession.status === 'paused' }"
            :disabled="!!d.fileAction.value || !['listening','paused','draining'].includes(recordingSession.status)"
            :aria-label="recordingSession.status === 'paused' ? '继续转写' : '暂停转写'"
            :title="recordingSession.status === 'paused' ? '继续转写' : '暂停转写'"
            @click="action(() => d.fileControl(recordingSession!.id, recordingSession!.status === 'paused' ? 'resume' : 'pause'))"
          >
            <span class="win-mic-pulse" aria-hidden="true" />
            <svg class="win-mic-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect x="9" y="2" width="6" height="12" rx="3" />
              <path d="M5 10a7 7 0 0 0 14 0" />
              <line x1="12" y1="19" x2="12" y2="22" />
            </svg>
            <span class="win-mic-label">{{ recordingSession.status === 'paused' ? '继续转写' : '暂停转写' }}</span>
          </button>

          <!-- Right: Stop Button -->
          <button
            type="button"
            class="win-side-btn win-side-btn--stop"
            :disabled="captureAction === 'stopping' || recordingSession.status === 'draining'"
            aria-label="结束听写"
            title="结束听写"
            @click="action(() => d.stop(recordingSession!.id))"
          >
            <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
              <rect x="5" y="5" width="14" height="14" rx="2" />
            </svg>
            <span class="sr-only">结束听写</span>
          </button>
        </div>

        <!-- Pill Actions Row -->
        <div class="dictation-dock__actions">
          <button
            type="button"
            class="win-chip-btn"
            :disabled="!recordingSession.draft_text"
            title="复制已完成正文"
            @click="copy"
          >
            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
              <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
            </svg>
            <span>复制正文</span>
          </button>
          <button
            type="button"
            class="win-chip-btn"
            title="前往正文视图"
            @click="openText"
          >
            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
              <polyline points="14 2 14 8 20 8"/>
              <line x1="16" y1="13" x2="8" y2="13"/>
              <line x1="16" y1="17" x2="8" y2="17"/>
            </svg>
            <span>正文</span>
          </button>
          <button
            type="button"
            class="win-chip-btn win-chip-btn--danger"
            title="取消本次会话的后续整理"
            @click="action(() => d.cancel(recordingSession!.id))"
          >
            取消后续处理
          </button>
        </div>

        <!-- Audio Volume Meter (Active when listening) -->
        <div v-if="capturing" class="dictation-dock__meter-wrap">
          <meter min="0" max="1" :value="level" aria-label="活动麦克风音量" />
        </div>

        <!-- Extra Details Tray (Toggled by Settings Gear) -->
        <div v-if="showDetails" class="dictation-dock__details-tray">
          <div class="dictation-dock__details-row">
            <span>面板位置：</span>
            <button v-if="customPosition" type="button" class="win-link-btn" @click="resetPosition">复位到底部</button>
            <span v-else class="text-muted">默认停靠</span>
          </div>
          <div class="dictation-dock__details-row">
            <span>当前输入：</span>
            <span>{{ captureLabel || '默认音频流' }}</span>
          </div>
        </div>

        <!-- Upload Progress -->
        <div v-if="recordingSession.mode === 'upload' && recordingSession.progress" class="dictation-dock__note">
          <template v-if="recordingSession.progress.total_samples">
            转写 {{ Math.floor(recordingSession.progress.transcribed_samples / recordingSession.progress.total_samples * 100) }}%
            <progress :value="recordingSession.progress.transcribed_samples" :max="recordingSession.progress.total_samples" aria-label="实际音频转写进度" />
          </template>
          <span> · 识别失败 {{ recordingSession.progress.asr_failed }} 段 · 整理 {{ recordingSession.progress.cleanup_done }}/{{ recordingSession.progress.cleanup_total }} · 整理失败 {{ recordingSession.progress.cleanup_failed }} 段</span>
        </div>

        <!-- Warnings & Alerts -->
        <p v-if="d.inputWarning.value && !localActive" class="dictation-dock__note" role="status">{{ d.inputWarning.value }}</p>
        <p v-if="connection" class="dictation-dock__note" role="status">{{ connection }}</p>
        <p v-if="error" class="dictation-dock__error" role="alert">{{ error }}</p>
        <p v-if="['paused','pausing'].includes(recordingSession.status) || captureAction === 'resuming'" class="dictation-dock__note">暂停保留会话名额，不独占 GPU；其他用户仍可识别。关闭录音窗口或断网超过宽限期后会结束会话。</p>
      </template>

      <!-- Collapsed Quick Controls -->
      <div v-else class="dictation-dock__collapsed-bar">
        <button
          type="button"
          class="win-collapsed-toggle"
          :class="{ 'is-listening': capturing }"
          :disabled="!!captureAction || !!d.fileAction.value || !['listening','paused','draining'].includes(recordingSession.status) || (recordingSession.mode === 'live' && recordingSession.status === 'draining')"
          :aria-label="recordingSession.status === 'paused' ? '继续听写' : '暂停听写'"
          @click="action(recordingSession.mode === 'upload' ? () => d.fileControl(recordingSession!.id, recordingSession!.status === 'paused' ? 'resume' : 'pause') : recordingSession.status === 'paused' ? d.resume : d.pause)"
        >
          <svg v-if="recordingSession.status === 'paused'" viewBox="0 0 24 24" width="12" height="12" fill="currentColor">
            <polygon points="6,4 20,12 6,20" />
          </svg>
          <svg v-else viewBox="0 0 24 24" width="12" height="12" fill="currentColor">
            <rect x="6" y="4" width="4" height="16" rx="1" />
            <rect x="14" y="4" width="4" height="16" rx="1" />
          </svg>
          <span>{{ recordingSession.status === 'paused' ? '继续' : '暂停' }}</span>
        </button>
      </div>
    </section>
  </aside>
</template>

<style>
/* Cohesive Workbench Acrylic Variables */
:root {
  --win-dock-bg: rgba(255, 255, 255, 0.94);
  --win-dock-border: rgba(206, 204, 196, 0.85);
  --win-dock-shadow: 0 16px 36px rgba(45, 42, 38, 0.12), 0 3px 10px rgba(45, 42, 38, 0.04);
  --win-mic-bg: #ffffff;
  --win-mic-border: #dcdad3;
  --win-mic-color: #62646f;
  --win-mic-active-bg: linear-gradient(135deg, #6366f1 0%, #4f46e5 100%);
  --win-mic-active-color: #ffffff;
  --win-mic-active-glow: 0 8px 24px rgba(79, 70, 229, 0.42), 0 2px 6px rgba(79, 70, 229, 0.25);
  --win-mic-shadow: 0 4px 12px rgba(45, 42, 38, 0.08), 0 1px 3px rgba(45, 42, 38, 0.04);
  --win-side-bg: #f5f4ef;
  --win-side-border: #dcdad3;
  --win-side-color: #555761;
  --win-chip-bg: #ffffff;
  --win-chip-border: #dcdad3;
  --win-handle-bg: rgba(90, 88, 85, 0.32);
  --win-divider: rgba(0, 0, 0, 0.06);
  --win-tray-bg: rgba(0, 0, 0, 0.04);
}

.dark-mode {
  --win-dock-bg: rgba(32, 33, 37, 0.94);
  --win-dock-border: rgba(255, 255, 255, 0.12);
  --win-dock-shadow: 0 18px 44px rgba(0, 0, 0, 0.55), 0 3px 10px rgba(0, 0, 0, 0.3);
  --win-mic-bg: #2a2b30;
  --win-mic-border: rgba(255, 255, 255, 0.12);
  --win-mic-color: #a1a2af;
  --win-mic-active-bg: linear-gradient(135deg, #818cf8 0%, #6366f1 100%);
  --win-mic-active-color: #ffffff;
  --win-mic-active-glow: 0 8px 24px rgba(99, 102, 241, 0.5), 0 2px 8px rgba(99, 102, 241, 0.3);
  --win-mic-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
  --win-side-bg: #24252a;
  --win-side-border: rgba(255, 255, 255, 0.1);
  --win-side-color: #c5c5ce;
  --win-chip-bg: #24252a;
  --win-chip-border: rgba(255, 255, 255, 0.1);
  --win-handle-bg: rgba(255, 255, 255, 0.28);
  --win-divider: rgba(255, 255, 255, 0.08);
  --win-tray-bg: rgba(255, 255, 255, 0.05);
}
</style>

<style scoped>
/* Main Panel: Compact, Floating, Draggable Acrylic Widget */
.dictation-dock {
  position: fixed;
  z-index: 90;
  bottom: 16px;
  left: calc(240px + (100vw - 240px) / 2);
  transform: translateX(-50%);
  width: 310px;
  max-width: calc(100vw - 24px);
  padding: 6px 14px 14px;
  background: var(--win-dock-bg);
  backdrop-filter: blur(24px) saturate(160%);
  -webkit-backdrop-filter: blur(24px) saturate(160%);
  border: 1px solid var(--win-dock-border);
  border-radius: 20px;
  box-shadow: var(--win-dock-shadow);
  user-select: none;
  box-sizing: border-box;
  transition: box-shadow var(--motion-fast) var(--ease-out), border-color var(--motion-fast) var(--ease-out);
}

.dictation-dock.is-dragging {
  box-shadow: 0 22px 50px rgba(45, 42, 38, 0.22), 0 4px 12px rgba(45, 42, 38, 0.08);
  cursor: grabbing !important;
}

.dictation-dock.is-collapsed {
  width: 250px;
  padding: 4px 12px 10px;
}

/* Top Drag Handle Bar */
.dictation-dock__drag-bar {
  display: flex;
  justify-content: center;
  align-items: center;
  height: 14px;
  cursor: grab;
  padding: 2px 0 6px;
  touch-action: none;
}

.dictation-dock__handle {
  width: 36px;
  height: 4px;
  border-radius: 999px;
  background: var(--win-handle-bg);
  transition: background var(--motion-fast) var(--ease-out), width var(--motion-fast) var(--ease-out);
}

.dictation-dock__drag-bar:hover .dictation-dock__handle,
.dictation-dock.is-dragging .dictation-dock__handle {
  background: var(--primary, #4f46e5);
  width: 44px;
}

/* Header & Status Row */
.dictation-dock__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 2px 0 6px;
  border-bottom: 1px solid var(--win-divider);
}

.dictation-dock__title-group {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  flex: 1;
}

.dictation-dock__title-group strong {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.dictation-dock__signal {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 12px;
  font-size: 10px;
  color: var(--text-muted);
  transition: color var(--motion-fast);
}

.dictation-dock__signal.is-on {
  color: var(--color-success, #18784a);
  text-shadow: 0 0 6px rgba(24, 120, 74, 0.45);
}

.dictation-dock__time {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
  margin-left: auto;
  padding: 1px 6px;
  background: var(--win-tray-bg);
  border-radius: 4px;
}

.win-header-btn {
  background: transparent;
  border: none;
  cursor: pointer;
  padding: 2px 6px;
  font-size: 12px;
  color: var(--text-secondary);
  border-radius: 6px;
  display: inline-flex;
  align-items: center;
  gap: 3px;
  transition: all var(--motion-fast) var(--ease-out);
}

.win-header-btn:hover {
  background: var(--win-tray-bg);
  color: var(--text-primary);
}

/* Hero Controls Trio (Win+H Style: Left Settings, Center Mic, Right Stop) */
.dictation-dock__hero {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 24px;
  padding: 14px 0 8px;
}

/* Center Hero Mic Button */
.win-mic-btn {
  position: relative;
  width: 58px;
  height: 58px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  border: 1px solid var(--win-mic-border);
  outline: none;
  background: var(--win-mic-bg);
  color: var(--win-mic-color);
  box-shadow: var(--win-mic-shadow);
  transition: transform var(--motion-fast) var(--ease-out), box-shadow var(--motion-fast) var(--ease-out), background var(--motion-fast) var(--ease-out), color var(--motion-fast);
}

.win-mic-btn:hover:not(:disabled) {
  transform: translateY(-2px);
  box-shadow: 0 8px 20px rgba(0, 0, 0, 0.12);
}

.win-mic-btn:active:not(:disabled) {
  transform: scale(0.95);
}

.win-mic-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* Active Listening State: Striking Vibrant Indigo Gradient + Breathing Pulse Ring */
.win-mic-btn.is-listening {
  background: var(--win-mic-active-bg);
  color: var(--win-mic-active-color);
  border-color: transparent;
  box-shadow: var(--win-mic-active-glow);
}

.win-mic-btn.is-listening:hover:not(:disabled) {
  transform: translateY(-2px) scale(1.03);
  box-shadow: 0 10px 28px rgba(79, 70, 229, 0.5);
}

.win-mic-btn.is-listening::before {
  content: "";
  position: absolute;
  inset: -7px;
  border-radius: 50%;
  border: 2px solid var(--primary, #4f46e5);
  opacity: 0.45;
  animation: mic-wave 2s infinite cubic-bezier(0.2, 0.8, 0.2, 1);
  pointer-events: none;
}

@keyframes mic-wave {
  0% {
    transform: scale(0.96);
    opacity: 0.75;
  }
  100% {
    transform: scale(1.36);
    opacity: 0;
  }
}

.win-mic-icon {
  width: 27px;
  height: 27px;
  position: relative;
  z-index: 2;
  transition: transform var(--motion-fast);
}

.win-mic-btn.is-listening .win-mic-icon {
  filter: drop-shadow(0 1px 2px rgba(0, 0, 0, 0.25));
}

.win-mic-pulse {
  position: absolute;
  inset: -6px;
  border-radius: 50%;
  background: radial-gradient(circle, rgba(79, 70, 229, 0.35) 0%, rgba(79, 70, 229, 0) 72%);
  pointer-events: none;
  z-index: 1;
  transition: transform 60ms ease-out, opacity 80ms ease-out;
}

/* Accessible hidden label for screen readers & tests */
.win-mic-label,
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border-width: 0;
}

/* Flanking Buttons (Settings & Stop) */
.win-side-btn {
  width: 38px;
  height: 38px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  border: 1px solid var(--win-side-border);
  outline: none;
  background: var(--win-side-bg);
  color: var(--win-side-color);
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.04);
  transition: all var(--motion-fast) var(--ease-out);
}

.win-side-btn:hover:not(:disabled) {
  transform: translateY(-1px);
  background: #ffffff;
  color: var(--text-primary);
  box-shadow: 0 4px 10px rgba(0, 0, 0, 0.08);
}

:global(.dark-mode) .win-side-btn:hover:not(:disabled) {
  background: #36373e;
}

.win-side-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.win-side-btn--stop:hover:not(:disabled) {
  background: var(--color-error-bg, #fff0f1);
  color: var(--color-error, #c32e39);
  border-color: rgba(195, 46, 57, 0.3);
}

.win-side-btn.is-active {
  background: var(--primary-alpha-10, #eeedff);
  color: var(--primary, #4f46e5);
  border-color: var(--primary, #4f46e5);
}

/* Pill Actions Row */
.dictation-dock__actions {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  margin-top: 6px;
  flex-wrap: wrap;
}

.win-chip-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  font-weight: 500;
  padding: 4px 10px;
  border-radius: 999px;
  background: var(--win-chip-bg);
  color: var(--text-primary);
  border: 1px solid var(--win-chip-border);
  cursor: pointer;
  transition: all var(--motion-fast) var(--ease-out);
}

.win-chip-btn:hover:not(:disabled) {
  background: var(--primary-alpha-10, #eeedff);
  border-color: var(--primary, #4f46e5);
  color: var(--primary, #4f46e5);
}

.win-chip-btn--danger:hover:not(:disabled) {
  background: var(--color-error-bg, #fff0f1);
  border-color: var(--color-error, #c32e39);
  color: var(--color-error, #c32e39);
}

.win-chip-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

/* Volume Meter */
.dictation-dock__meter-wrap {
  display: flex;
  justify-content: center;
  margin-top: 8px;
}

.dictation-dock meter {
  width: 90px;
  height: 6px;
  border-radius: 999px;
  overflow: hidden;
}

/* Collapsed Quick Bar */
.dictation-dock__collapsed-bar {
  display: flex;
  align-items: center;
  justify-content: center;
  margin-top: 6px;
}

.win-collapsed-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 500;
  padding: 4px 12px;
  border-radius: 999px;
  background: var(--win-chip-bg);
  border: 1px solid var(--win-chip-border);
  cursor: pointer;
  color: var(--text-primary);
  transition: all var(--motion-fast);
}

.win-collapsed-toggle.is-listening {
  background: var(--primary, #4f46e5);
  color: #ffffff;
  border-color: transparent;
  box-shadow: 0 2px 8px rgba(79, 70, 229, 0.35);
}

.win-collapsed-toggle:hover:not(:disabled) {
  background: var(--primary-alpha-10);
  border-color: var(--primary);
  color: var(--primary);
}

/* Details Tray & Notes */
.dictation-dock__details-tray {
  margin-top: 8px;
  padding: 8px 10px;
  background: var(--win-tray-bg);
  border-radius: 8px;
  font-size: 12px;
  color: var(--text-secondary);
}

.dictation-dock__details-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 4px;
}

.dictation-dock__details-row:last-child {
  margin-bottom: 0;
}

.win-link-btn {
  background: none;
  border: none;
  color: var(--primary);
  cursor: pointer;
  padding: 0;
  font-size: 12px;
  text-decoration: underline;
}

.dictation-dock__note,
.dictation-dock__error {
  font-size: 12px;
  color: var(--text-secondary);
  margin: 6px 0 0;
  overflow-wrap: anywhere;
  text-align: center;
}

.dictation-dock__error {
  color: var(--color-error);
  font-weight: 500;
  background: var(--color-error-bg);
  padding: 4px 8px;
  border-radius: 6px;
}

.dictation-dock__local + .dictation-dock__live {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--win-divider);
}

/* Responsive adjustments */
@media (max-width: 900px) {
  .dictation-dock {
    left: 50%;
    transform: translateX(-50%);
  }
}

.app-shell--compact .dictation-dock {
  left: 50%;
  transform: translateX(-50%);
}
</style>
