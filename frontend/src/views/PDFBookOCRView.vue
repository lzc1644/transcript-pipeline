<script setup lang="ts">
import {
  NAlert,
  NButton,
  NCard,
  NEmpty,
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
  NTag,
  useDialog,
  useMessage,
} from "naive-ui";
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";

import {
  deletePDFBookOCRTask,
  getFrontendSettings,
  getPDFBookOCRTask,
  listPDFBookOCRTasks,
  pdfBookOCRArchiveUrl,
  pdfBookOCRResultUrl,
  retryPDFBookOCR,
  submitPDFBookOCR,
  type PDFBookOCRItem,
  type PDFBookOCRTask,
} from "../api/client";
import RemoteDirectoryUpload from "../components/RemoteDirectoryUpload.vue";
import RemoteFileUpload from "../components/RemoteFileUpload.vue";

type InputMode = "file" | "directory";

const message = useMessage();
const dialog = useDialog();
const deletingTaskId = ref("");
const downloading = ref(false);
const inputMode = ref<InputMode>("file");
const currentTask = ref<PDFBookOCRTask | null>(null);
const activeTaskId = ref("");
const submitting = ref(false);
const retrying = ref(false);
const historyLoading = ref(false);
const historyError = ref("");
const taskHistory = ref<PDFBookOCRTask[]>([]);
const pollingHandle = ref<number | null>(null);
const pdfExtensions = [".pdf"];
const reasoningOptions = [
  { label: "低", value: "low" },
  { label: "中", value: "medium" },
  { label: "高", value: "high" },
];

const form = reactive({
  input_path: "",
  ocr_model: "",
  ocr_reasoning_effort: "",
  ocr_max_concurrency: 40 as number | null,
  ocr_submit_interval_seconds: 5 as number | null,
});

const isTaskRunning = computed(() => {
  return currentTask.value?.status === "pending" || currentTask.value?.status === "running";
});

const taskItems = computed(() => currentTask.value?.items ?? []);
const downloadableCount = computed(() => taskItems.value.filter((item) => item.success && item.output_file).length);
const canRetryMissingPages = computed(() => {
  const task = currentTask.value;
  if (!task || isTaskRunning.value || task.status === "success") {
    return false;
  }
  return task.status === "partial" || task.status === "failed";
});

function taskStatusLabelFor(status: PDFBookOCRTask["status"] | undefined): string {
  if (status === "pending") {
    return "等待开始";
  }
  if (status === "running") {
    return "正在识别";
  }
  if (status === "success") {
    return "已完成";
  }
  if (status === "partial") {
    return "待补页";
  }
  if (status === "failed") {
    return "识别失败";
  }
  return "尚未提交";
}

const taskStatusLabel = computed(() => taskStatusLabelFor(currentTask.value?.status));

function taskStatusTypeFor(
  status: PDFBookOCRTask["status"] | undefined,
): "default" | "info" | "success" | "warning" | "error" {
  if (status === "success") {
    return "success";
  }
  if (status === "failed") {
    return "error";
  }
  if (status === "partial") {
    return "warning";
  }
  if (status === "pending" || status === "running") {
    return "info";
  }
  return "default";
}

const taskStatusType = computed(() => taskStatusTypeFor(currentTask.value?.status));

function stopPolling() {
  if (pollingHandle.value !== null) {
    window.clearInterval(pollingHandle.value);
    pollingHandle.value = null;
  }
}

function startPolling() {
  stopPolling();
  pollingHandle.value = window.setInterval(() => {
    void refreshTask();
  }, 2000);
}

async function refreshTask() {
  if (!activeTaskId.value) {
    return;
  }
  const taskId = activeTaskId.value;
  try {
    const task = await getPDFBookOCRTask(taskId);
    if (activeTaskId.value !== taskId) {
      return;
    }
    currentTask.value = task;
    if (task.status !== "pending" && task.status !== "running") {
      stopPolling();
      void loadTaskHistory();
    }
  } catch (caught) {
    if (activeTaskId.value !== taskId) {
      return;
    }
    stopPolling();
    message.error(caught instanceof Error ? caught.message : "读取 PDF OCR 任务状态失败");
  }
}

