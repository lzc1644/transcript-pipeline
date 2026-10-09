<script setup lang="ts">
import { RouterLink } from "vue-router";
defineEmits<{ navigate: [] }>();
const groups = [
  { label: "任务", items: [
    { to: "/single-job", label: "新建单任务", icon: "M12 5v14M5 12h14" },
    { to: "/batch-job", label: "批量任务", icon: "M8 4h12v12H8zM4 8v12h12" },
    { to: "/jobs", label: "任务列表", icon: "M9 6h11M9 12h11M9 18h11M4 6h1M4 12h1M4 18h1" },
  ] },
  { label: "工具", items: [
    { to: "/stage-runner", label: "单阶段运行", icon: "M5 4h14v5H5zM5 15h14v5H5zM12 9v6" },
    { to: "/pdf-book-ocr", label: "PDF 书籍 OCR", icon: "M14 3H5v18h14V8zM14 3v5h5M8 13h8M8 17h5" },
  ] },
  { label: "设置", items: [
    { to: "/settings", label: "运行设置", icon: "M4 7h16M4 17h16M9 4v6M15 14v6" },
  ] },
];
</script>

<template>
  <nav class="workbench-nav" aria-label="主导航">
    <div v-for="group in groups" :key="group.label" class="nav-group">
      <p class="nav-group__label">{{ group.label }}</p>
      <RouterLink v-for="item in group.items" :key="item.to" :to="item.to" class="nav-link" active-class="is-active" @click="$emit('navigate')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path :d="item.icon" /></svg>
        <span>{{ item.label }}</span>
      </RouterLink>
    </div>
  </nav>
</template>
