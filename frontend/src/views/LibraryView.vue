<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import { NAlert, NButton, NCheckbox, NInput, NSpace, NTag, useDialog, useMessage } from "naive-ui";
import LibraryBookDetail from "../components/LibraryBookDetail.vue";
import LibraryMaterialComposer from "../components/LibraryMaterialComposer.vue";
import { getFrontendSettings, type FrontendSettings } from "../api/client";
import { library, LibraryApiError, type HistoryCandidate, type ImportResult, type LibraryBook, type LibraryJob, type LibraryMaterial, type LibraryRange, type LibraryRevision, type OCRSettings } from "../api/library";

const message = useMessage();
const route = useRoute();
const dialog = useDialog();
const tab = ref<"books" | "materials">("books");
const books = ref<LibraryBook[]>([]);
const bookCount = ref(0);
const bookPage = ref(1);
const query = ref("");
const showArchived = ref(false);
const book = ref<LibraryBook | null>(null);
const bookDirty = ref(false);
const bookNames = ref<Record<string, string>>({});
const materials = ref<LibraryMaterial[]>([]);
const material = ref<LibraryMaterial | null>(null);
const name = ref("");
const ranges = ref<LibraryRange[]>([]);
const revisions = ref<LibraryRevision[]>([]);
const jobs = ref<LibraryJob[]>([]);
const candidates = ref<HistoryCandidate[] | null>(null);
const selectedCandidates = ref<string[]>([]);
const importResults = ref<ImportResult[] | null>(null);
const settings = ref<FrontendSettings | null>(null);
const uploadInput = ref<HTMLInputElement | null>(null);
const uploading = ref(false);
const working = ref(false);
const showComposer = ref(false);
const pageError = ref("");
const materialConflict = ref<LibraryMaterial | null>(null);
const draggedIndex = ref<number | null>(null);
let poll: number | null = null;