async function loadTaskHistory() {
  historyLoading.value = true;
  historyError.value = "";
  try {
    const tasks = (await listPDFBookOCRTasks()).items;
    taskHistory.value = tasks;
    if (!activeTaskId.value && tasks.length > 0) {
      activeTaskId.value = tasks[0].id;
      await refreshTask();
      if (isTaskRunning.value) {
        startPolling();
      }
    }
  } catch (caught) {
    historyError.value = caught instanceof Error ? caught.message : "加载 PDF OCR 任务历史失败";
    message.error(caught instanceof Error ? caught.message : "加载 PDF OCR 任务历史失败");
  } finally {
    historyLoading.value = false;
  }
}

async function loadTask(taskId: string) {
  stopPolling();
  activeTaskId.value = taskId;
  await refreshTask();
  if (isTaskRunning.value) {
    startPolling();
  }
}

function taskSourceLabel(task: PDFBookOCRTask): string {
  const sourcePath = task.input_summary?.input_path ?? "";
  const pathParts = sourcePath.split(/[\\/]/).filter(Boolean);
  return pathParts[pathParts.length - 1] || "PDF OCR 任务";
}

async function loadDefaults() {
  try {
    const settings = await getFrontendSettings();
    if (!form.ocr_model) {
      form.ocr_model = settings.ocr_model;
    }
    if (!form.ocr_reasoning_effort) {
      form.ocr_reasoning_effort = settings.ocr_reasoning_effort;
    }
    if (Number.isFinite(settings.ocr_max_concurrency)) {
      form.ocr_max_concurrency = settings.ocr_max_concurrency;
    }
    if (Number.isFinite(settings.ocr_submit_interval_seconds)) {
      form.ocr_submit_interval_seconds = settings.ocr_submit_interval_seconds;
    }
  } catch (caught) {
    message.error(caught instanceof Error ? caught.message : "读取 OCR 默认设置失败");
  }
}

function sourceModeChanged() {
  form.input_path = "";
}

function itemStatusType(item: PDFBookOCRItem): "success" | "warning" | "error" {
  if (item.success) {
    return "success";
  }
  return item.completed_pages > 0 ? "warning" : "error";
}

function itemStatusLabel(item: PDFBookOCRItem): string {
  if (item.success) {
    return "完成";
  }
  return item.completed_pages > 0 ? "待补页" : "失败";
}

function pageErrorEntries(item: PDFBookOCRItem): Array<[string, string]> {
  return Object.entries(item.page_errors ?? {}).sort(([left], [right]) => Number(left) - Number(right));
}

function openResult(item: PDFBookOCRItem) {
  if (!currentTask.value || !item.output_file) {
    message.error("当前结果不可下载。");
    return;
  }
  window.location.href = pdfBookOCRResultUrl(currentTask.value.id, item.output_file);
}

async function downloadAll() {
  if (!currentTask.value || isTaskRunning.value || !downloadableCount.value) {
    return;
  }
  const taskId = currentTask.value.id;
  downloading.value = true;
  try {
    const response = await fetch(pdfBookOCRArchiveUrl(taskId));
    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.detail || "打包下载失败");
    }
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = `${taskId}-txt.zip`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (caught) {
    message.error(caught instanceof Error ? caught.message : "打包下载失败");
  } finally {
    downloading.value = false;
  }
}

function confirmDelete(task: PDFBookOCRTask) {
  if (task.status === "pending" || task.status === "running" || deletingTaskId.value) {
    return;
  }
  dialog.warning({
    title: "删除 PDF OCR 项目？",
    content: `将永久删除“${taskSourceLabel(task)}”的任务记录、TXT 结果和页检查点，之后无法补页恢复。上传的 PDF 会保留。`,
    positiveText: "确认删除",
    negativeText: "取消",
    onPositiveClick: async () => {
      deletingTaskId.value = task.id;
      try {
        await deletePDFBookOCRTask(task.id);
        if (activeTaskId.value === task.id) {
          stopPolling();
          activeTaskId.value = "";
          currentTask.value = null;
        }
        taskHistory.value = taskHistory.value.filter((item) => item.id !== task.id);
        await loadTaskHistory();
        message.success("PDF OCR 项目已删除。");
      } catch (caught) {
        message.error(caught instanceof Error ? caught.message : "删除 PDF OCR 项目失败");
        return false;
      } finally {
        deletingTaskId.value = "";
      }
    },
  });
}

