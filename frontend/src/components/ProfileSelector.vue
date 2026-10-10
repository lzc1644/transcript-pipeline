<script setup lang="ts">
import { NSelect } from "naive-ui";
import { computed } from "vue";

const props = defineProps<{
  modelValue: string | null;
  options: string[];
  loading?: boolean;
  placeholder?: string;
}>();

const emit = defineEmits<{
  "update:modelValue": [value: string | null];
}>();

const profileLabels: Record<string, string> = {
  local_cpu: "本机 CPU（标准）",
  local_cpu_high_accuracy: "本机 CPU（高精度）",
  wsl2_gpu: "WSL2 GPU（标准）",
  wsl2_gpu_high_accuracy: "WSL2 GPU（高精度）",
  wsl2_gpu_max_accuracy: "WSL2 GPU（最高精度）",
};

function profileLabel(value: string): string {
  return profileLabels[value] ?? `自定义配置（${value}）`;
}

const selectOptions = computed(() => props.options.map((value) => ({ label: profileLabel(value), value })));
</script>

<template>
  <div class="profile-selector">
  <n-select
    :value="modelValue"
    :options="selectOptions"
    :loading="loading"
    :placeholder="placeholder ?? '选择配置方案'"
    clearable
    @update:value="emit('update:modelValue', $event)"
  />
  <p class="prompt-hint">Whisper 的模型、精度和 beam 参数仅用于 Whisper；其它 ASR 不使用这些参数。</p>
  </div>
</template>

<style scoped>
.profile-selector { width: 100%; min-width: 0; }
.prompt-hint { margin: 6px 0 0; }
</style>
