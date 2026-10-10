<script setup lang="ts">
import {
  NAlert,
  NButton,
  NCard,
  NFlex,
  NForm,
  NFormItem,
  NGrid,
  NGridItem,
  NInput,
  NInputNumber,
  NRadioButton,
  NRadioGroup,
  NSelect,
  NSpace,
  useMessage,
} from "naive-ui";
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";

import { getJob, getRefineDefaultInstruction, type JobState, submitJob } from "../api/client";
import FastModeSwitch from "../components/FastModeSwitch.vue";
import JobStatusCard from "../components/JobStatusCard.vue";
import ProfileSelector from "../components/ProfileSelector.vue";
import AsrCandidateSelector from "../components/AsrCandidateSelector.vue";
import RemoteFileUpload from "../components/RemoteFileUpload.vue";
import { useConfigOptions } from "../composables/useConfigOptions";

const message = useMessage();
const {
  asrCandidates,
  defaultAsrCandidate,
  defaultSecondaryAsrCandidate,
  activeProfile,
  defaultFastMode,
  defaultOcrFastMode,
  defaultOcrMaxConcurrency,
  defaultOcrModel,
  defaultOcrReasoningEffort,
  defaultOcrSubmitIntervalSeconds,
  defaultOutputDir,
  error,
  loading,
  profiles,
  referenceExtensions,
  videoExtensions,
} = useConfigOptions();
const jobState = ref<JobState | null>(null);
const submitting = ref(false);
const promptLoading = ref(false);
const defaultRefinePrompt = ref("");
const pollHandle = ref<number | null>(null);

const form = reactive({
  content_type: "book_club",
  video: "",
  reference: "",
  output_dir: "",
  profile: "",
  asr_candidate: "",
  secondary_asr_candidate: "",
  fast_mode: null as boolean | null,
  ocr_fast_mode: null as boolean | null,
  ocr_model: "",
  ocr_reasoning_effort: "",
  ocr_max_concurrency: 40 as number | null,
  ocr_submit_interval_seconds: 5 as number | null,
  book_name: "",
  chapter: "",
  glossary_file: "",
  refine_prompt: "",
});

const ocrReasoningOptions = [
  { label: "低", value: "low" },
  { label: "中", value: "medium" },
  { label: "高", value: "high" },
  { label: "很高", value: "xhigh" },
];
const contentTypeOptions = [
  { label: "读书会整理", value: "book_club" },
  { label: "对谈转录", value: "conversation" },
];
const videoAccept = computed(() => videoExtensions.value.join(","));
const referenceAccept = computed(() => referenceExtensions.value.join(","));
const glossaryAccept = ".txt,.md";
const isConversation = computed(() => form.content_type === "conversation");

watch(defaultAsrCandidate, value => { if (!form.asr_candidate) form.asr_candidate = value; });
watch(defaultSecondaryAsrCandidate, value => { form.secondary_asr_candidate = value; });

watch(activeProfile, (value) => {
  if (!form.profile && value) {
    form.profile = value;
  }
});

watch(defaultOutputDir, (value) => {
  if (!form.output_dir && value) {
    form.output_dir = value;
  }
});

watch(defaultFastMode, value => { if (form.fast_mode === null) form.fast_mode = value; });
watch(defaultOcrFastMode, value => { if (form.ocr_fast_mode === null) form.ocr_fast_mode = value; });
watch(defaultOcrModel, (value) => {
  if (!form.ocr_model && value) {
    form.ocr_model = value;
  }
});
watch(defaultOcrReasoningEffort, (value) => {
  if (!form.ocr_reasoning_effort && value) {
    form.ocr_reasoning_effort = value;
  }
});
watch(defaultOcrMaxConcurrency, (value) => {
  if (Number.isFinite(value)) {
    form.ocr_max_concurrency = value;
  }
});
watch(defaultOcrSubmitIntervalSeconds, (value) => {
  if (Number.isFinite(value)) {
    form.ocr_submit_interval_seconds = value;
  }
});

function stopPolling() {
  if (pollHandle.value !== null) {
    window.clearInterval(pollHandle.value);
    pollHandle.value = null;
  }
}

async function refreshJob(jobId: string) {
  const state = await getJob(jobId);
  jobState.value = state;
  if (state.status === "success" || state.status === "partial" || state.status === "failed") {
    stopPolling();
  }
}

function startPolling(jobId: string) {
  stopPolling();
  pollHandle.value = window.setInterval(() => {
    void refreshJob(jobId).catch((caught) => {
      message.error(caught instanceof Error ? caught.message : "刷新任务状态失败");
      stopPolling();
    });
  }, 2000);
}

function effectiveRefinePrompt(): string | null {
  const currentPrompt = form.refine_prompt.trim();
  if (!currentPrompt || currentPrompt === defaultRefinePrompt.value.trim()) {
    return null;
  }
  return form.refine_prompt;
}

function resetRefinePrompt() {
  form.refine_prompt = defaultRefinePrompt.value;
}

