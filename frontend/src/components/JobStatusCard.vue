<script setup lang="ts">
import {
  NAlert,
  NButton,
  NCard,
  NCollapseTransition,
  NDescriptions,
  NDescriptionsItem,
  NDropdown,
  NFlex,
  NProgress,
  NSelect,
  NTag,
  useDialog,
  useMessage,
} from "naive-ui";
import { computed, ref, watch } from "vue";
import {
  batchItemResultUrl,
  batchResultUrl,
  deleteBatch,
  deleteJob,
  deleteStageRun,
  jobResultUrl,
  rerunBatchItem,
  rerunJob,
  retryStageRun,
  type BatchItemState,
  type JobInputSummary,
  type OCRProgressItem,
  type ResultDownloadFormat,
} from "../api/client";
import JobArtifactsViewer from "./JobArtifactsViewer.vue";

interface IdentityEntry {
  label: string;
  value: string;
  fullValue: string;
}

const props = defineProps<{
  title?: string;
  state: Record<string, unknown>;
  defaultExpanded?: boolean;
}>();

const emit = defineEmits<{
  (e: "deleted"): void;
  (e: "rerun", jobId: string): void;
}>();

const message = useMessage();
const dialog = useDialog();

const isDeleting = ref(false);
const isRerunning = ref(false);
const isRetryingStageRun = ref(false);
const rerunStage = ref("refine");
const batchItemRerunStages = ref<Record<string, string>>({});
const rerunningBatchItemIds = ref<string[]>([]);
const expandedBatchArtifactJobId = ref("");
const expanded = ref(props.defaultExpanded ?? false);

const kind = computed(() => String(props.state.kind ?? ""));
const status = computed(() => String(props.state.status ?? ""));
const stateId = computed(() => String(props.state.id ?? ""));
const inputSummary = computed<JobInputSummary>(() => {
  const rawSummary = props.state.input_summary;
  if (!rawSummary || typeof rawSummary !== "object" || Array.isArray(rawSummary)) {
    return {};
  }
  return rawSummary as JobInputSummary;
});
const batchItems = computed<BatchItemState[]>(() => {
  return Array.isArray(props.state.items) ? (props.state.items as BatchItemState[]) : [];
});
const hasBatchItems = computed(() => batchItems.value.length > 0);
const hasBatchOutputs = computed(() => {
  return batchItems.value.some((item) => item.status === "success" && Boolean(item.copied_output_path));
});
const ocrItems = computed<OCRProgressItem[]>(() => {
  return Array.isArray(props.state.ocr_items) ? (props.state.ocr_items as OCRProgressItem[]) : [];
});
const hasOcrProgress = computed(() => ocrItems.value.length > 0 || props.state.pages_total !== undefined);

watch(
  () => stateId.value,
  () => {
    expanded.value = props.defaultExpanded ?? false;
  },
);

const canDelete = computed(() => {
  return status.value !== "running" && status.value !== "pending";
});

const canRerun = computed(() => {
  return kind.value === "job" && status.value !== "running" && status.value !== "pending";
});
const canRetryStageRun = computed(() => {
  return (
    kind.value === "stage-run"
    && String(props.state.current_stage ?? "") === "prepare-reference"
    && (status.value === "partial" || status.value === "failed")
  );
});

const canViewArtifacts = computed(() => kind.value === "job");
const canDownloadSingleResult = computed(() => kind.value === "job" && status.value === "success" && Boolean(props.state.output_path));
const canDownloadBatchResult = computed(() => kind.value === "batch" && hasBatchOutputs.value);
const resultDownloadOptions: { label: string; key: ResultDownloadFormat }[] = [
  { label: "下载 Markdown", key: "markdown" },
  { label: "下载 TXT", key: "txt" },
];

function openDownload(url: string) {
  window.location.href = url;
}

function toResultDownloadFormat(key: string | number): ResultDownloadFormat {
  return key === "txt" ? "txt" : "markdown";
}

function handleSingleResultDownload(format: ResultDownloadFormat = "markdown") {
  if (!stateId.value) {
    message.error("缺少任务 ID，无法下载结果。");
    return;
  }
  openDownload(jobResultUrl(stateId.value, format));
}

function handleBatchResultDownload(format: ResultDownloadFormat = "markdown") {
  if (!stateId.value) {
    message.error("缺少批量任务 ID，无法下载结果。");
    return;
  }
  openDownload(batchResultUrl(stateId.value, format));
}

function handleBatchItemResultDownload(item: BatchItemState, format: ResultDownloadFormat = "markdown") {
  if (!stateId.value || !item.job_id) {
    message.error("缺少批量子任务 ID，无法下载结果。");
    return;
  }
  openDownload(batchItemResultUrl(stateId.value, item.job_id, format));
}

function textValue(value: unknown) {
  return String(value ?? "").trim();
}

function firstNonEmpty(...values: unknown[]) {
  for (const value of values) {
    const text = textValue(value);
    if (text) {
      return text;
    }
  }
  return "";
}

function displayFileName(path?: unknown) {
  const normalized = textValue(path);
  if (!normalized) {
    return "-";
  }
  return normalized.split(/[\\/]/).pop() || normalized;
}

