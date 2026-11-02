<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { onBeforeRouteLeave, useRoute } from "vue-router";
import {
  NAlert,
  NButton,
  NCheckbox,
  NInput,
  NSpace,
  NTag,
  useDialog,
  useMessage,
} from "naive-ui";
import LibraryBookDetail from "../components/LibraryBookDetail.vue";
import LibraryMaterialComposer from "../components/LibraryMaterialComposer.vue";
import { getFrontendSettings, type FrontendSettings } from "../api/client";
import {
  library,
  LibraryApiError,
  type HistoryCandidate,
  type ImportResult,
  type LibraryBook,
  type LibraryJob,
  type LibraryMaterial,
  type LibraryRange,
  type LibraryRevision,
  type OCRSettings,
} from "../api/library";

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

const viewMode = ref<"grid" | "list">(
  (typeof localStorage !== "undefined" &&
    (localStorage.getItem("transcript_library_view_mode") as "grid" | "list")) ||
    "grid"
);

const coverErrors = ref<Record<string, boolean>>({});

function handleCoverError(bookId: string) {
  coverErrors.value[bookId] = true;
}

function switchViewMode(mode: "grid" | "list") {
  viewMode.value = mode;
  try {
    localStorage.setItem("transcript_library_view_mode", mode);
  } catch {}
}

let poll: number | null = null;

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败";
}

function switchTab(next: "books" | "materials") {
  if (
    tab.value === "books" &&
    next !== "books" &&
    bookDirty.value &&
    !window.confirm("当前有未保存的页面或资料修改，切换后将丢失。继续吗？")
  ) {
    return;
  }
  if (next !== "books") {
    bookDirty.value = false;
  }
  tab.value = next;
}

function bookName(id: string): string {
  return bookNames.value[id] || `书籍 ${id}`;
}

function jobLabel(job: LibraryJob): string {
  if (job.kind === "import") {
    return "历史导入";
  }
  if (job.kind === "material") {
    return `材料生成${job.name ? ` · ${job.name}` : ""}`;
  }
  return `PDF 识别 · ${bookName(job.book_id ?? "")}`;
}

function jobStatus(status: string): string {
  const map: Record<string, string> = {
    pending: "等待开始",
    running: "进行中",
    partial: "部分完成",
    failed: "失败",
    interrupted: "已中断",
  };
  return map[status] ?? status;
}

function rememberBook(value: LibraryBook) {
  bookNames.value[value.id] = [value.title, value.edition].filter(Boolean).join(" · ");
}

const isMaterialDirty = computed(() => {
  if (!material.value) return Boolean(name.value.trim() || ranges.value.length);
  return (
    name.value !== material.value.name ||
    JSON.stringify(ranges.value) !== JSON.stringify(material.value.ranges)
  );
});

const hasBookChanges = computed(() => Boolean(book.value && bookDirty.value));
const hasMaterialChanges = computed(() => Boolean(isMaterialDirty.value));

const hasUnsavedChanges = computed(
  () => hasBookChanges.value || hasMaterialChanges.value
);

function getUnsavedPrompt(): string {
  if (hasBookChanges.value && hasMaterialChanges.value) {
    return "当前有未保存的书籍修改和材料草稿，离开后将丢失。确定要离开吗？";
  }
  if (hasBookChanges.value) {
    return "当前有未保存的页面或书籍修改，离开后将丢失。确定要离开吗？";
  }
  if (hasMaterialChanges.value) {
    return "当前材料草稿有未保存的修改，离开后将丢失。确定要离开吗？";
  }
  return "当前有未保存的修改，离开后将丢失。确定要离开吗？";
}

onBeforeRouteLeave(() => {
  if (hasUnsavedChanges.value) {
    if (!window.confirm(getUnsavedPrompt())) {
      return false;
    }
  }
});

function handleBeforeUnload(event: BeforeUnloadEvent) {
  if (hasUnsavedChanges.value) {
    event.preventDefault();
    event.returnValue = "";
    return "";
  }
}

const selectedPages = computed(() =>
  ranges.value.reduce((total, range) => total + range.end_page - range.start_page + 1, 0)
);

const repeatedPages = computed(() => {
  const counts = new Map<string, number>();
  for (const range of ranges.value) {
    for (let page = range.start_page; page <= range.end_page; page++) {
      const key = `${range.book_id}:${page}`;
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
  }
  return [...counts.values()].filter((count) => count > 1).length;
});

const activeJobs = computed(() =>
  jobs.value.filter((item) =>
    ["pending", "running", "partial", "failed", "interrupted"].includes(item.status)
  )
);

const deletingBook = computed(() => {
  if (!book.value) return null;
  return (
    books.value.find((item) => item.id === book.value?.id && item.state === "deleting") ??
    (book.value.state === "deleting" ? book.value : null)
  );
});

const deletingMaterial = computed(() => {
  if (!material.value) return null;
  return (
    materials.value.find((item) => item.id === material.value?.id && item.state === "deleting") ??
    (material.value.state === "deleting" ? material.value : null)
  );
});

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
    books.value = result.items;
    bookCount.value = result.total;
    result.items.forEach(rememberBook);
    pageError.value = "";

    const deleting = result.items.find((item) => item.id === book.value?.id && item.state === "deleting");
    if (deleting && book.value) {
      book.value = { ...book.value, state: "deleting", cleanup_error: deleting.cleanup_error };
    }
  } catch (error) {
    pageError.value = errorText(error);
  }
}

async function loadMaterials() {
  try {
    materials.value = (await library.materials(showArchived.value)).items;
    const deleting = materials.value.find((item) => item.id === material.value?.id && item.state === "deleting");
    if (deleting && material.value) {
      material.value = { ...material.value, state: "deleting", cleanup_error: deleting.cleanup_error };
    }
  } catch (error) {
    pageError.value = errorText(error);
  }
}

