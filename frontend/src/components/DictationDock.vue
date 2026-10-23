<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { NButton, useMessage } from "naive-ui";
import { useDictation } from "../composables/useDictation";

const emit = defineEmits<{ height: [value: number] }>();
const router = useRouter(), message = useMessage(), d = useDictation();
const { recordingSession, recognizing, capturing, captureAction, captureLabel, level, connection, error } = d;
const collapsed = ref(false), panel = ref<HTMLElement | null>(null);
const compactStatus = computed(() => {
  if (captureAction.value) return { starting: '准备中', pausing: '暂停中', resuming: '继续中', stopping: '结束中' }[captureAction.value];
  if (recordingSession.value?.status === 'paused') return '已暂停';
  if (recordingSession.value?.status === 'draining') return '收尾中';
  return capturing.value ? '听写中' : '采音已停止';
});
const local = d.local;
const { active: localActive, draft: localDraft, capturing: localCapturing, recordedSamples, busy: localBusy } = local;
function clock(samples: number) { const s = Math.floor(samples / 16000); return `${String(Math.floor(s / 60)).padStart(2,'0')}:${String(s % 60).padStart(2,'0')}`; }
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
onBeforeUnmount(() => { observer?.disconnect(); emit("height", 0); });
async function action(callback: () => Promise<unknown>) {
  try { await callback(); } catch (cause) { error.value = cause instanceof Error ? cause.message : String(cause); }
}
async function copy() {
  const snapshot = recordingSession.value?.draft_text ?? "";
  try { await navigator.clipboard.writeText(snapshot); message.success("已复制活动会话的已完成正文"); }
  catch { message.error("剪贴板不可用，请打开正文下载 TXT 或选中文字复制"); }
}
async function openText() { d.showRecording(); await router.push("/dictation?mode=live"); }
</script>

