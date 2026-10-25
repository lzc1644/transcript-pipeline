<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { NButton } from "naive-ui";
import { useDictation } from "../composables/useDictation";
const props = defineProps<{ terms: string[]; cleanup: boolean; cleanupModel: string; documentCleanupModel: string;
  cleanupEffort: string; documentEffort: string }>();
const emit = defineEmits<{ history: [] }>();
const d = useDictation(), local = d.local;
const { draft, drafts, busy, active, capturing, level, error, recordedSamples, playbackURL, submittingId, readOnly } = local;
const player = ref<HTMLAudioElement | null>(null);
const states = { recording: "上次未结束", paused: "已暂停", ended: "已结束", interrupted: "意外中断，已保存部分可用" };
function duration(samples: number) { const s = Math.floor(samples / 16000); return `${String(Math.floor(s / 60)).padStart(2,'0')}:${String(s % 60).padStart(2,'0')}`; }
const canSubmit = computed(() => !!draft.value?.samples && draft.value.submission?.status !== 'unknown' && !readOnly.value && !active.value && !busy.value && !submittingId.value && !d.recognizing.value && !d.busy.value && !d.uploadUncertain.value);
async function action(callback: () => Promise<unknown>) { try { await callback(); } catch (cause) { error.value = cause instanceof Error ? cause.message : String(cause); } }
async function begin(id?: string) { player.value?.pause(); await local.begin(id); }
async function download() {
  player.value?.pause(); const result = await local.preview();
  const link = document.createElement('a'); link.href = playbackURL.value; link.download = `录音-${result.draft.id}.wav`; link.click();
}
async function submit() {
  if (!canSubmit.value || !draft.value) return;
  player.value?.pause();
  const { blob, draft: frozen } = await local.freezeForSubmission();
  const state = await d.upload(new File([blob], `recording-${frozen.id}.wav`, { type: 'audio/wav' }), props.terms, props.cleanup,
    props.cleanupModel, props.documentCleanupModel, props.cleanupEffort, props.documentEffort);
  await local.submission(frozen.id, state ? 'submitted' : d.uploadUncertain.value ? 'unknown' : 'failed', frozen.samples, state?.id);
}
function remove(id: string) {
  if (confirm('只删除本浏览器中的这条录音草稿？服务器录音与文字不会删除。此操作不能撤销。')) void action(() => local.remove(id));
}
onMounted(() => action(local.load));
onBeforeUnmount(() => { d.playback.release(player.value); player.value?.pause(); });
function claimPlayback() { if (player.value) void action(async () => d.playback.claim(player.value!)); }
</script>

