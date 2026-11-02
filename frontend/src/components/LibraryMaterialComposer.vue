<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import {
  NAlert,
  NButton,
  NInput,
  NInputNumber,
  NModal,
  useDialog,
  useMessage,
} from "naive-ui";
import { getFrontendSettings } from "../api/client";
import {
  library,
  LibraryApiError,
  type LibraryBook,
  type LibraryJob,
  type LibraryMaterial,
  type LibraryRange,
  type LibraryRevision,
  type OCRSettings,
} from "../api/library";

const props = withDefaults(
  defineProps<{
    show: boolean;
    purpose?: "task" | "standalone";
  }>(),
  { purpose: "task" }
);

const emit = defineEmits<{
  "update:show": [value: boolean];
  selected: [revision: LibraryRevision];
  saved: [material: LibraryMaterial];
}>();

const message = useMessage();
const dialog = useDialog();

const tab = ref<"books" | "preview" | "material">("books");
const books = ref<LibraryBook[]>([]);
const bookNames = ref<Record<string, string>>({});
const query = ref("");
const bookPage = ref(1);
const bookCount = ref(0);
const loadingBooks = ref(false);
const booksError = ref("");
const book = ref<LibraryBook | null>(null);

// Page navigation & continuous scrolling state
const page = ref(1);
const jumpPage = ref<number | null>(1);
const first = ref<number | null>(1);
const last = ref<number | null>(1);

// Continuous scroll: list of page numbers rendered in the vertical stream
const renderedPages = ref<number[]>([]);
const pageRetries = ref<Record<number, number>>({});
const pageStatus = ref<Record<number, "loading" | "ready" | "error">>({});
const previewScrollRef = ref<HTMLElement | null>(null);

// Display modes: 'page' (Fit page height) vs 'width' (Fit page width)
const fitMode = ref<"page" | "width">("page");
const focusMode = ref(false);
const zoom = ref(100);

function fitToPage() {
  fitMode.value = "page";
  zoom.value = 100;
}

function fitToWidth() {
  fitMode.value = "width";
  zoom.value = 100;
}

function zoomIn() {
  if (zoom.value < 250) {
    zoom.value = Math.min(250, zoom.value + 25);
  }
}

function zoomOut() {
  if (zoom.value > 50) {
    zoom.value = Math.max(50, zoom.value - 25);
  }
}

function resetZoom() {
  zoom.value = 100;
}

const name = ref(`参考材料 ${new Date().toLocaleString("zh-CN", { hour12: false })}`);
const ranges = ref<LibraryRange[]>([]);
const material = ref<LibraryMaterial | null>(null);
const conflict = ref<LibraryMaterial | null>(null);

type OCRJob = LibraryJob & { pages?: number[] };
const jobs = ref<OCRJob[]>([]);
const trackedJobs = new Set<string>();
const busy = ref(false);
const draggedIndex = ref<number | null>(null);

let booksRequest = 0;
let poll: number | null = null;
let refreshingJobs = false;
let scrollThrottled = false;
let isJumpingScroll = false;

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败";
}

function rememberBook(value: LibraryBook) {
  bookNames.value[value.id] = [value.title, value.edition].filter(Boolean).join(" · ");
}

function bookName(id: string): string {
  return bookNames.value[id] || `书籍 ${id}`;
}

const dirty = computed(() => {
  if (!material.value) return true;
  return (
    name.value.trim() !== material.value.name ||
    JSON.stringify(ranges.value) !== JSON.stringify(material.value.ranges)
  );
});

const pageCount = computed(() =>
  ranges.value.reduce((total, item) => total + item.end_page - item.start_page + 1, 0)
);