function displaySourceName(source?: unknown) {
  const normalized = textValue(source);
  if (!normalized) {
    return "";
  }
  if (/^https?:\/\//i.test(normalized)) {
    try {
      const url = new URL(normalized);
      const lastSegment = url.pathname.split("/").filter(Boolean).pop();
      return lastSegment || url.hostname;
    } catch {
      return displayFileName(normalized);
    }
  }
  return displayFileName(normalized);
}

function displayDateTime(value?: unknown) {
  const rawValue = textValue(value);
  if (!rawValue) {
    return "-";
  }
  const parsed = new Date(rawValue);
  if (Number.isNaN(parsed.getTime())) {
    return rawValue;
  }
  return parsed.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function summaryField(key: keyof JobInputSummary) {
  return textValue(inputSummary.value[key]);
}

function itemStatusType(rawStatus?: string): "success" | "error" | "info" | "warning" {
  if (rawStatus === "success") {
    return "success";
  }
  if (rawStatus === "failed") {
    return "error";
  }
  if (rawStatus === "running") {
    return "info";
  }
  return "warning";
}

const videoSource = computed(() => firstNonEmpty(summaryField("video_source")));
const referenceSource = computed(() => firstNonEmpty(summaryField("reference_source")));
const contentType = computed(() => firstNonEmpty(summaryField("content_type")));
const outputDir = computed(() => firstNonEmpty(summaryField("output_dir")));
const manifestSource = computed(() => firstNonEmpty(summaryField("manifest")));
const videosDir = computed(() => firstNonEmpty(summaryField("videos_dir")));
const referenceDir = computed(() => firstNonEmpty(summaryField("reference_dir")));
const sharedReference = computed(() => firstNonEmpty(summaryField("shared_reference")));
const bookName = computed(() => firstNonEmpty(summaryField("book_name")));
const chapter = computed(() => firstNonEmpty(summaryField("chapter")));
const glossaryFile = computed(() => firstNonEmpty(summaryField("glossary_file")));
const createdAtText = computed(() => displayDateTime(props.state.created_at));
const updatedAtText = computed(() => displayDateTime(props.state.updated_at));

const kindLabel = computed(() => {
  if (kind.value === "job") {
    return "单任务";
  }
  if (kind.value === "batch") {
    return "批量任务";
  }
  if (kind.value === "stage-run") {
    return "单阶段";
  }
  return "任务";
});

const defaultCardTitle = computed(() => {
  if (kind.value === "job") {
    const sourceName = displaySourceName(videoSource.value) || displaySourceName(props.state.output_path);
    return sourceName ? `视频：${sourceName}` : `单任务 ${stateId.value || "未命名"}`;
  }
  if (kind.value === "batch") {
    const directoryName = displaySourceName(videosDir.value);
    if (directoryName) {
      return `批量目录：${directoryName}`;
    }
    const manifestName = displaySourceName(manifestSource.value);
    if (manifestName) {
      return `批量清单：${manifestName}`;
    }
    const firstVideoName = displaySourceName(batchItems.value[0]?.video_source);
    if (firstVideoName) {
      return `批量任务：${firstVideoName} 等 ${batchItems.value.length} 个视频`;
    }
    return `批量任务 ${stateId.value || "未命名"}`;
  }
  if (kind.value === "stage-run") {
    const stageName = textValue(props.state.current_stage);
    return stageName ? `单阶段：${stageName}` : `单阶段 ${stateId.value || "未命名"}`;
  }
  return stateId.value ? `任务 ${stateId.value}` : "任务状态";
});

const cardTitle = computed(() => textValue(props.title) || defaultCardTitle.value);

const identitySubtitle = computed(() => {
  const parts = [kindLabel.value];
  if (stateId.value) {
    parts.push(`ID ${stateId.value}`);
  }
  if (createdAtText.value !== "-") {
    parts.push(`创建 ${createdAtText.value}`);
  }
  return parts.join(" · ");
});

function makeIdentityEntry(label: string, rawValue: unknown, displayValue?: string): IdentityEntry | null {
  const fullValue = textValue(rawValue);
  if (!fullValue) {
    return null;
  }
  return {
    label,
    value: displayValue || displaySourceName(fullValue) || fullValue,
    fullValue,
  };
}

const identityEntries = computed<IdentityEntry[]>(() => {
  const entries: Array<IdentityEntry | null> = [];
  if (kind.value === "job") {
    entries.push(makeIdentityEntry("内容类型", contentType.value, contentType.value));
    entries.push(makeIdentityEntry("原始视频", videoSource.value));
    entries.push(
      contentType.value === "conversation" && !referenceSource.value
        ? { label: "参考源", value: "无", fullValue: "无" }
        : makeIdentityEntry("参考源", referenceSource.value)
    );
    entries.push(makeIdentityEntry("输出目录", outputDir.value, outputDir.value));
  } else if (kind.value === "batch") {
    entries.push(makeIdentityEntry("内容类型", contentType.value, contentType.value));
    entries.push(makeIdentityEntry("批量目录", videosDir.value));
    entries.push(makeIdentityEntry("批量清单", manifestSource.value));
    entries.push(makeIdentityEntry("参考目录", referenceDir.value));
    entries.push(makeIdentityEntry("共享参考", sharedReference.value));
    entries.push(makeIdentityEntry("输出目录", outputDir.value, outputDir.value));
    if (!videosDir.value && batchItems.value[0]?.video_source) {
      entries.push(makeIdentityEntry("首个视频", batchItems.value[0].video_source));
    }
  } else {
    entries.push(makeIdentityEntry("产物位置", props.state.output_path, textValue(props.state.output_path)));
  }
  entries.push(makeIdentityEntry("书名", bookName.value, bookName.value));
  entries.push(makeIdentityEntry("章节", chapter.value, chapter.value));
  entries.push(makeIdentityEntry("术语表", glossaryFile.value));
  return entries.filter((entry): entry is IdentityEntry => Boolean(entry));
});

const hasInputSummary = computed(() => identityEntries.value.length > 0);

function batchItemTitle(item: BatchItemState, index: number) {
  return displaySourceName(item.video_source) || item.job_id || `子任务 ${index + 1}`;
}

const BATCH_ITEM_STAGE_LABELS: Record<string, string> = {
  pending: "等待开始",
  "extract-audio": "音频提取",
  transcribe: "语音转写",
  "prepare-reference": "准备参考",
  refine: "校对润色",
  "export-markdown": "导出文档",
  done: "已完成",
};

function batchItemPipelineStages(item: BatchItemState): string[] {
  return item.content_type === "conversation"
    ? ["extract-audio", "transcribe", "refine", "export-markdown"]
    : ["extract-audio", "transcribe", "prepare-reference", "refine", "export-markdown"];
}

function batchItemStageLabel(stageName: string): string {
  return BATCH_ITEM_STAGE_LABELS[stageName] ?? stageName;
}

function getBatchItemStageState(
  item: BatchItemState,
  stageName: string,
): "completed" | "active" | "failed" | "pending" {
  const completedStages = item.completed_stages ?? [];
  if (item.status === "success" || completedStages.includes(stageName)) {
    return "completed";
  }

  const currentStage = textValue(item.current_stage);
  const failedStage = textValue(item.failed_stage);
  if (failedStage === stageName || (item.status === "failed" && currentStage === stageName)) {
    return "failed";
  }
  if (currentStage === stageName && item.status !== "pending") {
    return "active";
  }

  // 兼容旧任务记录：旧记录尚未保存 completed_stages 时，已越过的阶段仍可正确显示完成。
  const stages = batchItemPipelineStages(item);
  const currentStageIndex = stages.indexOf(currentStage);
  if (currentStageIndex > stages.indexOf(stageName)) {
    return "completed";
  }
  return "pending";
}

function batchItemStageProgress(item: BatchItemState): number {
  const stages = batchItemPipelineStages(item);
  if (item.status === "success") {
    return 100;
  }
  const completedCount = stages.filter((stageName) => {
    return getBatchItemStageState(item, stageName) === "completed";
  }).length;
  return Math.round((completedCount / stages.length) * 100);
}

function batchItemProgressStatus(item: BatchItemState): "default" | "success" | "error" | "warning" {
  if (item.status === "success") {
    return "success";
  }
  if (item.status === "failed") {
    return "error";
  }
  if (item.status === "partial") {
    return "warning";
  }
  return "default";
}

function batchItemCurrentStageLabel(item: BatchItemState): string {
  return batchItemStageLabel(textValue(item.current_stage) || textValue(item.failed_stage) || "pending");
}

function toggleBatchItemArtifacts(item: BatchItemState) {
  const jobId = batchItemKey(item);
  if (!jobId) {
    return;
  }
  expandedBatchArtifactJobId.value = expandedBatchArtifactJobId.value === jobId ? "" : jobId;
}

function handleDelete() {
  dialog.warning({
    title: "确认删除",
    content: "确定要物理删除此条历史记录吗？此操作不可逆，将永久删除关联的数据状态与缓存文件！",
    positiveText: "确定删除",
    negativeText: "不删除",
    onPositiveClick: async () => {
      isDeleting.value = true;
      try {
        const id = String(props.state.id ?? "");
        const kind = String(props.state.kind ?? "");
        if (kind === "job") {
          await deleteJob(id);
        } else if (kind === "batch") {
          await deleteBatch(id);
        } else if (kind === "stage-run") {
          await deleteStageRun(id);
        } else {
          throw new Error(`未知的任务类型: ${kind}`);
        }
        message.success("删除成功");
        emit("deleted");
      } catch (caught) {
        message.error(caught instanceof Error ? caught.message : "删除失败");
      } finally {
        isDeleting.value = false;
      }
    }
  });
}

const statusType = computed<"success" | "error" | "info" | "warning">(() => {
  if (status.value === "success") {
    return "success";
  }
  if (status.value === "failed") {
    return "error";
  }
  if (status.value === "running") {
    return "info";
  }
  return "warning";
});

const isRunning = computed(() => status.value === "running");

const MAIN_PIPELINE_STAGES = [
  { key: "extract-audio", label: "音频提取", index: 1 },
  { key: "transcribe", label: "语音转写", index: 2 },
  { key: "prepare-reference", label: "准备参考", index: 3 },
  { key: "refine", label: "校对润色", index: 4 },
  { key: "export-markdown", label: "导出文档", index: 5 },
];

const DEBUG_PIPELINE_STAGES = [
  { key: "extract-audio", label: "音频提取", index: 1 },
  { key: "transcribe", label: "语音转写", index: 2 },
  { key: "prepare-reference", label: "准备参考", index: 3 },
  { key: "align", label: "文本对齐", index: 4 },
  { key: "classify", label: "段落分类", index: 5 },
  { key: "refine", label: "校对润色", index: 6 },
  { key: "export-markdown", label: "导出文档", index: 7 },
];

const pipelineStages = computed(() => {
  return kind.value === "stage-run" ? DEBUG_PIPELINE_STAGES : MAIN_PIPELINE_STAGES;
});

const rerunStageOptions = computed(() => MAIN_PIPELINE_STAGES.map((stage) => ({
  label: `${stage.label} (${stage.key})`,
  value: stage.key,
})));
const rerunStageKeys = computed(() => new Set(MAIN_PIPELINE_STAGES.map((stage) => stage.key)));

function batchItemKey(item: BatchItemState) {
  return textValue(item.job_id);
}

function defaultBatchItemRerunStage(item: BatchItemState) {
  const failedStage = textValue(item.failed_stage);
  if (rerunStageKeys.value.has(failedStage)) {
    return failedStage;
  }
  return "refine";
}

function batchItemRerunStageValue(item: BatchItemState) {
  const key = batchItemKey(item);
  return batchItemRerunStages.value[key] || defaultBatchItemRerunStage(item);
}

function setBatchItemRerunStage(item: BatchItemState, stage: string) {
  const key = batchItemKey(item);
  if (!key) {
    return;
  }
  batchItemRerunStages.value = {
    ...batchItemRerunStages.value,
    [key]: stage,
  };
}

function canRerunBatchItem(item: BatchItemState) {
  const itemStatus = textValue(item.status);
  return (
    kind.value === "batch" &&
    status.value !== "running" &&
    status.value !== "pending" &&
    Boolean(batchItemKey(item)) &&
    (itemStatus === "success" || itemStatus === "partial" || itemStatus === "failed")
  );
}

function isBatchItemRerunning(item: BatchItemState) {
  const key = batchItemKey(item);
  return Boolean(key) && rerunningBatchItemIds.value.includes(key);
}

watch(
  () => String(props.state.current_stage ?? ""),
  (currentStage) => {
    if (rerunStageKeys.value.has(currentStage)) {
      rerunStage.value = currentStage;
    }
  },
  { immediate: true },
);

async function handleRerun() {
  const id = String(props.state.id ?? "");
  if (!id) {
    message.error("缺少任务 ID，无法重跑。");
    return;
  }
  isRerunning.value = true;
  try {
    await rerunJob(id, { start_stage: rerunStage.value, backend: "codex_api" });
    message.success(`已从 ${rerunStage.value} 重新启动任务`);
    emit("rerun", id);
  } catch (caught) {
    message.error(caught instanceof Error ? caught.message : "重跑任务失败");
  } finally {
    isRerunning.value = false;
  }
}

async function handleStageRunRetry() {
  const id = stateId.value;
  if (!id) {
    message.error("缺少阶段任务 ID，无法补页。");
    return;
  }
  isRetryingStageRun.value = true;
  try {
    await retryStageRun(id);
    message.success("已在原阶段任务中重试缺失页。");
    emit("rerun", id);
  } catch (caught) {
    message.error(caught instanceof Error ? caught.message : "重试缺失页失败");
  } finally {
    isRetryingStageRun.value = false;
  }
}

function pageErrorEntries(item: OCRProgressItem): Array<[string, string]> {
  return Object.entries(item.page_errors ?? {}).sort(([left], [right]) => Number(left) - Number(right));
}

async function handleBatchItemRerun(item: BatchItemState) {
  const batchId = stateId.value;
  const itemJobId = batchItemKey(item);
  if (!batchId || !itemJobId) {
    message.error("缺少批量任务或子任务 ID，无法重跑。");
    return;
  }

  const startStage = batchItemRerunStageValue(item);
  rerunningBatchItemIds.value = [...rerunningBatchItemIds.value, itemJobId];
  try {
    await rerunBatchItem(batchId, itemJobId, { start_stage: startStage, backend: "codex_api" });
    message.success(`已从 ${startStage} 重跑子任务`);
    emit("rerun", batchId);
  } catch (caught) {
    message.error(caught instanceof Error ? caught.message : "重跑子任务失败");
  } finally {
    rerunningBatchItemIds.value = rerunningBatchItemIds.value.filter((id) => id !== itemJobId);
  }
}

// 根据整体状态推导每个阶段的展示状态，避免展开详情时只看到原始状态码。
function getStepState(stageKey: string): "completed" | "active" | "failed" | "pending" {
  const currentStage = String(props.state.current_stage ?? props.state.failed_stage ?? "");
  
  const stageIndex = pipelineStages.value.findIndex((s) => s.key === stageKey);
  const currentStageIndex = pipelineStages.value.findIndex((s) => s.key === currentStage);
  
  if (status.value === "success") {
    return "completed";
  }
  
  if (status.value === "failed") {
    if (currentStage === stageKey) {
      return "failed";
    }
    if (currentStageIndex !== -1 && stageIndex < currentStageIndex) {
      return "completed";
    }
    return "pending";
  }
  
  if (status.value === "running") {
    if (currentStage === stageKey) {
      return "active";
    }
    if (currentStageIndex !== -1) {
      return stageIndex < currentStageIndex ? "completed" : "pending";
    }
    return stageIndex === 0 ? "active" : "pending";
  }
  if (status.value === "partial") {
    if (currentStage === stageKey) {
      return "active";
    }
    if (currentStageIndex !== -1 && stageIndex < currentStageIndex) {
      return "completed";
    }
    return "pending";
  }
  
  return "pending";
}
</script>

<template>
  <n-card :title="cardTitle" class="status-card" size="medium">
    <template #header-extra>
      <n-flex align="center" :size="10" wrap>
        <span
          class="status-pulse"
          :class="{
            'is-running': isRunning,
            'is-success': state.status === 'success',
            'is-error': state.status === 'failed'
          }"
        ></span>
        <n-tag :type="statusType" round size="medium" :bordered="false" class="status-badge">
          {{ String(state.status ?? "-") }}
        </n-tag>
        <n-dropdown
          v-if="canDownloadSingleResult"
          trigger="click"
          :options="resultDownloadOptions"
          @select="(key) => handleSingleResultDownload(toResultDownloadFormat(key))"
        >
          <n-button
            type="success"
            size="small"
            secondary
            @click.stop
          >
            下载结果
          </n-button>
        </n-dropdown>
        <n-dropdown
          v-if="canDownloadBatchResult"
          trigger="click"
          :options="resultDownloadOptions"
          @select="(key) => handleBatchResultDownload(toResultDownloadFormat(key))"
        >
          <n-button
            type="success"
            size="small"
            secondary
            @click.stop
          >
            下载全部结果
          </n-button>
        </n-dropdown>
        <n-button size="small" secondary :aria-expanded="expanded" @click.stop="expanded = !expanded">
          {{ expanded ? "收起详情" : "展开详情" }}
        </n-button>
        <n-button
          v-if="canDelete"
          type="error"
          size="small"
          quaternary
          :loading="isDeleting"
          @click.stop="handleDelete"
          class="delete-btn"
        >
          删除
        </n-button>
      </n-flex>
    </template>

    <n-flex vertical :size="16">
      <div class="identity-panel">
        <div class="identity-subtitle">{{ identitySubtitle }}</div>
        <div v-if="identityEntries.length" class="identity-list">
          <div
            v-for="entry in identityEntries"
            :key="`${entry.label}-${entry.fullValue}`"
            class="identity-item"
            :title="entry.fullValue"
          >
            <span>{{ entry.label }}</span>
            <strong>{{ entry.value }}</strong>
          </div>
        </div>
      </div>

      <div class="status-summary">
        <div class="summary-cell">
          <span class="summary-label">当前阶段</span>
          <strong>{{ String(state.current_stage ?? "-") || "-" }}</strong>
        </div>
        <div class="summary-cell">
          <span class="summary-label">任务类型</span>
          <strong>{{ String(state.kind ?? "-") || "-" }}</strong>
        </div>
        <div class="summary-cell">
          <span class="summary-label">更新时间</span>
          <strong>{{ updatedAtText }}</strong>
        </div>
        <div v-if="state.total !== undefined" class="summary-cell is-wide">
          <span class="summary-label">批量进度</span>
          <n-flex :size="8" align="center" wrap>
            <n-tag size="small" type="info" :bordered="false">总数 {{ String(state.total ?? 0) }}</n-tag>
            <n-tag size="small" type="success" :bordered="false">成功 {{ String(state.success ?? 0) }}</n-tag>
            <n-tag v-if="state.partial !== undefined" size="small" type="warning" :bordered="false">
              待补页 {{ String(state.partial ?? 0) }}
            </n-tag>
            <n-tag size="small" type="error" :bordered="false">失败 {{ String(state.failed ?? 0) }}</n-tag>
          </n-flex>
        </div>
      </div>

      <n-alert
        v-if="canDownloadSingleResult"
        type="success"
        :bordered="false"
        class="result-hint"
      >
        结果已保存在服务器，局域网使用者可直接点击“下载结果”选择 Markdown 或 TXT。
      </n-alert>
      <n-alert
        v-else-if="canDownloadBatchResult"
        type="success"
        :bordered="false"
        class="result-hint"
      >
        批量任务已产生可下载结果，可下载全部成功产物，也可展开后按子任务分别选择 Markdown 或 TXT。
      </n-alert>

      <n-collapse-transition :show="expanded">
        <n-flex vertical :size="20" class="status-detail">
          <div v-if="kind !== 'batch'" class="pipeline-section">
            <h4 class="pipeline-title">处理进度</h4>
            <div class="pipeline-flow">
              <div
                v-for="stage in pipelineStages"
                :key="stage.key"
                class="pipeline-step"
                :class="{
                  'is-completed': getStepState(stage.key) === 'completed',
                  'is-active': getStepState(stage.key) === 'active',
                  'is-failed': getStepState(stage.key) === 'failed'
                }"
              >
                <div class="pipeline-step__circle">
                  <span v-if="getStepState(stage.key) === 'completed'">✓</span>
                  <span v-else-if="getStepState(stage.key) === 'failed'">✕</span>
                  <span v-else>{{ stage.index }}</span>
                </div>
                <span class="pipeline-step__label">{{ stage.label }}</span>
              </div>
            </div>
          </div>

          <div v-if="canRerun" class="rerun-section">
            <div>
              <h4 class="rerun-title">从指定阶段重新运行</h4>
              <p class="rerun-copy">复用当前任务的输入、中间目录和生成配置，只从所选阶段继续执行后续流水线。</p>
            </div>
            <n-flex align="center" :size="10" wrap>
              <n-select
                v-model:value="rerunStage"
                :options="rerunStageOptions"
                class="rerun-select"
              />
              <n-button
                type="primary"
                secondary
                :loading="isRerunning"
                @click="handleRerun"
              >
                {{ status === "partial" && rerunStage === "prepare-reference" ? "重试缺失页并继续" : "从该阶段重跑" }}
              </n-button>
            </n-flex>
          </div>

          <div v-if="canRetryStageRun" class="rerun-section">
            <div>
              <h4 class="rerun-title">重试准备参考缺失页</h4>
              <p class="rerun-copy">沿用当前阶段任务的输入、OCR 参数和页检查点，只请求尚未成功的页面。</p>
            </div>
            <n-button
              type="warning"
              secondary
              :loading="isRetryingStageRun"
              @click="handleStageRunRetry"
            >
              重试缺失页
            </n-button>
          </div>

          <div v-if="hasOcrProgress" class="ocr-progress-section">
            <div class="batch-items-section__head">
              <h4 class="batch-items-title">PDF OCR 页进度</h4>
              <span class="batch-items-count">
                {{ String(state.pages_completed ?? 0) }}/{{ String(state.pages_total ?? 0) }} 页完成
              </span>
            </div>
            <div class="batch-items-list">
              <div v-for="item in ocrItems" :key="item.source_file" class="batch-item-row">
                <div class="batch-item-main">
                  <strong>{{ item.source_file }}</strong>
                  <span>已完成 {{ item.completed_pages }}/{{ item.page_count }} 页</span>
                  <span v-if="item.failed_page_numbers.length" class="batch-item-error">
                    待补页：{{ item.failed_page_numbers.join("、") }}
                  </span>
                  <p
                    v-for="[pageNumber, pageError] in pageErrorEntries(item)"
                    :key="`${item.source_file}-${pageNumber}`"
                    class="batch-item-error"
                  >
                    第 {{ pageNumber }} 页：{{ pageError }}
                  </p>
                </div>
                <n-tag :type="item.resumable ? 'warning' : 'success'" :bordered="false" size="small">
                  {{ item.resumable ? "待补页" : "完成" }}
                </n-tag>
              </div>
            </div>
          </div>

          <JobArtifactsViewer
            v-if="canViewArtifacts"
            :job-id="String(state.id ?? '')"
          />

          <div v-if="hasBatchItems" class="batch-items-section">
            <div class="batch-items-section__head">
              <h4 class="batch-items-title">批量子任务列表</h4>
              <span class="batch-items-count">{{ batchItems.length }} 个子任务</span>
            </div>
            <div class="batch-items-list">
              <div
                v-for="(item, index) in batchItems"
                :key="item.job_id || index"
                class="batch-item-row batch-child-row"
              >
                <div class="batch-child-row__head">
                  <div class="batch-item-main">
                    <strong>{{ batchItemTitle(item, index) }}</strong>
                    <span v-if="item.job_id" class="batch-item-id">任务 ID：{{ item.job_id }}</span>
                    <span v-if="item.content_type === 'conversation' && !item.reference_source">参考源：无</span>
                    <span v-else-if="item.reference_source">参考源：{{ displaySourceName(item.reference_source) }}</span>
                    <span v-if="item.output_dir">输出目录：{{ item.output_dir }}</span>
                    <span v-if="item.failed_stage" class="batch-item-muted">失败阶段：{{ batchItemStageLabel(item.failed_stage) }}</span>
                    <span v-if="item.pages_total">OCR 页进度：{{ item.pages_completed ?? 0 }}/{{ item.pages_total }}</span>
                    <p v-if="item.error_message" class="batch-item-error">{{ item.error_message }}</p>
                  </div>
                  <n-flex align="center" :size="8" wrap class="batch-item-actions">
                    <n-tag size="small" :type="itemStatusType(item.status)" :bordered="false">
                      {{ item.status || "-" }}
                    </n-tag>
                    <n-dropdown
                      trigger="click"
                      :options="resultDownloadOptions"
                      :disabled="item.status !== 'success' || !item.copied_output_path"
                      @select="(key) => handleBatchItemResultDownload(item, toResultDownloadFormat(key))"
                    >
                      <n-button
                        size="small"
                        type="primary"
                        secondary
                        :disabled="item.status !== 'success' || !item.copied_output_path"
                      >
                        下载结果
                      </n-button>
                    </n-dropdown>
                    <n-select
                      size="small"
                      :value="batchItemRerunStageValue(item)"
                      :options="rerunStageOptions"
                      :disabled="!canRerunBatchItem(item) || isBatchItemRerunning(item)"
                      class="batch-item-rerun-select"
                      @update:value="(value) => setBatchItemRerunStage(item, value)"
                    />
                    <n-button
                      size="small"
                      type="warning"
                      secondary
                      :disabled="!canRerunBatchItem(item)"
                      :loading="isBatchItemRerunning(item)"
                      @click="handleBatchItemRerun(item)"
                    >
                      重跑
                    </n-button>
                  </n-flex>
                </div>

                <div class="batch-child-progress" :class="`is-${String(item.status ?? 'pending')}`">
                  <div class="batch-child-progress__summary">
                    <span>当前阶段：{{ batchItemCurrentStageLabel(item) }}</span>
                    <strong>{{ batchItemStageProgress(item) }}%</strong>
                  </div>
                  <n-progress
                    type="line"
                    :percentage="batchItemStageProgress(item)"
                    :status="batchItemProgressStatus(item)"
                    :show-indicator="false"
                    :height="5"
                  />
                  <ol class="batch-child-stage-track">
                    <li
                      v-for="stageName in batchItemPipelineStages(item)"
                      :key="stageName"
                      :class="`is-${getBatchItemStageState(item, stageName)}`"
                    >
                      <span class="batch-child-stage-dot"></span>
                      <span>{{ batchItemStageLabel(stageName) }}</span>
                    </li>
                  </ol>
                </div>

                <div v-if="item.job_id" class="batch-child-artifact-action">
                  <n-button size="small" secondary type="primary" @click="toggleBatchItemArtifacts(item)">
                    {{ expandedBatchArtifactJobId === item.job_id ? "收起中间产物" : "查看中间产物" }}
                  </n-button>
                  <span>产物会随子任务推进陆续出现。</span>
                </div>
                <JobArtifactsViewer
                  v-if="expandedBatchArtifactJobId === item.job_id"
                  :job-id="item.job_id"
                  :batch-id="stateId"
                  class="batch-child-artifacts"
                />
              </div>
            </div>
          </div>

          <n-descriptions label-placement="top" :column="1" size="small" class="status-grid">
            <n-descriptions-item label="当前运行阶段">
              <span class="font-semibold">{{ String(state.current_stage ?? "-") || "-" }}</span>
            </n-descriptions-item>
            <n-descriptions-item label="任务类型 (Kind)">
              <n-tag size="small" :bordered="false" type="info">{{ String(state.kind ?? "-") || "-" }}</n-tag>
            </n-descriptions-item>
            <n-descriptions-item v-if="state.asr_engine || state.asr_candidate" label="语音转文字" :span="2">
              {{ state.asr_engine && state.asr_model ? `${state.asr_engine} / ${state.asr_model}` : state.asr_candidate }}
            </n-descriptions-item>
            <n-descriptions-item v-if="state.secondary_asr_candidate" label="第二 ASR（任务配置）" :span="2">
              {{ state.secondary_asr_candidate }}
            </n-descriptions-item>
            <n-descriptions-item label="服务器保存路径" :span="2">
              <span class="status-card__path">{{ String(state.output_path ?? "-") || "-" }}</span>
            </n-descriptions-item>
            <template v-if="hasInputSummary">
              <n-descriptions-item v-if="videoSource" label="原始视频" :span="2">
                <span class="status-card__path">{{ videoSource }}</span>
              </n-descriptions-item>
              <n-descriptions-item v-if="referenceSource || contentType === 'conversation'" label="参考源" :span="2">
                <span class="status-card__path">{{ referenceSource || "无" }}</span>
              </n-descriptions-item>
              <n-descriptions-item v-if="videosDir" label="批量目录" :span="2">
                <span class="status-card__path">{{ videosDir }}</span>
              </n-descriptions-item>
              <n-descriptions-item v-if="manifestSource" label="批量清单" :span="2">
                <span class="status-card__path">{{ manifestSource }}</span>
              </n-descriptions-item>
              <n-descriptions-item v-if="referenceDir" label="参考目录" :span="2">
                <span class="status-card__path">{{ referenceDir }}</span>
              </n-descriptions-item>
              <n-descriptions-item v-if="sharedReference" label="共享参考" :span="2">
                <span class="status-card__path">{{ sharedReference }}</span>
              </n-descriptions-item>
              <n-descriptions-item v-if="outputDir" label="任务输出目录" :span="2">
                <span class="status-card__path">{{ outputDir }}</span>
              </n-descriptions-item>
            </template>
            <n-descriptions-item label="创建时间">
              <span class="text-muted">{{ createdAtText }}</span>
            </n-descriptions-item>
            <n-descriptions-item label="更新时间">
              <span class="text-muted">{{ updatedAtText }}</span>
            </n-descriptions-item>

            <template v-if="state.total !== undefined">
              <n-descriptions-item label="视频总任务数">
                {{ String(state.total ?? "-") || "-" }}
              </n-descriptions-item>
              <n-descriptions-item label="状态统计">
                <n-flex :size="8">
                  <n-tag size="small" type="success" :bordered="false">成功 {{ String(state.success ?? 0) }}</n-tag>
                  <n-tag v-if="state.partial !== undefined" size="small" type="warning" :bordered="false">
                    待补页 {{ String(state.partial ?? 0) }}
                  </n-tag>
                  <n-tag size="small" type="error" :bordered="false">失败 {{ String(state.failed ?? 0) }}</n-tag>
                </n-flex>
              </n-descriptions-item>
            </template>
          </n-descriptions>

          <n-alert
            v-if="String(state.error_message ?? '')"
            class="status-card__error"
            :type="status === 'partial' ? 'warning' : 'error'"
            :title="`流水线执行错误报告`"
            :bordered="false"
          >
            <p class="error-text-content">{{ String(state.error_message) }}</p>
          </n-alert>
        </n-flex>
      </n-collapse-transition>
    </n-flex>
  </n-card>
