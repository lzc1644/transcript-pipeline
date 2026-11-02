<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import {
  NAlert,
  NButton,
  NInput,
  NInputNumber,
  NSpace,
  NTag,
  useMessage,
} from "naive-ui";
import {
  library,
  LibraryApiError,
  type LibraryBook,
  type LibraryPage,
  type LibraryRange,
  type PageRevision,
} from "../api/library";

const props = defineProps<{
  book: LibraryBook;
}>();

const emit = defineEmits<{
  close: [];
  updated: [book: LibraryBook];
  addRange: [range: LibraryRange];
  startOCR: [book: LibraryBook, pages: number[] | null];
  dirty: [value: boolean];
}>();

const message = useMessage();
const currentPage = ref(1);
const jumpPage = ref<number | null>(1);
const first = ref<number | null>(1);
const last = ref<number | null>(1);
const page = ref<LibraryPage | null>(null);
const text = ref("");
const note = ref("");
const loading = ref(false);
const saving = ref(false);
const previewState = ref<"loading" | "ready" | "error">("loading");
const previewRetry = ref(0);
const panel = ref<"preview" | "text">("preview");
const history = ref<PageRevision[] | null>(null);
const conflict = ref<LibraryPage | null>(null);
const editingMetadata = ref(false);
const metadataConflict = ref<LibraryBook | null>(null);
const metadata = ref<LibraryBook>({ ...props.book, tags: [...props.book.tags] });
const tagText = ref(props.book.tags.join("，"));

let pageRequest = 0;

const changed = computed(() => {
  return page.value !== null && text.value !== (page.value.text ?? "");
});

const metadataChanged = computed(() => {
  return (
    editingMetadata.value &&
    (metadata.value.title !== props.book.title ||
      metadata.value.author !== props.book.author ||
      metadata.value.edition !== props.book.edition ||
      tagText.value !== props.book.tags.join("，"))
  );
});

const canSave = computed(() => {
  return page.value !== null && (changed.value || page.value.current_revision_id === null);
});

const previewUrl = computed(() => {
  return `${library.previewUrl(props.book.id, currentPage.value)}?reload=${previewRetry.value}`;
});

const status = computed(() => {
  if (!page.value) return "加载中";
  if (page.value.current_revision_id !== null) {
    return page.value.source_kind === "manual" ? "人工修订" : "文字已准备";
  }
  if (page.value.ocr_status === "failed") return "识别失败";
  if (page.value.ocr_status === "running") return "正在识别";
  return "尚无文字";
});

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败";
}

async function loadPage(number: number, force = false) {
  if (saving.value) return;
  if (number < 1 || number > props.book.page_count) {
    message.warning(`请输入 1—${props.book.page_count} 的 PDF 物理页码。`);
    return;
  }
  if (!force && number !== currentPage.value && changed.value && !window.confirm("本页有未保存的修改，离开后将丢失。继续吗？")) {
    return;
  }

  const token = ++pageRequest;
  currentPage.value = number;
  jumpPage.value = number;
  first.value = number;
  last.value = number;
  history.value = null;
  conflict.value = null;
  note.value = "";
  previewState.value = "loading";
  loading.value = true;

  try {
    const data = await library.getPage(props.book.id, number);
    if (token !== pageRequest) return;
    page.value = data;
    text.value = data.text ?? "";
  } catch (error) {
    if (token === pageRequest) {
      page.value = null;
      message.error(errorText(error));
    }
  } finally {
    if (token === pageRequest) {
      loading.value = false;
    }
  }
}

async function save() {
  if (!page.value) return;
  saving.value = true;
  try {
    page.value = await library.savePage(
      props.book.id,
      currentPage.value,
      text.value,
      page.value.current_revision_id,
      note.value
    );
    text.value = page.value.text ?? "";
    note.value = "";
    conflict.value = null;
    history.value = null;
    emit("updated", await library.getBook(props.book.id));
  } catch (error) {
    if (error instanceof LibraryApiError && error.status === 409) {
      try {
        conflict.value = await library.getPage(props.book.id, currentPage.value);
      } catch (loadError) {
        message.error(errorText(loadError));
      }
      message.warning("此页已被其他人修改。你的输入仍保留，请比较后选择如何继续。");
    } else {
      message.error(errorText(error));
    }
  } finally {
    saving.value = false;
  }
}

function useLatestBase() {
  if (!conflict.value) return;
  page.value = conflict.value;
  conflict.value = null;
  message.info("已采用最新修订作为基准；你的输入没有改变，请检查后再次保存。");
}