function errorText(error: unknown) { return error instanceof Error ? error.message : "操作失败"; }
function switchTab(next: "books" | "materials") {
  if (tab.value === "books" && next !== "books" && bookDirty.value && !window.confirm("当前有未保存的页面或资料修改，切换后将丢失。继续吗？")) return;
  if (next !== "books") bookDirty.value = false;
  tab.value = next;
}
function bookName(id: string) { return bookNames.value[id] || `书籍 ${id}`; }
function jobLabel(job: LibraryJob) { return job.kind === "import" ? "历史导入" : job.kind === "material" ? `材料生成${job.name ? ` · ${job.name}` : ""}` : `PDF 识别 · ${bookName(job.book_id ?? "")}`; }
function jobStatus(status: string) { return ({ pending: "等待开始", running: "进行中", partial: "部分完成", failed: "失败", interrupted: "已中断" } as Record<string, string>)[status] ?? status; }
function rememberBook(value: LibraryBook) { bookNames.value[value.id] = [value.title, value.edition].filter(Boolean).join(" · "); }
const isMaterialDirty = computed(() => !material.value || name.value !== material.value.name || JSON.stringify(ranges.value) !== JSON.stringify(material.value.ranges));
const selectedPages = computed(() => ranges.value.reduce((total, range) => total + range.end_page - range.start_page + 1, 0));
const repeatedPages = computed(() => {
  const counts = new Map<string, number>();
  for (const range of ranges.value) for (let page = range.start_page; page <= range.end_page; page++) {
    const key = `${range.book_id}:${page}`; counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return [...counts.values()].filter(count => count > 1).length;
});
const activeJobs = computed(() => jobs.value.filter(item => ["pending", "running", "partial", "failed", "interrupted"].includes(item.status)));
const deletingBook = computed(() => book.value && (books.value.find(item => item.id === book.value?.id && item.state === "deleting") ?? (book.value.state === "deleting" ? book.value : null)));
const deletingMaterial = computed(() => material.value && (materials.value.find(item => item.id === material.value?.id && item.state === "deleting") ?? (material.value.state === "deleting" ? material.value : null)));
const ocrSettings = computed<OCRSettings>(() => ({
  model: settings.value?.ocr_model || undefined,
  reasoning_effort: settings.value?.ocr_reasoning_effort || undefined,
  fast_mode: settings.value?.ocr_fast_mode ?? undefined,
  max_concurrency: settings.value?.ocr_max_concurrency,
  submit_interval_seconds: settings.value?.ocr_submit_interval_seconds,
}));

async function loadBooks() {
  try {
    const result = await library.listBooks(query.value, bookPage.value, showArchived.value);
    books.value = result.items; bookCount.value = result.total; result.items.forEach(rememberBook); pageError.value = "";
    const deleting = result.items.find(item => item.id === book.value?.id && item.state === "deleting");
    if (deleting && book.value) book.value = { ...book.value, state: "deleting", cleanup_error: deleting.cleanup_error };
  } catch (error) { pageError.value = errorText(error); }
}
async function loadMaterials() {
  try {
    materials.value = (await library.materials(showArchived.value)).items;
    const deleting = materials.value.find(item => item.id === material.value?.id && item.state === "deleting");
    if (deleting && material.value) material.value = { ...material.value, state: "deleting", cleanup_error: deleting.cleanup_error };
  }
  catch (error) { pageError.value = errorText(error); }
}
async function refreshJobs() {
  try {
    const oldActive = jobs.value.some(item => item.status === "running" || item.status === "pending");
    jobs.value = (await library.jobs()).items;
    if (oldActive && !jobs.value.some(item => item.status === "running" || item.status === "pending")) {
      await loadBooks();
      if (book.value) { const updated = await library.getBook(book.value.id); book.value = updated; rememberBook(updated); }
      if (material.value && !isMaterialDirty.value) await openMaterial(material.value.id, true);
    }
  } catch (error) { pageError.value = errorText(error); }
}
async function openBook(value: LibraryBook) {
  try { book.value = await library.getBook(value.id); rememberBook(book.value); tab.value = "books"; }
  catch (error) { message.error(errorText(error)); }
}
function updatedBook(value: LibraryBook) { book.value = value; rememberBook(value); void loadBooks(); }
function addRange(value: LibraryRange) { ranges.value.push(value); }
async function upload(event: Event) {
  const input = event.target as HTMLInputElement;
  const files = [...(input.files ?? [])]; if (!files.length) return;
  uploading.value = true;
  for (const file of files) {
    try { const result = await library.uploadBook(file); rememberBook(result); message.success(result.duplicate ? `${file.name} 已在书库中。` : `已入库：${result.title}`); }
    catch (error) { message.error(`${file.name}：${errorText(error)}`); }
  }
  input.value = ""; uploading.value = false; bookPage.value = 1; await loadBooks();
}
async function toggleBook(value: LibraryBook) {
  try { await library.archiveBook(value, !value.archived); if (book.value?.id === value.id) book.value = null; await loadBooks(); message.success(value.archived ? "书籍已恢复。" : "书籍已归档。"); }
  catch (error) { message.error(errorText(error)); }
}
function confirmPurge(kind: "book" | "material", id: string, label: string) {
  dialog.warning({ title: "永久清理", content: `将永久清理“${label}”。仍被引用的资料不能清理。`, positiveText: "继续", negativeText: "取消", onPositiveClick: () => {
    dialog.error({ title: "再次确认永久清理", content: "此操作不可撤销。确定继续吗？", positiveText: "确认清理", negativeText: "取消", onPositiveClick: async () => {
      try {
        if (kind === "book") { await library.purgeBook(id); if (book.value?.id === id) { book.value = null; bookDirty.value = false; } }
        else { await library.purgeMaterial(id); if (material.value?.id === id) { material.value = null; name.value = ""; ranges.value = []; revisions.value = []; } }
        await Promise.all([loadBooks(), loadMaterials()]); message.success("已清理。");
      }
      catch (error) { message.error(errorText(error)); }
    } });
  } });
}
async function loadCandidates() {
  try { candidates.value = (await library.candidates()).items; selectedCandidates.value = []; importResults.value = null; }
  catch (error) { message.error(errorText(error)); }
}
function clearPreviews() {
  dialog.warning({ title: "清理预览缓存", content: "仅删除可重新生成的页面预览，不会删除 PDF 原件或逐页文字。", positiveText: "清理缓存", negativeText: "取消", onPositiveClick: async () => {
    try { const result = await library.clearPreviews(); message.success(`已清理 ${result.removed_files} 张预览。`); }
    catch (error) { message.error(errorText(error)); }
  } });
}
function toggleCandidate(id: string, checked: boolean) {
  selectedCandidates.value = checked ? [...selectedCandidates.value, id] : selectedCandidates.value.filter(value => value !== id);
}
async function importSelected() {
  if (!selectedCandidates.value.length) return;
  working.value = true;
  try { importResults.value = (await library.importHistory(selectedCandidates.value)).items; selectedCandidates.value = []; await loadBooks(); message.success("历史导入已完成，请查看报告。"); }
  catch (error) { message.error(errorText(error)); }
  finally { working.value = false; }
}
function confirmOCR(requests: { book: LibraryBook; pages: number[] | null }[]) {
  const count = requests.reduce((sum, item) => sum + (item.pages?.length ?? Math.max(0, item.book.page_count - item.book.ready_pages)), 0);
  if (!count) { message.info("所选页面已经准备完成。"); return; }
  if (!settings.value) { message.error("无法读取当前 OCR 设置，请稍后重试。"); return; }
  const description = `需要识别 ${count} 页。模型：${settings.value.ocr_model}；最大并发：${settings.value.ocr_max_concurrency}；投递间隔：${settings.value.ocr_submit_interval_seconds} 秒。`;
  dialog.warning({ title: "确认启动 OCR", content: description, positiveText: "启动识别", negativeText: "取消", onPositiveClick: async () => {
    let submitted = 0;
    for (const item of requests) {
      try { await library.startOCR(item.book.id, item.pages, ocrSettings.value); submitted++; }
      catch (error) { message.error(`${item.book.title}：${errorText(error)}`); }
    }
    await refreshJobs(); if (submitted) message.info(`已提交 ${submitted} 个识别请求，完成的页会保留。`);
  } });
}
async function resume(job: LibraryJob) {
  dialog.warning({ title: "继续识别", content: "将沿用这次作业的模型和并发设置，只处理尚未完成的页面。", positiveText: "继续", negativeText: "取消", onPositiveClick: async () => {
    try { await library.resume(job.id); await refreshJobs(); } catch (error) { message.error(errorText(error)); }
  } });
}
function newMaterial() {
  if ((name.value || ranges.value.length) && isMaterialDirty.value && !window.confirm("当前材料有未保存的修改，确定新建？")) return;
  material.value = null; name.value = ""; ranges.value = []; revisions.value = []; materialConflict.value = null; tab.value = "materials";
}
async function openMaterial(id: string, force = false) {
  if (!force && (name.value || ranges.value.length) && isMaterialDirty.value && !window.confirm("当前材料有未保存的修改，确定切换？")) return;
  try {
    const current = await library.getMaterial(id);
    material.value = current; name.value = current.name; ranges.value = current.ranges.map(range => ({ ...range }));
    materialConflict.value = null; revisions.value = (await library.revisions(id)).items;
    for (const id of new Set(current.ranges.map(range => range.book_id))) {
      if (!bookNames.value[id]) { try { rememberBook(await library.getBook(id)); } catch { /* Archived or removed sources remain identified by ID. */ } }
    }
    tab.value = "materials";
  } catch (error) { message.error(errorText(error)); }
}
async function composerSelected(revision: LibraryRevision) {
  showComposer.value = false;
  await loadMaterials();
  if (bookDirty.value) {
    message.success("材料已生成。当前页面修改仍保留，可在“本次材料”中查看新版本。");
    return;
  }
  await openMaterial(revision.material_id);
}
async function saveMaterial(): Promise<LibraryMaterial | null> {
  if (!name.value.trim()) { message.warning("请填写材料名称。"); return null; }
  if (!ranges.value.length) { message.warning("请先从书籍详情加入至少一个页段。"); return null; }
  working.value = true;
  try {
    const saved = material.value
      ? await library.updateMaterial(material.value, name.value.trim(), ranges.value)
      : await library.createMaterial(name.value.trim(), ranges.value);
    material.value = saved; name.value = saved.name; ranges.value = saved.ranges.map(range => ({ ...range })); materialConflict.value = null;
    revisions.value = (await library.revisions(saved.id)).items; await loadMaterials(); message.success("材料草稿已保存。"); return saved;
  } catch (error) {
    if (error instanceof LibraryApiError && error.status === 409 && material.value) {
      try { materialConflict.value = await library.getMaterial(material.value.id); } catch (next) { message.error(errorText(next)); }
      message.warning("材料草稿已被其他人修改。当前输入仍保留，请先比较。");
    } else message.error(errorText(error));
    return null;
  } finally { working.value = false; }
}
function useLatestMaterialBase() {
  if (!materialConflict.value) return;
  material.value = materialConflict.value; materialConflict.value = null;
  message.info("已采用最新版本作为基准；你的名称和页段保持不变，请检查后再保存。");
}
async function publishMaterial() {
  let current = material.value;
  if (isMaterialDirty.value) current = await saveMaterial();
  if (!current) return;
  if (current.missing_pages) { message.warning(`还有 ${current.missing_pages} 页尚未准备，可先识别缺页。`); return; }
  working.value = true;
  try { await library.publish(current); revisions.value = (await library.revisions(current.id)).items; await loadMaterials(); message.success("固定材料版本已生成。"); }
  catch (error) {
    if (error instanceof LibraryApiError && error.status === 409) {
      try { materialConflict.value = await library.getMaterial(current.id); } catch (next) { message.error(errorText(next)); }
    }
    message.error(errorText(error));
  }
  finally { working.value = false; }
}
async function startMissing() {
  let current = material.value;
  if (isMaterialDirty.value) current = await saveMaterial();
  if (!current) return;
  const groups = new Map<string, number[]>();
  for (const item of current.missing_page_items) groups.set(item.book_id, [...(groups.get(item.book_id) ?? []), item.page_number]);
  try {
    const requests = await Promise.all([...groups].map(async ([id, pages]) => ({ book: await library.getBook(id), pages })));
    confirmOCR(requests);
  } catch (error) { message.error(errorText(error)); }
}
async function toggleMaterial(value: LibraryMaterial) {
  try { await library.archiveMaterial(value, !value.archived); if (material.value?.id === value.id) { material.value = null; name.value = ""; ranges.value = []; revisions.value = []; } await loadMaterials(); message.success(value.archived ? "材料已恢复。" : "材料已归档。"); }
  catch (error) { message.error(errorText(error)); }
}
function moveRange(from: number, to: number) {
  if (to < 0 || to >= ranges.value.length || from === to) return;
  const [item] = ranges.value.splice(from, 1); if (item) ranges.value.splice(to, 0, item);
}
function onDrop(index: number) { if (draggedIndex.value !== null) moveRange(draggedIndex.value, index); draggedIndex.value = null; }
async function switchArchived() { bookPage.value = 1; await Promise.all([loadBooks(), loadMaterials()]); }
onMounted(async () => {
  try { settings.value = await getFrontendSettings(); } catch (error) { pageError.value = errorText(error); }
  await Promise.all([loadBooks(), loadMaterials(), refreshJobs()]);
  if (route.query.import === "1") await loadCandidates();
  poll = window.setInterval(() => void refreshJobs(), 3000);
});
onBeforeUnmount(() => { if (poll !== null) window.clearInterval(poll); });
</script>

<template>
  <div class="workbench-page library-view">
    <section class="page-heading"><div><h2>PDF 书库</h2><p>保存书籍和逐页文字，按读书会需要组合参考材料。</p></div><n-button type="primary" @click="showComposer = true">选书组装材料</n-button></section>
    <n-alert v-if="pageError" type="error" :bordered="false">{{ pageError }}</n-alert>
    <div class="library-tabs" aria-label="书库视图">
      <n-button :type="tab === 'books' ? 'primary' : 'default'" @click="switchTab('books')">书籍</n-button>
      <n-button :type="tab === 'materials' ? 'primary' : 'default'" @click="switchTab('materials')">本次材料<span v-if="ranges.length"> · {{ ranges.length }} 段</span></n-button>
    </div>
    <section v-if="activeJobs.length" class="jobs-panel">
      <h3>处理任务</h3>
      <div v-for="job in activeJobs" :key="`${job.kind}-${job.id}`" class="job-row"><span>{{ jobLabel(job) }} · {{ jobStatus(job.status) }} · {{ job.completed }}/{{ job.total }}<span v-if="job.kind === 'ocr'"> 页</span><span v-if="job.failed_pages"> · 失败 {{ job.failed_pages }} 页</span></span>
        <n-button v-if="job.kind === 'ocr' && ['partial', 'failed', 'interrupted'].includes(job.status)" size="small" @click="resume(job)">继续 / 重试</n-button>
        <p v-if="job.kind !== 'ocr' && ['partial', 'failed', 'interrupted'].includes(job.status)">{{ job.kind === 'import' ? '请回到历史导入重新选择来源。' : '请回到材料重新生成版本。' }}</p>
        <p v-if="job.error" class="error-text">{{ job.error }}</p><p v-for="(error, number) in job.errors" :key="number" class="error-text">第 {{ number }} 页：{{ error }}</p>
      </div>
    </section>
    <template v-if="tab === 'books'">
      <div v-if="deletingBook">
        <n-alert type="warning" title="清理未完成" :bordered="false">{{ deletingBook.cleanup_error || '清理已中断，请重试永久清理。' }}</n-alert>
        <n-space><n-button @click="book = null; bookDirty = false">返回书库</n-button><n-button type="error" secondary @click="confirmPurge('book', deletingBook.id, deletingBook.title)">重试永久清理</n-button></n-space>
      </div>
      <LibraryBookDetail v-else-if="book" :key="book.id" :book="book" @close="book = null" @updated="updatedBook" @add-range="addRange" @dirty="bookDirty = $event" @start-o-c-r="(value, pages) => confirmOCR([{ book: value, pages }])" />
      <section v-else class="library-list">
        <div class="list-toolbar"><n-input v-model:value="query" placeholder="搜索书名、作者、版本或标签" clearable @keyup.enter="bookPage = 1; loadBooks()" /><n-button @click="bookPage = 1; loadBooks()">搜索</n-button>
          <input ref="uploadInput" type="file" accept=".pdf,application/pdf" multiple class="visually-hidden" @change="upload" />
          <n-button secondary :loading="uploading" @click="uploadInput?.click()">上传 PDF</n-button><n-button @click="loadCandidates">导入历史 OCR</n-button><n-button @click="clearPreviews">清理预览缓存</n-button>
          <n-checkbox v-model:checked="showArchived" @update:checked="switchArchived">查看归档</n-checkbox>
        </div>
        <n-alert type="info" :bordered="false">上传不会自动开始 OCR。相同 PDF 会提示已有书籍。</n-alert>
        <div v-if="candidates !== null" class="import-panel"><div class="section-heading"><h3>历史 OCR 候选</h3><n-button text @click="candidates = null">收起</n-button></div>
          <p>只导入勾选的来源；多份结果存在冲突时，请选择要使用的来源。</p>
          <p v-if="!candidates.length">没有可导入的历史资料。</p>
          <div v-for="item in candidates" :key="item.id" class="import-item"><n-checkbox :checked="selectedCandidates.includes(item.id)" @update:checked="checked => toggleCandidate(item.id, checked)">{{ item.title }} · {{ item.source_file }} · 可复用 {{ item.reusable_pages }}/{{ item.page_count }} 页</n-checkbox><small v-if="item.conflicting_pages?.length">与其他历史来源的第 {{ item.conflicting_pages.join('、') }} 页不一致，请选择来源。</small><small v-for="warning in item.warnings" :key="warning">{{ warning }}</small></div>
          <n-button type="primary" secondary :disabled="!selectedCandidates.length" :loading="working" @click="importSelected">导入所选</n-button>
          <div v-if="importResults"><h4>导入报告</h4><p v-for="(result, index) in importResults" :key="index">{{ result.status }} · 导入 {{ result.imported_pages }} 页 <span v-if="result.error">· {{ result.error }}</span><span v-for="warning in result.warnings" :key="warning"> · {{ warning }}</span></p></div>
        </div>
        <p v-if="!books.length">{{ showArchived ? '没有归档书籍。' : '书库还没有书。上传 PDF 或从历史任务导入。' }}</p>
        <div v-for="item in books" :key="item.id" class="book-row"><div><strong v-if="item.state === 'deleting'">{{ item.title }}</strong><button v-else class="book-title" @click="openBook(item)">{{ item.title }}</button><p v-if="item.state === 'deleting'" class="cleanup-error">清理未完成：{{ item.cleanup_error || '清理已中断，请重试永久清理。' }}</p><template v-else><p>{{ [item.author, item.edition, ...item.tags].filter(Boolean).join(' · ') || '未填写作者或版本' }}</p><p>PDF {{ item.page_count }} 页 · 已识别 {{ item.recognized_pages }} 页 · 文字已准备 {{ item.ready_pages }} 页 · 人工修订过 {{ item.edited_pages }} 页<span v-if="item.failed_pages"> · 失败 {{ item.failed_pages }} 页</span> · 更新 {{ item.updated_at }}</p></template></div>
          <n-space v-if="item.state === 'deleting'" wrap><n-button size="small" type="error" secondary @click="confirmPurge('book', item.id, item.title)">重试永久清理</n-button></n-space>
          <n-space v-else wrap><n-button size="small" @click="openBook(item)">查看</n-button><n-button size="small" @click="toggleBook(item)">{{ item.archived ? '恢复' : '归档' }}</n-button><n-button v-if="item.archived" size="small" type="error" text @click="confirmPurge('book', item.id, item.title)">永久清理</n-button></n-space>
        </div>
        <div class="pagination"><n-button size="small" :disabled="bookPage <= 1" @click="bookPage--; loadBooks()">上一页</n-button><span>第 {{ bookPage }} 页 · 共 {{ bookCount }} 本</span><n-button size="small" :disabled="bookPage * 20 >= bookCount" @click="bookPage++; loadBooks()">下一页</n-button></div>
      </section>
    </template>
    <section v-else class="materials-layout">
      <aside class="material-list"><div class="section-heading"><h3>材料</h3><n-button size="small" @click="newMaterial">新建</n-button></div>
        <n-checkbox v-model:checked="showArchived" @update:checked="switchArchived">查看归档</n-checkbox>
        <p v-if="!materials.length">暂无{{ showArchived ? '归档' : '已保存' }}材料。</p>
        <div v-for="item in materials" :key="item.id" class="material-row"><strong v-if="item.state === 'deleting'">{{ item.name }}</strong><button v-else class="book-title" @click="openMaterial(item.id)">{{ item.name }}</button><small v-if="item.state === 'deleting'" class="cleanup-error">清理未完成：{{ item.cleanup_error || '清理已中断，请重试永久清理。' }}</small><small v-else>{{ item.page_count }} 页 · 已准备 {{ item.ready_pages }} 页</small><n-space v-if="item.state === 'deleting'" wrap><n-button size="tiny" type="error" secondary @click="confirmPurge('material', item.id, item.name)">重试永久清理</n-button></n-space><n-space v-else wrap><n-button size="tiny" @click="toggleMaterial(item)">{{ item.archived ? '恢复' : '归档' }}</n-button><n-button v-if="item.archived" size="tiny" type="error" text @click="confirmPurge('material', item.id, item.name)">永久清理</n-button></n-space></div>
      </aside>
      <n-alert v-if="deletingMaterial" type="warning" title="材料清理未完成" :bordered="false">{{ deletingMaterial.cleanup_error || '清理已中断，请重试永久清理。' }}</n-alert>
      <div v-else class="material-editor"><div class="section-heading"><h3>{{ material ? '材料草稿' : '本次材料' }}</h3><n-tag v-if="isMaterialDirty" size="small">未保存</n-tag></div>
        <label>材料名称<n-input v-model:value="name" placeholder="例如：第十二次读书会" /></label>
        <p>已选 {{ selectedPages }} 页<span v-if="material && !isMaterialDirty"> · 已准备 {{ material.ready_pages }} 页 · 缺少 {{ material.missing_pages }} 页</span></p>
        <n-alert v-if="repeatedPages" type="warning" :bordered="false">有 {{ repeatedPages }} 个物理页被重复选择；顺序和重复都会保留。</n-alert>
        <p v-if="!ranges.length">从书籍详情选择 PDF 物理页段，加入本次材料。</p>
        <ol class="ranges"><li v-for="(range, index) in ranges" :key="`${index}-${range.book_id}-${range.start_page}-${range.end_page}`" draggable="true" @dragstart="draggedIndex = index" @dragover.prevent @drop.prevent="onDrop(index)" @dragend="draggedIndex = null"><span class="drag-handle" aria-hidden="true">⠿</span><span>{{ bookName(range.book_id) }} · PDF 第 {{ range.start_page }}—{{ range.end_page }} 页</span><n-space><n-button size="tiny" :disabled="index === 0" :aria-label="`上移第 ${index + 1} 段`" @click="moveRange(index, index - 1)">上移</n-button><n-button size="tiny" :disabled="index === ranges.length - 1" :aria-label="`下移第 ${index + 1} 段`" @click="moveRange(index, index + 1)">下移</n-button><n-button size="tiny" text type="error" :aria-label="`移除第 ${index + 1} 段`" @click="ranges.splice(index, 1)">移除</n-button></n-space></li></ol>
        <n-space wrap><n-button type="primary" :loading="working" @click="saveMaterial">保存材料</n-button><n-button secondary :disabled="!ranges.length" @click="startMissing">识别缺页</n-button><n-button secondary :disabled="!ranges.length" :loading="working" @click="publishMaterial">生成固定版本</n-button><n-button @click="tab = 'books'">继续选书</n-button></n-space>
        <n-alert v-if="materialConflict" type="warning" title="材料草稿已被其他人修改" :bordered="false">你的输入仍保留。最新草稿：{{ materialConflict.name }}，{{ materialConflict.ranges.length }} 段、{{ materialConflict.page_count }} 页。<pre>{{ JSON.stringify(materialConflict.ranges, null, 2) }}</pre><n-button size="small" @click="useLatestMaterialBase">以最新版本为基准继续编辑</n-button></n-alert>
        <div v-if="material" class="versions"><h4>固定版本</h4><p v-if="!revisions.length">尚无固定版本；文字准备完整后可生成。</p>
          <div v-for="version in revisions" :key="version.id" class="version-row"><div><strong>第 {{ version.revision }} 版</strong> · {{ version.page_count }} 页 · {{ version.created_at }} <n-tag v-if="version.has_updates" size="small" type="warning">页文字有更新</n-tag><span v-if="version.status !== 'ready'"> · {{ version.status }} {{ version.error }}</span></div><n-space v-if="version.status === 'ready'" wrap><a :href="library.revisionUrl(version.id, 'pdf')" target="_blank">PDF</a><a :href="library.revisionUrl(version.id, 'txt')" target="_blank">TXT</a><a :href="library.revisionUrl(version.id, 'sources')" target="_blank">来源清单</a></n-space></div>
          <p v-if="revisions.some(v => v.has_updates)" class="muted">页文字已更新；选择“生成固定版本”使用最新文字，旧版本保持不变。</p>
        </div>
      </div>
    </section>
    <LibraryMaterialComposer v-model:show="showComposer" purpose="standalone" @saved="loadMaterials" @selected="composerSelected" />
  </div>
</template>

<style scoped>
.library-view { max-width:1500px; }.library-tabs,.list-toolbar,.section-heading,.pagination { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }.section-heading { justify-content:space-between; }.section-heading h3,.jobs-panel h3 { margin:0; }.list-toolbar .n-input { flex:1 1 240px; }.library-list,.material-editor,.material-list { display:grid; gap:16px; align-content:start; min-width:0; }
.book-row,.material-row,.version-row,.job-row { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; padding:14px 0; border-top:1px solid var(--border-subtle); flex-wrap:wrap; }.book-row p { margin:5px 0 0; color:var(--text-secondary); }.book-title { border:0; padding:0; color:var(--text-primary); background:none; cursor:pointer; text-align:left; font:inherit; font-weight:650; }.book-title:hover { color:var(--accent-color, #4f46e5); text-decoration:underline; }.pagination { justify-content:center; color:var(--text-secondary); }.visually-hidden { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0,0,0,0); }
.jobs-panel,.import-panel { padding:16px; background:var(--surface-subtle); border-radius:8px; }.job-row { padding:8px 0; }.job-row p { flex-basis:100%; margin:0; }.error-text,.cleanup-error { color:var(--color-error, #c03434) !important; }.import-item { display:grid; gap:4px; padding:8px 0; }.import-item small,.muted,.material-row small { color:var(--text-secondary); }.import-panel p { margin:8px 0; }
.materials-layout { display:grid; grid-template-columns:minmax(190px, 260px) minmax(0,1fr); gap:24px; }.material-list { border-right:1px solid var(--border-subtle); padding-right:20px; }.material-row { display:grid; gap:8px; }.material-editor label { display:grid; gap:6px; }.material-editor p { margin:0; color:var(--text-secondary); }.ranges { margin:0; padding-left:25px; }.ranges li { padding:10px 0; border-top:1px solid var(--border-subtle); }.ranges li > span:nth-child(2) { display:inline-block; margin-right:12px; overflow-wrap:anywhere; }.ranges .n-space { display:inline-flex; }.drag-handle { cursor:grab; margin-right:8px; color:var(--text-muted); }.versions { display:grid; gap:8px; border-top:1px solid var(--border-subtle); padding-top:16px; }.versions h4 { margin:0; }.version-row a { color:var(--accent-color, #4f46e5); }
@media(max-width:700px) { .materials-layout { grid-template-columns:1fr; }.material-list { border-right:0; border-bottom:1px solid var(--border-subtle); padding:0 0 16px; }.ranges li .n-space { display:flex; margin-top:8px; }.list-toolbar .n-input { flex-basis:100%; } }
</style>
