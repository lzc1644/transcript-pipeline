<script setup lang="ts">
import { NSwitch } from "naive-ui";

defineProps<{
  modelValue: boolean | null;
  label: string;
  disabled?: boolean;
}>();
const emit = defineEmits<{ "update:modelValue": [value: boolean] }>();
</script>

<template>
  <div class="fast-mode-control">
    <span>{{ label }}</span>
    <n-switch
      :value="modelValue ?? false"
      :disabled="disabled"
      :aria-label="label"
      @update:value="emit('update:modelValue', $event)"
    />
  </div>
</template>

<style scoped>
.fast-mode-control {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  width: 100%;
  margin-bottom: 12px;
}
</style>