async function refreshJobs() {
  try {
    const oldActive = jobs.value.some((item) => item.status === "running" || item.status === "pending");
    jobs.value = (await library.jobs()).items;
    const newActive = jobs.value.some((item) => item.status === "running" || item.status === "pending");

    if (oldActive && !newActive) {
      await loadBooks();
      if (book.value) {
        const updated = await library.getBook(book.value.id);
        book.value = updated;
        rememberBook(updated);
      }
      if (material.value && !isMaterialDirty.value) {
        await openMaterial(material.value.id, true);
      }
    }
  } catch (error) {
    // Only set pageError if no error is already displayed
    if (!pageError.value) {
      pageError.value = errorText(error);
    }
  }
}

async function openBook(value: LibraryBook) {
  try {
    book.value = await library.getBook(value.id);
    rememberBook(book.value);
    tab.value = "books";
  } catch (error) {
    message.error(errorText(error));
  }
}

function updatedBook(value: LibraryBook) {
  book.value = value;
  rememberBook(value);
  void loadBooks();
}

function addRange(value: LibraryRange) {
  ranges.value.push(value);
}

async function upload(event: Event) {
  const input = event.target as HTMLInputElement;
  const files = [...(input.files ?? [])];
  if (!files.length) return;

  uploading.value = true;
  for (const file of files) {
    try {
      const result = await library.uploadBook(file);
      rememberBook(result);
      message.success(
        result.duplicate ? `${file.name} 已在书库中。` : `已入库：${result.title}`
      );
    } catch (error) {
      message.error(`${file.name}：${errorText(error)}`);
    }
  }
  input.value = "";
  uploading.value = false;
  bookPage.value = 1;
  await loadBooks();
}

async function toggleBook(value: LibraryBook) {
  try {
    await library.archiveBook(value, !value.archived);
    if (book.value?.id === value.id) {
      book.value = null;
    }
    await loadBooks();
    message.success(value.archived ? "书籍已恢复。" : "书籍已归档。");
  } catch (error) {
    message.error(errorText(error));
  }
}

function confirmPurge(kind: "book" | "material", id: string, label: string) {
  dialog.warning({
    title: "永久清理",
    content: `将永久清理“${label}”。仍被引用的资料不能清理。`,
    positiveText: "继续",
    negativeText: "取消",
    onPositiveClick: () => {
      dialog.error({
        title: "再次确认永久清理",
        content: "此操作不可撤销。确定继续吗？",
        positiveText: "确认清理",
        negativeText: "取消",
        onPositiveClick: async () => {
          try {
            if (kind === "book") {
              await library.purgeBook(id);
              if (book.value?.id === id) {
                book.value = null;
                bookDirty.value = false;
              }
            } else {
              await library.purgeMaterial(id);
              if (material.value?.id === id) {
                material.value = null;
                name.value = "";
                ranges.value = [];
                revisions.value = [];
              }
            }
            await Promise.all([loadBooks(), loadMaterials()]);
            message.success("已清理。");
          } catch (error) {
            message.error(errorText(error));
          }
        },
      });
    },
  });
}

async function loadCandidates() {
  try {
    candidates.value = (await library.candidates()).items;
    selectedCandidates.value = [];
    importResults.value = null;
  } catch (error) {
    message.error(errorText(error));
  }
}

function clearPreviews() {
  dialog.warning({
    title: "清理预览缓存",
    content: "仅删除可重新生成的页面预览，不会删除 PDF 原件或逐页文字。",
    positiveText: "清理缓存",
    negativeText: "取消",
    onPositiveClick: async () => {
      try {
        const result = await library.clearPreviews();
        coverErrors.value = {};
        message.success(`已清理 ${result.removed_files} 张预览。`);
      } catch (error) {
        message.error(errorText(error));
      }
    },
  });
}

function toggleCandidate(id: string, checked: boolean) {
  selectedCandidates.value = checked
    ? [...selectedCandidates.value, id]
    : selectedCandidates.value.filter((value) => value !== id);
}

async function importSelected() {
  if (!selectedCandidates.value.length) return;
  working.value = true;
  try {
    importResults.value = (await library.importHistory(selectedCandidates.value)).items;
    selectedCandidates.value = [];
    await loadBooks();
    message.success("历史导入已完成，请查看报告。");
  } catch (error) {
    message.error(errorText(error));
  } finally {
    working.value = false;
  }
}

async function confirmOCR(requests: { book: LibraryBook; pages: number[] | null }[]) {
  const count = requests.reduce(
    (sum, item) => sum + (item.pages?.length ?? Math.max(0, item.book.page_count - item.book.ready_pages)),
    0
  );
  if (!count) {
    message.info("所选页面已经准备完成。");
    return;
  }

  if (!settings.value) {
    try {
      settings.value = await getFrontendSettings();
    } catch {
      message.error("无法读取当前 OCR 设置，请稍后重试。");
      return;
    }
  }

  const description = `需要识别 ${count} 页。模型：${settings.value.ocr_model}；最大并发：${settings.value.ocr_max_concurrency}；投递间隔：${settings.value.ocr_submit_interval_seconds} 秒。`;
  dialog.warning({
    title: "确认启动 OCR",
    content: description,
    positiveText: "启动识别",
    negativeText: "取消",
    onPositiveClick: async () => {
      let submitted = 0;
      for (const item of requests) {
        try {
          await library.startOCR(item.book.id, item.pages, ocrSettings.value);
          submitted++;
        } catch (error) {
          message.error(`${item.book.title}：${errorText(error)}`);
        }
      }
      await refreshJobs();
      if (submitted) {
        message.info(`已提交 ${submitted} 个识别请求，完成的页会保留。`);
      }
    },
  });
}

async function resume(job: LibraryJob) {
  dialog.warning({
    title: "继续识别",
    content: "将沿用这次作业的模型和并发设置，只处理尚未完成的页面。",
    positiveText: "继续",
    negativeText: "取消",
    onPositiveClick: async () => {
      try {
        await library.resume(job.id);
        await refreshJobs();
      } catch (error) {
        message.error(errorText(error));
      }
    },
  });
}