<template>
  <aside v-if="(recognizing && recordingSession) || localActive" ref="panel" class="dictation-dock" :class="{ 'is-collapsed': collapsed }" aria-label="常驻语音控制">
    <section v-if="localActive && localDraft" class="dictation-dock__local" aria-label="本地录音控制">
      <div class="dictation-dock__status" role="status"><strong>{{ localCapturing ? '● 本地录音中' : 'Ⅱ 本地录音已暂停 · 麦克风关闭' }}</strong><span>{{ clock(recordedSamples) }}</span><n-button text size="small" @click="collapsed = !collapsed">{{ collapsed ? '展开' : '收起' }}</n-button></div>
      <div class="dictation-dock__actions">
        <n-button :disabled="localBusy" @click="action(localCapturing ? local.pause : () => local.begin(localDraft!.id))">{{ localCapturing ? '暂停录音' : '继续录音' }}</n-button>
        <template v-if="!collapsed"><n-button :disabled="localBusy" @click="action(local.end)">结束录音</n-button><n-button quaternary @click="router.push('/dictation?mode=record')">录音草稿</n-button></template>
      </div>
      <p v-if="!collapsed" class="dictation-dock__note">{{ localBusy ? '正在保存' : '已保存' }} {{ clock(localDraft.samples) }} · 本地录音不占识别资源</p>
      <p v-if="d.inputWarning.value" class="dictation-dock__note" role="status">{{ d.inputWarning.value }}</p>
      <p v-if="local.error.value" class="dictation-dock__error" role="alert">{{ local.error.value }}</p>
    </section>
    <section v-if="recognizing && recordingSession" aria-label="活动识别控制">
    <div class="dictation-dock__status" role="status">
      <span class="dictation-dock__signal" :class="{ 'is-on': capturing }" aria-hidden="true">{{ recordingSession.status === 'paused' ? 'Ⅱ' : '●' }}</span>
      <strong>{{ recordingSession.mode === 'upload' ? recordingSession.phase : collapsed ? compactStatus : captureLabel }}</strong>
      <span class="dictation-dock__time">{{ duration }}</span>
      <meter v-if="!collapsed && capturing" min="0" max="1" :value="level" aria-label="活动麦克风音量" />
      <n-button v-if="collapsed" size="small" :disabled="!!captureAction || !!d.fileAction.value || !['listening','paused','draining'].includes(recordingSession.status) || (recordingSession.mode === 'live' && recordingSession.status === 'draining')" @click="action(recordingSession.mode === 'upload' ? () => d.fileControl(recordingSession!.id, recordingSession!.status === 'paused' ? 'resume' : 'pause') : recordingSession.status === 'paused' ? d.resume : d.pause)">{{ recordingSession.status === 'paused' ? '继续' : '暂停' }}</n-button>
      <n-button text size="small" :aria-expanded="!collapsed" @click="collapsed = !collapsed">{{ collapsed ? '展开' : '收起' }}</n-button>
    </div>
    <template v-if="!collapsed">
      <p v-if="d.inputWarning.value && !localActive" class="dictation-dock__note" role="status">{{ d.inputWarning.value }}</p>
      <p v-if="connection" class="dictation-dock__note" role="status">{{ connection }}</p>
      <p v-if="error" class="dictation-dock__error" role="alert">{{ error }}</p>
      <div class="dictation-dock__actions">
        <n-button v-if="recordingSession.mode === 'live'" type="primary" :disabled="!!captureAction || !['listening','paused'].includes(recordingSession.status)" @click="action(recordingSession.status === 'paused' ? d.resume : d.pause)">{{ recordingSession.status === 'paused' ? '继续听写' : '暂停听写' }}</n-button>
        <n-button v-else type="primary" :disabled="!!d.fileAction.value || !['listening','paused','draining'].includes(recordingSession.status)" @click="action(() => d.fileControl(recordingSession!.id, recordingSession!.status === 'paused' ? 'resume' : 'pause'))">{{ recordingSession.status === 'paused' ? '继续转写' : '暂停转写' }}</n-button>
        <n-button :disabled="captureAction === 'stopping' || recordingSession.status === 'draining'" @click="action(() => d.stop(recordingSession!.id))">结束听写</n-button>
        <n-button :disabled="!recordingSession.draft_text" @click="copy">复制正文</n-button>
        <n-button quaternary @click="openText">正文</n-button>
        <n-button quaternary @click="action(() => d.cancel(recordingSession!.id))">取消后续处理</n-button>
      </div>
      <div v-if="recordingSession.mode === 'upload' && recordingSession.progress" class="dictation-dock__note">
        <template v-if="recordingSession.progress.total_samples">转写 {{ Math.floor(recordingSession.progress.transcribed_samples / recordingSession.progress.total_samples * 100) }}% <progress :value="recordingSession.progress.transcribed_samples" :max="recordingSession.progress.total_samples" aria-label="实际音频转写进度" /></template>
        <span> · 识别失败 {{ recordingSession.progress.asr_failed }} 段 · 整理 {{ recordingSession.progress.cleanup_done }}/{{ recordingSession.progress.cleanup_total }}（当前应整理） · 整理失败 {{ recordingSession.progress.cleanup_failed }} 段</span>
      </div>
      <p v-if="['paused','pausing'].includes(recordingSession.status) || captureAction === 'resuming'" class="dictation-dock__note">暂停不释放模型和识别资源；关闭录音窗口或断网超过宽限期后会结束会话。</p>
    </template>
    </section>
  </aside>
</template>

<style scoped>
.dictation-dock { position: fixed; z-index: 90; bottom: 12px; left: 240px; right: 24px; max-width: 960px; margin: 0 auto; padding: 16px 20px calc(16px + env(safe-area-inset-bottom)); background: var(--surface-raised); border: 1px solid var(--border-strong); border-radius: 12px; box-shadow: 0 4px 24px #0002; }
.dictation-dock__local + section { margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--border-subtle); }
.dictation-dock__status { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
.dictation-dock__status strong { flex: 1; min-width: 100px; font-size: 14px; font-weight: 600; overflow-wrap: anywhere; }
.dictation-dock__signal { color: var(--text-muted); }.dictation-dock__signal.is-on { color: var(--color-success); }
.dictation-dock__time { color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.dictation-dock meter { width: 64px; }
.dictation-dock__actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 12px; }
.dictation-dock__note, .dictation-dock__error { font-size: 13px; color: var(--text-secondary); margin: 10px 0 0; overflow-wrap: anywhere; }
.dictation-dock__error { color: var(--color-error); }
.app-shell--compact .dictation-dock { left: 12px; right: 12px; }
@media(max-width:900px) { .dictation-dock { left: 16px; right: 16px; } }
@media(max-width:600px) { .dictation-dock { padding: 12px 14px calc(12px + env(safe-area-inset-bottom)); }.dictation-dock__status { gap: 8px; }.dictation-dock meter { width: 44px; } }
</style>