<template>
  <section class="recording-draft" aria-label="本地录音转写">
    <h3>先录下来，再慢慢整理</h3>
    <p>录音先保存在本浏览器，不等待模型、不占识别资源。结束后可试听、下载 WAV，再提交转写。默认最多4小时（原始音频约460.8 MB）；浏览器存储和临时磁盘空间不足仍可能提前停止，请及时下载备份。</p>
    <p v-if="error" class="draft-error" role="alert">{{ error }}</p>
    <div v-if="draft" class="draft-current">
      <strong>{{ capturing ? '正在录音' : readOnly ? '另一窗口正在操作（只读）' : states[draft.status] }} · 已录制 {{ duration(recordedSamples) }}</strong>
      <p role="status">{{ !busy && draft.samples === recordedSamples ? '已保存到本浏览器' : '正在保存' }} {{ duration(draft.samples) }}（{{ draft.samples }} 个样本）<span v-if="recordedSamples !== draft.samples"> · 尚未保存 {{ recordedSamples - draft.samples }} 个样本</span></p>
      <p v-if="readOnly" role="status">此草稿由另一窗口录音或提交，仅可查看。请返回原窗口控制；它结束或关闭后，可重新检查草稿。</p>
      <meter v-if="capturing" min="0" max="1" :value="level" aria-label="本地录音音量" />
      <audio v-if="playbackURL" ref="player" :src="playbackURL" controls preload="metadata" aria-label="录音试听" @play="claimPlayback" />
      <div class="draft-actions">
        <template v-if="readOnly">
          <n-button :disabled="busy" @click="action(() => local.select(draft!.id))">重新检查草稿</n-button>
        </template>
        <template v-else-if="active">
          <n-button v-if="capturing" :disabled="busy" @click="action(local.pause)">暂停录音</n-button>
          <n-button v-else :disabled="busy || d.ownsCapture.value" @click="action(() => begin(draft!.id))">继续录音</n-button>
          <n-button :disabled="busy" @click="action(local.end)">结束录音</n-button>
          <n-button v-if="!capturing" :disabled="busy || !draft.samples" @click="action(local.preview)">试听已保存录音</n-button>
        </template>
        <template v-else>
          <n-button :disabled="busy || !!submittingId || d.ownsCapture.value" @click="action(() => begin(draft!.id))">继续录音</n-button>
          <n-button :disabled="busy || !draft.samples" @click="action(local.preview)">试听录音</n-button>
          <n-button :disabled="busy || !draft.samples" @click="action(download)">下载录音 WAV</n-button>
          <n-button type="primary" :disabled="!canSubmit" @click="action(submit)">{{ submittingId ? '正在提交转写' : '开始转写' }}</n-button>
          <n-button type="error" quaternary :disabled="busy || !!submittingId" @click="remove(draft.id)">删除本地草稿</n-button>
        </template>
      </div>
      <div v-if="d.uploadProgress.value" role="status"><p>上传 {{ d.uploadProgress.value.loaded }} / {{ d.uploadProgress.value.total }} 字节</p><progress :value="d.uploadProgress.value.loaded" :max="d.uploadProgress.value.total" aria-label="草稿实际上传进度" /><n-button @click="d.cancelUpload">取消上传</n-button></div>
      <p v-if="draft.submission?.status === 'submitted'">已提交 {{ duration(draft.submission.samples) }} 音频版本，服务器会话 {{ draft.submission.sessionId }}。本地草稿仍保留。</p>
      <p v-if="draft.submission?.status === 'failed'">上传失败，录音保留。可下载，或手动重新提交。</p>
      <div v-if="draft.submission?.status === 'unknown'">
        <p>提交结果不明确，不自动重复提交。请先检查历史是否已有对应会话。</p>
        <n-button @click="emit('history')">检查服务器历史</n-button>
        <n-button :disabled="readOnly || busy" @click="action(async () => { d.confirmUploadChecked(); await local.submission(draft!.id, 'failed', draft!.submission!.samples); })">已检查历史，允许手动重试</n-button>
      </div>
    </div>
    <n-button v-if="!active" :disabled="busy || !!submittingId || d.ownsCapture.value || !!d.captureAction.value" @click="action(() => begin())">新建本地录音</n-button>
    <h4>本浏览器的录音草稿</h4>
    <p v-if="!drafts.length">还没有本地草稿。刷新后可以从这里打开已保存的录音，不会自动开启麦克风。</p>
    <article v-for="item in drafts" :key="item.id" class="draft-row">
      <div><strong>{{ new Date(item.createdAt).toLocaleString() }} · {{ duration(item.samples) }}</strong><p>{{ states[item.status] }} · {{ item.samples }} 个已保存样本</p></div>
      <n-button :disabled="active || busy || !!submittingId" @click="action(() => local.select(item.id))">打开草稿</n-button>
    </article>
  </section>
</template>

<style scoped>
.recording-draft {
  display: grid;
  gap: 16px;
}
.recording-draft h3, .recording-draft h4, .recording-draft p {
  margin: 0;
}
.recording-draft h3 {
  font-size: 18px;
  font-weight: 600;
  color: var(--text-primary);
}
.recording-draft h4 {
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
  margin-top: 12px;
}
.recording-draft > p, .draft-current > p, .draft-row p {
  color: var(--text-secondary);
  font-size: 13px;
  overflow-wrap: anywhere;
}
.draft-current {
  display: grid;
  gap: 12px;
  background: var(--surface-raised);
  border: 1px solid var(--border-subtle);
  padding: 18px 20px;
  border-radius: 8px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
}
.draft-current strong {
  font-size: 15px;
  color: var(--text-primary);
}
.draft-current audio {
  width: 100%;
  max-width: 560px;
  height: 40px;
}
.draft-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin-top: 6px;
}
.draft-row {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  justify-content: space-between;
  align-items: center;
  padding: 14px 16px;
  background: var(--surface-raised);
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  transition: border-color var(--motion-fast) var(--ease-out);
}
.draft-row:hover {
  border-color: var(--border-strong);
}
.draft-row strong {
  font-size: 14px;
  color: var(--text-primary);
}
.draft-error {
  padding: 12px 16px;
  border-left: 3px solid var(--color-error);
  background: var(--color-error-bg);
  color: var(--color-error);
  border-radius: 0 6px 6px 0;
  font-size: 13px;
}
@media (max-width: 600px) {
  .draft-current {
    padding: 14px 16px;
  }
  .draft-actions {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 8px;
  }
  .draft-actions > .n-button--primary-type,
  .draft-actions > .n-button:first-child {
    grid-column: 1 / -1;
  }
  .draft-actions > .n-button {
    width: 100%;
  }
  .draft-row {
    flex-direction: column;
    align-items: flex-start;
  }
  .draft-row > .n-button {
    width: 100%;
  }
}
</style>