function newMaterial() {
  if (
    (name.value || ranges.value.length) &&
    isMaterialDirty.value &&
    !window.confirm("当前材料有未保存的修改，确定新建？")
  ) {
    return;
  }
  material.value = null;
  name.value = "";
  ranges.value = [];
  revisions.value = [];
  materialConflict.value = null;
  tab.value = "materials";
}

async function openMaterial(id: string, force = false) {
  if (
    !force &&
    (name.value || ranges.value.length) &&
    isMaterialDirty.value &&
    !window.confirm("当前材料有未保存的修改，确定切换？")
  ) {
    return;
  }
  try {
    const current = await library.getMaterial(id);
    material.value = current;
    name.value = current.name;
    ranges.value = current.ranges.map((range) => ({ ...range }));
    materialConflict.value = null;
    revisions.value = (await library.revisions(id)).items;

    for (const bookId of new Set(current.ranges.map((range) => range.book_id))) {
      if (!bookNames.value[bookId]) {
        try {
          rememberBook(await library.getBook(bookId));
        } catch {
          // Archived or removed sources remain identified by ID
        }
      }
    }
    tab.value = "materials";
  } catch (error) {
    message.error(errorText(error));
  }
}

async function composerSelected(revision: LibraryRevision) {
  showComposer.value = false;
  await loadMaterials();
  if (bookDirty.value) {
    message.success("材料已生成。当前页面修改仍保留，可在“参考材料”中查看新版本。");
    return;
  }
  await openMaterial(revision.material_id);
}

async function saveMaterial(): Promise<LibraryMaterial | null> {
  if (!name.value.trim()) {
    message.warning("请填写材料名称。");
    return null;
  }
  if (!ranges.value.length) {
    message.warning("请先从书籍详情加入至少一个页段。");
    return null;
  }
  working.value = true;
  try {
    const saved = material.value
      ? await library.updateMaterial(material.value, name.value.trim(), ranges.value)
      : await library.createMaterial(name.value.trim(), ranges.value);

    material.value = saved;
    name.value = saved.name;
    ranges.value = saved.ranges.map((range) => ({ ...range }));
    materialConflict.value = null;
    revisions.value = (await library.revisions(saved.id)).items;
    await loadMaterials();
    message.success("材料草稿已保存。");
    return saved;
  } catch (error) {
    if (error instanceof LibraryApiError && error.status === 409 && material.value) {
      try {
        materialConflict.value = await library.getMaterial(material.value.id);
      } catch (next) {
        message.error(errorText(next));
      }
      message.warning("材料草稿已被其他人修改。当前输入仍保留，请先比较。");
    } else {
      message.error(errorText(error));
    }
    return null;
  } finally {
    working.value = false;
  }
}

function useLatestMaterialBase() {
  if (!materialConflict.value) return;
  material.value = materialConflict.value;
  materialConflict.value = null;
  message.info("已采用最新版本作为基准；你的名称和页段保持不变，请检查后再保存。");
}

async function publishMaterial() {
  let current = material.value;
  if (isMaterialDirty.value) {
    current = await saveMaterial();
  }
  if (!current) return;

  if (current.missing_pages) {
    message.warning(`还有 ${current.missing_pages} 页尚未准备，可先识别缺页。`);
    return;
  }

  working.value = true;
  try {
    await library.publish(current);
    revisions.value = (await library.revisions(current.id)).items;
    await loadMaterials();
    message.success("固定材料版本已生成。");
  } catch (error) {
    if (error instanceof LibraryApiError && error.status === 409) {
      try {
        materialConflict.value = await library.getMaterial(current.id);
      } catch (next) {
        message.error(errorText(next));
      }
    }
    message.error(errorText(error));
  } finally {
    working.value = false;
  }
}

async function startMissing() {
  let current = material.value;
  if (isMaterialDirty.value) {
    current = await saveMaterial();
  }
  if (!current) return;

  const groups = new Map<string, number[]>();
  for (const item of current.missing_page_items) {
    groups.set(item.book_id, [...(groups.get(item.book_id) ?? []), item.page_number]);
  }

  try {
    const requests = await Promise.all(
      [...groups].map(async ([id, pages]) => ({
        book: await library.getBook(id),
        pages,
      }))
    );
    await confirmOCR(requests);
  } catch (error) {
    message.error(errorText(error));
  }
}

async function toggleMaterial(value: LibraryMaterial) {
  try {
    await library.archiveMaterial(value, !value.archived);
    if (material.value?.id === value.id) {
      material.value = null;
      name.value = "";
      ranges.value = [];
      revisions.value = [];
    }
    await loadMaterials();
    message.success(value.archived ? "材料已恢复。" : "材料已归档。");
  } catch (error) {
    message.error(errorText(error));
  }
}

function moveRange(from: number, to: number) {
  if (to < 0 || to >= ranges.value.length || from === to) return;
  const [item] = ranges.value.splice(from, 1);
  if (item) {
    ranges.value.splice(to, 0, item);
  }
}

function onDrop(index: number) {
  if (draggedIndex.value !== null) {
    moveRange(draggedIndex.value, index);
  }
  draggedIndex.value = null;
}

async function switchArchived() {
  bookPage.value = 1;
  await Promise.all([loadBooks(), loadMaterials()]);
}

onMounted(async () => {
  window.addEventListener("beforeunload", handleBeforeUnload);
  try {
    settings.value = await getFrontendSettings();
  } catch (error) {
    pageError.value = errorText(error);
  }

  await Promise.all([loadBooks(), loadMaterials(), refreshJobs()]);

  if (route.query.import === "1") {
    await loadCandidates();
  }

  poll = window.setInterval(() => void refreshJobs(), 3000);
});

onBeforeUnmount(() => {
  window.removeEventListener("beforeunload", handleBeforeUnload);
  if (poll !== null) {
    window.clearInterval(poll);
  }
});
</script>