</template>

<style scoped>
.status-card { min-width: 0; }
.status-card :deep(.n-card-header) { align-items: flex-start; flex-wrap: wrap; gap: 12px; }
.status-card :deep(.n-card-header__main) { flex: 1 1 200px; min-width: 0; overflow-wrap: anywhere; line-height: 1.4; font-size: 15px; }
.status-card :deep(.n-card-header__extra) { max-width: 100%; margin-left: 0; }
.status-badge { font-weight: 600; }
.identity-panel { display: grid; gap: 12px; }
.identity-subtitle { color: var(--text-muted); font-size: 12px; overflow-wrap: anywhere; }
.identity-list { display: flex; flex-wrap: wrap; gap: 12px 24px; }
.identity-item { display: grid; gap: 4px; min-width: min(180px, 100%); max-width: 100%; }
.identity-item span, .summary-label { color: var(--text-muted); font-size: 12px; }
.identity-item strong { font-size: 13px; font-weight: 500; overflow-wrap: anywhere; }
.status-summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(140px, 100%), 1fr)); gap: 12px 16px; padding: 16px 0; border-block: 1px solid var(--border-subtle); }
.summary-cell { min-width: 0; }
.summary-cell.is-wide { grid-column: 1 / -1; }
.summary-cell strong { display: block; margin-top: 4px; font-size: 13px; font-weight: 500; overflow-wrap: anywhere; }
.status-detail { padding-top: 8px; }
.pipeline-section, .batch-items-section { min-width: 0; }
.pipeline-title, .batch-items-title, .rerun-title { margin: 0 0 12px; font-size: 14px; font-weight: 600; }
.pipeline-flow { display: grid; grid-template-columns: repeat(auto-fit, minmax(100px, 1fr)); gap: 12px 8px; }
.pipeline-step { display: flex; align-items: center; gap: 8px; color: var(--text-muted); font-size: 12px; }
.pipeline-step__circle { display: grid; place-items: center; flex: none; width: 24px; height: 24px; border: 1px solid var(--border-strong); border-radius: 50%; background: var(--surface-subtle); }
.pipeline-step.is-completed { color: var(--color-success); }
.pipeline-step.is-active { color: var(--primary); font-weight: 600; }
.pipeline-step.is-failed { color: var(--color-error); }
.pipeline-step.is-completed .pipeline-step__circle, .pipeline-step.is-active .pipeline-step__circle, .pipeline-step.is-failed .pipeline-step__circle { border-color: currentColor; }
.rerun-section { display: flex; flex-wrap: wrap; align-items: center; gap: 16px; padding: 16px; background: var(--surface-subtle); border-radius: 8px; }
.rerun-section > * { min-width: 0; width: 100%; }
.rerun-title { margin-bottom: 4px; }
.rerun-copy { margin: 0; color: var(--text-secondary); font-size: 12px; }
.rerun-select { width: 100%; min-width: 0; }
.ocr-progress-section { padding: 16px; border-radius: 8px; background: var(--color-warning-bg); }
.batch-items-section__head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.batch-items-count { color: var(--text-muted); font-size: 12px; }
.batch-items-list { display: grid; gap: 12px; }
.batch-item-row { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 16px; padding: 16px 0; border-top: 1px solid var(--border-subtle); }
.batch-child-row { display: grid; gap: 12px; }
.batch-child-row__head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; }
.batch-item-main { min-width: 0; display: grid; gap: 4px; overflow-wrap: anywhere; }
.batch-item-main strong { font-size: 13px; }
.batch-item-main span { color: var(--text-secondary); font-size: 12px; }
.batch-item-main .batch-item-id { color: var(--text-muted); }
.batch-item-main .batch-item-muted { color: var(--color-warning); }
.batch-item-error { margin: 4px 0 0; color: var(--color-error); font-size: 12px; overflow-wrap: anywhere; }
.batch-item-actions { max-width: 100%; }
.batch-item-rerun-select { width: 170px; max-width: 100%; }
.batch-child-progress { display: grid; gap: 12px; padding: 12px 0; border-block: 1px solid var(--border-subtle); }
.batch-child-progress__summary { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; color: var(--text-secondary); font-size: 12px; }
.batch-child-stage-track { display: grid; grid-template-columns: repeat(auto-fit, minmax(88px, 1fr)); gap: 8px; margin: 0; padding: 0; list-style: none; }
.batch-child-stage-track li { display: flex; align-items: center; gap: 8px; color: var(--text-muted); font-size: 12px; }
.batch-child-stage-dot { width: 7px; height: 7px; flex: none; border: 1px solid currentColor; border-radius: 50%; }
.batch-child-stage-track .is-completed { color: var(--color-success); }
.batch-child-stage-track .is-active { color: var(--primary); font-weight: 600; }
.batch-child-stage-track .is-failed { color: var(--color-error); }
.batch-child-stage-track .is-completed .batch-child-stage-dot, .batch-child-stage-track .is-active .batch-child-stage-dot, .batch-child-stage-track .is-failed .batch-child-stage-dot { background: currentColor; }
.batch-child-artifact-action { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; color: var(--text-muted); font-size: 12px; }
.status-grid { min-width: 0; }
.status-grid :deep(table) { table-layout: fixed; width: 100%; }
.text-muted { font-size: 12px; color: var(--text-muted); }
.status-card__path { display: block; font-family: ui-monospace, monospace; font-size: 12px; color: var(--text-secondary); overflow-wrap: anywhere; }
.error-text-content { margin: 4px 0 0; font-family: ui-monospace, monospace; font-size: 12px; color: var(--color-error); white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
