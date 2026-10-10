<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import {
  NAlert, NButton, NCard, NCheckbox, NEmpty, NForm, NFormItem, NInput,
  NInputNumber, NSelect, NSpace, NTag, useDialog, useMessage,
} from "naive-ui";
import {
  exportMemoryContext, getMemoryAnalysis, getMemoryExperiment, importMemoryAnalysis,
  importMemoryResponse, listExperimentMemories, listMemoryExperiments, memoryContextUrl,
  memoryRequestUrl, prepareMemoryExperiment, reviewMemory, startMemoryAnalysis, uploadMemorySource,
  type MemoryAnalysis, type MemoryContextResult, type MemoryEntry, type MemoryExperiment,
  type MemoryHistory, type MemoryMetadata, type MemoryRole, type MemoryStatus,
} from "../api/proofreadingMemory";

const message = useMessage();
const dialog = useDialog();
const busy = ref("");
const error = ref("");
const notice = ref("");
const history = ref<MemoryHistory[]>([]);
const current = ref<MemoryExperiment | null>(null);
const entries = ref<MemoryEntry[]>([]);
const analysis = ref<MemoryAnalysis | null>(null);
const responseJson = ref("");
const offlineMode = ref<"offline" | "replay">("offline");
const consent = ref(false);
const model = ref("");
const filter = ref<MemoryStatus | "all">("all");
const reviewer = ref("");
const reason = ref("");
const query = ref("");
const maxItems = ref<number | null>(8);
const maxChars = ref<number | null>(4000);
const frozen = ref<MemoryContextResult | null>(null);
const block = ref("");
const sources = reactive<Partial<Record<MemoryRole, { token: string; name: string }>>>({});
const form = reactive({ book: "", speaker: "", content: "reading" as "reading" | "conversation",
  dataset: null as MemoryMetadata["dataset_kind"] | null, referenceKind: "reference_text", referenceVersion: "unknown" });
const slots: { role: MemoryRole; label: string; accept: string }[] = [
  { role: "asr", label: "ASR 原稿", accept: ".txt,.md" },
  { role: "system", label: "系统校对稿", accept: ".txt,.md,.json" },
  { role: "human", label: "人工修改稿", accept: ".txt,.md" },
  { role: "reference", label: "参考 / 已有 OCR 文本（可选）", accept: ".txt,.md" },
];
const datasetOptions = [{ label: "真实人工修改", value: "real_human" }, { label: "构造实验（synthetic）", value: "synthetic" }];
const contentOptions = [{ label: "原文朗读", value: "reading" }, { label: "讲解 / 问答 / 对话", value: "conversation" }];
const statusLabels: Record<MemoryStatus, string> = { pending: "待审核", approved: "已批准", rejected: "已拒绝", disabled: "已停用" };
const statusOptions = [{ label: "全部状态", value: "all" }, ...Object.entries(statusLabels).map(([value, label]) => ({ value, label }))];
const kindLabels: Record<string, string> = { term: "术语 / 专名", correction_case: "纠错案例", reading_preference: "阅读感偏好", preservation_case: "保留案例" };
const running = computed(() => analysis.value?.status === "pending" || analysis.value?.status === "running");
const locked = computed(() => Boolean(busy.value) || running.value);
const visibleEntries = computed(() => entries.value.filter(e => filter.value === "all" || e.status === filter.value));
let timer: ReturnType<typeof setInterval> | undefined;
let disposed = false;
let selection = 0;
let fetchingAnalysis = false;