<template>
  <div class="workbench-page library-view">
    <section class="page-heading">
      <div>
        <h2>PDF 书库</h2>
        <p>保存书籍和逐页文字，按读书会需要组合参考材料。</p>
      </div>
      <n-button type="primary" aria-label="选书组装材料" @click="showComposer = true">
        组合参考材料
      </n-button>
    </section>

    <n-alert v-if="pageError" type="error" :bordered="false">{{ pageError }}</n-alert>

    <div class="library-tabs" aria-label="书库视图">
      <n-button
        :type="tab === 'books' ? 'primary' : 'default'"
        @click="switchTab('books')"
      >
        书籍
      </n-button>
      <n-button
        :type="tab === 'materials' ? 'primary' : 'default'"
        aria-label="参考材料 (本次材料)"
        @click="switchTab('materials')"
      >
        参考材料<span v-if="ranges.length"> · {{ ranges.length }} 段</span>
      </n-button>
    </div>

    <!-- Active Jobs Panel -->
    <section v-if="activeJobs.length" class="jobs-panel">
      <h3>处理任务</h3>
      <div
        v-for="job in activeJobs"
        :key="`${job.kind}-${job.id}`"
        class="job-row"
      >
        <span>
          {{ jobLabel(job) }} · {{ jobStatus(job.status) }} · {{ job.completed }}/{{ job.total }}<span v-if="job.kind === 'ocr'"> 页</span><span v-if="job.failed_pages"> · 失败 {{ job.failed_pages }} 页</span>
        </span>
        <n-button
          v-if="job.kind === 'ocr' && ['partial', 'failed', 'interrupted'].includes(job.status)"
          size="small"
          @click="resume(job)"
        >
          继续 / 重试
        </n-button>
        <p v-if="job.kind !== 'ocr' && ['partial', 'failed', 'interrupted'].includes(job.status)">
          {{ job.kind === 'import' ? '请回到历史导入重新选择来源。' : '请回到材料重新生成版本。' }}
        </p>
        <p v-if="job.error" class="error-text">{{ job.error }}</p>
        <p
          v-for="(error, number) in job.errors"
          :key="number"
          class="error-text"
        >
          第 {{ number }} 页：{{ error }}
        </p>
      </div>
    </section>

    <!-- Books View -->
    <template v-if="tab === 'books'">
      <div v-if="deletingBook" class="deleting-notice">
        <n-alert type="warning" title="清理未完成" :bordered="false">
          {{ deletingBook.cleanup_error || '清理已中断，请重试永久清理。' }}
        </n-alert>
        <n-space>
          <n-button @click="book = null; bookDirty = false">返回书库</n-button>
          <n-button
            type="error"
            secondary
            @click="confirmPurge('book', deletingBook.id, deletingBook.title)"
          >
            重试永久清理
          </n-button>
        </n-space>
      </div>

      <LibraryBookDetail
        v-else-if="book"
        :key="book.id"
        :book="book"
        @close="book = null"
        @updated="updatedBook"
        @add-range="addRange"
        @dirty="bookDirty = $event"
        @start-o-c-r="(value, pages) => confirmOCR([{ book: value, pages }])"
      />

      <section v-else class="library-list">
        <div class="list-toolbar">
          <n-input
            v-model:value="query"
            placeholder="搜索书名、作者、版本或标签"
            clearable
            @keyup.enter="bookPage = 1; loadBooks()"
          />
          <n-button @click="bookPage = 1; loadBooks()">搜索</n-button>
          <input
            ref="uploadInput"
            type="file"
            accept=".pdf,application/pdf"
            multiple
            class="visually-hidden"
            @change="upload"
          />
          <n-button secondary :loading="uploading" @click="uploadInput?.click()">
            上传 PDF
          </n-button>
          <n-button @click="loadCandidates">导入历史 OCR</n-button>
          <n-button @click="clearPreviews">清理预览缓存</n-button>
          <n-checkbox v-model:checked="showArchived" @update:checked="switchArchived">
            查看归档
          </n-checkbox>
          <div class="view-mode-group" role="group" aria-label="布局模式">
            <n-button
              size="small"
              :type="viewMode === 'grid' ? 'primary' : 'default'"
              :secondary="viewMode !== 'grid'"
              @click="switchViewMode('grid')"
            >
              网格封面
            </n-button>
            <n-button
              size="small"
              :type="viewMode === 'list' ? 'primary' : 'default'"
              :secondary="viewMode !== 'list'"
              title="电脑双列 / 手机自适应单列"
              @click="switchViewMode('list')"
            >
              <span class="desktop-text">双列列表</span>
              <span class="mobile-text">详细列表</span>
            </n-button>
          </div>
        </div>

        <n-alert type="info" :bordered="false">
          上传不会自动开始 OCR。相同 PDF 会提示已有书籍。
        </n-alert>

        <!-- Import History Panel -->
        <div v-if="candidates !== null" class="import-panel">
          <div class="section-heading">
            <h3>历史 OCR 候选</h3>
            <n-button text @click="candidates = null">收起</n-button>
          </div>
          <p>只导入勾选的来源；多份结果存在冲突时，请选择要使用的来源。</p>
          <p v-if="!candidates.length" class="muted">没有可导入的历史资料。</p>
          <div
            v-for="item in candidates"
            :key="item.id"
            class="import-item"
          >
            <n-checkbox
              :checked="selectedCandidates.includes(item.id)"
              @update:checked="(checked) => toggleCandidate(item.id, checked)"
            >
              {{ item.title }} · {{ item.source_file }} · 可复用 {{ item.reusable_pages }}/{{ item.page_count }} 页
            </n-checkbox>
            <small v-if="item.conflicting_pages?.length">
              与其他历史来源的第 {{ item.conflicting_pages.join('、') }} 页不一致，请选择来源。
            </small>
            <small v-for="warning in item.warnings" :key="warning">{{ warning }}</small>
          </div>
          <n-button
            type="primary"
            secondary
            :disabled="!selectedCandidates.length"
            :loading="working"
            @click="importSelected"
          >
            导入所选
          </n-button>

          <div v-if="importResults" class="import-results">
            <h4>导入报告</h4>
            <p v-for="(result, index) in importResults" :key="index">
              {{ result.status }} · 导入 {{ result.imported_pages }} 页
              <span v-if="result.error">· {{ result.error }}</span>
              <span v-for="warning in result.warnings" :key="warning"> · {{ warning }}</span>
            </p>
          </div>
        </div>

        <p v-if="!books.length" class="empty-books-note">
          {{ showArchived ? '没有归档书籍。' : '书库还没有书。上传 PDF 或从历史任务导入。' }}
        </p>

        <div
          v-else
          class="books-container"
          :class="viewMode === 'grid' ? 'mode-grid' : 'mode-list'"
        >
          <!-- Grid Mode (网格封面: PDF 第一页预览) -->
          <template v-if="viewMode === 'grid'">
            <div
              v-for="item in books"
              :key="item.id"
              class="book-row book-card book-card--grid"
            >
              <div
                class="book-cover-wrap"
                :class="{ 'is-clickable': item.state !== 'deleting' }"
                @click="item.state !== 'deleting' && openBook(item)"
              >
                <img
                  v-if="!coverErrors[item.id]"
                  :src="library.previewUrl(item.id, 1)"
                  :alt="item.title"
                  loading="lazy"
                  class="book-cover-img"
                  @error="handleCoverError(item.id)"
                />
                <div v-else class="book-cover-fallback">
                  <span class="fallback-icon">📄</span>
                  <span class="fallback-text">封面预览</span>
                </div>

                <div class="book-cover-badges">
                  <span v-if="item.state === 'deleting'" class="badge badge-error">清理中</span>
                  <span
                    v-else-if="item.ready_pages >= item.page_count && item.page_count > 0"
                    class="badge badge-success"
                  >
                    已就绪 {{ item.page_count }}页
                  </span>
                  <span
                    v-else-if="item.ready_pages > 0"
                    class="badge badge-info"
                  >
                    {{ item.ready_pages }}/{{ item.page_count }}页
                  </span>
                  <span v-else class="badge badge-neutral">待识别</span>
                </div>
              </div>

              <div class="book-card-info">
                <strong v-if="item.state === 'deleting'" class="book-title-static">{{ item.title }}</strong>
                <button
                  v-else
                  class="book-title"
                  :title="item.title"
                  @click="openBook(item)"
                >
                  {{ item.title }}
                </button>

                <p v-if="item.state === 'deleting'" class="cleanup-error">
                  清理未完成：{{ item.cleanup_error || '清理已中断，请重试永久清理。' }}
                </p>
                <template v-else>
                  <p
                    class="book-meta"
                    :title="[item.author, item.edition, ...item.tags].filter(Boolean).join(' · ')"
                  >
                    {{ [item.author, item.edition, ...item.tags].filter(Boolean).join(' · ') || '未填写作者或版本' }}
                  </p>
                  <p class="book-stats">
                    PDF {{ item.page_count }} 页
                    <span v-if="item.recognized_pages"> · 已识 {{ item.recognized_pages }}</span>
                    <span v-if="item.ready_pages"> · 就绪 {{ item.ready_pages }}</span>
                    <span v-if="item.failed_pages" class="cleanup-error"> · 失败 {{ item.failed_pages }} 页</span>
                  </p>
                </template>
              </div>

              <div class="book-card-actions">
                <n-space v-if="item.state === 'deleting'" wrap>
                  <n-button
                    size="small"
                    type="error"
                    secondary
                    @click="confirmPurge('book', item.id, item.title)"
                  >
                    重试永久清理
                  </n-button>
                </n-space>
                <n-space v-else wrap>
                  <n-button size="small" @click="openBook(item)">查看</n-button>
                  <n-button size="small" @click="toggleBook(item)">
                    {{ item.archived ? '恢复' : '归档' }}
                  </n-button>
                  <n-button
                    v-if="item.archived"
                    size="small"
                    type="error"
                    text
                    @click="confirmPurge('book', item.id, item.title)"
                  >
                    永久清理
                  </n-button>
                </n-space>
              </div>
            </div>
          </template>

          <!-- List Mode (双列列表) -->
          <template v-else>
            <div
              v-for="item in books"
              :key="item.id"
              class="book-row book-card book-card--list"
            >
              <div
                class="book-thumb-wrap"
                :class="{ 'is-clickable': item.state !== 'deleting' }"
                @click="item.state !== 'deleting' && openBook(item)"
              >
                <img
                  v-if="!coverErrors[item.id]"
                  :src="library.previewUrl(item.id, 1)"
                  :alt="item.title"
                  loading="lazy"
                  class="book-thumb-img"
                  @error="handleCoverError(item.id)"
                />
                <div v-else class="book-thumb-fallback">📄</div>
              </div>

              <div class="book-list-main">
                <div class="book-card-info">
                  <strong v-if="item.state === 'deleting'" class="book-title-static">{{ item.title }}</strong>
                  <button
                    v-else
                    class="book-title"
                    :title="item.title"
                    @click="openBook(item)"
                  >
                    {{ item.title }}
                  </button>

                  <p v-if="item.state === 'deleting'" class="cleanup-error">
                    清理未完成：{{ item.cleanup_error || '清理已中断，请重试永久清理。' }}
                  </p>
                  <template v-else>
                    <p class="book-meta">
                      {{ [item.author, item.edition, ...item.tags].filter(Boolean).join(' · ') || '未填写作者或版本' }}
                    </p>
                    <p class="book-stats">
                      PDF {{ item.page_count }} 页 · 已识别 {{ item.recognized_pages }} 页 · 文字已准备 {{ item.ready_pages }} 页
                      <span v-if="item.edited_pages"> · 修订 {{ item.edited_pages }} 页</span>
                      <span v-if="item.failed_pages" class="cleanup-error"> · 失败 {{ item.failed_pages }} 页</span>
                      <span class="updated-time"> · 更新 {{ item.updated_at }}</span>
                    </p>
                  </template>
                </div>

                <div class="book-card-actions">
                  <n-space v-if="item.state === 'deleting'" wrap>
                    <n-button
                      size="small"
                      type="error"
                      secondary
                      @click="confirmPurge('book', item.id, item.title)"
                    >
                      重试永久清理
                    </n-button>
                  </n-space>
                  <n-space v-else wrap>
                    <n-button size="small" @click="openBook(item)">查看</n-button>
                    <n-button size="small" @click="toggleBook(item)">
                      {{ item.archived ? '恢复' : '归档' }}
                    </n-button>
                    <n-button
                      v-if="item.archived"
                      size="small"
                      type="error"
                      text
                      @click="confirmPurge('book', item.id, item.title)"
                    >
                      永久清理
                    </n-button>
                  </n-space>
                </div>
              </div>
            </div>
          </template>
        </div>

        <div class="pagination">
          <n-button
            size="small"
            :disabled="bookPage <= 1"
            @click="bookPage--; loadBooks()"
          >
            上一页
          </n-button>
          <span>第 {{ bookPage }} 页 · 共 {{ bookCount }} 本</span>
          <n-button
            size="small"
            :disabled="bookPage * 20 >= bookCount"
            @click="bookPage++; loadBooks()"
          >
            下一页
          </n-button>
        </div>
      </section>
    </template>

    <!-- Materials View -->
    <section v-else class="materials-layout">
      <aside class="material-list">
        <div class="section-heading">
          <h3>材料</h3>
          <n-button size="small" @click="newMaterial">新建</n-button>
        </div>

        <n-checkbox v-model:checked="showArchived" @update:checked="switchArchived">
          查看归档
        </n-checkbox>

        <p v-if="!materials.length" class="muted">
          暂无{{ showArchived ? '归档' : '已保存' }}材料。
        </p>

        <div
          v-for="item in materials"
          :key="item.id"
          class="material-row"
        >
          <strong v-if="item.state === 'deleting'">{{ item.name }}</strong>
          <button v-else class="book-title" @click="openMaterial(item.id)">
            {{ item.name }}
          </button>
          <small v-if="item.state === 'deleting'" class="cleanup-error">
            清理未完成：{{ item.cleanup_error || '清理已中断，请重试永久清理。' }}
          </small>
          <small v-else>{{ item.page_count }} 页 · 已准备 {{ item.ready_pages }} 页</small>

          <n-space v-if="item.state === 'deleting'" wrap>
            <n-button
              size="tiny"
              type="error"
              secondary
              @click="confirmPurge('material', item.id, item.name)"
            >
              重试永久清理
            </n-button>
          </n-space>
          <n-space v-else wrap>
            <n-button size="tiny" @click="toggleMaterial(item)">
              {{ item.archived ? '恢复' : '归档' }}
            </n-button>
            <n-button
              v-if="item.archived"
              size="tiny"
              type="error"
              text
              @click="confirmPurge('material', item.id, item.name)"
            >
              永久清理
            </n-button>
          </n-space>
        </div>
      </aside>

      <n-alert
        v-if="deletingMaterial"
        type="warning"
        title="材料清理未完成"
        :bordered="false"
      >
        {{ deletingMaterial.cleanup_error || '清理已中断，请重试永久清理。' }}
      </n-alert>

      <div v-else class="material-editor">
        <div class="section-heading">
          <h3>{{ material ? '材料草稿' : '参考材料' }}</h3>
          <n-tag v-if="isMaterialDirty" size="small" type="warning" :bordered="false">
            未保存
          </n-tag>
        </div>

        <label>
          材料名称
          <n-input v-model:value="name" placeholder="例如：第十二次读书会" />
        </label>

        <p>
          已选 {{ selectedPages }} 页
          <span v-if="material && !isMaterialDirty">
            · 已准备 {{ material.ready_pages }} 页 · 缺少 {{ material.missing_pages }} 页
          </span>
        </p>

        <n-alert v-if="repeatedPages" type="warning" :bordered="false">
          有 {{ repeatedPages }} 个物理页被重复选择；顺序和重复都会保留。
        </n-alert>

        <p v-if="!ranges.length" class="muted">
          从书籍详情选择 PDF 物理页段，加入参考材料。
        </p>

        <ol class="ranges">
          <li
            v-for="(range, index) in ranges"
            :key="`${index}-${range.book_id}-${range.start_page}-${range.end_page}`"
            draggable="true"
            @dragstart="draggedIndex = index"
            @dragover.prevent
            @drop.prevent="onDrop(index)"
            @dragend="draggedIndex = null"
          >
            <span class="drag-handle" aria-hidden="true">⠿</span>
            <span>{{ bookName(range.book_id) }} · PDF 第 {{ range.start_page }}—{{ range.end_page }} 页</span>
            <n-space>
              <n-button
                size="tiny"
                :disabled="index === 0"
                :aria-label="`上移第 ${index + 1} 段`"
                @click="moveRange(index, index - 1)"
              >
                上移
              </n-button>
              <n-button
                size="tiny"
                :disabled="index === ranges.length - 1"
                :aria-label="`下移第 ${index + 1} 段`"
                @click="moveRange(index, index + 1)"
              >
                下移
              </n-button>
              <n-button
                size="tiny"
                text
                type="error"
                :aria-label="`移除第 ${index + 1} 段`"
                @click="ranges.splice(index, 1)"
              >
                移除
              </n-button>
            </n-space>
          </li>
        </ol>

        <n-space wrap>
          <n-button type="primary" :loading="working" @click="saveMaterial">
            保存材料
          </n-button>
          <n-button secondary :disabled="!ranges.length" @click="startMissing">
            识别缺页
          </n-button>
          <n-button
            secondary
            :disabled="!ranges.length"
            :loading="working"
            @click="publishMaterial"
          >
            生成固定版本
          </n-button>
          <n-button @click="tab = 'books'">继续选书</n-button>
        </n-space>

        <n-alert
          v-if="materialConflict"
          type="warning"
          title="材料草稿已被其他人修改"
          :bordered="false"
        >
          你的输入仍保留。最新草稿：{{ materialConflict.name }}，{{ materialConflict.ranges.length }} 段、{{ materialConflict.page_count }} 页。
          <pre class="conflict-preview">{{ JSON.stringify(materialConflict.ranges, null, 2) }}</pre>
          <n-button size="small" @click="useLatestMaterialBase">
            以最新版本为基准继续编辑
          </n-button>
        </n-alert>

        <div v-if="material" class="versions">
          <h4>固定版本</h4>
          <p v-if="!revisions.length" class="muted">
            尚无固定版本；文字准备完整后可生成。
          </p>
          <div
            v-for="version in revisions"
            :key="version.id"
            class="version-row"
          >
            <div>
              <strong>第 {{ version.revision }} 版</strong> · {{ version.page_count }} 页 · {{ version.created_at }}
              <n-tag v-if="version.has_updates" size="small" type="warning" :bordered="false">
                页文字有更新
              </n-tag>
              <span v-if="version.status !== 'ready'"> · {{ version.status }} {{ version.error }}</span>
            </div>
            <n-space v-if="version.status === 'ready'" wrap>
              <a :href="library.revisionUrl(version.id, 'pdf')" target="_blank" rel="noopener">PDF</a>
              <a :href="library.revisionUrl(version.id, 'txt')" target="_blank" rel="noopener">TXT</a>
              <a :href="library.revisionUrl(version.id, 'sources')" target="_blank" rel="noopener">来源清单</a>
            </n-space>
          </div>
          <p v-if="revisions.some((v) => v.has_updates)" class="muted">
            页文字已更新；选择“生成固定版本”使用最新文字，旧版本保持不变。
          </p>
        </div>
      </div>
    </section>

    <LibraryMaterialComposer
      v-model:show="showComposer"
      purpose="standalone"
      @saved="loadMaterials"
      @selected="composerSelected"
    />
  </div>