async function submit() {
  if (!form.input_path) {
    message.warning("请先选择并上传 PDF 书籍或 PDF 目录。");
    return;
  }
  if (form.ocr_submit_interval_seconds === null || form.ocr_max_concurrency === null) {
    message.warning("请填写投递间隔和最大并发数。");
    return;
  }

  submitting.value = true;
  try {
    const response = await submitPDFBookOCR({
      input_path: form.input_path,
      ocr_model: form.ocr_model || null,
      ocr_reasoning_effort: form.ocr_reasoning_effort || null,
      ocr_max_concurrency: form.ocr_max_concurrency,
      ocr_submit_interval_seconds: form.ocr_submit_interval_seconds,
    });
    activeTaskId.value = response.task_id;
    await refreshTask();
    await loadTaskHistory();
    if (isTaskRunning.value) {
      startPolling();
    }
    message.success(`PDF OCR 任务已提交：${response.task_id}`);
  } catch (caught) {
    message.error(caught instanceof Error ? caught.message : "提交 PDF OCR 任务失败");
  } finally {
    submitting.value = false;
  }
}

async function retryMissingPages() {
  if (!currentTask.value || !canRetryMissingPages.value) {
    return;
  }
  retrying.value = true;
  try {
    const response = await retryPDFBookOCR(currentTask.value.id);
    activeTaskId.value = response.task_id;
    await refreshTask();
    await loadTaskHistory();
    startPolling();
    message.success("已重新启动，只处理尚未成功的页面。");
  } catch (caught) {
    message.error(caught instanceof Error ? caught.message : "重试 PDF OCR 缺失页失败");
  } finally {
    retrying.value = false;
  }
}

watch(inputMode, sourceModeChanged);
onMounted(() => {
  void loadDefaults();
  void loadTaskHistory();
});
onBeforeUnmount(stopPolling);
</script>

