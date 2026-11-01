<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { NButton, NSelect, NSpace, useMessage } from "naive-ui";
import { library, type LibraryMaterial, type LibraryRevision } from "../api/library";

const props = defineProps<{ modelValue: LibraryRevision | null }>();
const emit = defineEmits<{ "update:modelValue": [value: LibraryRevision | null] }>();
const message = useMessage();
const materials = ref<LibraryMaterial[]>([]);
const materialId = ref<string | null>(props.modelValue?.material_id ?? null);
const revisions = ref<LibraryRevision[]>([]);
const loading = ref(false);
const materialOptions = computed(() => materials.value.map(item => ({ label: item.name, value: item.id })));
const revisionOptions = computed(() => revisions.value.map(item => ({ label: `第 ${item.revision} 版 · ${item.page_count} 页${item.has_updates ? ' · 页文字有更新' : ''}`, value: item.id })));
async function selectMaterial(id: string | null) {
  materialId.value = id; emit("update:modelValue", null); revisions.value = [];
  if (!id) return;
  loading.value = true;
  try { revisions.value = (await library.revisions(id)).items.filter(item => item.status === "ready"); }
  catch (error) { message.error(error instanceof Error ? error.message : "加载材料版本失败"); }
  finally { loading.value = false; }
}
function selectRevision(id: string | null) { emit("update:modelValue", revisions.value.find(item => item.id === id) ?? null); }
onMounted(async () => {
  try {
    materials.value = (await library.materials()).items.filter(item => item.state !== "deleting");
    if (materialId.value) revisions.value = (await library.revisions(materialId.value)).items.filter(item => item.status === "ready");
  } catch (error) { message.error(error instanceof Error ? error.message : "加载材料失败"); }
});
</script>
<template>
  <div class="library-picker">
    <n-select :value="materialId" :options="materialOptions" placeholder="选择已保存材料" clearable @update:value="selectMaterial" />
    <n-select :value="modelValue?.id ?? null" :options="revisionOptions" :disabled="!materialId" :loading="loading" placeholder="选择固定版本" @update:value="selectRevision" />
    <p v-if="materialId && !loading && !revisions.length">该材料尚无固定版本，请在书库准备缺页并生成版本。</p>
    <p v-if="modelValue">已选择：{{ modelValue.name }} · 第 {{ modelValue.revision }} 版 · {{ modelValue.book_count }} 本书，{{ modelValue.page_count }} 页，文字已准备完成<span v-if="modelValue.has_updates">（书库页文字有更新，当前版本不变）</span></p>
    <n-space><a :href="modelValue ? library.revisionUrl(modelValue.id, 'pdf') : '/library'" target="_blank">{{ modelValue ? '查看材料 PDF' : '打开书库' }}</a><n-button v-if="modelValue" size="tiny" text @click="emit('update:modelValue', null)">更换版本</n-button></n-space>
  </div>
</template>
<style scoped>.library-picker { display:grid; gap:10px; width:100%; }.library-picker p { margin:0; color:var(--text-secondary); }.library-picker a { color:var(--accent-color, #4f46e5); }</style>