function stopPolling() { if (timer) clearInterval(timer); timer = undefined; }
function report(caught: unknown) { error.value = caught instanceof Error ? caught.message : "实验操作失败，请重试。"; }
async function perform(label: string, action: () => Promise<void>) {
  if (busy.value) return;
  busy.value = label; error.value = ""; notice.value = "";
  try { await action(); } catch (caught) { report(caught); } finally { busy.value = ""; }
}
async function refreshEntries() {
  if (!current.value) return;
  const id = current.value.id;
  const result = await listExperimentMemories(id);
  if (!disposed && current.value?.id === id) entries.value = result.items;
}
async function pollAnalysis() {
  if (!current.value || !analysis.value || fetchingAnalysis) return;
  const id = current.value.id; const analysisId = analysis.value.id;
  fetchingAnalysis = true;
  try {
    const result = await getMemoryAnalysis(id, analysisId);
    if (disposed || current.value?.id !== id || analysis.value?.id !== analysisId) return;
    analysis.value = result;
    if (result.status === "success" || result.status === "failed") stopPolling();
    if (result.status === "failed") error.value = result.error_message || "分析失败，没有候选自动入库。";
  } catch (caught) {
    if (!disposed && current.value?.id === id) { stopPolling(); report(caught); }
  } finally { fetchingAnalysis = false; }
}
async function activate(experiment: MemoryExperiment) {
  stopPolling(); selection += 1;
  current.value = experiment; entries.value = []; responseJson.value = ""; frozen.value = null; block.value = "";
  consent.value = false; query.value = experiment.request.sources.asr.text;
  analysis.value = experiment.analyses?.[0] ?? null;
  await refreshEntries();
  if (analysis.value) {
    await pollAnalysis();
    if (running.value) timer = setInterval(() => void pollAnalysis(), 1500);
  }
}
async function openExperiment(id: string) {
  await perform("读取实验", async () => {
    const version = ++selection;
    const result = await getMemoryExperiment(id);
    if (!disposed && selection === version) await activate(result);
  });
}
async function refreshHistory() {
  await perform("读取历史", async () => { history.value = (await listMemoryExperiments()).items; });
}
async function upload(role: MemoryRole, event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  delete sources[role];
  await perform("上传输入", async () => {
    if (file.size > 1024 * 1024 || file.size === 0) throw new Error("每份输入须为非空 UTF-8 文本，最多 1 MiB。");
    sources[role] = await uploadMemorySource(role, file);
  });
  input.value = "";
}
async function prepare() {
  await perform("准备分析", async () => {
    if (!form.book.trim() || !form.speaker.trim() || !form.dataset) throw new Error("请明确填写书籍、讲者和数据来源类型。");
    if (!sources.asr || !sources.system || !sources.human) throw new Error("请分别上传 ASR、系统稿和人工稿。");
    const metadata: MemoryMetadata = { dataset_kind: form.dataset, scope: {
      book_id: form.book.trim(), speaker_id: form.speaker.trim(), content_type: form.content,
    } };
    const inputs = Object.fromEntries(Object.entries(sources).map(([role, value]) => [role, value!.token]));
    const result = await prepareMemoryExperiment({ inputs, metadata,
      ...(sources.reference ? { reference_metadata: { source_kind: form.referenceKind,
        version: form.referenceVersion.trim() || "unknown", book_id: metadata.scope.book_id } } : {}),
    });
    await activate(result);
    history.value = (await listMemoryExperiments()).items;
    notice.value = "请求已保存。尚未联网，也没有候选自动入库。";
  });
}
async function analyze() {
  if (!current.value || !consent.value || running.value) return;
  await perform("提交分析", async () => {
    const result = await startMemoryAnalysis(current.value!.id, model.value.trim() || undefined);
    analysis.value = { id: result.analysis_id, status: "pending" };
    consent.value = false;
    await pollAnalysis();
    if (running.value) timer = setInterval(() => void pollAnalysis(), 1500);
  });
}
async function loadResponse(event: Event) {
  const input = event.target as HTMLInputElement; const file = input.files?.[0];
  if (!file) return;
  await perform("读取响应", async () => {
    if (file.size > 1024 * 1024) throw new Error("响应 JSON 最多 1 MiB。");
    responseJson.value = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
  });
  input.value = "";
}
async function importResponse(remote = false) {
  if (!current.value) return;
  await perform("保存候选", async () => {
    const result = remote && analysis.value ? await importMemoryAnalysis(current.value!.id, analysis.value.id)
      : await importMemoryResponse(current.value!.id, responseJson.value, offlineMode.value);
    await refreshEntries();
    notice.value = `已处理 ${result.candidate_count} 条候选：新记忆 ${result.inserted.length}，去重 ${result.duplicates.length}，新增未审核出处 ${result.occurrence_inserted.length}。未自动批准。`;
  });
}
function confirmReview(entry: MemoryEntry, action: "approve" | "reject" | "disable") {
  if (!current.value || locked.value) return;
  if (!reviewer.value.trim() || !reason.value.trim()) { error.value = "审核必须填写审核人和理由。"; return; }
  const id = current.value.id; const who = reviewer.value.trim(); const why = reason.value.trim();
  const label = { approve: "批准", reject: "拒绝", disable: "停用" }[action];
  dialog.warning({ title: `确认${label}此版本？`,
    content: `${entry.candidate.text}\n范围：${entry.candidate.scope.book_id} / ${entry.candidate.scope.speaker_id}。审核当前 v${entry.version}，不代表独立审核全部出现记录；不改旧冻结文件。`,
    positiveText: `确认${label}`, negativeText: "取消",
    onPositiveClick: async () => {
      let succeeded = false;
      await perform("审核记忆", async () => {
        await reviewMemory(id, entry, action, who, why);
        succeeded = true;
        await refreshEntries();
        notice.value = `记忆已${label}。历史冻结上下文保持不变。`;
        message.success(`已${label}`);
      });
      return succeeded;
    },
  });
}
async function exportContext() {
  if (!current.value) return;
  await perform("导出上下文", async () => {
    if (maxItems.value === null || maxChars.value === null) throw new Error("请填写完整的导出预算。");
    const result = await exportMemoryContext(current.value!.id, query.value, maxItems.value, maxChars.value);
    frozen.value = result; block.value = "";
    const response = await fetch(memoryContextUrl(current.value!.id, result.context_id, "txt"));
    if (!response.ok) throw new Error("上下文已冻结，但预览读取失败；可用下载链接读取。");
    block.value = await response.text();
    notice.value = `已冻结 ${result.context.selected.length} 条已批准记忆；不会自动交给阶段 6。`;
  });
}
function excerpt(role: "system" | "human", span: [number, number]) {
  // Python offsets count Unicode code points, not JavaScript UTF-16 code units.
  return Array.from(current.value?.request.sources[role].text ?? "").slice(span[0], span[1]).join("");
}
function historyLabel(item: MemoryHistory) {
  return `${item.metadata.scope.book_id || "未指定书籍"} / ${item.metadata.scope.speaker_id || "未指定讲者"} · ${item.metadata.dataset_kind} · ${item.id.slice(0, 8)}`;
}
onMounted(async () => {
  await perform("读取历史", async () => {
    history.value = (await listMemoryExperiments()).items;
    if (!disposed && history.value[0]) await activate(await getMemoryExperiment(history.value[0].id));
  });
});
onBeforeUnmount(() => { disposed = true; selection += 1; stopPolling(); });
</script>