const duplicateCount = computed(() => {
  const counts = new Map<string, number>();
  for (const range of ranges.value) {
    for (let number = range.start_page; number <= range.end_page; number++) {
      const key = `${range.book_id}:${number}`;
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
  }
  return [...counts.values()].filter((count) => count > 1).length;
});

const currentMaterial = computed(() => {
  return !dirty.value && !conflict.value ? material.value : null;
});

const hasActiveJobs = computed(() =>
  jobs.value.some((job) => job.status === "pending" || job.status === "running")
);

function pageUrl(pageNumber: number): string {
  if (!book.value) return "";
  const retry = pageRetries.value[pageNumber] ?? 0;
  return `${library.previewUrl(book.value.id, pageNumber)}?reload=${retry}`;
}

function onPageImageLoad(pageNumber: number) {
  pageStatus.value[pageNumber] = "ready";
}

function onPageImageError(pageNumber: number) {
  pageStatus.value[pageNumber] = "error";
}

function reloadPageImage(pageNumber: number) {
  pageRetries.value[pageNumber] = (pageRetries.value[pageNumber] ?? 0) + 1;
  pageStatus.value[pageNumber] = "loading";
}

async function loadBooks() {
  const token = ++booksRequest;
  loadingBooks.value = true;
  booksError.value = "";
  try {
    const result = await library.listBooks(query.value, bookPage.value);
    if (token !== booksRequest) return;
    books.value = result.items;
    bookCount.value = result.total;
    result.items.forEach(rememberBook);
  } catch (error) {
    if (token === booksRequest) {
      booksError.value = errorText(error);
    }
  } finally {
    if (token === booksRequest) {
      loadingBooks.value = false;
    }
  }
}

function searchBooks() {
  bookPage.value = 1;
  void loadBooks();
}

function selectBook(value: LibraryBook) {
  if (busy.value || value.state === "deleting") return;
  book.value = value;
  rememberBook(value);
  page.value = 1;
  jumpPage.value = 1;
  first.value = 1;
  last.value = 1;
  pageRetries.value = {};
  pageStatus.value = {};
  zoom.value = 100;

  // Initially render page 1; scrolling down dynamically appends subsequent pages
  renderedPages.value = [1];
  pageStatus.value[1] = "loading";

  tab.value = "preview";
  nextTick(() => {
    if (previewScrollRef.value) {
      previewScrollRef.value.scrollTop = 0;
    }
  });
}

function loadNextPage() {
  if (!book.value) return;
  const maxPage = Math.max(...renderedPages.value);
  if (maxPage < book.value.page_count) {
    const next = maxPage + 1;
    if (!renderedPages.value.includes(next)) {
      renderedPages.value.push(next);
      pageStatus.value[next] = "loading";
    }
  }
}

function goToPage(number: number | null) {
  if (
    !book.value ||
    number === null ||
    !Number.isInteger(number) ||
    number < 1 ||
    number > book.value.page_count
  ) {
    if (book.value) {
      message.warning(`请输入 1—${book.value.page_count} 的 PDF 物理页码。`);
    }
    return;
  }

  if (first.value === page.value && last.value === page.value) {
    first.value = number;
    last.value = number;
  }
  page.value = number;
  jumpPage.value = number;

  // Ensure target page is in renderedPages list
  if (!renderedPages.value.includes(number)) {
    const toRender = [number];
    if (number + 1 <= book.value.page_count) toRender.push(number + 1);
    if (number + 2 <= book.value.page_count) toRender.push(number + 2);
    renderedPages.value = toRender;
    for (const p of toRender) {
      if (!pageStatus.value[p]) {
        pageStatus.value[p] = "loading";
      }
    }

    isJumpingScroll = true;
    nextTick(() => {
      if (previewScrollRef.value) {
        previewScrollRef.value.scrollTop = 0;
      }
      setTimeout(() => {
        isJumpingScroll = false;
      }, 300);
    });
    return;
  }

  isJumpingScroll = true;
  nextTick(() => {
    const container = previewScrollRef.value;
    const targetEl = document.getElementById(`pdf-page-${number}`);
    if (container && targetEl) {
      const containerRect = container.getBoundingClientRect();
      const targetRect = targetEl.getBoundingClientRect();
      const targetScrollTop = container.scrollTop + (targetRect.top - containerRect.top);
      container.scrollTo({ top: Math.max(0, targetScrollTop), behavior: "smooth" });
    }
    setTimeout(() => {
      isJumpingScroll = false;
    }, 400);
  });
}

function onPreviewWheel(e: WheelEvent) {
  if (!book.value) return;

  // 1. Ctrl / Meta + Wheel: Zoom in / Zoom out
  if (e.ctrlKey || e.metaKey) {
    e.preventDefault();
    if (e.deltaY < 0) {
      zoomIn();
    } else if (e.deltaY > 0) {
      zoomOut();
    }
    return;
  }

  // 2. Normal Wheel: Proactive continuous downward paging near bottom
  if (isJumpingScroll) return;
  const container = previewScrollRef.value;
  if (!container) return;

  if (e.deltaY > 0) {
    const maxPage = Math.max(...renderedPages.value);
    if (maxPage < book.value.page_count) {
      if (container.scrollTop + container.clientHeight >= container.scrollHeight - 60) {
        loadNextPage();
      }
    }
  }
}

function setRangeFromCurrentPage() {
  if (!book.value) return;
  first.value = page.value;
  last.value = page.value;
}

function setStartPage(pageNumber: number) {
  first.value = pageNumber;
  if (last.value === null || last.value < pageNumber) {
    last.value = pageNumber;
  }
}

function setEndPage(pageNumber: number) {
  last.value = pageNumber;
  if (first.value === null || first.value > pageNumber) {
    first.value = pageNumber;
  }
}

function onPreviewScroll() {
  if (scrollThrottled) return;
  scrollThrottled = true;
  requestAnimationFrame(() => {
    scrollThrottled = false;
    handleScrollPaging();
  });
}

function handleScrollPaging() {
  const container = previewScrollRef.value;
  if (!container || !book.value) return;

  const { scrollTop, scrollHeight, clientHeight } = container;

  // 1. Continuous downward paging: load next pages when near bottom
  if (scrollTop + clientHeight >= scrollHeight - 500) {
    const maxPage = Math.max(...renderedPages.value);
    if (maxPage < book.value.page_count) {
      const nextPages: number[] = [];
      if (maxPage + 1 <= book.value.page_count) nextPages.push(maxPage + 1);
      if (maxPage + 2 <= book.value.page_count) nextPages.push(maxPage + 2);

      for (const p of nextPages) {
        if (!renderedPages.value.includes(p)) {
          renderedPages.value.push(p);
          pageStatus.value[p] = "loading";
        }
      }
    }
  }

  // 2. Continuous upward paging: load previous pages when near top
  if (scrollTop <= 120 && !isJumpingScroll) {
    const minPage = Math.min(...renderedPages.value);
    if (minPage > 1) {
      const prevPages: number[] = [];
      if (minPage - 1 >= 1) prevPages.unshift(minPage - 1);
      if (minPage - 2 >= 1) prevPages.unshift(minPage - 2);

      const toAdd = prevPages.filter((p) => !renderedPages.value.includes(p));
      if (toAdd.length) {
        const oldScrollHeight = container.scrollHeight;
        const oldScrollTop = container.scrollTop;
        renderedPages.value.unshift(...toAdd);
        for (const p of toAdd) {
          pageStatus.value[p] = "loading";
        }
        nextTick(() => {
          container.scrollTop = oldScrollTop + (container.scrollHeight - oldScrollHeight);
        });
      }
    }
  }

  // 3. Update active page indicator from scroll position
  if (!isJumpingScroll) {
    const containerRect = container.getBoundingClientRect();
    const midpoint = containerRect.top + containerRect.height * 0.45;

    let closestPage = page.value;
    let minDistance = Infinity;

    for (const pageNum of renderedPages.value) {
      const el = document.getElementById(`pdf-page-${pageNum}`);
      if (el) {
        const r = el.getBoundingClientRect();
        if (r.top <= midpoint && r.bottom >= midpoint) {
          closestPage = pageNum;
          break;
        }
        const dist = Math.abs(r.top - midpoint);
        if (dist < minDistance) {
          minDistance = dist;
          closestPage = pageNum;
        }
      }
    }

    if (closestPage !== page.value) {
      page.value = closestPage;
      jumpPage.value = closestPage;
    }
  }
}

function addRange() {
  if (
    !book.value ||
    first.value === null ||
    last.value === null ||
    !Number.isInteger(first.value) ||
    !Number.isInteger(last.value) ||
    first.value < 1 ||
    last.value > book.value.page_count ||
    first.value > last.value
  ) {
    message.warning(`请输入 1—${book.value?.page_count ?? 0} 的顺序页段。`);
    return;
  }
  ranges.value.push({
    book_id: book.value.id,
    start_page: first.value,
    end_page: last.value,
  });
}

function moveRange(from: number, to: number) {
  if (busy.value || to < 0 || to >= ranges.value.length || from === to) return;
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

function jobStatus(status: string): string {
  const map: Record<string, string> = {
    pending: "等待开始",
    running: "识别中",
    success: "已完成",
    partial: "部分完成",
    failed: "失败",
    interrupted: "已中断",
  };
  return map[status] ?? status;
}

function stopPolling() {
  if (poll !== null) {
    window.clearInterval(poll);
    poll = null;
  }
}

function updatePolling() {
  if (!props.show || !hasActiveJobs.value) {
    stopPolling();
    return;
  }
  if (poll === null) {
    poll = window.setInterval(() => void refreshJobs(), 3000);
  }
}

async function refreshMaterial(): Promise<LibraryMaterial | null> {
  const base = material.value;
  if (!base) return null;

  const current = await library.getMaterial(base.id);
  if (!material.value || material.value.id !== base.id || material.value.version !== base.version) {
    return null;
  }

  if (current.version !== base.version) {
    if (conflict.value?.version !== current.version) {
      message.warning("材料草稿已被其他人修改。你的页段仍保留，请比较后再保存。");
    }
    conflict.value = current;
    return null;
  }

  material.value = current;
  return current;
}

async function refreshJobs() {
  if (refreshingJobs || !props.show || (!material.value && !jobs.value.length)) return;
  refreshingJobs = true;
  try {
    if (material.value) {
      await refreshMaterial();
    }
    const result = await library.jobs();
    if (!props.show) return;

    const missing = new Set(
      (material.value?.missing_page_items ?? []).map((item) => `${item.book_id}:${item.page_number}`)
    );
    jobs.value = (result.items as OCRJob[]).filter(
      (item) =>
        item.kind === "ocr" &&
        (trackedJobs.has(item.id) ||
          (["pending", "running", "partial", "failed", "interrupted"].includes(item.status) &&
            item.pages?.some((number) => missing.has(`${item.book_id}:${number}`))))
    );
  } catch (error) {
    message.error(`刷新识别状态失败：${errorText(error)}`);
  } finally {
    refreshingJobs = false;
    updatePolling();
  }
}

async function reportConflict(base: LibraryMaterial) {
  try {
    const latest = await library.getMaterial(base.id);
    if (latest.version !== base.version) {
      conflict.value = latest;
      message.warning("材料草稿已被其他人修改。你的名称和页段仍保留，请比较后再保存。");
    }
  } catch (error) {
    message.error(errorText(error));
  }
}

function useLatestBase() {
  if (!conflict.value) return;
  material.value = conflict.value;
  conflict.value = null;
  message.info("已采用最新草稿作为基准；你的名称和页段保持不变，请检查后再保存。");
}

async function ensureSaved(): Promise<LibraryMaterial | null> {
  if (!name.value.trim()) {
    message.warning("请填写材料名称。");
    tab.value = "material";
    return null;
  }
  if (!ranges.value.length) {
    message.warning("请先选择书籍并加入页段。");
    return null;
  }
  if (conflict.value) {
    tab.value = "material";
    message.warning("请先比较草稿冲突。");
    return null;
  }

  const base = material.value;
  try {
    if (base && !dirty.value) {
      const current = await refreshMaterial();
      if (current?.missing_pages) {
        await refreshJobs();
      }
      return conflict.value ? null : current;
    }

    const saved = base
      ? await library.updateMaterial(base, name.value.trim(), ranges.value)
      : await library.createMaterial(name.value.trim(), ranges.value);

    material.value = saved;
    name.value = saved.name;
    ranges.value = saved.ranges.map((item) => ({ ...item }));
    conflict.value = null;
    emit("saved", saved);

    if (saved.missing_pages) {
      await refreshJobs();
    }
    return conflict.value ? null : material.value;
  } catch (error) {
    if (error instanceof LibraryApiError && error.status === 409 && base) {
      await reportConflict(base);
    }
    message.error(errorText(error));
    return null;
  }
}

async function saveDraft() {
  if (busy.value) return;
  busy.value = true;
  try {
    if (await ensureSaved()) {
      message.success("材料草稿已保存。");
    }
  } finally {
    busy.value = false;
  }
}

async function publish() {
  if (busy.value) return;
  busy.value = true;
  try {
    const saved = await ensureSaved();
    if (!saved) return;

    if (saved.missing_pages) {
      tab.value = "material";
      message.warning(`还有 ${saved.missing_pages} 页尚未准备，请先识别缺页。`);
      return;
    }

    try {
      const revision = await library.publish(saved);
      emit("selected", revision);
      emit("update:show", false);
      message.success(props.purpose === "task" ? "已用于本次任务。" : "固定材料版本已生成。");
    } catch (error) {
      if (error instanceof LibraryApiError && error.status === 409) {
        await reportConflict(saved);
      }
      message.error(errorText(error));
    }
  } finally {
    busy.value = false;
  }
}

async function startMissing() {
  if (busy.value || hasActiveJobs.value) return;
  busy.value = true;
  try {
    const saved = await ensureSaved();
    if (!saved) return;

    if (!saved.missing_page_items.length) {
      message.info("所选页面已经准备完成。");
      return;
    }
    if (hasActiveJobs.value) {
      tab.value = "material";
      message.info("相关页面正在识别，请等待完成。");
      return;
    }

    const settings = await getFrontendSettings();
    const options: OCRSettings = {
      model: settings.ocr_model,
      reasoning_effort: settings.ocr_reasoning_effort,
      fast_mode: settings.ocr_fast_mode,
      max_concurrency: settings.ocr_max_concurrency,
      submit_interval_seconds: settings.ocr_submit_interval_seconds,
    };

    const groups = new Map<string, Set<number>>();
    for (const item of saved.missing_page_items) {
      const pages = groups.get(item.book_id) ?? new Set<number>();
      pages.add(item.page_number);
      groups.set(item.book_id, pages);
    }
    const total = [...groups.values()].reduce((sum, pages) => sum + pages.size, 0);

    tab.value = "material";
    dialog.warning({
      title: "确认识别缺页",
      content: `需要识别 ${total} 个不同原页。模型：${settings.ocr_model}；推理强度：${settings.ocr_reasoning_effort}；最大并发：${settings.ocr_max_concurrency}；投递间隔：${settings.ocr_submit_interval_seconds} 秒；快速模式：${settings.ocr_fast_mode ? "开启" : "关闭"}。`,
      positiveText: "启动识别",
      negativeText: "取消",
      onPositiveClick: async () => {
        busy.value = true;
        let submitted = 0;
        try {
          for (const [bookId, pages] of groups) {
            try {
              const job = await library.startOCR(bookId, [...pages], options);
              trackedJobs.add(job.id);
              jobs.value = [job, ...jobs.value.filter((item) => item.id !== job.id)];
              submitted++;
            } catch (error) {
              message.error(`${bookName(bookId)}：${errorText(error)}`);
            }
          }
          if (submitted) {
            message.info(`已提交 ${submitted} 个识别请求，完成的页会保留。`);
            await refreshJobs();
          }
        } finally {
          busy.value = false;
          updatePolling();
        }
      },
    });
  } catch (error) {
    message.error(errorText(error));
  } finally {
    busy.value = false;
  }
}

function resume(job: LibraryJob) {
  dialog.warning({
    title: "继续识别",
    content: "将沿用这次作业的模型和并发设置，只处理尚未完成的页面。",
    positiveText: "继续",
    negativeText: "取消",
    onPositiveClick: async () => {
      try {
        const next = await library.resume(job.id);
        jobs.value = jobs.value.map((item) => (item.id === job.id ? next : item));
        updatePolling();
      } catch (error) {
        message.error(errorText(error));
      }
    },
  });
}

function updateShow(value: boolean) {
  if (!busy.value) {
    emit("update:show", value);
  }
}

watch(
  () => props.show,
  async (show) => {
    if (!show) {
      stopPolling();
      return;
    }
    void loadBooks();
    try {
      if (material.value || jobs.value.length) {
        await refreshJobs();
      }
    } catch (error) {
      message.error(`刷新材料状态失败：${errorText(error)}`);
    }
    updatePolling();
  },
  { immediate: true }
);

onBeforeUnmount(stopPolling);
</script>

<template>
  <n-modal :show="show" :mask-closable="false" @update:show="updateShow">
    <div
      class="composer"
      role="dialog"
      aria-modal="true"
      aria-labelledby="composer-title"
    >
      <header class="composer-header">
        <div class="composer-header__title">
          <h2 id="composer-title">选书与组合参考材料</h2>
          <p>按 PDF 物理页选择，支持鼠标连续下滑或输入跳转，页段按加入顺序排列。</p>
        </div>
        <n-button :disabled="busy" @click="updateShow(false)">
          {{ purpose === 'task' ? '返回任务' : '关闭' }}
        </n-button>
        <div class="mobile-tabs" aria-label="组装步骤">
          <n-button
            size="small"
            :type="tab === 'books' ? 'primary' : 'default'"
            :aria-pressed="tab === 'books'"
            @click="tab = 'books'"
          >
            选书
          </n-button>
          <n-button
            size="small"
            :type="tab === 'preview' ? 'primary' : 'default'"
            :aria-pressed="tab === 'preview'"
            @click="tab = 'preview'"
          >
            预览选页
          </n-button>
          <n-button
            size="small"
            :type="tab === 'material' ? 'primary' : 'default'"
            :aria-pressed="tab === 'material'"
            aria-label="参考材料 (本次材料)"
            @click="tab = 'material'"
          >
            参考材料<span v-if="ranges.length"> · {{ ranges.length }}</span>
          </n-button>
        </div>
      </header>

      <div
        class="composer-body"
        :class="{ 'focus-mode': focusMode }"
      >
        <!-- Left: Books Panel -->
        <section
          class="books-panel composer-panel"
          :class="{ 'mobile-hidden': tab !== 'books' }"
          aria-label="选择书籍"
        >
          <h3>选择书籍</h3>
          <div class="search-row">
            <n-input
              v-model:value="query"
              placeholder="搜索书名、作者、版本或标签"
              clearable
              aria-label="搜索书籍"
              @keyup.enter="searchBooks"
            />
            <n-button :loading="loadingBooks" @click="searchBooks">
              搜索
            </n-button>
          </div>

          <n-alert v-if="booksError" type="error" :bordered="false">
            {{ booksError }}
            <n-button size="tiny" @click="loadBooks">重试</n-button>
          </n-alert>

          <p v-if="!loadingBooks && !booksError && !books.length && query" class="muted">
            没有找到匹配的书籍。
          </p>
          <div v-else-if="!loadingBooks && !booksError && !books.length" class="empty-books">
            <p>书库还没有书。</p>
            <a href="/pdf/library" target="_blank" rel="noopener">上传或导入 PDF</a>
            <n-button size="small" secondary @click="loadBooks">刷新书库</n-button>
          </div>

          <div class="books-list">
            <button
              v-for="item in books"
              :key="item.id"
              class="book-choice"
              :class="{ selected: book?.id === item.id }"
              :disabled="busy || item.state === 'deleting'"
              :aria-pressed="book?.id === item.id"
              @click="selectBook(item)"
            >
              <strong>{{ item.title }}</strong>
              <span>{{ [item.author, item.edition].filter(Boolean).join(' · ') || '未填写作者或版本' }}</span>
              <small>PDF {{ item.page_count }} 页 · 文字 {{ item.ready_pages }}/{{ item.page_count }} 页</small>
            </button>
          </div>

          <div class="pagination">
            <n-button
              size="small"
              :disabled="loadingBooks || bookPage <= 1"
              @click="bookPage--; loadBooks()"
            >
              上一页
            </n-button>
            <span>第 {{ bookPage }} 页 · 共 {{ bookCount }} 本</span>
            <n-button
              size="small"
              :disabled="loadingBooks || bookPage * 20 >= bookCount"
              @click="bookPage++; loadBooks()"
            >
              下一页
            </n-button>
          </div>
        </section>

        <!-- Middle: Preview Panel with Continuous Scroll -->
        <section
          class="preview-panel composer-panel"
          :class="{ 'mobile-hidden': tab !== 'preview' }"
          aria-label="预览选页"
        >
          <template v-if="book">
            <div class="preview-controls">
              <!-- Book title and View Mode options -->
              <div class="controls-top-row">
                <h3 class="preview-title" :title="book.title">
                  {{ book.title }}
                  <small v-if="book.edition"> · {{ book.edition }}</small>
                </h3>
                <div class="view-options">
                  <n-button
                    size="tiny"
                    :type="fitMode === 'page' && zoom === 100 ? 'primary' : 'default'"
                    secondary
                    @click="fitToPage"
                  >
                    适应整页
                  </n-button>
                  <n-button
                    size="tiny"
                    :type="fitMode === 'width' && zoom === 100 ? 'primary' : 'default'"
                    secondary
                    @click="fitToWidth"
                  >
                    适应宽度
                  </n-button>

                  <div class="zoom-toolbar" role="group" aria-label="页面缩放">
                    <n-button
                      size="tiny"
                      secondary
                      :disabled="zoom <= 50"
                      title="缩小 (Ctrl + 滚轮向下)"
                      @click="zoomOut"
                    >
                      缩小
                    </n-button>
                    <button
                      type="button"
                      class="zoom-reset-btn"
                      title="点击重置缩放为 100%"
                      @click="resetZoom"
                    >{{ zoom }}%</button>
                    <n-button
                      size="tiny"
                      secondary
                      :disabled="zoom >= 250"
                      title="放大 (Ctrl + 滚轮向上)"
                      @click="zoomIn"
                    >
                      放大
                    </n-button>
                  </div>

                  <n-button
                    size="tiny"
                    class="desktop-only"
                    :type="focusMode ? 'primary' : 'default'"
                    secondary
                    :title="focusMode ? '恢复三栏视图' : '全屏预览 (隐藏左右栏)'"
                    @click="focusMode = !focusMode"
                  >
                    {{ focusMode ? '恢复三栏' : '全屏' }}
                  </n-button>
                </div>
              </div>

              <!-- Action row: Page navigation + Selection range -->
              <div class="controls-action-row">
                <!-- Page navigation bar: jumps, prev/next -->
                <div class="page-nav">
                  <n-button
                    size="small"
                    :disabled="page <= 1"
                    @click="goToPage(page - 1)"
                  >
                    上一页
                  </n-button>
                  <label>
                    PDF 第
                    <n-input-number
                      v-model:value="jumpPage"
                      :min="1"
                      :max="book.page_count"
                      :precision="0"
                      :show-button="false"
                      :input-props="{ 'aria-label': '跳转 PDF 页码' }"
                      size="small"
                      class="page-number"
                      @keyup.enter="goToPage(jumpPage)"
                    />
                    / {{ book.page_count }} 页
                  </label>
                  <n-button size="small" @click="goToPage(jumpPage)">
                    跳转
                  </n-button>
                  <n-button
                    size="small"
                    :disabled="page >= book.page_count"
                    @click="goToPage(page + 1)"
                  >
                    下一页
                  </n-button>
                </div>

                <!-- Selection range tool bar -->
                <div class="select-range">
                  <strong>加入参考材料</strong>
                  <label>
                    第
                    <n-input-number
                      v-model:value="first"
                      :min="1"
                      :max="book.page_count"
                      :precision="0"
                      :show-button="false"
                      :disabled="busy"
                      :input-props="{ 'aria-label': '起始 PDF 页码' }"
                      size="small"
                      class="page-number"
                      @keyup.enter="addRange"
                    />
                    页
                  </label>
                  <label>
                    至
                    <n-input-number
                      v-model:value="last"
                      :min="1"
                      :max="book.page_count"
                      :precision="0"
                      :show-button="false"
                      :disabled="busy"
                      :input-props="{ 'aria-label': '结束 PDF 页码' }"
                      size="small"
                      class="page-number"
                      @keyup.enter="addRange"
                    />
                    页
                  </label>
                  <n-button
                    type="primary"
                    secondary
                    size="small"
                    :disabled="busy"
                    @click="addRange"
                  >
                    加入页段
                  </n-button>
                </div>
              </div>
            </div>

            <!-- Continuous Scroll View Area -->
            <div
              ref="previewScrollRef"
              class="preview-scroll"
              :class="{
                'mode-fit-page': fitMode === 'page',
                'mode-fit-width': fitMode === 'width',
                'is-zoomed': zoom !== 100,
              }"
              :style="{ '--zoom': zoom / 100 }"
              @scroll="onPreviewScroll"
              @wheel="onPreviewWheel"
            >
              <div
                v-for="pageNum in renderedPages"
                :id="`pdf-page-${pageNum}`"
                :key="`${book.id}-${pageNum}-${pageRetries[pageNum] ?? 0}`"
                class="pdf-page-card"
                :class="{ 'is-current': pageNum === page }"
              >
                <div class="page-card-header">
                  <span class="page-badge">第 {{ pageNum }} / {{ book.page_count }} 页</span>
                  <div class="page-quick-actions">
                    <button
                      type="button"
                      class="quick-action-link"
                      @click="setStartPage(pageNum)"
                    >
                      设为起始
                    </button>
                    <span class="action-divider">·</span>
                    <button
                      type="button"
                      class="quick-action-link"
                      @click="setEndPage(pageNum)"
                    >
                      设为结束
                    </button>
                  </div>
                </div>

                <div class="page-image-wrapper">
                  <p
                    v-if="pageStatus[pageNum] === 'loading'"
                    role="status"
                    class="page-loading-text"
                  >
                    正在渲染第 {{ pageNum }} 页…
                  </p>

                  <n-alert
                    v-if="pageStatus[pageNum] === 'error'"
                    type="error"
                    :bordered="false"
                    class="page-error-alert"
                  >
                    第 {{ pageNum }} 页渲染失败。
                    <n-button size="tiny" @click="reloadPageImage(pageNum)">
                      重新加载
                    </n-button>
                  </n-alert>

                  <img
                    v-show="pageStatus[pageNum] === 'ready'"
                    :src="pageUrl(pageNum)"
                    :alt="`PDF 第 ${pageNum} 页`"
                    :data-identity="`${book.id}:${pageNum}:${pageRetries[pageNum] ?? 0}`"
                    class="pdf-page-img"
                    @load="onPageImageLoad(pageNum)"
                    @error="onPageImageError(pageNum)"
                  />
                </div>
              </div>

              <!-- Bottom hint & continuous load trigger -->
              <div class="scroll-bottom-indicator">
                <div v-if="renderedPages.length && Math.max(...renderedPages) < book.page_count" class="load-more-row">
                  <n-button size="small" secondary @click="loadNextPage">
                    加载下一页（第 {{ Math.max(...renderedPages) + 1 }} 页）
                  </n-button>
                  <small class="muted">向下滑动或点击加载更多</small>
                </div>
                <span v-else class="muted">
                  已到底部（共 {{ book.page_count }} 页）
                </span>
              </div>
            </div>
          </template>

          <div v-else class="preview-empty">
            <h3>预览选页</h3>
            <p>选择一本书，在这里预览原页并加入需要的页段。</p>
            <n-button class="mobile-only" @click="tab = 'books'">
              选择书籍
            </n-button>
          </div>
        </section>

        <!-- Right: Material Panel -->
        <section
          class="material-panel composer-panel"
          :class="{ 'mobile-hidden': tab !== 'material' }"
          aria-label="参考材料"
        >
          <h3>参考材料</h3>
          <label class="material-name">
            材料名称
            <n-input
              v-model:value="name"
              :disabled="busy"
              aria-label="材料名称"
            />
          </label>

          <n-alert v-if="duplicateCount" type="warning" :bordered="false">
            有 {{ duplicateCount }} 个原页被重复选择，顺序和重复都会保留。
          </n-alert>

          <p v-if="!ranges.length" class="muted">
            选择书籍并加入页段，可跨书组合。
          </p>
          <ol v-else class="ranges">
            <li
              v-for="(range, index) in ranges"
              :key="`${index}-${range.book_id}-${range.start_page}-${range.end_page}`"
              :draggable="!busy"
              @dragstart="draggedIndex = index"
              @dragover.prevent
              @drop.prevent="onDrop(index)"
              @dragend="draggedIndex = null"
            >
              <div>
                <span class="drag-handle" aria-hidden="true">⠿</span>
                <strong>{{ bookName(range.book_id) }}</strong>
                <br />
                <span>PDF 第 {{ range.start_page }}—{{ range.end_page }} 页</span>
              </div>
              <div class="range-actions">
                <n-button
                  size="tiny"
                  :disabled="busy || index === 0"
                  :aria-label="`上移第 ${index + 1} 段`"
                  @click="moveRange(index, index - 1)"
                >
                  上移
                </n-button>
                <n-button
                  size="tiny"
                  :disabled="busy || index === ranges.length - 1"
                  :aria-label="`下移第 ${index + 1} 段`"
                  @click="moveRange(index, index + 1)"
                >
                  下移
                </n-button>
                <n-button
                  size="tiny"
                  text
                  type="error"
                  :disabled="busy"
                  :aria-label="`移除第 ${index + 1} 段`"
                  @click="ranges.splice(index, 1)"
                >
                  移除
                </n-button>
              </div>
            </li>
          </ol>

          <n-alert
            v-if="conflict"
            type="warning"
            title="材料草稿已被其他人修改"
            :bordered="false"
          >
            你的名称和页段仍保留。最新草稿：{{ conflict.name }}，{{ conflict.page_count }} 页。
            <ol class="conflict-ranges">
              <li v-for="(item, index) in conflict.ranges" :key="index">
                {{ bookName(item.book_id) }} · PDF 第 {{ item.start_page }}—{{ item.end_page }} 页
              </li>
            </ol>
            <n-button size="small" :disabled="busy" @click="useLatestBase">
              以最新版本为基准继续编辑
            </n-button>
          </n-alert>

          <div v-if="jobs.length" class="job-list">
            <h4>缺页识别</h4>
            <p v-if="jobs.some((job) => ['partial', 'failed', 'interrupted'].includes(job.status))" class="muted">
              继续或重试沿用原任务设置；新识别使用当前设置。
            </p>
            <div v-for="job in jobs" :key="job.id" class="job">
              <div>
                <strong>{{ bookName(job.book_id || '') }}</strong> · {{ jobStatus(job.status) }} · {{ job.completed }}/{{ job.total }} 页
                <span v-if="job.failed_pages"> · 失败 {{ job.failed_pages }} 页</span>
              </div>
              <p v-if="job.error" class="job-error">{{ job.error }}</p>
              <p v-for="(error, number) in job.errors" :key="number" class="job-error">
                第 {{ number }} 页：{{ error }}
              </p>
              <n-button
                v-if="['partial', 'failed', 'interrupted'].includes(job.status)"
                size="small"
                :disabled="busy"
                @click="resume(job)"
              >
                继续 / 重试
              </n-button>
            </div>
          </div>
        </section>
      </div>

      <footer class="composer-footer">
        <div class="summary" aria-live="polite">
          <strong>已选 {{ pageCount }} 页</strong>
          <span v-if="currentMaterial">
            · 已准备 {{ currentMaterial.ready_pages }} 页 · 缺少 {{ currentMaterial.missing_pages }} 页
          </span>
          <span v-else-if="ranges.length"> · 保存后检查文字状态</span>
          <span v-if="currentMaterial && currentMaterial.missing_pages !== currentMaterial.missing_page_items.length">
            （{{ currentMaterial.missing_page_items.length }} 个不同原页）
          </span>
          <n-button
            v-if="currentMaterial && currentMaterial.missing_pages > 0"
            text
            size="tiny"
            :disabled="busy"
            @click="refreshJobs"
          >
            刷新状态
          </n-button>
        </div>

        <div class="footer-actions">
          <n-button :disabled="!ranges.length || busy" @click="saveDraft">
            保存草稿
          </n-button>
          <n-button
            secondary
            :disabled="!ranges.length || busy || hasActiveJobs || (!!currentMaterial && currentMaterial.missing_pages === 0)"
            @click="startMissing"
          >
            识别缺页
          </n-button>
          <n-button
            type="primary"
            :disabled="!ranges.length || busy || (!!currentMaterial && currentMaterial.missing_pages > 0)"
            :loading="busy"
            @click="publish"
          >
            {{ purpose === 'task' ? '用于本次任务' : '生成材料' }}
          </n-button>
        </div>
      </footer>
    </div>
  </n-modal>
</template>

<style scoped>
.composer {
  width: min(1640px, 98vw);
  height: min(960px, 96dvh);
  display: grid;
  grid-template-rows: auto minmax(0, 1fr) auto;
  overflow: hidden;
  color: var(--text-primary);
  background: var(--surface-raised);
  border-radius: 12px;
  box-shadow: 0 16px 60px rgba(0, 0, 0, 0.25);
}

.composer-header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 20px;
  border-bottom: 1px solid var(--border-subtle);
}