async function loadDefaultRefinePrompt() {
  const currentPrompt = form.refine_prompt.trim();
  const previousDefaultPrompt = defaultRefinePrompt.value.trim();
  const shouldReplacePrompt = !currentPrompt || currentPrompt === previousDefaultPrompt;
  promptLoading.value = true;
  try {
    const response = await getRefineDefaultInstruction(form.content_type);
    defaultRefinePrompt.value = response.prompt;
    if (shouldReplacePrompt) {
      form.refine_prompt = response.prompt;
    }
  } catch (caught) {
    message.error(caught instanceof Error ? caught.message : "读取阶段六默认指令失败");
  } finally {
    promptLoading.value = false;
  }
}

async function handleJobRerun(jobId: string) {
  await refreshJob(jobId);
  startPolling(jobId);
}

async function submit() {
  if (!form.video || !form.output_dir) {
    message.warning("视频和服务器默认输出目录是必填项。");
    return;
  }
  if (!isConversation.value && !form.reference) {
    message.warning("读书会整理模式下参考源是必填项。");
    return;
  }
  if (
    !isConversation.value
    && (form.ocr_max_concurrency === null || form.ocr_submit_interval_seconds === null)
  ) {
    message.warning("请填写 PDF OCR 投递间隔和最大并发数。");
    return;
  }
  submitting.value = true;
  try {
    const response = await submitJob({
      video: form.video,
      reference: isConversation.value ? null : form.reference,
      output_dir: form.output_dir,
      content_type: form.content_type,
      profile: form.profile || null,
      asr_candidate: form.asr_candidate || null,
      secondary_asr_candidate: form.secondary_asr_candidate,
      backend: "codex_api",
      ocr_backend: "codex_api",
      fast_mode: form.fast_mode,
      ocr_fast_mode: isConversation.value ? false : form.ocr_fast_mode,
      ocr_model: form.ocr_model || null,
      ocr_reasoning_effort: form.ocr_reasoning_effort || null,
      ocr_max_concurrency: form.ocr_max_concurrency,
      ocr_submit_interval_seconds: form.ocr_submit_interval_seconds,
      book_name: form.book_name || null,
      chapter: form.chapter || null,
      glossary_file: form.glossary_file || null,
      refine_prompt: effectiveRefinePrompt(),
    });
    await refreshJob(response.job_id);
    startPolling(response.job_id);
    message.success(`任务已成功提交：${response.job_id}`);
  } catch (caught) {
    message.error(caught instanceof Error ? caught.message : "提交失败");
  } finally {
    submitting.value = false;
  }
}

onMounted(() => {
  void loadDefaultRefinePrompt();
});
watch(
  () => form.content_type,
  () => {
    if (isConversation.value) {
      form.reference = "";
    }
    void loadDefaultRefinePrompt();
  }
);
onBeforeUnmount(stopPolling);
</script>

