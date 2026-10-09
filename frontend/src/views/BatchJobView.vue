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
  NSelect,
  NRadioButton,
  NRadioGroup,
  NSpace,
  NTag,
  useMessage,
} from "naive-ui";
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";

import { getBatch, getRefineDefaultInstruction, listFs, submitBatchJob, type FileItem, type JobState } from "../api/client";
import BackendSelector from "../components/BackendSelector.vue";
import ProfileSelector from "../components/ProfileSelector.vue";
import AsrCandidateSelector from "../components/AsrCandidateSelector.vue";
import RemoteDirectoryUpload from "../components/RemoteDirectoryUpload.vue";
import RemoteFileUpload from "../components/RemoteFileUpload.vue";
import { useConfigOptions } from "../composables/useConfigOptions";

type BatchMode = "manifest" | "paired-dir" | "shared-reference" | "conversation-dir";

interface SourcePreview {
  videoCount: number;
  ignoredCount: number;
  referenceCount: number;
  missingReferences: string[];
  duplicateReferences: string[];
}

const message = useMessage();
const {
  asrCandidates,
  defaultAsrCandidate,
  defaultSecondaryAsrCandidate,
  activeProfile,
  backends,
  defaultBackend,
  defaultOcrBackend,
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
const batchState = ref<JobState | null>(null);
const submitting = ref(false);
const previewLoading = ref(false);
const previewError = ref("");
const sourcePreview = ref<SourcePreview | null>(null);
const promptLoading = ref(false);
const defaultRefinePrompt = ref("");
const pollHandle = ref<number | null>(null);

const form = reactive<{
  mode: BatchMode;
  content_type: string;
  manifest: string;
  videos_dir: string;
  reference_dir: string;
  shared_reference: string;
  output_dir: string;
  profile: string;
  asr_candidate: string;
  secondary_asr_candidate: string;
  backend: string;
  ocr_backend: string;
  ocr_model: string;
  ocr_reasoning_effort: string;
  ocr_max_concurrency: number | null;
  ocr_submit_interval_seconds: number | null;
  remote_concurrency: number | null;
  book_name: string;
  chapter: string;
  glossary_file: string;
  refine_prompt: string;
}>({
  mode: "manifest",
  content_type: "book_club",
  manifest: "",
  videos_dir: "",
  reference_dir: "",
  shared_reference: "",
  output_dir: "",
  profile: "",
  asr_candidate: "",
  secondary_asr_candidate: "",
  backend: "",
  ocr_backend: "",
  ocr_model: "",
  ocr_reasoning_effort: "",
  ocr_max_concurrency: 40,
  ocr_submit_interval_seconds: 5,
  remote_concurrency: 2,
  book_name: "",
  chapter: "",
  glossary_file: "",
  refine_prompt: "",
});

const ocrBackendOptions = [
  { label: "Codex API", value: "codex_api" },
];
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
const manifestAccept = ".json,.yaml,.yml";
const referenceAccept = computed(() => referenceExtensions.value.join(","));
const glossaryAccept = ".txt,.md";

const modeTip = computed(() => {
  if (form.mode === "manifest") {
    return "Manifest 模式将按指定的清单 JSON/YAML 提交；未声明 content_type 的条目会继承当前选择的默认任务类型。";
  }
  if (form.mode === "paired-dir") {
    return "目录配对模式会自动扫描视频目录下所有视频，并在参考目录中匹配同名（basename）的 .txt、.md 或 .pdf 参考文本。";
  }
  if (form.mode === "conversation-dir") {
    return "对谈录屏目录模式只扫描视频目录，不需要参考源，适合访谈、讨论和自由对话录屏。";
  }
  return "共享参考模式会自动扫描视频目录下所有视频，并为它们全部配置同一个共享的参考文本文件或 URL 地址。";
});

const requiredWarning = computed(() => {
  if (form.mode === "manifest" && !form.manifest) {
    return "请选择 Manifest 清单文件。";
  }
  if (form.mode !== "manifest" && (!form.videos_dir || !form.output_dir)) {
    return "请选择视频源目录，并等待服务器默认输出目录加载完成。";
  }
  if (form.mode === "paired-dir" && !form.reference_dir) {
    return "目录配对模式下参考源目录是必填项。";
  }
  if (form.mode === "shared-reference" && !form.shared_reference.trim()) {
    return "共享参考模式下需要指定共享的参考源或 URL。";
  }
  if (!form.remote_concurrency || form.remote_concurrency < 1) {
    return "流水线远程并发度必须是大于等于 1 的整数。";
  }
  if (form.ocr_max_concurrency === null || form.ocr_submit_interval_seconds === null) {
    return "请填写 PDF OCR 投递间隔和最大并发数。";
  }
  return "";
});

const previewHasBlockingIssue = computed(() => {
  if (!sourcePreview.value) {
    return false;
  }
  return (
    sourcePreview.value.videoCount === 0 ||
    sourcePreview.value.missingReferences.length > 0 ||
    sourcePreview.value.duplicateReferences.length > 0
  );
});

const batchItems = computed(() => batchState.value?.items ?? []);

const effectiveContentType = computed(() => {
  if (form.mode === "conversation-dir") {
    return "conversation";
  }
  if (form.mode === "paired-dir" || form.mode === "shared-reference") {
    return "book_club";
  }
  return form.content_type;
});

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

watch(defaultBackend, (value) => {
  if (!form.backend && value) {
    form.backend = value;
  }
});

watch(defaultOcrBackend, (value) => {
  if (!form.ocr_backend && value) {
    form.ocr_backend = value;
  }
});
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

watch(
  () => [form.mode, form.videos_dir, form.reference_dir, form.shared_reference, form.output_dir],
  () => {
    sourcePreview.value = null;
    previewError.value = "";
  }
);

watch(
  () => form.mode,
  (mode) => {
    if (mode === "conversation-dir") {
      form.content_type = "conversation";
      form.reference_dir = "";
      form.shared_reference = "";
    }
    if (mode === "paired-dir" || mode === "shared-reference") {
      form.content_type = "book_club";
    }
  }
);

watch(effectiveContentType, () => {
  void loadDefaultRefinePrompt();
});

function stopPolling() {
  if (pollHandle.value !== null) {
    window.clearInterval(pollHandle.value);
    pollHandle.value = null;
  }
}

async function refreshBatch(batchId: string) {
  const state = await getBatch(batchId);
  batchState.value = state;
  if (state.status === "success" || state.status === "partial" || state.status === "failed") {
    stopPolling();
  }
}

function startPolling(batchId: string) {
  stopPolling();
  pollHandle.value = window.setInterval(() => {
    void refreshBatch(batchId).catch((caught) => {
      message.error(caught instanceof Error ? caught.message : "刷新批量任务状态失败");
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
    const response = await getRefineDefaultInstruction(effectiveContentType.value);
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

function fileStem(name: string): string {
  const dotIndex = name.lastIndexOf(".");
  return dotIndex > 0 ? name.slice(0, dotIndex) : name;
}

function fileSuffix(name: string): string {
  const dotIndex = name.lastIndexOf(".");
  return dotIndex >= 0 ? name.slice(dotIndex).toLowerCase() : "";
}

function isAllowedFile(item: FileItem, extensions: string[]): boolean {
  return !item.is_dir && extensions.includes(fileSuffix(item.name));
}

function countReferencesByStem(items: FileItem[]) {
  const counter = new Map<string, number>();
  for (const item of items) {
    if (!isAllowedFile(item, referenceExtensions.value)) {
      continue;
    }
    const stem = fileStem(item.name);
    counter.set(stem, (counter.get(stem) ?? 0) + 1);
  }
  return counter;
}

async function inspectSources(): Promise<boolean> {
  if (form.mode === "manifest") {
    return true;
  }
  if (requiredWarning.value) {
    message.warning(requiredWarning.value);
    return false;
  }

  previewLoading.value = true;
  previewError.value = "";
  try {
    const videosResponse = await listFs(form.videos_dir, "all", false);
    const videoFiles = videosResponse.items.filter((item) => isAllowedFile(item, videoExtensions.value));
    const ignoredFiles = videosResponse.items.filter((item) => !item.is_dir && !isAllowedFile(item, videoExtensions.value));

    let referenceCount = 0;
    let missingReferences: string[] = [];
    let duplicateReferences: string[] = [];

    if (form.mode === "paired-dir") {
      const referenceResponse = await listFs(form.reference_dir, "all", false);
      const referenceFiles = referenceResponse.items.filter((item) => isAllowedFile(item, referenceExtensions.value));
      referenceCount = referenceFiles.length;
      const referenceCounter = countReferencesByStem(referenceResponse.items);
      missingReferences = videoFiles
        .map((item) => fileStem(item.name))
        .filter((stem) => !referenceCounter.has(stem));
      duplicateReferences = [...referenceCounter.entries()]
        .filter(([, count]) => count > 1)
        .map(([stem]) => stem);
    }

    sourcePreview.value = {
      videoCount: videoFiles.length,
      ignoredCount: ignoredFiles.length,
      referenceCount,
      missingReferences,
      duplicateReferences,
    };

    if (videoFiles.length === 0) {
      previewError.value = `视频目录中没有检测到可处理的视频文件，支持格式：${videoExtensions.value.join("、")}`;
      message.warning(previewError.value);
      return false;
    }
    if (missingReferences.length > 0) {
      previewError.value = `有 ${missingReferences.length} 个视频在参考目录中缺少对应的同名文本文件。`;
      message.warning(previewError.value);
      return false;
    }
    if (duplicateReferences.length > 0) {
      previewError.value = `有 ${duplicateReferences.length} 个基名匹配到重复的参考文本文件。`;
      message.warning(previewError.value);
      return false;
    }

    message.success("输入源目录批量扫描检查通过！");
    return true;
  } catch (caught) {
    previewError.value = caught instanceof Error ? caught.message : "输入目录检查失败";
    message.error(previewError.value);
    return false;
  } finally {
    previewLoading.value = false;
  }
}

function buildPayload() {
  return {
    manifest: form.mode === "manifest" ? form.manifest : null,
    videos_dir: form.mode === "manifest" ? null : form.videos_dir,
    reference_dir: form.mode === "paired-dir" ? form.reference_dir : null,
    shared_reference: form.mode === "shared-reference" ? form.shared_reference.trim() : null,
    output_dir: form.mode === "manifest" ? null : form.output_dir,
    content_type: effectiveContentType.value,
    profile: form.profile || null,
    asr_candidate: form.asr_candidate || null,
    secondary_asr_candidate: form.secondary_asr_candidate,
    backend: form.backend || null,
    ocr_backend: form.ocr_backend || null,
    ocr_model: form.ocr_model || null,
    ocr_reasoning_effort: form.ocr_reasoning_effort || null,
    ocr_max_concurrency: form.ocr_max_concurrency,
    ocr_submit_interval_seconds: form.ocr_submit_interval_seconds,
    remote_concurrency: form.remote_concurrency,
    book_name: form.book_name || null,
    chapter: form.chapter || null,
    glossary_file: form.glossary_file || null,
    refine_prompt: effectiveRefinePrompt(),
  };
}

async function submit() {
  if (requiredWarning.value) {
    message.warning(requiredWarning.value);
    return;
  }
  if (form.mode !== "manifest") {
    const validSources = await inspectSources();
    if (!validSources) {
      return;
    }
  }

  submitting.value = true;
  try {
    const response = await submitBatchJob(buildPayload());
    await refreshBatch(response.batch_id);
    startPolling(response.batch_id);
    message.success(`批量流水线任务已提交：${response.batch_id}`);
  } catch (caught) {
    message.error(caught instanceof Error ? caught.message : "提交失败");
  } finally {
    submitting.value = false;
  }
}

function itemStatusType(item: { status?: unknown }): "success" | "error" | "info" | "warning" {
  const status = String(item.status ?? "");
  if (status === "success") {
    return "success";
  }
  if (status === "failed") {
    return "error";
  }
  if (status === "running") {
    return "info";
  }
  return "warning";
}

const statusType = computed<"success" | "error" | "info" | "warning">(() => {
  const status = String(batchState.value?.status ?? "");
  if (status === "success") {
    return "success";
  }
  if (status === "failed") {
    return "error";
  }
  if (status === "running") {
    return "info";
  }
  return "warning";
});

onMounted(() => {
  void loadDefaultRefinePrompt();
});
onBeforeUnmount(stopPolling);
</script>

<template>
  <div class="workbench-page batch-job-view">
    <section class="page-heading">
      <div><h2>批量任务</h2><p>选择清单或上传目录，预检输入后统一提交。每个视频的处理结果独立保留。</p></div>
      <a v-if="batchState" href="#batch-results">查看当前批次 ↓</a>
    </section>

    <n-alert v-if="error" type="error" :title="error" :bordered="false" class="glass-alert" />

    <n-card class="view-card form-panel" :bordered="false">

      <n-form label-placement="top">
        <n-space vertical :size="20">
          <!-- Mode Toggle Selection -->
          <div class="mode-selector-wrapper">
            <span class="mode-label">输入模式</span>
            <n-radio-group v-model:value="form.mode" size="medium" class="mode-radio-group">
              <n-radio-button value="manifest" class="mode-radio-btn">Manifest 配置清单</n-radio-button>
              <n-radio-button value="paired-dir" class="mode-radio-btn">目录自动配对</n-radio-button>
              <n-radio-button value="shared-reference" class="mode-radio-btn">目录共享参考</n-radio-button>
              <n-radio-button value="conversation-dir" class="mode-radio-btn">对谈录屏目录</n-radio-button>
            </n-radio-group>
          </div>

          <n-alert type="info" :title="modeTip" :bordered="false" class="mode-alert" />
          <n-alert v-if="requiredWarning" type="warning" :title="requiredWarning" :bordered="false" class="warn-alert" />

          <n-grid :cols="2" :x-gap="20" :y-gap="4" responsive="screen" item-responsive>
            <!-- Left Fields -->
            <n-grid-item span="2 m:1">
              <div class="form-section">
                <h3 class="form-section-title">输入文件与目录</h3>
                
                <n-form-item v-if="form.mode === 'manifest'" label="Manifest 清单文件" required>
                  <n-space vertical class="w-full">
                    <RemoteFileUpload
                      v-model="form.manifest"
                      kind="manifest"
                      label="Manifest 清单"
                      :accept="manifestAccept"
                      button-text="选择并上传本机 Manifest"
                    />
                    <n-input v-model:value="form.manifest" readonly placeholder="上传后自动生成服务器路径" />
                  </n-space>
                </n-form-item>
                <n-form-item v-if="form.mode === 'manifest'" label="Manifest 默认任务类型" required>
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

                <template v-else>
                  <n-form-item label="视频源目录" required>
                    <n-space vertical class="w-full">
                      <RemoteDirectoryUpload
                        v-model="form.videos_dir"
                        kind="video"
                        label="视频"
                        :extensions="videoExtensions"
                        button-text="选择并上传本机视频目录"
                      />
                      <n-input v-model:value="form.videos_dir" readonly placeholder="上传后自动生成服务器目录" />
                    </n-space>
                  </n-form-item>
                  
                  <n-form-item v-if="form.mode === 'paired-dir'" label="参考源目录" required>
                    <n-space vertical class="w-full">
                      <RemoteDirectoryUpload
                        v-model="form.reference_dir"
                        kind="reference"
                        label="参考源"
                        :extensions="referenceExtensions"
                        button-text="选择并上传本机参考源目录"
                      />
                      <n-input v-model:value="form.reference_dir" readonly placeholder="上传后自动生成服务器目录" />
                    </n-space>
                  </n-form-item>
                  
                  <n-form-item v-if="form.mode === 'shared-reference'" label="共享参考源文件或 URL" required>
                    <n-space vertical class="w-full">
                      <n-input v-model:value="form.shared_reference" placeholder="可粘贴 https:// 网址，或上传本机共享参考源" />
                      <RemoteFileUpload
                        v-model="form.shared_reference"
                        kind="reference"
                        label="共享参考文件"
                        :accept="referenceAccept"
                        button-text="选择并上传本机共享参考"
                      />
                    </n-space>
                  </n-form-item>
                  
                  <n-form-item label="成果获取方式" required>
                    <n-alert type="info" :bordered="false" class="server-output-note">
                      <div class="server-output-note__body">
                        <span>批量处理完成后，到任务列表选择 Markdown 或 TXT 下载全部结果，也可展开后按子任务分别下载。</span>
                        <small>服务器默认保存目录：{{ form.output_dir || "配置加载中..." }}</small>
                      </div>
                    </n-alert>
                  </n-form-item>
                </template>
              </div>
            </n-grid-item>

            <n-grid-item span="2 m:1">
              <div class="form-section">
                <h3 class="form-section-title">常用配置 · 仅本次批量任务</h3>
                <n-grid :cols="2" :x-gap="12" :y-gap="0" responsive="screen" item-responsive>
                  <n-grid-item span="2 m:1">
                    <n-form-item label="语音转文字模型">
                      <AsrCandidateSelector v-model="form.asr_candidate" v-model:secondary-candidate="form.secondary_asr_candidate" :options="asrCandidates" :loading="loading" />
                    </n-form-item>
                  </n-grid-item>
                  <n-grid-item span="2 m:1">
                    <n-form-item label="配置方案">
                      <ProfileSelector v-model="form.profile" :options="profiles" :loading="loading" />
                    </n-form-item>
                  </n-grid-item>
                  <n-grid-item span="2 m:1">
                    <n-form-item label="推理服务">
                      <BackendSelector v-model="form.backend" :options="backends" :loading="loading" />
                    </n-form-item>
                  </n-grid-item>
                </n-grid>
                <details class="advanced-options">
                  <summary>高级参数 · 并发、OCR 与整理指令</summary>
                  <n-grid :cols="2" :x-gap="12" :y-gap="0" responsive="screen" item-responsive>
                    <n-grid-item span="2">
                      <n-form-item label="术语词表">
                        <n-space vertical class="w-full">
                          <RemoteFileUpload v-model="form.glossary_file" kind="glossary" label="术语词表" :accept="glossaryAccept" button-text="选择并上传本机词表" />
                          <n-input v-model:value="form.glossary_file" readonly placeholder="可选，上传后自动生成服务器路径" />
                        </n-space>
                      </n-form-item>
                    </n-grid-item>
                  <n-grid-item span="2 m:1">
                    <n-form-item label="PDF OCR 服务">
                      <n-select
                        v-model:value="form.ocr_backend"
                        :options="ocrBackendOptions"
                        clearable
                        placeholder="使用默认 OCR 服务"
                      />
                    </n-form-item>
                  </n-grid-item>
                  <n-grid-item span="2 m:1">
                    <n-form-item label="批量远程并发度" required>
                      <n-input-number v-model:value="form.remote_concurrency" :min="1" :precision="0" class="w-full" />
                    </n-form-item>
                  </n-grid-item>
                  <n-grid-item span="2 m:1">
                    <n-form-item label="PDF OCR 模型">
                      <n-input v-model:value="form.ocr_model" placeholder="例如 gpt-6-luna" />
                    </n-form-item>
                  </n-grid-item>
                  <n-grid-item span="2 m:1">
                    <n-form-item label="PDF OCR 推理强度">
                      <n-select v-model:value="form.ocr_reasoning_effort" :options="ocrReasoningOptions" />
                    </n-form-item>
                  </n-grid-item>
                  <n-grid-item span="2 m:1">
                    <n-form-item label="页面投递间隔（秒）" required>
                      <n-input-number
                        v-model:value="form.ocr_submit_interval_seconds"
                        :min="0"
                        :step="0.5"
                        class="w-full"
                      />
                    </n-form-item>
                  </n-grid-item>
                  <n-grid-item span="2 m:1">
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
                      <n-input v-model:value="form.book_name" placeholder="可选，用于 ASR 提示词" />
                    </n-form-item>
                  </n-grid-item>
                  <n-grid-item span="2">
                    <n-form-item label="章节名称">
                      <n-input v-model:value="form.chapter" placeholder="可选，用于 ASR 提示词" />
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
                          <span class="prompt-hint">文本框内为当前默认指令，会应用到本次批量任务的每个子任务；不修改时提交会使用项目默认指令。</span>
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
            </n-grid-item>
          </n-grid>

          <!-- Input Dir Scanning and Preview Statistics -->
          <div v-if="form.mode !== 'manifest'" class="batch-preview">
            <n-space align="center" justify="space-between" class="w-full" wrap>
              <n-button secondary type="primary" :loading="previewLoading" @click="inspectSources" class="inspect-btn">
                <template #icon>
                  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="width:14px;height:14px"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
                </template>
                预检输入目录
              </n-button>
              <span class="batch-preview__hint">
                允许视频：<span class="ext-tag">{{ videoExtensions.join("、") || "-" }}</span>
                <template v-if="form.mode !== 'conversation-dir'">
                  ；参考文本：<span class="ext-tag">{{ referenceExtensions.join("、") || "-" }}</span>
                </template>
              </span>
            </n-space>

            <n-alert v-if="previewError" class="batch-preview__alert" type="error" :title="previewError" :bordered="false" />
            
            <div v-if="sourcePreview" class="preview-stats-grid">
              <div class="stat-bubble is-success">
                <span class="stat-num">{{ sourcePreview.videoCount }}</span>
                <span class="stat-label">待处理视频</span>
              </div>
              <div v-if="form.mode === 'paired-dir'" class="stat-bubble is-info">
                <span class="stat-num">{{ sourcePreview.referenceCount }}</span>
                <span class="stat-label">检测参考文件</span>
              </div>
              <div class="stat-bubble is-muted">
                <span class="stat-num">{{ sourcePreview.ignoredCount }}</span>
                <span class="stat-label">其他忽略文件</span>
              </div>
              
              <div v-if="sourcePreview.missingReferences.length || sourcePreview.duplicateReferences.length" class="alert-stats-box">
                <div v-if="sourcePreview.missingReferences.length" class="err-stat">
                  ⚠️ 缺少参考：{{ sourcePreview.missingReferences.join("、") }}
                </div>
                <div v-if="sourcePreview.duplicateReferences.length" class="err-stat">
                  ⚠️ 重复匹配：{{ sourcePreview.duplicateReferences.join("、") }}
                </div>
              </div>
            </div>
          </div>

          <!-- Bottom Actions -->
          <n-flex justify="end" class="form-action-area">
            <n-button
              type="primary"
              size="large"
              :loading="submitting"
              :disabled="Boolean(requiredWarning) || previewHasBlockingIssue"
              @click="submit"
              class="submit-btn"
            >
              <template #icon>
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:18px;height:18px"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
              </template>
              提交批量任务
            </n-button>
          </n-flex>
        </n-space>
      </n-form>
    </n-card>

    <!-- Batch Job Running Status Details -->
    <n-card id="batch-results" v-slot:default v-if="batchState" title="当前批次" class="view-card batch-results">
      <n-space vertical :size="16">
        <div class="batch-summary-glow">
          <div class="summary-top">
            <strong class="batch-id-text">{{ batchState.id }}</strong>
            <n-tag :type="statusType" round :bordered="false" class="summary-status-badge">
              {{ batchState.status }}
            </n-tag>
          </div>
          
          <div class="summary-counters">
            <div class="counter-item">
              <span class="c-label">当前流水线阶段</span>
              <span class="c-val text-primary font-semibold">{{ batchState.current_stage || "准备中..." }}</span>
            </div>
            <div class="counter-item">
              <span class="c-label">总任务数</span>
              <span class="c-val">{{ batchState.total ?? "-" }}</span>
            </div>
            <div class="counter-item">
              <span class="c-label">执行成功</span>
              <span class="c-val text-success">{{ batchState.success ?? "-" }}</span>
            </div>
            <div class="counter-item">
              <span class="c-label">执行失败</span>
              <span class="c-val text-error">{{ batchState.failed ?? "-" }}</span>
            </div>
          </div>
          
          <div v-if="batchState.output_path" class="summary-path-box">
            <span class="p-title">汇总输出路径：</span>
            <span class="p-content">{{ batchState.output_path }}</span>
          </div>
        </div>

        <n-alert v-if="batchState.error_message" type="error" :title="batchState.error_message" :bordered="false" class="glass-alert" />
        
        <!-- Batch Sub-jobs List -->
        <div v-if="batchItems.length" class="sub-jobs-section">
          <h3 class="sub-jobs-title">子任务详情</h3>
          <div class="batch-items">
            <div
              v-for="(item, index) in batchItems"
              :key="String(item.job_id ?? item.video_source ?? index)"
              class="batch-item-modern"
            >
              <div class="batch-item__head">
                <div class="item-name-box">
                  <span class="sub-index">#{{ index + 1 }}</span>
                  <strong class="sub-id">{{ String(item.job_id || `未分配ID`) }}</strong>
                </div>
                <n-tag :type="itemStatusType(item)" size="small" :bordered="false" round class="sub-badge">
                  {{ String(item.status ?? "-") }}
                </n-tag>
              </div>
              <div class="batch-item-grid-modern">
                <div class="grid-cell"><span class="cell-label">配置模式:</span> {{ String(item.mode ?? "-") }}</div>
                <div class="grid-cell"><span class="cell-label">任务类型:</span> {{ String(item.content_type ?? "-") }}</div>
                <div class="grid-cell"><span class="cell-label">失败阶段:</span> <span :class="{'text-error font-semibold': item.failed_stage}">{{ String(item.failed_stage || "-") }}</span></div>
                <div class="grid-cell span-all"><span class="cell-label">视频路径:</span> <span class="mono-path">{{ String(item.video_source ?? "-") || "-" }}</span></div>
                <div class="grid-cell span-all"><span class="cell-label">参考源:</span> <span class="mono-path">{{ String(item.reference_source ?? "") || "无" }}</span></div>
                <div class="grid-cell span-all"><span class="cell-label">输出路径:</span> <span class="mono-path is-out">{{ String(item.copied_output_path ?? "-") || "-" }}</span></div>
                <div v-if="String(item.error_message ?? '')" class="grid-cell span-all err-cell">
                  <span class="cell-label">错误详情:</span> {{ String(item.error_message) }}
                </div>
              </div>
            </div>
          </div>
        </div>
      </n-space>
    </n-card>
  </div>
</template>

<style scoped>
.mode-selector-wrapper { display: grid; gap: 12px; }
.mode-label { font-weight: 600; }
.mode-radio-group { display: flex; flex-wrap: wrap; gap: 8px; }
.mode-radio-btn { border-radius: 8px; }
.batch-results { padding-top: 24px; border-top: 1px solid var(--border-subtle); }
.batch-preview__hint { font-size: 12px; color: var(--text-muted); overflow-wrap: anywhere; }
.ext-tag, .sub-index { color: var(--text-secondary); font-size: 12px; }
.preview-stats-grid { display: flex; gap: 24px; flex-wrap: wrap; }
.stat-bubble { display: grid; gap: 4px; }
.stat-num { font-size: 24px; font-weight: 600; font-variant-numeric: tabular-nums; }
.stat-label { font-size: 12px; color: var(--text-muted); }
.alert-stats-box { width: 100%; padding: 12px; border-radius: 8px; background: var(--color-warning-bg); color: var(--color-warning); overflow-wrap: anywhere; }
.batch-summary-glow { display: grid; gap: 16px; }
.summary-top { display: flex; flex-wrap: wrap; gap: 12px; justify-content: space-between; align-items: center; }
.batch-id-text, .sub-id { font-family: ui-monospace, monospace; font-size: 13px; overflow-wrap: anywhere; }
.summary-counters { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; padding: 16px 0; border-block: 1px solid var(--border-subtle); }
.counter-item { display: grid; gap: 4px; }
.c-label { font-size: 12px; color: var(--text-muted); }
.c-val { font-size: 16px; font-weight: 600; overflow-wrap: anywhere; }
.summary-path-box { font-size: 12px; overflow-wrap: anywhere; }
.p-title, .cell-label { color: var(--text-muted); margin-right: 8px; }
.p-content, .mono-path { font-family: ui-monospace, monospace; overflow-wrap: anywhere; }
.sub-jobs-title { margin: 16px 0 0; font-size: 14px; }
.batch-item-modern { display: grid; gap: 12px; padding: 16px 0; border-top: 1px solid var(--border-subtle); }
.item-name-box { display: flex; gap: 8px; align-items: baseline; min-width: 0; }
.batch-item-grid-modern { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(240px, 100%), 1fr)); gap: 8px 16px; font-size: 12px; color: var(--text-secondary); }
.span-all { grid-column: 1 / -1; }
.err-cell { padding: 8px 12px; background: var(--color-error-bg); color: var(--color-error); overflow-wrap: anywhere; }
@media (max-width: 600px) { .summary-counters { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
