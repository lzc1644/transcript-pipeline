<script setup lang="ts">
import {
  NAlert,
  NButton,
  NEmpty,
  NFlex,
  NSelect,
  NSkeleton,
  NTag,
  useMessage,
} from "naive-ui";
import { computed, ref, watch } from "vue";

import {
  getBatchItemArtifact,
  getJobArtifact,
  listBatchItemArtifacts,
  listJobArtifacts,
  type JobArtifact,
  type JobArtifactContent,
} from "../api/client";

const props = defineProps<{
  jobId: string;
  batchId?: string;
}>();

const message = useMessage();
const artifacts = ref<JobArtifact[]>([]);
const selectedArtifactId = ref<string | null>(null);
const selectedArtifact = ref<JobArtifactContent | null>(null);
const loadingList = ref(false);
const loadingContent = ref(false);
const errorText = ref("");

const existingArtifacts = computed(() => artifacts.value.filter((item) => item.exists));

const artifactOptions = computed(() =>
  artifacts.value.map((item) => ({
    label: `${item.label} · ${item.stage}${item.exists ? "" : " · 未生成"}`,
    value: item.id,
    disabled: !item.exists,
  })),
);

const selectedMeta = computed(() => artifacts.value.find((item) => item.id === selectedArtifactId.value) ?? null);

const formattedSize = computed(() => {
  const size = selectedMeta.value?.size ?? 0;
  if (size < 1024) {
    return `${size} B`;
  }
  if (size < 1024 * 1024) {
    return `${(size / 1024).toFixed(1)} KB`;
  }
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
});

async function loadArtifacts() {
  if (!props.jobId) {
    return;
  }
  loadingList.value = true;
  errorText.value = "";
  selectedArtifact.value = null;
  try {
    const response = props.batchId
      ? await listBatchItemArtifacts(props.batchId, props.jobId)
      : await listJobArtifacts(props.jobId);
    artifacts.value = response.items;
    const firstExisting = response.items.find((item) => item.exists);
    selectedArtifactId.value = firstExisting?.id ?? null;
  } catch (caught) {
    errorText.value = caught instanceof Error ? caught.message : "加载阶段产物失败";
  } finally {
    loadingList.value = false;
  }
}

async function loadSelectedArtifact() {
  if (!props.jobId || !selectedArtifactId.value) {
    selectedArtifact.value = null;
    return;
  }
  loadingContent.value = true;
  errorText.value = "";
  try {
    selectedArtifact.value = props.batchId
      ? await getBatchItemArtifact(props.batchId, props.jobId, selectedArtifactId.value)
      : await getJobArtifact(props.jobId, selectedArtifactId.value);
  } catch (caught) {
    selectedArtifact.value = null;
    errorText.value = caught instanceof Error ? caught.message : "读取阶段产物失败";
  } finally {
    loadingContent.value = false;
  }
}

async function writeClipboardText(content: string) {
  if (window.isSecureContext && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(content);
      return;
    } catch {
      // HTTPS 页面也可能因浏览器权限策略拒绝 Clipboard API，继续尝试兼容方案。
    }
  }

  const textarea = document.createElement("textarea");
  textarea.value = content;
  textarea.setAttribute("readonly", "");
  textarea.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0";
  document.body.append(textarea);
  textarea.select();

  try {
    if (!document.execCommand("copy")) {
      throw new Error("浏览器未允许复制到剪贴板");
    }
  } finally {
    textarea.remove();
  }
}

async function copyContent() {
  const content = selectedArtifact.value?.content ?? "";
  if (!content) {
    message.warning("当前没有可复制的产物内容。");
    return;
  }

  try {
    await writeClipboardText(content);
    message.success("已复制当前产物内容");
  } catch {
    message.error("复制失败，请检查浏览器的剪贴板权限后重试。");
  }
}

watch(() => [props.batchId, props.jobId], loadArtifacts, { immediate: true });
watch(selectedArtifactId, loadSelectedArtifact);
</script>

<template>
  <section class="artifacts-panel">
    <n-flex align="center" justify="space-between" :size="12" wrap>
      <div>
        <h4 class="artifacts-title">阶段文字产物</h4>
        <p class="artifacts-copy">查看每个阶段已经生成的文本或 JSON，用于判断从哪个阶段重跑。</p>
      </div>
      <n-button size="small" secondary type="primary" :loading="loadingList" @click="loadArtifacts">
        刷新产物
      </n-button>
    </n-flex>

    <n-alert v-if="errorText" type="error" :title="errorText" :bordered="false" class="artifacts-alert" />

    <template v-if="loadingList">
      <n-skeleton text :repeat="3" />
    </template>

    <template v-else-if="artifacts.length === 0 || existingArtifacts.length === 0">
      <n-empty v-if="!errorText" description="当前任务还没有可查看的文字产物。" />
    </template>

    <template v-else>
      <div class="artifact-toolbar">
        <n-select
          v-model:value="selectedArtifactId"
          :options="artifactOptions"
          class="artifact-select"
        />
        <n-button size="small" secondary :disabled="!selectedArtifact?.content" @click="copyContent">复制内容</n-button>
      </div>

      <div v-if="selectedMeta" class="artifact-meta">
        <n-tag size="small" type="info" :bordered="false">{{ selectedMeta.stage }}</n-tag>
        <n-tag size="small" :bordered="false">{{ selectedMeta.content_type }}</n-tag>
        <span>{{ formattedSize }}</span>
        <span class="artifact-path">{{ selectedMeta.path }}</span>
      </div>

      <div class="artifact-preview">
        <n-skeleton v-if="loadingContent" text :repeat="8" />
        <pre v-else>{{ selectedArtifact?.content || "暂无内容" }}</pre>
      </div>
    </template>
  </section>
</template>

<style scoped>
.artifacts-panel { display: grid; gap: 16px; min-width: 0; padding-top: 20px; border-top: 1px solid var(--border-subtle); }
.artifacts-title { margin: 0 0 4px; font-size: 14px; font-weight: 600; }
.artifacts-copy { margin: 0; font-size: 12px; color: var(--text-secondary); }
.artifact-toolbar { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; min-width: 0; }
.artifact-select { flex: 1 1 220px; min-width: 0; }
.artifact-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; min-width: 0; color: var(--text-muted); font-size: 12px; }
.artifact-path { width: 100%; overflow-wrap: anywhere; }
.artifact-preview { max-height: 420px; min-width: 0; overflow: auto; border: 1px solid var(--border-subtle); border-radius: 8px; background: var(--surface-subtle); }
.artifact-preview pre { margin: 0; padding: 16px; color: var(--text-primary); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12px; line-height: 1.8; white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