<template>
  <div class="workbench-page memory-page" :aria-busy="locked">
    <div class="page-heading"><div><h2>校对记忆实验</h2><p>从人工修改中提出可复核经验。记忆仅作保真校对辅助，不替代原文或人工判断。</p></div><n-tag type="warning">独立实验 · 未接入阶段 6</n-tag></div>
    <n-alert type="info" :show-icon="false">默认离线。模型只提出候选；出现记录只是可追溯、未独立审核的佐证，不是自动审核或置信度学习。</n-alert>
    <n-alert v-if="error" type="error" title="操作未完成" role="alert">{{ error }}</n-alert>
    <n-alert v-if="notice" type="success" :show-icon="false" role="status">{{ notice }}</n-alert>
    <div class="workbench-columns">
      <section class="workspace-primary">
        <n-card class="view-card" title="1 · 准备三稿">
          <n-form label-placement="top">
            <div class="memory-fields">
              <n-form-item label="书籍标识"><n-input v-model:value="form.book" :input-props="{ 'aria-label': '书籍标识' }" placeholder="同一本书保持相同标识" :disabled="locked" /></n-form-item>
              <n-form-item label="讲者标识"><n-input v-model:value="form.speaker" :input-props="{ 'aria-label': '讲者标识' }" placeholder="未知时使用 unknown-speaker:任务ID" :disabled="locked" /></n-form-item>
              <n-form-item label="内容类型"><n-select v-model:value="form.content" :input-props="{ 'aria-label': '内容类型' }" :options="contentOptions" :disabled="locked" /></n-form-item>
              <n-form-item label="数据来源（必须明确）"><n-select v-model:value="form.dataset" :input-props="{ 'aria-label': '数据来源' }" :options="datasetOptions" placeholder="选择真实人工或构造实验" :disabled="locked" /></n-form-item>
            </div>
            <div v-for="slot in slots" :key="slot.role" class="memory-upload">
              <label :for="`memory-${slot.role}`">{{ slot.label }}</label>
              <input :id="`memory-${slot.role}`" type="file" :accept="slot.accept" :aria-label="slot.label" :disabled="locked" @change="upload(slot.role, $event)" />
              <small v-if="sources[slot.role]">已上传：{{ sources[slot.role]?.name }}</small>
              <n-button v-if="slot.role === 'reference' && sources.reference" size="small" :disabled="locked" @click="delete sources.reference">移除参考文本</n-button>
            </div>
            <div v-if="sources.reference" class="memory-fields">
              <n-form-item label="参考来源"><n-select v-model:value="form.referenceKind" :input-props="{ 'aria-label': '参考来源' }" :disabled="locked" :options="[{ label: '已有参考文本', value: 'reference_text' }, { label: '已有 OCR 文本（可能有误）', value: 'ocr_text' }]" /></n-form-item>
              <n-form-item label="参考版本"><n-input v-model:value="form.referenceVersion" :input-props="{ 'aria-label': '参考版本' }" :disabled="locked" /></n-form-item>
            </div>
            <p class="prompt-hint">三稿需各自上传，UTF-8、每份最多 1 MiB。系统 JSON 必须有顶层 final_markdown。长改动或全稿超预算会明确拒绝，请选用较小片段，不会偷偷截断。</p>
            <n-button type="primary" :loading="busy === '准备分析'" :disabled="locked" @click="prepare">准备分析请求（不联网）</n-button>
          </n-form>
        </n-card>
      </section>
      <section class="workspace-inspector">
        <n-card class="view-card" title="实验历史">
          <n-space vertical>
            <n-button size="small" :disabled="Boolean(busy)" @click="refreshHistory">刷新实验历史</n-button>
            <n-empty v-if="!history.length && !busy" description="尚无实验。先上传三稿并准备请求。" />
            <button v-for="item in history" :key="item.id" class="memory-history" :class="{ 'is-selected': current?.id === item.id }" :disabled="Boolean(busy)" @click="openExperiment(item.id)">{{ historyLabel(item) }}</button>
          </n-space>
        </n-card>
        <n-card v-if="current" class="view-card" title="2 · 当前请求与分析">
          <p class="memory-meta">当前保存范围：<strong>{{ current.request.metadata.scope.book_id }} / {{ current.request.metadata.scope.speaker_id }}</strong><br />{{ current.request.metadata.dataset_kind }} · {{ current.request.metadata.scope.content_type }}<br />请求 {{ current.request.request_id }}</p>
          <p>修改 {{ current.request.coverage.total_changes }} 处，选中 {{ current.request.coverage.selected_ids.length }} 个片段。{{ current.request.coverage.partial ? '本次为部分分析。' : '所有可用修改片段已选中。' }}</p>
          <p class="prompt-hint">保留样本仅取第一个相同区间；未修改不等于听音确认。左侧编辑属于新实验，不会改变当前请求。</p>
          <a :href="memoryRequestUrl(current.id)" download>下载自包含分析请求 JSON</a>
          <details class="memory-details"><summary>查看修改片段</summary>
            <article v-for="fragment in current.request.fragments" :key="fragment.fragment_id" class="memory-fragment">
              <strong>{{ fragment.fragment_id }} · {{ fragment.kind === 'change' ? '修改' : '未修改样本' }}</strong>
              <p class="prompt-hint">ASR 对齐：{{ fragment.asr_alignment.method }}（不是语义 / 音频对齐）</p>
              <p>系统稿</p><pre>{{ excerpt('system', fragment.system) }}</pre>
              <p>人工稿</p><pre>{{ excerpt('human', fragment.human) }}</pre>
            </article>
          </details>
          <div class="form-section">
            <h3 class="form-section-title">可选：联网模型分析</h3>
            <n-input v-model:value="model" :input-props="{ 'aria-label': '实验模型' }" placeholder="留空使用运行设置中的模型，仅作用本次" :disabled="locked" />
            <p class="prompt-hint">会发送选中的系统 / 人工稿窗口、ASR 和可选参考文本至已配置 Responses 后端；不会发送本地文件路径。此操作可能产生模型费用。</p>
            <n-checkbox v-model:checked="consent" :disabled="locked">我同意将上述文本发送至已配置的模型后端</n-checkbox>
            <n-space class="memory-actions">
              <n-button type="primary" :disabled="locked || !consent" :loading="running" @click="analyze">联网分析并提出候选</n-button>
              <n-button :disabled="Boolean(busy)" @click="openExperiment(current.id)">刷新当前实验</n-button>
            </n-space>
            <p v-if="running" role="status">正在后台分析；离开页面不会取消，也不会自动重试或入库。</p>
            <n-alert v-if="analysis?.status === 'failed'" type="error">{{ analysis.error_message }}</n-alert>
            <template v-if="analysis?.status === 'success'">
              <p>模型返回 {{ analysis.candidate_count }} 条候选，尚未自动保存。</p>
              <details class="memory-details"><summary>查看模型候选 JSON</summary><pre>{{ JSON.stringify(analysis.response, null, 2) }}</pre></details>
              <n-button :disabled="locked" @click="importResponse(true)">将模型候选保存为待审核</n-button>
            </template>
          </div>
          <details class="memory-details"><summary>离线：导入响应 JSON</summary>
            <p class="prompt-hint">响应必须匹配当前请求 ID，引用逐字合法；整批校验，不自动修复。回放只是工程验证，不是模型质量证据。</p>
            <label for="memory-response-file">从本机选择响应 JSON</label>
            <input id="memory-response-file" type="file" accept=".json" aria-label="离线响应 JSON" :disabled="locked" @change="loadResponse" />
            <n-input v-model:value="responseJson" type="textarea" :input-props="{ 'aria-label': '响应 JSON 内容' }" :autosize="{ minRows: 4, maxRows: 10 }" placeholder="粘贴完整响应 JSON 或选择文件" :disabled="locked" />
            <n-select v-model:value="offlineMode" :input-props="{ 'aria-label': '响应来源声明' }" :options="[{ label: '离线分析结果', value: 'offline' }, { label: '构造 / 回放响应', value: 'replay' }]" :disabled="locked" />
            <n-button :disabled="locked || !responseJson.trim()" @click="importResponse(false)">导入为待审核候选</n-button>
          </details>
        </n-card>
        <n-empty v-else description="准备请求后，可在这里检查修改、分析并保存候选。" />
      </section>
    </div>
    <section v-if="current" class="form-section">
      <n-card class="view-card" title="3 · 人工审核记忆">
        <p class="prompt-hint">仅列出当前已保存请求的书籍、讲者、内容类型及数据集范围。批准只审核当前版本；后续出现记录不会自动被批准。</p>
        <div class="memory-fields">
          <n-form-item label="审核人"><n-input v-model:value="reviewer" :input-props="{ 'aria-label': '审核人' }" :disabled="locked" /></n-form-item>
          <n-form-item label="审核理由"><n-input v-model:value="reason" :input-props="{ 'aria-label': '审核理由' }" placeholder="说明证据及适用限制" :disabled="locked" /></n-form-item>
        </div>
        <n-space><n-select v-model:value="filter" :input-props="{ 'aria-label': '记忆状态筛选' }" :options="statusOptions" style="width: 150px" /><n-button :disabled="locked" @click="perform('读取记忆', refreshEntries)">刷新记忆</n-button></n-space>
        <n-empty v-if="!visibleEntries.length" description="此范围 / 状态下暂无记忆；先分析或导入候选。" />
        <article v-for="entry in visibleEntries" :key="entry.memory_id" class="memory-entry">
          <n-space align="center"><strong>{{ kindLabels[entry.candidate.kind] || entry.candidate.kind }}</strong><n-tag :type="entry.status === 'approved' ? 'success' : entry.status === 'pending' ? 'warning' : 'default'">{{ statusLabels[entry.status] }} · v{{ entry.version }}</n-tag><span>{{ entry.occurrences.length }} 条未独立审核的出现记录</span></n-space>
          <p>{{ entry.candidate.text }}</p><p class="prompt-hint">提案理由：{{ entry.candidate.reason }}</p>
          <p v-if="entry.candidate.observed_form">观察词形：{{ entry.candidate.observed_form }} → {{ entry.candidate.preferred_form || '未指定' }}</p>
          <n-alert v-for="warning in entry.candidate.warnings" :key="warning" type="warning" :show-icon="false">{{ warning }}</n-alert>
          <details class="memory-details"><summary>查看当前版本证据与审核来源</summary>
            <p class="memory-meta">{{ entry.memory_id }} · 请求 {{ entry.candidate.request_id }}</p>
            <p v-if="entry.review.reviewer">审核人：{{ entry.review.reviewer }}；理由：{{ entry.review.reason }}</p>
            <div v-for="(evidence, index) in entry.candidate.evidence" :key="index" class="memory-evidence">
              <strong>{{ evidence.source_id }} · [{{ evidence.start }}, {{ evidence.end }})</strong>
              <pre>{{ evidence.excerpt }}</pre><p class="memory-meta">SHA256 {{ evidence.source_sha256 }}<br />PDF 物理页：{{ evidence.pdf_page?.join('–') || '未确定' }}</p>
            </div>
          </details>
          <details class="memory-details"><summary>查看未独立审核的提案出现记录（非置信度）</summary>
            <div v-for="occurrence in entry.occurrences" :key="occurrence.occurrence_id" class="memory-evidence">
              <p class="memory-meta">{{ occurrence.occurrence_id }}<br />{{ occurrence.request_id }} · {{ occurrence.response_mode }} · 未独立审核</p>
              <p>{{ occurrence.proposal.reason }}</p><pre>{{ JSON.stringify(occurrence.proposal.evidence, null, 2) }}</pre>
            </div>
          </details>
          <n-space class="memory-actions">
            <template v-if="entry.status === 'pending'"><n-button type="primary" :disabled="locked" @click="confirmReview(entry, 'approve')">批准此版本</n-button><n-button :disabled="locked" @click="confirmReview(entry, 'reject')">拒绝此版本</n-button></template>
            <n-button v-if="entry.status === 'approved'" :disabled="locked" @click="confirmReview(entry, 'disable')">停用此记忆</n-button>
          </n-space>
        </article>
      </n-card>
    </section>
    <section v-if="current" class="form-section">
      <n-card class="view-card" title="4 · 导出冻结上下文">
        <p class="prompt-hint">使用当前已保存请求的精确范围与数据集，不使用左侧未保存的新输入。默认取 ASR 作为检索文本，可替换为下一份待校对稿。仅选择已批准版本；超预算整条省略。</p>
        <n-input v-model:value="query" type="textarea" :input-props="{ 'aria-label': '上下文检索文本' }" :autosize="{ minRows: 3, maxRows: 8 }" :disabled="locked" />
        <div class="memory-fields memory-actions">
          <n-form-item label="最多条目"><n-input-number v-model:value="maxItems" :input-props="{ 'aria-label': '最多条目' }" :min="0" :max="50" :precision="0" :disabled="locked" /></n-form-item>
          <n-form-item label="完整模型块字符预算"><n-input-number v-model:value="maxChars" :input-props="{ 'aria-label': '完整模型块字符预算' }" :min="0" :max="40000" :precision="0" :disabled="locked" /></n-form-item>
        </div>
        <n-button type="primary" :disabled="locked" @click="exportContext">导出冻结上下文</n-button>
        <template v-if="frozen">
          <p role="status">选中 {{ frozen.context.selected.length }} 条；可用 {{ frozen.context.eligible_count }} 条；省略 {{ frozen.context.omitted.length }} 条；模型块 {{ frozen.context.block_chars }} 字符。</p>
          <p class="memory-meta">冻结指纹：{{ frozen.context.fingerprint }}</p>
          <p v-if="!frozen.context.selected.length">未选中任何记忆：请检查审核状态、词形匹配、精确范围及预算。</p>
          <p v-for="note in frozen.context.notes" :key="note">{{ note }}</p>
          <n-space><a :href="memoryContextUrl(current.id, frozen.context_id, 'json')" download>下载冻结 JSON</a><a :href="memoryContextUrl(current.id, frozen.context_id, 'txt')" download>下载模型数据块 TXT</a></n-space>
          <details class="memory-details"><summary>预览冻结模型数据块</summary><pre>{{ block || '空数据块：没有选中的已批准记忆。' }}</pre></details>
          <p class="prompt-hint">历史快照不会随停用 / 修订改变。下载仅用于受控实验，不会自动注入生产阶段 6。</p>
        </template>
      </n-card>
    </section>
  </div>