</template>

<style scoped>
.library-view {
  max-width: 1500px;
}

.library-tabs,
.list-toolbar,
.section-heading,
.pagination {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.section-heading {
  justify-content: space-between;
}

.section-heading h3,
.jobs-panel h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
}

.list-toolbar .n-input {
  flex: 1 1 240px;
}

.library-list,
.material-editor,
.material-list {
  display: grid;
  gap: 16px;
  align-content: start;
  min-width: 0;
}

.view-mode-group {
  display: inline-flex;
  gap: 4px;
  margin-left: auto;
}

.mobile-text {
  display: none;
}

.desktop-text {
  display: inline;
}

.books-container.mode-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 18px;
  align-items: stretch;
}

.books-container.mode-list {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  align-items: stretch;
}

/* Card base overrides */
.book-card {
  border-top: none !important;
  padding: 0 !important;
  background: var(--surface-raised);
  border: 1px solid var(--border-subtle);
  border-radius: 10px;
  color: var(--text-primary);
  transition: transform var(--motion-fast) var(--ease-out),
              box-shadow var(--motion-fast) var(--ease-out),
              border-color var(--motion-fast) var(--ease-out);
  box-sizing: border-box;
}

.book-card:hover {
  border-color: var(--primary);
  box-shadow: 0 8px 20px rgba(0, 0, 0, 0.08);
}