<template>
  <div class="workbench-page pdf-book-ocr-view">
    <section class="page-heading">
      <div><h2>PDF 书籍 OCR</h2><p>上传 PDF 或整套书籍目录，查看逐页状态并下载完整 TXT。</p></div>
    </section>

    <n-alert type="info" :bordered="false" class="pdf-book-ocr-view__notice">
      本页只接收 PDF。目录上传会保留原有子目录层级；每本书的 TXT 仅在全部页面成功后才会出现。TXT 保留页内段落，真实换页处添加 OCR_PAGE_BREAK 横线标记（含原页码），便于后续校对识别跨页硬换行。
    </n-alert>

    <div class="workbench-columns">
      <div class="workspace-primary">
        <n-card class="view-card pdf-book-ocr-panel pdf-book-ocr-panel--input" :bordered="false">
          <template #header>
            <n-flex align="center" :size="10">
              <span>选择来源与运行设置</span>
            </n-flex>
          </template>

          <n-form label-placement="top">
            <n-form-item label="输入方式">
              <n-radio-group v-model:value="inputMode" name="pdf-ocr-input-mode">
                <n-radio-button value="file">单本 PDF</n-radio-button>
                <n-radio-button value="directory">PDF 目录</n-radio-button>
              </n-radio-group>
            </n-form-item>

            <n-form-item :label="inputMode === 'file' ? 'PDF 书籍' : 'PDF 书籍目录'" required>
              <RemoteFileUpload
                v-if="inputMode === 'file'"
                v-model="form.input_path"
                kind="pdf_ocr"
                label="PDF 书籍"
                accept=".pdf"
                button-text="选择并上传本机 PDF"
              />
              <RemoteDirectoryUpload
                v-else
                v-model="form.input_path"
                kind="pdf_ocr"
                label="PDF"
                :extensions="pdfExtensions"
                button-text="选择并上传本机 PDF 目录"
              />
            </n-form-item>

            <n-form-item label="OCR 推理强度">
              <n-select
                v-model:value="form.ocr_reasoning_effort"
                :options="reasoningOptions"
                clearable
                placeholder="沿用运行设置"
              />
            </n-form-item>

            <n-form-item label="OCR 模型">
              <n-input v-model:value="form.ocr_model" placeholder="留空则沿用运行设置" clearable />
            </n-form-item>

            <details class="advanced-options">
              <summary>高级参数 · 请求投递与并发</summary>
            <n-grid :cols="2" :x-gap="12" :y-gap="0" responsive="screen" item-responsive>
              <n-grid-item span="2 s:1">
                <n-form-item label="图片投递间隔（秒）" required>
                  <n-input-number
                    v-model:value="form.ocr_submit_interval_seconds"
                    :min="0"
                    :step="1"
                    class="w-full"
                    placeholder="默认 5 秒"
                  />
                </n-form-item>
              </n-grid-item>
              <n-grid-item span="2 s:1">
                <n-form-item label="最大并发请求数" required>
                  <n-input-number
                    v-model:value="form.ocr_max_concurrency"
                    :min="1"
                    :precision="0"
                    :step="1"
                    class="w-full"
                    placeholder="默认 40"
                  />
                </n-form-item>
              </n-grid-item>
            </n-grid>
            </details>

            <div class="pdf-book-ocr-panel__action">
              <n-button type="primary" size="large" :loading="submitting" :disabled="!form.input_path" @click="submit">
                开始识别
              </n-button>
              <span>模型与 API 密钥沿用“运行设置”；投递设置仅作用于本次任务及其缺页重试。</span>
            </div>
          </n-form>
        </n-card>
      </div>
      <aside class="workspace-inspector" aria-label="OCR 识别结果">
        <n-card class="view-card pdf-book-ocr-panel pdf-book-ocr-panel--result" :bordered="false">
          <template #header>
            <n-flex align="center" justify="space-between" :size="10">
              <n-flex align="center" :size="10">
                <span>识别结果</span>
              </n-flex>
              <n-tag :type="taskStatusType" :bordered="false">{{ taskStatusLabel }}</n-tag>
            </n-flex>
          </template>

          <n-empty v-if="!currentTask" description="提交任务后，这里会显示每本书的状态与下载入口。" />

          <template v-else>
            <div class="pdf-book-ocr-task" :class="{ 'is-running': isTaskRunning }">
              <div>
                <p class="pdf-book-ocr-task__label">任务 ID</p>
                <p class="pdf-book-ocr-task__id">{{ currentTask.id }}</p>
              </div>
              <div class="pdf-book-ocr-task__counts">
                <span>书籍 {{ currentTask.total ?? 0 }}</span>
                <span>完整 {{ currentTask.success ?? 0 }}</span>
                <span v-if="currentTask.pages_total">页面 {{ currentTask.pages_completed ?? 0 }}/{{ currentTask.pages_total }}</span>
                <span v-if="currentTask.pages_failed">待补 {{ currentTask.pages_failed }}</span>
              </div>
            </div>

            <n-flex class="pdf-book-ocr-task__actions" :size="10" wrap>
              <n-button
                type="primary"
                secondary
                :loading="downloading"
                :disabled="isTaskRunning || downloadableCount === 0"
                @click="downloadAll"
              >
                一键下载 TXT（ZIP · {{ downloadableCount }} 本）
              </n-button>
              <n-button
                type="error"
                secondary
                :loading="deletingTaskId === currentTask.id"
                :disabled="isTaskRunning || !!deletingTaskId || retrying"
                @click="confirmDelete(currentTask)"
              >删除项目</n-button>
              <span v-if="!isTaskRunning && currentTask.failed">仅打包完整书籍；未完成书籍列于 ZIP 内的 summary.json。</span>
              <span v-if="isTaskRunning">识别结束后可打包下载或删除项目。</span>
            </n-flex>

            <n-alert
              v-if="currentTask.error_message"
              :type="currentTask.status === 'partial' ? 'warning' : 'error'"
              :bordered="false"
              class="pdf-book-ocr-task__error"
            >
              {{ currentTask.error_message }}
            </n-alert>

            <div v-if="canRetryMissingPages" class="pdf-book-ocr-task__retry">
              <n-button type="warning" secondary :loading="retrying" @click="retryMissingPages">
                重试缺失页
              </n-button>
              <span>已成功页面会保留，只重新识别尚未完成的页。</span>
            </div>

            <n-empty v-if="!isTaskRunning && taskItems.length === 0" description="任务尚未产生可展示的结果。" />
            <div v-else class="pdf-book-ocr-results">
              <div v-for="item in taskItems" :key="item.source_file" class="pdf-book-ocr-result-item">
                <div class="pdf-book-ocr-result-item__main">
                  <n-tag size="small" :type="itemStatusType(item)" :bordered="false">
                    {{ itemStatusLabel(item) }}
                  </n-tag>
                  <strong>{{ item.source_file }}</strong>
                  <span v-if="item.page_count" class="pdf-book-ocr-result-item__meta">
                    {{ item.completed_pages }}/{{ item.page_count }} 页
                  </span>
                  <span v-else-if="item.success" class="pdf-book-ocr-result-item__meta">{{ item.text_length }} 字</span>
                </div>
                <p v-if="!item.success" class="pdf-book-ocr-result-item__error">{{ item.error || 'OCR 未返回结果。' }}</p>
                <div v-if="item.failed_page_numbers?.length" class="pdf-book-ocr-result-item__pages">
                  <strong>待重试页：</strong>
                  <span>{{ item.failed_page_numbers.join('、') }}</span>
                </div>
                <details v-if="pageErrorEntries(item).length" class="pdf-book-ocr-result-item__details">
                  <summary>查看逐页错误详情</summary>
                  <ul>
                    <li v-for="[pageNumber, error] in pageErrorEntries(item)" :key="pageNumber">
                      <strong>第 {{ pageNumber }} 页：</strong>{{ error }}
                    </li>
                  </ul>
                </details>
                <n-space v-if="item.success" :size="8" class="pdf-book-ocr-result-item__actions">
                  <n-button tertiary type="primary" size="small" @click="openResult(item)">下载 TXT</n-button>
                </n-space>
              </div>
            </div>
          </template>
        </n-card>
      </aside>
    </div>
        <n-card class="view-card pdf-book-ocr-history" :bordered="false">
          <template #header>
            <n-flex align="center" justify="space-between" :size="12" wrap>
              <div>
                <span>PDF OCR 任务历史</span>
                <p class="pdf-book-ocr-history__copy">选择任一记录可恢复结果查看；运行中的任务会继续刷新。</p>
              </div>
              <n-button size="small" secondary :loading="historyLoading" @click="loadTaskHistory">刷新历史</n-button>
            </n-flex>
          </template>

          <n-alert v-if="historyError" type="error" title="无法读取任务历史" :bordered="false">{{ historyError }}。请刷新重试。</n-alert>
          <p v-if="historyLoading" role="status" class="prompt-hint">正在读取任务历史…</p>
          <n-empty v-if="!historyLoading && !historyError && taskHistory.length === 0" description="还没有 PDF OCR 任务记录。" />
          <div v-if="taskHistory.length" class="pdf-book-ocr-history__list">
            <div
              v-for="task in taskHistory"
              :key="task.id"
              class="pdf-book-ocr-history-row"
            >
              <button
                type="button"
                class="pdf-book-ocr-history-item"
                :class="{ 'is-selected': currentTask?.id === task.id }"
                :aria-pressed="currentTask?.id === task.id"
                @click="loadTask(task.id)"
              >
                <div class="pdf-book-ocr-history-item__main">
                  <strong>{{ taskSourceLabel(task) }}</strong>
                  <span>{{ task.id }}</span>
                </div>
                <div class="pdf-book-ocr-history-item__meta">
                  <n-tag size="small" :type="taskStatusTypeFor(task.status)" :bordered="false">
                    {{ taskStatusLabelFor(task.status) }}
                  </n-tag>
                  <span>{{ task.updated_at }}</span>
                </div>
              </button>
              <n-button
                type="error"
                tertiary
                size="small"
                :loading="deletingTaskId === task.id"
                :disabled="task.status === 'pending' || task.status === 'running' || !!deletingTaskId || retrying"
                :aria-label="`删除 ${taskSourceLabel(task)}`"
                @click="confirmDelete(task)"
              >删除</n-button>
            </div>
          </div>
        </n-card>
  </div>
