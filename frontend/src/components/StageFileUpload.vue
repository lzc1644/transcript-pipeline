<script setup lang="ts">
import { NButton, NFlex, NTag, useMessage } from "naive-ui";
import { computed, ref, watch } from "vue";

import { uploadStageInput, type StageFileInputSlot } from "../api/client";

const props = defineProps<{
  stageName: string;
  slot: StageFileInputSlot;
  modelValue: string;
}>();

const emit = defineEmits<{
  "update:modelValue": [value: string];
}>();

const message = useMessage();
const fileInput = ref<HTMLInputElement | null>(null);
const uploading = ref(false);
const uploadedName = ref("");
const accept = computed(() => props.slot.extensions.join(","));

watch(
  () => [props.stageName, props.slot.key] as const,
  () => {
    uploadedName.value = "";
  },
);

function openFilePicker() {
  fileInput.value?.click();
}

async function handleFileChange(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) {
    return;
  }

  uploading.value = true;
  try {
    const response = await uploadStageInput(props.stageName, props.slot.key, file);
    uploadedName.value = response.name;
    emit("update:modelValue", response.path);
    message.success(`已上传${props.slot.label}：${response.name}`);
  } catch (caught) {
    message.error(caught instanceof Error ? caught.message : `${props.slot.label}上传失败`);
  } finally {
    uploading.value = false;
    input.value = "";
  }
}
</script>

<template>
  <n-flex align="center" :size="8" wrap class="stage-file-upload">
    <input
      ref="fileInput"
      type="file"
      :accept="accept"
      class="stage-file-upload__input"
      @change="handleFileChange"
    />
    <n-button secondary type="primary" :loading="uploading" @click="openFilePicker">
      选择{{ slot.label }}
    </n-button>
    <n-tag v-if="uploadedName" size="small" type="success" :bordered="false" class="stage-file-upload__name">
      {{ uploadedName }}
    </n-tag>
  </n-flex>
</template>

<style scoped>
.stage-file-upload__input {
  display: none;
}

.stage-file-upload { min-width: 0; width: 100%; }
.stage-file-upload__name { max-width: 100%; height: auto; padding-block: 4px; }
.stage-file-upload__name :deep(.n-tag__content) { white-space: normal; overflow-wrap: anywhere; }
</style>