.composer-header__title {
  min-width: 0;
}

.composer h2,
.composer h3,
.composer h4,
.composer p {
  margin: 0;
}

.composer h2 {
  font-size: 19px;
  font-weight: 650;
}

.composer h3 {
  font-size: 15px;
  font-weight: 600;
}

.composer-header p {
  margin-top: 3px;
  color: var(--text-secondary);
  font-size: 13px;
}

.composer-body {
  min-height: 0;
  height: 100%;
  display: grid;
  grid-template-columns: minmax(200px, 0.85fr) minmax(320px, 2.3fr) minmax(240px, 0.85fr);
  overflow: hidden;
  transition: grid-template-columns 200ms ease;
}

.composer-body.focus-mode {
  grid-template-columns: 1fr;
}

.composer-body.focus-mode .books-panel,
.composer-body.focus-mode .material-panel {
  display: none;
}

.composer-panel {
  min-width: 0;
  min-height: 0;
  padding: 14px 16px;
}

.books-panel,
.material-panel {
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.books-panel {
  border-right: 1px solid var(--border-subtle);
}

.material-panel {
  border-left: 1px solid var(--border-subtle);
}

.search-row,
.pagination,
.range-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.search-row .n-input {
  flex: 1 1 130px;
}

.books-list {
  display: grid;
  gap: 6px;
}

.book-choice {
  display: grid;
  gap: 4px;
  width: 100%;
  padding: 10px 12px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  color: var(--text-primary);
  text-align: left;
  font: inherit;
  cursor: pointer;
  overflow-wrap: anywhere;
  transition: background-color var(--motion-fast) var(--ease-out),
              border-color var(--motion-fast) var(--ease-out);
}

.book-choice:hover {
  background: var(--surface-subtle);
}

.book-choice.selected {
  background: var(--surface-subtle);
  border-color: var(--primary);
}

.book-choice:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: 1px;
}