async function showHistory() {
  try {
    history.value = (await library.pageHistory(props.book.id, currentPage.value)).items;
  } catch (error) {
    message.error(errorText(error));
  }
}

async function saveMetadata() {
  try {
    const tags = tagText.value
      .split(/[，,]/)
      .map((x) => x.trim())
      .filter(Boolean);
    const updated = await library.updateBook({
      ...metadata.value,
      tags,
    });
    metadata.value = { ...updated, tags: [...updated.tags] };
    editingMetadata.value = false;
    metadataConflict.value = null;
    emit("updated", updated);
    message.success("书目信息已保存。");
  } catch (error) {
    if (error instanceof LibraryApiError && error.status === 409) {
      try {
        metadataConflict.value = await library.getBook(props.book.id);
      } catch (next) {
        message.error(errorText(next));
      }
      message.warning("书目信息已被其他人修改，当前输入仍保留，请比较后再保存。");
    } else {
      message.error(errorText(error));
    }
  }
}

function useLatestMetadataBase() {
  if (!metadataConflict.value) return;
  metadata.value.version = metadataConflict.value.version;
  metadataConflict.value = null;
  message.info("已采用最新版本作为基准；你的输入没有改变，请检查后再次保存。");
}

function cancelMetadata() {
  metadata.value = { ...props.book, tags: [...props.book.tags] };
  tagText.value = props.book.tags.join("，");
  metadataConflict.value = null;
  editingMetadata.value = false;
}

function refreshPage() {
  if (changed.value && !window.confirm("本页有未保存的修改，刷新将丢失这些修改。继续吗？")) {
    return;
  }
  void loadPage(currentPage.value, true);
}

function closeDetail() {
  if (
    (changed.value || metadataChanged.value) &&
    !window.confirm("当前有未保存的页面或资料修改，离开后将丢失。继续吗？")
  ) {
    return;
  }
  emit("dirty", false);
  emit("close");
}

function addRange() {
  if (
    first.value === null ||
    last.value === null ||
    first.value < 1 ||
    last.value > props.book.page_count ||
    first.value > last.value
  ) {
    message.warning(`请输入 1—${props.book.page_count} 的顺序页段。`);
    return;
  }
  emit("addRange", {
    book_id: props.book.id,
    start_page: first.value,
    end_page: last.value,
  });
}

watch(
  () => props.book,
  (value) => {
    if (!editingMetadata.value) {
      metadata.value = { ...value, tags: [...value.tags] };
      tagText.value = value.tags.join("，");
    }
  }
);

watch(
  () => changed.value || metadataChanged.value,
  (value) => emit("dirty", value)
);

onMounted(() => void loadPage(1, true));
</script>