<template>
  <div class="workbench-page single-job-view">
    <section class="page-heading">
      <div><h2>新建单任务</h2><p>左侧上传录屏与参考源，右侧选择本次配置。提交后在下方查看进度与整理结果。</p></div>
    </section>

    <n-alert v-if="error" type="error" :title="error" :bordered="false" class="glass-alert" />

    <n-form label-placement="top" class="workbench-columns">
      <div class="workspace-primary">
        <n-card class="view-card form-panel" :bordered="false">
            <div class="form-section">
              <h3 class="form-section-title">输入文件</h3>
              <n-form-item label="任务类型" required>
                <n-radio-group v-model:value="form.content_type" size="medium">
                  <n-radio-button
                    v-for="item in contentTypeOptions"
                    :key="item.value"
                    :value="item.value"
                  >
                    {{ item.label }}
                  </n-radio-button>
                </n-radio-group>
              </n-form-item>
              <n-form-item label="视频文件" required>
                <n-space vertical class="w-full">
                  <RemoteFileUpload
                    v-model="form.video"
                    kind="video"
                    label="视频文件"
                    :accept="videoAccept"
                    button-text="选择并上传本机视频"
                  />
                  <n-input v-model:value="form.video" readonly placeholder="上传后自动生成服务器路径" />
                </n-space>
              </n-form-item>
              <n-form-item v-if="!isConversation" label="参考源文件或 URL" required>
                <n-space vertical class="w-full">
                  <n-input v-model:value="form.reference" placeholder="可粘贴 https:// 网址，或上传本机参考文件" />
                  <RemoteFileUpload
                    v-model="form.reference"
                    kind="reference"
                    label="参考源文件"
                    :accept="referenceAccept"
                    button-text="选择并上传本机参考源"
                  />
                </n-space>
              </n-form-item>
              <n-form-item label="成果获取方式" required>
                <n-alert type="info" :bordered="false" class="server-output-note">
                  <div class="server-output-note__body">
                    <span>处理完成后，到任务列表点击“下载结果”选择 Markdown 或 TXT。</span>
                    <small>服务器默认保存目录：{{ form.output_dir || "配置加载中..." }}</small>
                  </div>
                </n-alert>
              </n-form-item>
            </div>
        </n-card>
      </div>
      <aside class="workspace-inspector" aria-label="本次任务配置">
        <n-card class="view-card form-panel" :bordered="false">
            <div class="form-section">
              <h3 class="form-section-title">常用配置 · 仅本次任务</h3>
              <n-form-item label="语音转文字模型">
                <AsrCandidateSelector v-model="form.asr_candidate" v-model:secondary-candidate="form.secondary_asr_candidate" :options="asrCandidates" :loading="loading" />
              </n-form-item>
              <n-form-item label="配置方案">
                <ProfileSelector v-model="form.profile" :options="profiles" :loading="loading" />
              </n-form-item>
              <section class="fast-mode-options" aria-label="快速模式">
                <h4>快速模式 · 仅本次任务</h4>
                <FastModeSwitch v-model="form.fast_mode" label="AI 精修快速模式" :disabled="loading" />
                <FastModeSwitch v-if="!isConversation" v-model="form.ocr_fast_mode" label="PDF OCR 快速模式" :disabled="loading" />
                <p class="prompt-hint">通过 CPA 请求快速服务，可能增加额度消耗或费用；不改变模型或推理强度。PDF 开关仅在实际执行 OCR 时生效。</p>
              </section>
              <details class="advanced-options">
                <summary>高级参数 · OCR、术语与整理指令</summary>
                <n-grid :cols="2" :x-gap="12" :y-gap="0" responsive="screen" item-responsive>
                  <n-grid-item span="2">
                    <n-form-item label="术语词表">
                      <n-space vertical class="w-full">
                        <RemoteFileUpload v-model="form.glossary_file" kind="glossary" label="术语词表" :accept="glossaryAccept" button-text="选择并上传本机词表" />
                        <n-input v-model:value="form.glossary_file" readonly placeholder="可选，上传后自动生成服务器路径" />
                      </n-space>
                    </n-form-item>
                  </n-grid-item>
                <n-grid-item v-if="!isConversation" span="2 m:1">
                  <n-form-item label="PDF OCR 模型">
                    <n-input v-model:value="form.ocr_model" placeholder="例如 gpt-6-luna" />
                  </n-form-item>
                </n-grid-item>
                <n-grid-item v-if="!isConversation" span="2 m:1">
                  <n-form-item label="PDF OCR 推理强度">
                    <n-select v-model:value="form.ocr_reasoning_effort" :options="ocrReasoningOptions" />
                  </n-form-item>
                </n-grid-item>
                <n-grid-item v-if="!isConversation" span="2 m:1">
                  <n-form-item label="页面投递间隔（秒）" required>
                    <n-input-number
                      v-model:value="form.ocr_submit_interval_seconds"
                      :min="0"
                      :step="0.5"
                      class="w-full"
                    />
                  </n-form-item>
                </n-grid-item>
                <n-grid-item v-if="!isConversation" span="2 m:1">
                  <n-form-item label="最大在途请求数" required>
                    <n-input-number
                      v-model:value="form.ocr_max_concurrency"
                      :min="1"
                      :precision="0"
                      class="w-full"
                    />
                  </n-form-item>
                </n-grid-item>
                <n-grid-item span="2 m:1">
                  <n-form-item label="书籍名称">
                    <n-input v-model:value="form.book_name" placeholder="例如：《Lesp 读书会》" />
                  </n-form-item>
                </n-grid-item>
                <n-grid-item span="2">
                  <n-form-item label="章节名称">
                    <n-input v-model:value="form.chapter" placeholder="例如：第 1 章 导言" />
                  </n-form-item>
                </n-grid-item>
                <n-grid-item span="2">
                  <n-form-item label="阶段六指令">
                    <n-space vertical class="w-full">
                      <n-input
                        v-model:value="form.refine_prompt"
                        type="textarea"
                        :autosize="{ minRows: 10, maxRows: 18 }"
                        :loading="promptLoading"
                        placeholder="读取默认阶段六指令中..."
                      />
                      <n-flex justify="space-between" align="center" :size="12" wrap>
                        <span class="prompt-hint">文本框内为当前默认指令，可直接在此基础上调整；不修改时提交会使用项目默认指令。</span>
                        <n-button size="small" secondary :disabled="!defaultRefinePrompt" @click="resetRefinePrompt">
                          恢复默认指令
                        </n-button>
                      </n-flex>
                    </n-space>
                  </n-form-item>
                </n-grid-item>
              </n-grid>
              </details>
            </div>

        <n-flex justify="end" class="form-action-area">
          <n-button type="primary" size="large" :loading="submitting" @click="submit" class="submit-btn">
            <template #icon>
              <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:18px;height:18px"><polygon points="5 3 19 12 5 21 5 3"/></svg>
            </template>
            开始整理
          </n-button>
        </n-flex>
        </n-card>
      </aside>
    </n-form>
    <JobStatusCard v-if="jobState" title="当前任务" :state="jobState" default-expanded @rerun="handleJobRerun" />
  </div>
</template>

<style scoped>
.workbench-columns { grid-template-columns: repeat(2, minmax(0, 1fr)); }
@media (max-width: 1200px) {
  .workbench-columns { grid-template-columns: minmax(0, 1fr); }
}
</style>