.book-choice:disabled {
  opacity: 0.5;
  cursor: default;
}

.book-choice span,
.book-choice small,
.muted,
.pagination,
.empty-books {
  color: var(--text-secondary);
}

.empty-books {
  display: grid;
  justify-items: start;
  gap: 8px;
  padding: 24px 0;
}

.empty-books a {
  color: var(--primary);
}

.book-choice span,
.book-choice small {
  font-size: 12px;
}

.pagination {
  justify-content: center;
  font-size: 12px;
  margin-top: auto;
  padding-top: 8px;
}

/* Middle Preview Panel */
.preview-panel {
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  gap: 8px;
  padding: 10px 14px;
  min-height: 0;
  height: 100%;
  overflow: hidden;
}

.preview-controls {
  display: flex;
  flex-direction: column;
  gap: 8px;
  flex-shrink: 0;
}

.controls-top-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}

.controls-action-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
}

.preview-title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}

.preview-title small {
  color: var(--text-secondary);
  font-size: 13px;
  font-weight: 400;
}

.view-options {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.zoom-toolbar {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  background: var(--surface-subtle);
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  padding: 1px 3px;
}

.zoom-toolbar .n-button {
  padding: 0 6px;
  font-size: 12px;
  height: 22px;
}

.zoom-reset-btn {
  background: none;
  border: none;
  padding: 0 4px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
  cursor: pointer;
  border-radius: 3px;
  min-width: 38px;
  text-align: center;
  transition: color 150ms ease, background-color 150ms ease;
  height: 20px;
  line-height: 20px;
}

.zoom-reset-btn:hover {
  color: var(--primary);
  background: var(--surface-canvas);
}

.page-nav {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.page-nav label {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 13px;
}

.page-number {
  width: 68px;
}

.select-range {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 6px 12px;
  background: var(--surface-subtle);
  border-radius: 8px;
}

.select-range label {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 13px;
}

/* Continuous Scroll Window */
.preview-scroll {
  min-height: 0;
  height: 100%;
  overflow-y: auto;
  overflow-x: auto;
  background: var(--surface-subtle);
  padding: 8px 12px;
  border-radius: 8px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
  -webkit-overflow-scrolling: touch;
}

.pdf-page-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  background: var(--surface-canvas);
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
  transition: border-color 150ms ease, box-shadow 150ms ease;
  box-sizing: border-box;
  width: 100%;
}

.pdf-page-card.is-current {
  border-color: var(--primary);
  box-shadow: 0 4px 16px rgba(79, 70, 229, 0.12);
}

.pdf-page-card.is-current .page-badge {
  color: var(--primary);
  font-weight: 700;
}

.page-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 2px 4px 6px;
  font-size: 12px;
  color: var(--text-secondary);
  border-bottom: 1px solid var(--border-subtle);
  margin-bottom: 6px;
  flex-shrink: 0;
}