<template>
  <section class="book-detail">
    <div class="detail-heading">
      <div>
        <n-button text class="back-link" @click="closeDetail">← 返回书库</n-button>
        <h3>{{ book.title }}</h3>
        <p>{{ [book.author, book.edition, ...book.tags].filter(Boolean).join(' · ') || '尚未填写作者或版本' }}</p>
        <p>PDF {{ book.page_count }} 页 · 已识别 {{ book.recognized_pages }} 页 · 文字 {{ book.ready_pages }}/{{ book.page_count }} 页 · 人工修订过 {{ book.edited_pages }} 页<span v-if="book.failed_pages"> · 失败 {{ book.failed_pages }} 页</span></p>
      </div>
      <n-space wrap>
        <n-button @click="editingMetadata = !editingMetadata">
          {{ editingMetadata ? '收起编辑' : '编辑资料' }}
        </n-button>
        <n-button
          type="primary"
          secondary
          :disabled="book.ready_pages >= book.page_count"
          @click="emit('startOCR', book, null)"
        >
          整本预识别
        </n-button>
      </n-space>
    </div>

    <div v-if="editingMetadata" class="metadata-editor">
      <label>
        书名
        <n-input v-model:value="metadata.title" placeholder="输入书名" />
      </label>
      <label>
        作者
        <n-input v-model:value="metadata.author" placeholder="输入作者" />
      </label>
      <label>
        版本
        <n-input v-model:value="metadata.edition" placeholder="输入版本或出版社" />
      </label>
      <label>
        标签（逗号分隔）
        <n-input v-model:value="tagText" placeholder="例如：哲学，教材" />
      </label>
      <n-space>
        <n-button type="primary" @click="saveMetadata">保存资料</n-button>
        <n-button @click="cancelMetadata">取消</n-button>
      </n-space>
      <n-alert
        v-if="metadataConflict"
        type="warning"
        title="书目信息已被其他人修改"
        :bordered="false"
      >
        最新资料：{{ metadataConflict.title }} · {{ metadataConflict.author }} · {{ metadataConflict.edition }} · {{ metadataConflict.tags.join('，') }}<br />
        <n-button size="small" @click="useLatestMetadataBase">以最新版本为基准继续编辑</n-button>
      </n-alert>
    </div>

    <div class="page-controls">
      <div class="page-toolbar">
        <n-button
          size="small"
          :disabled="saving || currentPage <= 1"
          @click="loadPage(currentPage - 1)"
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
            :disabled="saving"
            size="small"
            class="page-number"
            @keyup.enter="jumpPage && loadPage(jumpPage)"
          />
          / {{ book.page_count }} 页
        </label>
        <n-button
          size="small"
          :disabled="saving"
          @click="jumpPage && loadPage(jumpPage)"
        >
          跳转
        </n-button>
        <n-button
          size="small"
          :disabled="saving || currentPage >= book.page_count"
          @click="loadPage(currentPage + 1)"
        >
          下一页
        </n-button>
      </div>

      <div class="mobile-switch" aria-label="页内容视图">
        <n-button
          size="small"
          :type="panel === 'preview' ? 'primary' : 'default'"
          @click="panel = 'preview'"
        >
          原页
        </n-button>
        <n-button
          size="small"
          :type="panel === 'text' ? 'primary' : 'default'"
          @click="panel = 'text'"
        >
          文字
        </n-button>
      </div>

      <div class="range-picker range-picker--desktop">
        <strong>加入参考材料</strong>
        <label>
          PDF 第
          <n-input-number
            v-model:value="first"
            :min="1"
            :max="book.page_count"
            :precision="0"
            :show-button="false"
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
            :input-props="{ 'aria-label': '结束 PDF 页码' }"
            size="small"
            class="page-number"
            @keyup.enter="addRange"
          />
          页
        </label>
        <n-button type="primary" secondary @click="addRange">加入页段</n-button>
      </div>

      <div class="range-picker range-picker--mobile">
        <strong>选页</strong>
        <n-input-number
          v-model:value="first"
          :min="1"
          :max="book.page_count"
          :precision="0"
          :show-button="false"
          :input-props="{ 'aria-label': '起始 PDF 页码' }"
          size="small"
          class="page-number"
        />
        <span>至</span>
        <n-input-number
          v-model:value="last"
          :min="1"
          :max="book.page_count"
          :precision="0"
          :show-button="false"
          :input-props="{ 'aria-label': '结束 PDF 页码' }"
          size="small"
          class="page-number"
        />
        <n-button type="primary" secondary size="small" @click="addRange">加入页段</n-button>
      </div>
    </div>

    <div class="page-columns">
      <section
        class="preview-pane"
        :class="{ 'mobile-hidden': panel !== 'preview' }"
      >
        <h4>原始页面</h4>
        <p v-if="previewState === 'loading'" role="status" class="preview-status">
          正在渲染第 {{ currentPage }} 页…
        </p>
        <n-alert v-if="previewState === 'error'" type="error" :bordered="false">
          页面预览渲染失败，请检查原 PDF 后重试。
          <n-button size="small" @click="previewState = 'loading'; previewRetry++">
            重新加载
          </n-button>
        </n-alert>
        <img
          v-show="previewState === 'ready'"
          :key="previewUrl"
          :src="previewUrl"
          :alt="`PDF 第 ${currentPage} 页`"
          @load="previewState = 'ready'"
          @error="previewState = 'error'"
        />
      </section>

      <section
        class="text-pane"
        :class="{ 'mobile-hidden': panel !== 'text' }"
      >
        <div class="text-heading">
          <h4>第 {{ currentPage }} 页文字</h4>
          <n-tag
            size="small"
            :type="page?.ocr_status === 'failed' ? 'error' : (page?.current_revision_id !== null ? 'success' : 'default')"
          >
            {{ status }}
          </n-tag>
        </div>

        <p v-if="loading" role="status" class="loading-hint">正在读取页面文字…</p>
        <template v-else-if="page">
          <n-alert v-if="page.ocr_status === 'failed'" type="error" :bordered="false">
            识别失败：{{ page.error || '请重试本页。' }}
          </n-alert>
          <p v-if="page.current_revision_id === null" class="muted">
            尚未识别。也可以直接输入文字；保存空白文本会确认这是空白页。
          </p>

          <n-input
            v-model:value="text"
            type="textarea"
            :disabled="saving"
            :autosize="{ minRows: 20 }"
            placeholder="输入或修改本页文字"
            aria-label="页面文字"
          />
          <n-input
            v-model:value="note"
            :disabled="saving"
            placeholder="修订说明（可选）"
            aria-label="修订说明"
          />

          <n-space wrap>
            <n-button
              type="primary"
              :loading="saving"
              :disabled="!canSave"
              @click="save"
            >
              保存修订
            </n-button>
            <n-button @click="showHistory">历史版本</n-button>
            <n-button :disabled="saving" @click="refreshPage">刷新文字</n-button>
            <n-button
              v-if="page.current_revision_id === null"
              @click="emit('startOCR', book, [currentPage])"
            >
              识别本页
            </n-button>
          </n-space>

          <n-alert
            v-if="conflict"
            type="warning"
            title="此页已被其他人修改"
            :bordered="false"
          >
            你的输入保留在上方。最新版本：
            <pre class="history-text">{{ conflict.text || '（空白页）' }}</pre>
            <n-button size="small" :disabled="saving" @click="useLatestBase">
              以最新版本为基准继续编辑
            </n-button>
          </n-alert>

          <div v-if="history !== null" class="history-list">
            <h5>历史版本</h5>
            <p v-if="history.length === 0" class="muted">暂无历史版本。</p>
            <details v-for="item in history" :key="item.id">
              <summary>
                {{ item.created_at }} · {{ item.source_kind }}
                <span v-if="item.note"> · {{ item.note }}</span>
              </summary>
              <pre class="history-text">{{ item.text || '（空白页）' }}</pre>
            </details>
          </div>
        </template>
      </section>
    </div>
  </section>
