<script setup lang="ts">
import {
  NAlert,
  NButton,
  NCard,
  NCheckbox,
  NEmpty,
  NFlex,
  NInput,
  NModal,
  NSpin,
  NTag,
  useMessage,
} from "naive-ui";
import { computed, ref, watch } from "vue";

import { listFs, type FileItem } from "../api/client";

const props = defineProps<{
  modelValue: string;
  mode: "file" | "dir";
  label: string;
  buttonText?: string;
}>();

const emit = defineEmits<{
  "update:modelValue": [value: string];
}>();

const message = useMessage();
const visible = ref(false);
const loading = ref(false);
const loadError = ref("");
const currentPath = ref("");
const parentPath = ref<string | null>(null);
const selectedPath = ref("");
const showHidden = ref(false);
const items = ref<FileItem[]>([]);

const storageKey = computed(() => `transcript-pipeline:file-browser:${props.label}`);

const segments = computed(() => {
  if (!currentPath.value) {
    return [];
  }
  const parts = currentPath.value.split("/").filter(Boolean);
  if (currentPath.value === "/") {
    return [{ label: "/", path: "/" }];
  }
  const mapped = parts.map((part, index) => ({
    label: part,
    path: `/${parts.slice(0, index + 1).join("/")}`,
  }));
  return [{ label: "/", path: "/" }, ...mapped];
});

const confirmDisabled = computed(() => {
  if (props.mode === "dir") {
    if (!currentPath.value) {
      return true;
    }
    if (!selectedPath.value || selectedPath.value === currentPath.value) {
      return false;
    }
  }
  if (!selectedPath.value) {
    return true;
  }
  const selectedItem = items.value.find((item) => item.path === selectedPath.value);
  if (!selectedItem) {
    return false;
  }
  return props.mode === "dir" ? !selectedItem.is_dir : selectedItem.is_dir;
});

async function loadEntries(targetPath: string | null) {
  loading.value = true;
  loadError.value = "";
  try {
    const response = await listFs(targetPath, "all", showHidden.value);
    currentPath.value = response.current_path;
    parentPath.value = response.parent_path;
    items.value = response.items;
  } catch (caught) {
    loadError.value = caught instanceof Error ? caught.message : "目录读取失败";
    message.error(caught instanceof Error ? caught.message : "目录读取失败");
  } finally {
    loading.value = false;
  }
}

function open() {
  visible.value = true;
  selectedPath.value = looksLikeLocalPath(props.modelValue) ? props.modelValue : "";
  const rememberedPath = localStorage.getItem(storageKey.value);
  void loadEntries(initialBrowsePath(props.modelValue, rememberedPath));
}

function looksLikeLocalPath(value: string): boolean {
  return value.startsWith("/") || value.startsWith("~");
}

function initialBrowsePath(value: string, rememberedPath: string | null): string | null {
  if (!looksLikeLocalPath(value)) {
    return rememberedPath;
  }
  if (props.mode === "dir") {
    return value;
  }
  const slashIndex = value.lastIndexOf("/");
  return slashIndex > 0 ? value.slice(0, slashIndex) : rememberedPath;
}

function close() {
  visible.value = false;
}

function selectItem(item: FileItem) {
  if (props.mode === "dir" && !item.is_dir) {
    return;
  }
  selectedPath.value = item.path;
}

function activateItem(item: FileItem) {
  if (item.is_dir) {
    void loadEntries(item.path);
    return;
  }
  if (props.mode === "file") {
    selectedPath.value = item.path;
    confirmSelection();
  }
}

function confirmSelection() {
  if (confirmDisabled.value) {
    return;
  }
  emit("update:modelValue", selectedPath.value || currentPath.value);
  localStorage.setItem(storageKey.value, selectedPath.value || currentPath.value);
  close();
}

function confirmCurrentDirectory() {
  if (props.mode !== "dir" || !currentPath.value) {
    return;
  }
  selectedPath.value = currentPath.value;
  confirmSelection();
}

watch(showHidden, () => {
  if (!visible.value) {
    return;
  }
  void loadEntries(currentPath.value);
});
</script>