.page-badge {
  font-weight: 600;
  color: var(--text-primary);
}

.page-quick-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}

.quick-action-link {
  background: none;
  border: none;
  padding: 2px 6px;
  color: var(--primary);
  cursor: pointer;
  font-size: 12px;
  border-radius: 4px;
  transition: background-color 150ms ease;
}

.quick-action-link:hover {
  background: var(--surface-subtle);
  text-decoration: underline;
}

.action-divider {
  color: var(--text-muted);
}

.page-image-wrapper {
  width: 100%;
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 200px;
}

.page-loading-text {
  padding: 24px;
  color: var(--text-secondary);
  font-size: 13px;
}

.page-error-alert {
  margin: 12px;
}

/* Desktop Fit Page Mode: Exactly 1 page fits cleanly in the view, scales with --zoom */
.mode-fit-page .pdf-page-card {
  height: calc((100% - 16px) * var(--zoom, 1));
  max-height: none;
  min-height: 0;
  width: auto;
  max-width: none;
  box-sizing: border-box;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  padding: 6px 12px 10px;
}

.mode-fit-page .page-card-header {
  margin-bottom: 6px;
  flex-shrink: 0;
}

.mode-fit-page .page-image-wrapper {
  flex: 1 1 0;
  min-height: 0;
  height: 100%;
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: visible;
}