</template>

<style scoped>
.pdf-book-ocr-panel__action { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; padding-top: 20px; color: var(--text-muted); font-size: 12px; }
.pdf-book-ocr-panel__action span { flex: 1 1 220px; }
.pdf-book-ocr-task { display: grid; gap: 12px; padding-bottom: 16px; border-bottom: 1px solid var(--border-subtle); }
.pdf-book-ocr-task__label, .pdf-book-ocr-task__id { margin: 0; font-size: 12px; }
.pdf-book-ocr-task__label { color: var(--text-muted); }
.pdf-book-ocr-task__id { color: var(--text-primary); font-family: ui-monospace, monospace; overflow-wrap: anywhere; }
.pdf-book-ocr-task__counts { display: flex; flex-wrap: wrap; gap: 8px 16px; color: var(--text-secondary); font-size: 12px; font-variant-numeric: tabular-nums; }
.pdf-book-ocr-task__actions, .pdf-book-ocr-task__retry { margin-top: 16px; color: var(--text-muted); font-size: 12px; }
.pdf-book-ocr-task__retry { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
.pdf-book-ocr-task__error { margin-top: 16px; }
.pdf-book-ocr-results { display: grid; margin-top: 16px; }
.pdf-book-ocr-result-item { display: grid; gap: 8px; padding: 16px 0; border-bottom: 1px solid var(--border-subtle); min-width: 0; }
.pdf-book-ocr-result-item__main { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; min-width: 0; }
.pdf-book-ocr-result-item__main strong { font-size: 13px; overflow-wrap: anywhere; min-width: 0; }
.pdf-book-ocr-result-item__meta { color: var(--text-muted); font-size: 12px; }
.pdf-book-ocr-result-item__error { margin: 0; color: var(--color-error); font-size: 12px; overflow-wrap: anywhere; }
.pdf-book-ocr-result-item__pages, .pdf-book-ocr-result-item__details { color: var(--text-secondary); font-size: 12px; overflow-wrap: anywhere; }
.pdf-book-ocr-result-item__details summary { color: var(--primary); cursor: pointer; padding: 4px 0; }
.pdf-book-ocr-result-item__details ul { display: grid; gap: 8px; padding-left: 20px; }
.pdf-book-ocr-history { padding-top: 24px; border-top: 1px solid var(--border-subtle); }
.pdf-book-ocr-history__copy { margin: 4px 0 0; color: var(--text-muted); font-size: 12px; font-weight: 400; }
.pdf-book-ocr-history__list { display: grid; gap: 4px; }
.pdf-book-ocr-history-row { display: flex; align-items: center; gap: 8px; }
.pdf-book-ocr-history-item { display: flex; flex: 1; min-width: 0; align-items: center; justify-content: space-between; gap: 16px; padding: 12px; border: 1px solid var(--border-subtle); border-radius: 8px; background: var(--surface-canvas); color: var(--text-primary); text-align: left; cursor: pointer; transition: background-color var(--motion-fast) var(--ease-out), border-color var(--motion-fast) var(--ease-out); }
.pdf-book-ocr-history-item:hover, .pdf-book-ocr-history-item.is-selected { border-color: var(--primary); background: var(--primary-alpha-10); }
.pdf-book-ocr-history-item__main, .pdf-book-ocr-history-item__meta { display: grid; min-width: 0; gap: 4px; }
.pdf-book-ocr-history-item__main strong, .pdf-book-ocr-history-item__main span { overflow-wrap: anywhere; }
.pdf-book-ocr-history-item__main span, .pdf-book-ocr-history-item__meta span { font-size: 12px; color: var(--text-muted); }
.pdf-book-ocr-history-item__meta { justify-items: end; flex-shrink: 0; }
@media (max-width: 600px) {
  .pdf-book-ocr-history-item { align-items: flex-start; flex-direction: column; gap: 8px; }
  .pdf-book-ocr-history-item__meta { justify-items: start; }
}
</style>