:global(.dark-mode) .book-card:hover {
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
}

/* Grid card layout */
.book-card--grid {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.book-card--grid .book-cover-wrap {
  position: relative;
  width: 100%;
  aspect-ratio: 1 / 1.36;
  background: var(--surface-subtle);
  border-bottom: 1px solid var(--border-subtle);
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}

.book-card--grid .book-cover-wrap.is-clickable {
  cursor: pointer;
}

.book-cover-img {
  width: 100%;
  height: 100%;
  object-fit: contain;
  background: transparent;
  transition: transform var(--motion-fast) var(--ease-out);
}

.book-card--grid:hover .book-cover-img {
  transform: scale(1.02);
}

.book-cover-fallback {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  color: var(--text-muted);
}

.fallback-icon {
  font-size: 32px;
}

.fallback-text {
  font-size: 12px;
}

.book-cover-badges {
  position: absolute;
  top: 8px;
  right: 8px;
  display: flex;
  gap: 4px;
}

.badge {
  font-size: 11px;
  font-weight: 500;
  padding: 2px 7px;
  border-radius: 999px;
  line-height: 1.3;
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
}

.badge-success {
  background: var(--color-success-bg, rgba(16, 185, 129, 0.16));
  color: var(--color-success, #059669);
  border: 1px solid rgba(16, 185, 129, 0.3);
}

.badge-info {
  background: var(--color-info-bg, var(--primary-alpha-10));
  color: var(--primary);
  border: 1px solid var(--primary-alpha-20);
}

.badge-neutral {
  background: var(--surface-subtle);
  color: var(--text-secondary);
  border: 1px solid var(--border-subtle);
}

.badge-error {
  background: var(--color-error-bg, rgba(239, 68, 68, 0.16));
  color: var(--color-error, #dc2626);
  border: 1px solid rgba(239, 68, 68, 0.3);
}

.book-card--grid .book-card-info {
  padding: 12px 14px 6px;
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

.book-card--grid .book-title {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  font-size: 14px;
  font-weight: 600;
  line-height: 1.35;
  text-align: left;
}

.book-title-static {
  font-size: 14px;
  font-weight: 600;
  line-height: 1.35;
  color: var(--text-primary);
}

.book-card--grid .book-meta {
  margin: 0 !important;
  font-size: 12px;
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.book-card--grid .book-stats {
  margin: 0 !important;
  font-size: 11px;
  color: var(--text-muted);
}

.book-card--grid .book-card-actions {
  padding: 6px 14px 12px;
  display: flex;
  align-items: center;
  gap: 8px;
}

/* List card layout (2 columns) */
.book-card--list {
  display: flex !important;
  align-items: flex-start;
  gap: 14px;
  padding: 14px !important;
}

.book-thumb-wrap {
  width: 66px;
  height: 90px;
  flex-shrink: 0;
  border-radius: 6px;
  overflow: hidden;
  background: var(--surface-subtle);
  border: 1px solid var(--border-subtle);
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.08);
}

.book-thumb-wrap.is-clickable {
  cursor: pointer;
}

.book-thumb-img {
  width: 100%;
  height: 100%;
  object-fit: contain;
  background: transparent;
}

.book-thumb-fallback {
  font-size: 26px;
  color: var(--text-muted);
}

.book-list-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 8px;
}

.book-card--list .book-card-info {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.book-card--list .book-title {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  font-size: 14px;
  font-weight: 600;
  line-height: 1.35;
  text-align: left;
}

.book-card--list .book-meta {
  margin: 0 !important;
  font-size: 12px;
  color: var(--text-secondary);
}

.book-card--list .book-stats {
  margin: 0 !important;
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.4;
}

.updated-time {
  color: var(--text-muted);
}

.book-card--list .book-card-actions {
  display: flex;
  align-items: center;
}

.book-row,
.material-row,
.version-row,
.job-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 0;
  border-top: 1px solid var(--border-subtle);
  flex-wrap: wrap;
}

.book-row p {
  margin: 5px 0 0;
  color: var(--text-secondary);
}

.book-title {
  border: 0;
  padding: 0;
  color: var(--text-primary);
  background: none;
  cursor: pointer;
  text-align: left;
  font: inherit;
  font-weight: 650;
  transition: color var(--motion-fast) var(--ease-out);
}

.book-title:hover {
  color: var(--accent-color, var(--primary));
  text-decoration: underline;
}

.pagination {
  justify-content: center;
  color: var(--text-secondary);
  padding: 12px 0;
}

.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
}

.jobs-panel,
.import-panel {
  padding: 16px;
  background: var(--surface-subtle);
  border-radius: 8px;
}

.job-row {
  padding: 8px 0;
}

.job-row p {
  flex-basis: 100%;
  margin: 0;
}

.error-text,
.cleanup-error {
  color: var(--color-error, #c03434) !important;
}

.import-item {
  display: grid;
  gap: 4px;
  padding: 8px 0;
}

.import-item small,
.muted,
.material-row small {
  color: var(--text-secondary);
}

.import-panel p {
  margin: 8px 0;
}

.import-results {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border-subtle);
}

.import-results h4 {
  margin: 0 0 8px;
}

.materials-layout {
  display: grid;
  grid-template-columns: minmax(190px, 260px) minmax(0, 1fr);
  gap: 24px;
}

.material-list {
  border-right: 1px solid var(--border-subtle);
  padding-right: 20px;
}

.material-row {
  display: grid;
  gap: 8px;
}

.material-editor label {
  display: grid;
  gap: 6px;
  font-size: 13px;
  color: var(--text-primary);
}

.material-editor p {
  margin: 0;
  color: var(--text-secondary);
}

.ranges {
  margin: 0;
  padding-left: 25px;
}

.ranges li {
  padding: 10px 0;
  border-top: 1px solid var(--border-subtle);
}

.ranges li > span:nth-child(2) {
  display: inline-block;
  margin-right: 12px;
  overflow-wrap: anywhere;
}

.ranges .n-space {
  display: inline-flex;
}

.drag-handle {
  cursor: grab;
  margin-right: 8px;
  color: var(--text-muted);
}

.versions {
  display: grid;
  gap: 8px;
  border-top: 1px solid var(--border-subtle);
  padding-top: 16px;
}

.versions h4 {
  margin: 0;
  font-size: 15px;
}

.version-row a {
  color: var(--accent-color, var(--primary));
}

.conflict-preview {
  margin: 8px 0;
  padding: 8px;
  background: var(--surface-canvas);
  border-radius: 4px;
  font-size: 12px;
  max-height: 180px;
  overflow: auto;
}

.deleting-notice {
  display: grid;
  gap: 12px;
}

.empty-books-note {
  color: var(--text-muted);
  padding: 16px 0;
}

@media (max-width: 860px) {
  .books-container.mode-list {
    grid-template-columns: 1fr;
    gap: 12px;
  }

  .desktop-text {
    display: none;
  }

  .mobile-text {
    display: inline;
  }
}

@media (max-width: 700px) {
  .materials-layout {
    grid-template-columns: 1fr;
  }

  .material-list {
    border-right: 0;
    border-bottom: 1px solid var(--border-subtle);
    padding: 0 0 16px;
  }

  .ranges li .n-space {
    display: flex;
    margin-top: 8px;
  }

  .list-toolbar .n-input {
    flex-basis: 100%;
  }

  .view-mode-group {
    margin-left: 0;
  }

  .book-card--list {
    padding: 12px !important;
    gap: 12px;
  }

  .book-thumb-wrap {
    width: 60px;
    height: 82px;
  }

  .book-card--list .book-title {
    font-size: 14px;
    line-height: 1.35;
    -webkit-line-clamp: 2;
  }

  .book-card--list .book-meta,
  .book-card--list .book-stats {
    font-size: 11px;
    line-height: 1.4;
  }
}

@media (max-width: 600px) {
  .books-container.mode-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
  }

  .book-card--grid .book-cover-wrap {
    aspect-ratio: 1 / 1.25;
    max-height: 175px;
  }

  .book-card--grid .book-card-info {
    padding: 8px 10px 4px;
    gap: 3px;
  }

  .book-card--grid .book-title {
    font-size: 13px;
    line-height: 1.3;
    -webkit-line-clamp: 2;
  }

  .book-card--grid .book-meta {
    font-size: 11px;
  }

  .book-card--grid .book-stats {
    font-size: 10px;
  }

  .book-card--grid .book-card-actions {
    padding: 4px 10px 8px;
    gap: 6px;
  }

  .book-card--grid .book-card-actions .n-button {
    font-size: 12px;
    padding: 0 8px;
    height: 26px;
  }
}

@media (max-width: 350px) {
  .books-container.mode-grid {
    grid-template-columns: 1fr;
    gap: 12px;
  }

  .book-card--grid .book-cover-wrap {
    max-height: 200px;
  }
}
</style>