.mode-fit-page .pdf-page-img {
  height: 100%;
  max-height: none;
  max-width: none;
  width: auto;
  object-fit: contain;
  background: white;
  border-radius: 4px;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.1);
  display: block;
}

/* Fit Width mode: Stretches image to full available width and scales with --zoom */
.mode-fit-width .pdf-page-card {
  width: calc(100% * var(--zoom, 1));
  max-width: calc(860px * var(--zoom, 1));
  height: auto;
  flex-shrink: 0;
  padding: 8px 12px 12px;
}

.mode-fit-width .page-image-wrapper {
  width: 100%;
  display: flex;
  justify-content: center;
  min-height: 280px;
}

.mode-fit-width .pdf-page-img {
  width: 100%;
  height: auto;
  max-height: none;
  max-width: none;
  background: white;
  border-radius: 4px;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.1);
  display: block;
}

.scroll-bottom-indicator {
  padding: 24px 0 36px;
  text-align: center;
  font-size: 13px;
  width: 100%;
  flex-shrink: 0;
}

.load-more-row {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
}

.preview-empty {
  display: grid;
  align-content: start;
  gap: 8px;
  color: var(--text-secondary);
  padding: 24px 0;
}

.material-name {
  display: grid;
  gap: 6px;
}

.ranges {
  padding: 0;
  margin: 0;
  list-style: none;
  counter-reset: range;
}