</template>

<style scoped>
.memory-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 16px; }
.memory-upload { display: grid; gap: 8px; padding-bottom: 20px; }
.memory-upload label { font-weight: 600; }
.memory-upload small, .memory-meta { color: var(--text-muted); overflow-wrap: anywhere; font-size: 12px; }
input[type=file] { width: 100%; min-width: 0; color: var(--text-secondary); }
input[type=file]::file-selector-button { padding: 8px 12px; margin-right: 8px; border: 1px solid var(--border-strong); border-radius: 8px; color: var(--text-primary); background: var(--surface-raised); cursor: pointer; }
.memory-history { width: 100%; padding: 10px 12px; border: 1px solid var(--border-subtle); background: var(--surface-canvas); color: var(--text-secondary); text-align: left; border-radius: 8px; overflow-wrap: anywhere; cursor: pointer; }
.memory-history.is-selected { border-color: var(--primary); color: var(--primary); }
.memory-details { margin: 16px 0; }
.memory-details > summary { cursor: pointer; color: var(--primary); padding: 6px 0; }
.memory-details > :not(summary) { margin-top: 12px; }
.memory-fragment, .memory-evidence { margin-top: 16px; padding: 12px; background: var(--surface-subtle); border-radius: 8px; }
.memory-entry { margin-top: 20px; padding: 20px 0; border-top: 1px solid var(--border-strong); overflow-wrap: anywhere; }
.memory-actions { margin-top: 16px; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; margin: 8px 0; padding: 12px; border-radius: 8px; background: var(--surface-subtle); font: 12px/1.7 ui-monospace, monospace; }
@media (max-width: 600px) { .memory-fields { grid-template-columns: minmax(0, 1fr); } }
</style>
