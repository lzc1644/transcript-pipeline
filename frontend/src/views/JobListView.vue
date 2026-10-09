<script setup lang="ts">
import { NAlert, NButton, NEmpty, NInput, NSpace, NTabs, NTabPane, useMessage } from "naive-ui";
import { computed, onMounted, onUnmounted, ref } from "vue";

import { listBatches, listJobs, listStageRuns, type JobState } from "../api/client";
import JobStatusCard from "../components/JobStatusCard.vue";

const message = useMessage();
const jobs = ref<JobState[]>([]);
const batches = ref<JobState[]>([]);
const stageRuns = ref<JobState[]>([]);
const loading = ref(false);
const pollHandle = ref<number | null>(null);
const loadError = ref("");
const search = ref("");
const searchTerms = computed(() => search.value.normalize("NFKC").toLocaleLowerCase().trim().split(/\s+/u).filter(Boolean));
const statusLabels: Record<string, string> = { pending: "等待中", running: "运行中", success: "成功 已完成", failed: "失败", partial: "部分完成" };
function matchingRecords<T extends JobState>(records: T[]): T[] {
  if (!searchTerms.value.length) return records;
  return records.filter(item => {
    const text = [
      item.id, item.status, statusLabels[item.status], item.current_stage, item.output_path, item.download_name,
      ...Object.values(item.input_summary ?? {}),
      ...(item.items ?? []).flatMap(child => [child.job_id, child.video_source, child.reference_source, child.book_name, child.chapter, child.output_dir]),
    ].filter(Boolean).join(" ").normalize("NFKC").toLocaleLowerCase();
    return searchTerms.value.every(term => text.includes(term));
  });
}
const filteredJobs = computed(() => matchingRecords(jobs.value));
const filteredBatches = computed(() => matchingRecords(batches.value));
const filteredStageRuns = computed(() => matchingRecords(stageRuns.value));
const matchedCount = computed(() => filteredJobs.value.length + filteredBatches.value.length + filteredStageRuns.value.length);

const totalCount = computed(() => jobs.value.length + batches.value.length + stageRuns.value.length);
const hasRunningRecords = computed(() => {
  return [...jobs.value, ...batches.value, ...stageRuns.value].some((item) => {
    return item.status === "running" || item.status === "pending";
  });
});

function stopPolling() {
  if (pollHandle.value !== null) {
    window.clearInterval(pollHandle.value);
    pollHandle.value = null;
  }
}

async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    const [jobResponse, batchResponse, stageRunResponse] = await Promise.all([
      listJobs(),
      listBatches(),
      listStageRuns(),
    ]);
    jobs.value = jobResponse.items;
    batches.value = batchResponse.items;
    stageRuns.value = stageRunResponse.items;
    if (!hasRunningRecords.value) {
      stopPolling();
    }
  } catch (caught) {
    loadError.value = caught instanceof Error ? caught.message : "加载任务列表失败";
    message.error(caught instanceof Error ? caught.message : "加载任务列表失败");
  } finally {
    loading.value = false;
  }
}

function startPolling() {
  stopPolling();
  pollHandle.value = window.setInterval(() => {
    void load();
  }, 2000);
}

async function handleRerun() {
  await load();
  if (hasRunningRecords.value) {
    startPolling();
  }
}

onMounted(async () => {
  await load();
  if (hasRunningRecords.value) {
    startPolling();
  }
});
onUnmounted(stopPolling);
</script>

<template>
  <div class="workbench-page job-list-view">
    <section class="page-heading">
      <div><h2>任务列表</h2><p>查看运行进度、重试失败阶段，或下载已完成的结果。</p></div>
      <n-button secondary :loading="loading" @click="load">刷新任务列表</n-button>
    </section>
    <n-input v-model:value="search" clearable placeholder="搜索任务 ID、文件名、书名、章节或路径" :input-props="{ 'aria-label': '搜索任务' }" />
    <div class="jobs-toolbar"><strong aria-live="polite">{{ searchTerms.length ? `匹配 ${matchedCount} / ${totalCount} 条记录` : `共 ${totalCount} 条记录` }}</strong><span v-if="hasRunningRecords">运行中任务每 2 秒刷新</span></div>
    <p v-if="searchTerms.length" class="prompt-hint">搜索同时覆盖三个分类，可切换下方标签查看。<n-button text type="primary" @click="search = ''">清空搜索</n-button></p>
    <n-alert v-if="loadError" type="error" title="任务列表加载失败" :bordered="false">{{ loadError }}。请点击刷新重试；已有记录仍保留。</n-alert>
    <p v-if="loading && totalCount === 0" role="status" class="prompt-hint">正在加载任务记录…</p>
    <n-empty v-else-if="!loadError && totalCount === 0" description="还没有任务记录。">
      <template #extra><router-link to="/single-job">创建第一个任务 →</router-link></template>
    </n-empty>

    <n-tabs v-if="totalCount > 0" type="line" animated>
      <n-tab-pane :name="'jobs'" :tab="`单任务 ${filteredJobs.length}`">
        <n-space vertical :size="16">
          <n-empty v-if="filteredJobs.length === 0" :description="searchTerms.length ? '没有匹配的单任务，试试其他关键词。' : '暂无单任务记录。'" />
          <JobStatusCard
            v-for="item in filteredJobs"
            :key="item.id"
            :state="item"
            @deleted="load"
            @rerun="handleRerun"
          />
        </n-space>
      </n-tab-pane>
      <n-tab-pane :name="'batches'" :tab="`批量任务 ${filteredBatches.length}`">
        <n-space vertical :size="16">
          <n-empty v-if="filteredBatches.length === 0" :description="searchTerms.length ? '没有匹配的批量任务，试试其他关键词。' : '暂无批量任务记录。'" />
          <JobStatusCard
            v-for="item in filteredBatches"
            :key="item.id"
            :state="item"
            @deleted="load"
            @rerun="handleRerun"
          />
        </n-space>
      </n-tab-pane>
      <n-tab-pane :name="'stage-runs'" :tab="`单阶段 ${filteredStageRuns.length}`">
        <n-space vertical :size="16">
          <n-empty v-if="filteredStageRuns.length === 0" :description="searchTerms.length ? '没有匹配的单阶段记录，试试其他关键词。' : '暂无单阶段记录。'" />
          <JobStatusCard
            v-for="item in filteredStageRuns"
            :key="item.id"
            :state="item"
            @deleted="load"
            @rerun="handleRerun"
          />
        </n-space>
      </n-tab-pane>
    </n-tabs>
  </div>
</template>

<style scoped>
.jobs-toolbar { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between; padding-bottom: 16px; border-bottom: 1px solid var(--border-subtle); }
.jobs-toolbar span { color: var(--text-muted); font-size: 12px; }
.job-list-view :deep(.status-card) { border: 0; border-bottom: 1px solid var(--border-subtle); border-radius: 0; }
.job-list-view :deep(.status-card > .n-card__content) { padding: 16px 0; }
</style>