<template>
  <div class="file-browser-field">
    <n-input
      :value="modelValue"
      :placeholder="`${label} 路径，可直接粘贴`"
      clearable
      @update:value="emit('update:modelValue', $event)"
      class="file-browser-field__input"
    />
    <n-button type="primary" @click="open" class="file-browser-field__button">
      {{ buttonText ?? `选择${label}` }}
    </n-button>
  </div>

  <n-modal v-model:show="visible" preset="card" :style="{ width: 'min(880px, 94vw)', borderRadius: '16px' }">
    <n-card :title="`选择 ${label}`" closable @close="close" class="modal-card">
      <n-flex vertical :size="16">
        <n-flex justify="space-between" align="center" wrap class="browser-toolbar">
          <n-flex align="center" wrap class="breadcrumbs-container">
            <n-button quaternary size="small" :disabled="!parentPath" @click="parentPath && loadEntries(parentPath)" class="up-btn">
              <template #icon>
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="width:14px;height:14px"><path d="M18 15l-6-6-6 6"/></svg>
              </template>
              返回上级
            </n-button>
            <div class="breadcrumb-trail">
              <template v-for="(segment, idx) in segments" :key="segment.path">
                <span v-if="idx > 0" class="breadcrumb-separator">/</span>
                <button type="button" class="breadcrumb-item-text" @click="loadEntries(segment.path)">
                  {{ segment.label }}
                </button>
              </template>
            </div>
          </n-flex>
          <n-checkbox v-model:checked="showHidden" class="hidden-files-check">显示隐藏文件</n-checkbox>
        </n-flex>

        <n-input :value="currentPath" readonly size="small" class="current-path-display" />

        <n-alert v-if="loadError" type="error" title="无法读取服务器目录" :bordered="false">{{ loadError }}<n-button text @click="loadEntries(currentPath || null)">重试</n-button></n-alert>
        <n-spin :show="loading">
          <div class="file-browser__list">
            <n-empty v-if="!loading && !loadError && items.length === 0" description="当前目录为空" />
            <button
              v-for="item in items"
              :key="item.path"
              type="button"
              class="file-browser__item"
              :class="{ 'is-selected': selectedPath === item.path, 'is-disabled': mode === 'dir' && !item.is_dir }"
              :aria-pressed="selectedPath === item.path"
              :disabled="mode === 'dir' && !item.is_dir"
              @keydown.enter.prevent="activateItem(item)"
              @click="selectItem(item)"
              @dblclick="activateItem(item)"
            >
              <n-flex align="center" :size="12">
                <!-- Gorgeous Left Icon -->
                <div class="item-icon-wrapper" :class="item.is_dir ? 'is-dir' : 'is-file'">
                  <svg v-if="item.is_dir" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" class="file-svg"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
                  <svg v-else xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" class="file-svg"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>
                </div>
                <div>
                  <strong class="item-name">{{ item.name }}</strong>
                  <div class="file-browser__path">{{ item.path }}</div>
                </div>
              </n-flex>
              <n-tag :type="item.is_dir ? 'info' : 'default'" size="small" round :bordered="false" class="item-tag">
                {{ item.is_dir ? "目录" : "文件" }}
              </n-tag>
            </button>
          </div>
        </n-spin>

        <n-flex justify="space-between" align="center" class="browser-footer">
          <span class="file-browser__hint">
            {{ mode === "dir" ? "可直接选择当前目录，也可单击子目录后确认。" : "双击文件可直接确认。" }}
          </span>
          <n-flex :size="10">
            <n-button v-if="mode === 'dir'" secondary type="primary" :disabled="!currentPath" @click="confirmCurrentDirectory" class="footer-btn">
              选择当前目录
            </n-button>
            <n-button @click="close" class="footer-btn">取消</n-button>
            <n-button type="primary" :disabled="confirmDisabled" @click="confirmSelection" class="footer-btn">
              {{ mode === "dir" ? "确认目录" : "确认选择" }}
            </n-button>
          </n-flex>
        </n-flex>
      </n-flex>
    </n-card>
  </n-modal>
</template>

<style scoped>
.modal-card { background: var(--surface-overlay); }
.browser-toolbar { background: var(--surface-subtle); padding: 12px; border-radius: 8px; }
.breadcrumbs-container { flex: 1; min-width: 0; }
.breadcrumb-trail { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; font-size: 13px; }
.breadcrumb-separator { color: var(--text-muted); }
.breadcrumb-item-text { color: var(--primary); cursor: pointer; padding: 4px; border: 0; background: transparent; border-radius: 4px; overflow-wrap: anywhere; }
.breadcrumb-item-text:hover { background: var(--primary-alpha-10); }
.current-path-display { font-family: ui-monospace, monospace; }
.item-icon-wrapper { display: flex; align-items: center; justify-content: center; width: 28px; height: 28px; flex: none; color: var(--text-muted); }
.item-icon-wrapper.is-dir { color: var(--primary); }
.file-svg { width: 18px; height: 18px; }
.file-browser__item > .n-flex { flex: 1; min-width: 0; }
.item-name { font-size: 13px; overflow-wrap: anywhere; }
.item-tag { flex: none; font-size: 11px; }
.browser-footer { margin-top: 8px; }
@media (max-width: 640px) {
  .file-browser-field { grid-template-columns: minmax(0, 1fr); }
  .browser-footer { align-items: stretch; flex-direction: column; }
  .file-browser__item { align-items: flex-start; }
}
</style>
