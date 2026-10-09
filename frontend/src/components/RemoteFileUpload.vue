<script setup lang="ts">
import { NButton, NFlex, NTag, useMessage } from "naive-ui";
import { ref } from "vue";

import { uploadFile, type UploadKind } from "../api/client";

const props = defineProps<{
  modelValue: string;
  kind: UploadKind;
  label: string;
  accept?: string;
  buttonText?: string;
}>();

const emit = defineEmits<{
  "update:modelValue": [value: string];
}>();

const message = useMessage();
const fileInput = ref<HTMLInputElement | null>(null);
const uploading = ref(false);
const uploadedName = ref("");

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
    const response = await uploadFile(file, props.kind);
    uploadedName.value = response.name;
    emit("update:modelValue", response.path);
    message.success(`已上传：${response.name}`);
  } catch (caught) {
    message.error(caught instanceof Error ? caught.message : "上传失败");
  } finally {
    uploading.value = false;
    input.value = "";
  }
}
</script>

<template>
  <n-flex align="center" :size="8" wrap class="remote-upload">
    <input
      ref="fileInput"
      type="file"
      :accept="accept"
      class="remote-upload__input"
      @change="handleFileChange"
    />
    <n-button secondary type="primary" :loading="uploading" @click="openFilePicker" class="remote-upload__button">
      {{ buttonText ?? `上传本机${label}` }}
    </n-button>
    <n-tag v-if="uploadedName" size="small" type="success" :bordered="false" class="remote-upload__tag">
      {{ uploadedName }}
    </n-tag>
  </n-flex>
</template>

<style scoped>
.remote-upload {
  min-width: 0;
}

.remote-upload__input {
  display: none;
}

.remote-upload__button {
  border-radius: 8px;
}

.remote-upload__tag { max-width: 100%; height: auto; padding-block: 4px; }
.remote-upload__tag :deep(.n-tag__content) { white-space: normal; overflow-wrap: anywhere; }
</style>