</template>

<style scoped>
.book-detail {
  display: grid;
  gap: 20px;
  min-width: 0;
}

.detail-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.detail-heading h3 {
  margin: 8px 0 4px;
  font-size: 22px;
  font-weight: 650;
}

.detail-heading p,
.muted {
  margin: 4px 0;
  color: var(--text-secondary);
}

.back-link {
  font-weight: 500;
  color: var(--text-secondary);
}

.back-link:hover {
  color: var(--primary);
}

.metadata-editor {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(190px, 100%), 1fr));
  gap: 12px;
  padding: 16px;
  background: var(--surface-subtle);
  border-radius: 8px;
}

.metadata-editor label {
  display: grid;
  gap: 5px;
  font-size: 13px;
  color: var(--text-primary);
}

.page-controls {
  display: grid;
  gap: 12px;
  min-width: 0;
}

.page-toolbar,
.range-picker {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.page-toolbar label,
.range-picker label {
  display: flex;
  align-items: center;
  gap: 5px;
}

.page-number {
  width: 78px;
}

.page-columns {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 24px;
  align-items: start;
}

.preview-pane,
.text-pane {
  min-width: 0;
  display: grid;
  gap: 12px;
}

.page-columns h4 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}

.preview-status,
.loading-hint {
  color: var(--text-secondary);
}

.preview-pane img {
  display: block;
  width: 100%;
  height: auto;
  background: white;
  box-shadow: 0 2px 14px rgba(0, 0, 0, 0.12);
  border-radius: 4px;
}

.text-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
}

.history-text {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font: inherit;
  max-height: 300px;
  overflow: auto;
  padding: 8px 12px;
  background: var(--surface-canvas);
  border-radius: 6px;
  border: 1px solid var(--border-subtle);
}

.history-list details {
  border-top: 1px solid var(--border-subtle);
  padding: 8px 0;
}

.history-list summary {
  cursor: pointer;
  color: var(--text-secondary);
  font-size: 13px;
}

.history-list h5 {
  margin: 0 0 8px;
  font-size: 14px;
}

.mobile-switch {
  display: none;
}

.range-picker--desktop {
  padding: 8px 0;
}

.range-picker--mobile {
  display: none;
}

@media (max-width: 700px) {
  .page-controls {
    position: sticky;
    top: 0;
    z-index: 20;
    align-self: start;
    gap: 8px;
    padding: 8px 0;
    background: var(--surface-canvas);
    border-bottom: 1px solid var(--border-subtle);
  }

  .page-controls .page-number {
    width: 62px;
  }

  .page-columns {
    grid-template-columns: 1fr;
  }

  .mobile-switch {
    display: flex;
    gap: 8px;
  }

  .mobile-hidden,
  .range-picker--desktop {
    display: none;
  }

  .range-picker--mobile {
    display: flex;
  }
}
</style>
