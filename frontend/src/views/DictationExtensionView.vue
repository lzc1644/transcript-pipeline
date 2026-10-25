<script setup lang="ts">
import { computed, ref } from "vue";
import { NButton } from "naive-ui";
import { useDictation } from "../composables/useDictation";
import { useDictationExtensionBridge } from "../composables/dictationExtensionBridge";
import { parseTerms } from "../composables/dictationTerms";
import DictationExtensionInstall from "../components/DictationExtensionInstall.vue";
const d = useDictation(), terms = ref("");
const bridge = useDictationExtensionBridge(d, () => parseTerms(terms.value));
async function endRecording(cancel = false) {
  const extension = bridge.state();
  if (extension.operationId) {
    await bridge.stop(cancel);
    // A rejected extension start must not hide the stop control for an existing normal-page owner.
    if (extension.sessionId || extension.phase !== "failed") return;
  }
  await bridge.cancelSetup();
  // The app controller may already own a normal-page recording after an SPA route change.
  const id = d.recordingSession.value?.id;
  if (id && d.ownsCapture.value) await d.stop(id);
  else if (d.captureAction.value) d.error.value = "原网页仍在准备，请回语音输入工作台结束准备。";
}
const duration = computed(() => {
  const n = Math.floor((d.recordingSession.value?.samples ?? 0) / 16000);
  return `${Math.floor(n / 60)}:${String(n % 60).padStart(2, "0")}`;
});
</script>
<template>
  <section class="workbench-page extension-recorder" aria-label="扩展录音页">
    <header class="page-heading"><div><h2>浏览器语音输入</h2><p>S1 开发验证 · 尚不回填文字</p><p>{{ bridge.connected.value ? '扩展已连接网页' : '请从扩展工具栏连接此服务' }}</p></div><a href="/dictation">语音输入工作台</a></header>
    <div class="extension-actions" aria-live="polite"><strong>{{ d.capturing.value ? '正在听写' : d.captureLabel.value }} · {{ duration }}</strong><n-button :disabled="!d.capturing.value && !d.captureAction.value" @click="endRecording()">结束</n-button><n-button @click="endRecording(true)">取消回填并停止</n-button></div>
    <p v-if="bridge.error.value || d.error.value" role="alert">{{ bridge.error.value || d.error.value }}</p>
    <DictationExtensionInstall />
    <div class="extension-paper" role="region" aria-label="本次听写结果"><p v-if="!d.recordingSession.value">启用扩展控制后，回到目标输入框按快捷键开始。</p><pre>{{ d.recordingSession.value?.draft_text }}</pre><p class="extension-preview">{{ d.partial.value.map(u => u.preview).join('') }}</p></div>
    <section class="dictation-settings"><h3>录音准备</h3><p v-if="bridge.captureKind.value === 'sidebar'">Firefox 使用原生侧栏采音。请在扩展面板打开侧栏并授权试音，听写期间保持它可见；本页只连接服务，不申请网页麦克风。</p><p>音频保存在您连接的服务；转录文字会发送给配置的校对供应商。目标输入框原文不离开目标页面。</p><label for="extension-terms">本次术语（主动选用，最多50项）</label><textarea id="extension-terms" v-model="terms" :disabled="d.recognizing.value" rows="2" /><div class="extension-actions"><n-button v-if="!bridge.enabled.value" type="primary" :disabled="!bridge.connected.value" :loading="d.deviceBusy.value" @click="bridge.enable">{{ bridge.captureKind.value === 'sidebar' ? '启用服务端控制' : '启用扩展控制并试音' }}</n-button><n-button v-else @click="bridge.disable">停止扩展控制</n-button><span>{{ bridge.enabled.value ? (bridge.captureKind.value === 'sidebar' ? '服务控制已启用 · 另需侧栏试音就绪' : '就绪 · 试音已释放麦克风') : '尚未启用' }}</span></div></section>
  </section>
</template>
<style scoped>
.extension-recorder { min-height: calc(100dvh - 40px); display: flex; flex-direction: column; gap: 16px; }
.extension-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; flex: none; }
.extension-paper { flex: 1; min-height: 200px; max-height: 50dvh; overflow: auto; padding: 20px; background: var(--surface-raised); border: 1px solid var(--border-strong); border-radius: 8px; }
pre { font: inherit; white-space: pre-wrap; overflow-wrap: anywhere; margin: 0; line-height: 1.8; }
.extension-preview { color: var(--text-muted); }
.dictation-settings { flex: none; }
.dictation-settings label { display: block; margin-bottom: 8px; }
.dictation-settings textarea { display: block; box-sizing: border-box; width: 100%; margin-bottom: 12px; padding: 8px; font: inherit; color: var(--text-primary); background: var(--surface-raised); border: 1px solid var(--border-strong); border-radius: 6px; }
@media(max-width: 600px) { .extension-recorder { height: auto; min-height: calc(100dvh - 40px); } .extension-paper { min-height: 240px; max-height: 50dvh; } }
</style>