.ranges li {
  counter-increment: range;
  display: grid;
  gap: 8px;
  padding: 10px 0;
  border-top: 1px solid var(--border-subtle);
  overflow-wrap: anywhere;
}

.ranges li > div:first-child::before {
  content: counter(range) '. ';
  font-weight: 600;
  color: var(--text-secondary);
}

.ranges li span {
  color: var(--text-secondary);
}

.drag-handle {
  cursor: grab;
  margin-right: 5px;
  color: var(--text-muted);
}

.range-actions {
  padding-left: 14px;
}

.conflict-ranges {
  padding-left: 20px;
}

.job-list {
  display: grid;
  gap: 8px;
  border-top: 1px solid var(--border-subtle);
  padding-top: 12px;
}

.job {
  display: grid;
  gap: 5px;
  font-size: 13px;
}

.job-error {
  color: var(--color-error, #c03434);
  overflow-wrap: anywhere;
}

.composer-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px 16px;
  padding: 12px 20px;
  border-top: 1px solid var(--border-subtle);
}

.summary {
  color: var(--text-secondary);
  font-size: 13px;
}

.summary strong {
  color: var(--text-primary);
}

.footer-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.mobile-tabs,
.mobile-only {
  display: none;
}

/* Mobile Responsiveness */
@media (max-width: 900px) {
  .composer {
    width: 100vw;
    height: 100dvh;
    border-radius: 0;
  }

  .composer-header {
    padding: 10px 14px;
    gap: 8px;
  }

  .composer h2 {
    font-size: 16px;
  }

  .composer-header p {
    display: none;
  }

  .mobile-tabs {
    display: flex;
    flex-basis: 100%;
    gap: 6px;
  }

  .mobile-only {
    display: inline-flex;
  }

  .desktop-only {
    display: none !important;
  }

  .composer-body {
    grid-template-columns: minmax(0, 1fr);
  }

  .composer-panel {
    padding: 10px 12px;
  }

  .composer-panel.mobile-hidden {
    display: none;
  }

  .books-panel,
  .material-panel {
    border: 0;
  }

  .preview-controls {
    gap: 6px;
  }

  .controls-top-row {
    flex-wrap: nowrap;
    justify-content: space-between;
    gap: 6px;
  }

  .preview-title {
    font-size: 14px;
    max-width: 180px;
  }

  .controls-action-row {
    display: flex;
    flex-direction: column;
    gap: 6px;
    width: 100%;
  }

  .page-nav {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: nowrap;
    gap: 4px;
    width: 100%;
  }

  .page-nav label {
    font-size: 12px;
    white-space: nowrap;
    gap: 2px;
  }

  .page-number {
    width: 50px;
  }

  .page-nav .n-button {
    padding: 0 8px;
    font-size: 12px;
    flex: none;
  }

  .select-range {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: nowrap;
    gap: 4px;
    padding: 6px 8px;
    font-size: 12px;
    width: 100%;
  }

  .select-range strong {
    font-size: 12px;
    white-space: nowrap;
  }

  .select-range label {
    font-size: 12px;
    white-space: nowrap;
    gap: 2px;
  }

  .select-range .page-number {
    width: 44px;
  }

  .select-range .n-button {
    padding: 0 8px;
    font-size: 12px;
    flex: none;
  }

  .preview-scroll {
    padding: 6px;
    gap: 10px;
  }

  .pdf-page-card {
    padding: 6px;
  }

  .zoom-toolbar {
    gap: 1px;
    padding: 0 2px;
  }

  .zoom-toolbar .n-button {
    padding: 0 4px;
    font-size: 11px;
    height: 20px;
  }

  .zoom-reset-btn {
    min-width: 32px;
    font-size: 10px;
    height: 18px;
    line-height: 18px;
    padding: 0 2px;
  }

  .mode-fit-page .pdf-page-card {
    height: auto;
    max-height: none;
    min-height: calc(560px * var(--zoom, 1));
    width: 100%;
  }

  .mode-fit-page .page-image-wrapper {
    flex: none;
    height: auto;
    min-height: calc(280px * var(--zoom, 1));
  }

  .mode-fit-page .pdf-page-img {
    width: calc(100% * var(--zoom, 1));
    height: auto;
    max-height: none;
    max-width: none;
  }

  .composer-footer {
    padding: 10px 14px;
    gap: 8px;
  }

  .footer-actions {
    width: 100%;
  }

  .footer-actions .n-button {
    flex: 1 1 auto;
  }
}
</style>
