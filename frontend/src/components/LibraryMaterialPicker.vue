<script setup lang="ts">
import { computed, ref } from "vue";
import { NAlert, NButton, NSelect, NSpace, NTag, useMessage } from "naive-ui";
import { library, type LibraryMaterial, type LibraryRevision } from "../api/library";
import LibraryMaterialComposer from "./LibraryMaterialComposer.vue";

const props = defineProps<{
  modelValue: LibraryRevision | null;
}>();

const emit = defineEmits<{
  "update:modelValue": [value: LibraryRevision | null];
}>();

const message = useMessage();
const composerOpen = ref(false);
const composerKey = ref(0);
const composedRevisionId = ref<string | null>(null);
const reuseOpen = ref(false);
const materials = ref<LibraryMaterial[]>([]);
const materialId = ref<string | null>(null);
const revisions = ref<LibraryRevision[]>([]);
const loading = ref(false);
const reuseLoading = ref(false);
const reuseError = ref("");
let revisionRequest = 0;

const materialOptions = computed(() =>
  materials.value.map((item) => ({
    label: item.name,
    value: item.id,
  }))
);

const revisionOptions = computed(() =>
  revisions.value.map((item) => ({
    label: `第 ${item.revision} 版 · ${item.page_count} 页${item.has_updates ? " · 页文字有更新" : ""}`,
    value: item.id,
  }))
);

function openComposer() {
  reuseOpen.value = false;
  composerOpen.value = true;
}

defineExpose({ openComposer });

function useComposed(revision: LibraryRevision) {
  composedRevisionId.value = revision.id;
  emit("update:modelValue", revision);
}

async function toggleReuse() {
  reuseOpen.value = !reuseOpen.value;
  if (!reuseOpen.value) return;

  reuseLoading.value = true;
  reuseError.value = "";
  try {
    const res = await library.materials();
    materials.value = res.items.filter((item) => item.state !== "deleting");
  } catch (error) {
    reuseError.value = error instanceof Error ? error.message : "加载材料失败";
  } finally {
    reuseLoading.value = false;
  }
}

async function selectMaterial(id: string | null) {
  const request = ++revisionRequest;
  materialId.value = id;
  revisions.value = [];
  loading.value = Boolean(id);
  if (!id) return;

  try {
    const result = await library.revisions(id);
    if (request === revisionRequest) {
      revisions.value = result.items.filter((item) => item.status === "ready");
    }
  } catch (error) {
    if (request === revisionRequest) {
      message.error(error instanceof Error ? error.message : "加载材料版本失败");
    }
  } finally {
    if (request === revisionRequest) {
      loading.value = false;
    }
  }
}

function selectRevision(id: string | null) {
  const revision = revisions.value.find((item) => item.id === id);
  if (!revision) return;

  // Reusing a saved version must not reopen an unrelated local composition.
  composerKey.value++;
  composedRevisionId.value = null;
  emit("update:modelValue", revision);
  reuseOpen.value = false;
}
</script>

<template>
  <div class="library-picker">
    <div v-if="modelValue" class="selected-material" aria-live="polite">
      <div class="selected-material__header">
        <strong>{{ modelValue.name }} · 第 {{ modelValue.revision }} 版</strong>
        <n-tag v-if="modelValue.has_updates" size="small" type="warning" :bordered="false">
          页文字有更新
        </n-tag>
      </div>
      <p>{{ modelValue.book_count }} 本书，{{ modelValue.page_count }} 页，文字已准备完成</p>
      <p v-if="modelValue.has_updates" class="update-notice">
        书库页文字有更新；本次仍使用已选版本。
      </p>
      <n-space align="center">
        <n-button secondary @click="openComposer">
          {{ composedRevisionId === modelValue.id ? '调整选页' : '选书组装新材料' }}
        </n-button>
        <a
          :href="library.revisionUrl(modelValue.id, 'pdf')"
          target="_blank"
          rel="noopener"
          class="preview-pdf-link"
        >
          查看材料 PDF
        </a>
      </n-space>
    </div>

    <div v-else class="compose-start">
      <n-button type="primary" secondary @click="openComposer">
        选书与组合参考材料
      </n-button>
      <p>预览原页，跨书选取本次需要的页段。</p>
    </div>

    <n-button
      text
      class="reuse-toggle"
      :aria-expanded="reuseOpen"
      @click="toggleReuse"
    >
      {{ reuseOpen ? '收起已有材料' : '复用已有材料' }}
    </n-button>

    <div v-if="reuseOpen" class="reuse-panel">
      <n-alert v-if="reuseError" type="error" :bordered="false" role="alert">
        {{ reuseError }}
      </n-alert>
      <n-select
        :value="materialId"
        :options="materialOptions"
        :loading="reuseLoading"
        placeholder="选择已保存材料"
        clearable
        @update:value="selectMaterial"
      />
      <n-select
        :value="modelValue?.material_id === materialId ? modelValue?.id : null"
        :options="revisionOptions"
        :disabled="!materialId"
        :loading="loading"
        placeholder="选择固定版本"
        @update:value="selectRevision"
      />
      <p v-if="!reuseLoading && !reuseError && !materials.length" class="muted">
        暂无已保存材料，请先选书组装。
      </p>
      <p v-else-if="materialId && !loading && !revisions.length" class="muted">
        该材料尚无可用版本，请在 PDF 处理中补齐缺页并生成版本。
      </p>
    </div>

    <LibraryMaterialComposer
      :key="composerKey"
      v-model:show="composerOpen"
      purpose="task"
      @selected="useComposed"
    />
  </div>
</template>

<style scoped>
.library-picker {
  display: grid;
  gap: 12px;
  width: 100%;
}

.library-picker p {
  margin: 0;
  color: var(--text-secondary);
}

.library-picker a {
  color: var(--accent-color, var(--primary));
}

.compose-start,
.selected-material,
.reuse-panel {
  display: grid;
  gap: 10px;
}

.compose-start {
  justify-items: start;
}

.selected-material {
  border-left: 3px solid var(--accent-color, var(--primary));
  padding-left: 14px;
  background: var(--surface-subtle);
  padding: 12px 14px;
  border-radius: 0 8px 8px 0;
}

.selected-material__header {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.update-notice {
  color: var(--color-warning) !important;
  font-size: 13px;
}

.preview-pdf-link {
  font-size: 13px;
  text-decoration: underline;
  text-underline-offset: 3px;
}

.preview-pdf-link:hover {
  opacity: 0.85;
}

.reuse-toggle {
  justify-self: start;
  color: var(--text-secondary);
  font-size: 13px;
}

.reuse-panel {
  padding: 14px;
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  background: var(--surface-subtle);
}

.muted {
  font-size: 13px;
  color: var(--text-muted);
}
</style>
