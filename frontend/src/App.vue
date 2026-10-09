<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, computed, watch, nextTick } from "vue";
import { useRoute } from "vue-router";
import {
  NConfigProvider, NDialogProvider, NMessageProvider, NButton,
  NDrawer, NDrawerContent, darkTheme, type GlobalThemeOverrides,
} from "naive-ui";
import NavBar from "./components/NavBar.vue";

const route = useRoute();
const drawerOpen = ref(false);
const menuButton = ref<InstanceType<typeof NButton> | null>(null);
const isDark = ref(localStorage.getItem("transcript-pipeline:theme") === "dark");
const pages: Record<string, [string, string]> = {
  "/single-job": ["任务", "新建单任务"],
  "/batch-job": ["任务", "批量任务"],
  "/jobs": ["任务", "任务列表"],
  "/stage-runner": ["工具", "单阶段运行"],
  "/pdf-book-ocr": ["工具", "PDF 书籍 OCR"],
  "/settings": ["设置", "运行设置"],
};
const pageContext = computed(() => pages[route.path] ?? ["工作台", "读书会整理"]);
function updateClass() {
  document.documentElement.classList.toggle("dark-mode", isDark.value);
  document.documentElement.style.colorScheme = isDark.value ? "dark" : "light";
}
function toggleTheme() {
  isDark.value = !isDark.value;
  localStorage.setItem("transcript-pipeline:theme", isDark.value ? "dark" : "light");
  updateClass();
}
function restoreMenuFocus() {
  if (window.matchMedia("(max-width: 900px)").matches) menuButton.value?.$el?.focus();
}
function onResize() {
  if (window.innerWidth > 900) drawerOpen.value = false;
}
watch(() => route.path, async () => {
  const wasOpen = drawerOpen.value;
  drawerOpen.value = false;
  await nextTick();
  if (!wasOpen) document.querySelector<HTMLElement>("#main-content")?.focus({ preventScroll: true });
});
onMounted(() => { updateClass(); window.addEventListener("resize", onResize); });
onBeforeUnmount(() => window.removeEventListener("resize", onResize));

const themeOverrides = computed<GlobalThemeOverrides>(() => {
  const dark = isDark.value;
  return {
    common: {
      primaryColor: dark ? "#a5a8ff" : "#4f46e5",
      primaryColorHover: dark ? "#bfc1ff" : "#4338ca",
      primaryColorPressed: dark ? "#8b8ff2" : "#3730a3",
      primaryColorSuppl: dark ? "#a5a8ff" : "#4f46e5",
      infoColor: dark ? "#a5a8ff" : "#4f46e5",
      bodyColor: dark ? "#18191c" : "#eeede8",
      cardColor: dark ? "#18191c" : "#eeede8",
      modalColor: dark ? "#202125" : "#f5f4ef",
      popoverColor: dark ? "#24252a" : "#f5f4ef",
      borderColor: dark ? "#36373e" : "#ceccc4",
      ...(!dark ? { inputColor: "#f5f4ef", tableColor: "#eeede8" } : {}),
      textColorBase: dark ? "#eeeef2" : "#23242a",
      textColor1: dark ? "#eeeef2" : "#23242a",
      textColor2: dark ? "#c5c5ce" : "#555761",
      textColor3: dark ? "#a1a2af" : "#62646f",
      borderRadius: "8px",
      fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
    },
    Card: { borderRadius: "8px", titleFontSizeMedium: "16px", titleFontWeight: "600", paddingMedium: "20px", boxShadow: "none" },
    Button: { borderRadiusMedium: "8px", fontWeight: "500" },
    Dialog: { borderRadius: "12px" },
    Drawer: { color: dark ? "#202125" : "#e5e4de" },
    ...(!dark ? {
      Input: { color: "#f5f4ef", colorFocus: "#f5f4ef", colorDisabled: "#e5e4de" },
      Select: { peers: { InternalSelection: { color: "#f5f4ef", colorActive: "#f5f4ef", colorDisabled: "#e5e4de" } } },
      Radio: { buttonColor: "#f5f4ef", buttonColorActive: "#f5f4ef" },
    } : {}),
  };
});
</script>

<template>
  <n-config-provider :theme="isDark ? darkTheme : null" :theme-overrides="themeOverrides">
    <n-dialog-provider>
      <n-message-provider>
        <div class="app-shell">
          <a class="skip-link" href="#main-content">跳至主要内容</a>
          <aside class="app-sidebar" aria-label="工作台导航">
            <div class="app-brand"><span class="app-brand__mark" aria-hidden="true">文</span><div><strong>读书会整理</strong><small>Transcript Pipeline</small></div></div>
            <NavBar />
            <p class="sidebar-note">上传 · 整理 · 人工校对</p>
          </aside>
          <div class="app-workspace">
            <header class="app-header">
              <n-button ref="menuButton" class="mobile-menu-button" quaternary aria-label="打开导航菜单" :aria-expanded="drawerOpen" aria-controls="mobile-navigation" @click="drawerOpen = true">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16" /></svg>
              </n-button>
              <div class="page-context"><span>{{ pageContext[0] }}</span><span aria-hidden="true">/</span><h1>{{ pageContext[1] }}</h1></div>
              <n-button quaternary class="theme-toggle-btn" @click="toggleTheme" :aria-label="isDark ? '切换至浅色模式' : '切换至深色模式'" :title="isDark ? '切换至浅色模式' : '切换至深色模式'">
                <svg v-if="isDark" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>
                <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></svg>
              </n-button>
            </header>
            <main id="main-content" class="app-content" tabindex="-1">
              <router-view v-slot="{ Component }"><transition name="page" mode="out-in"><component :is="Component" /></transition></router-view>
            </main>
          </div>
          <n-drawer v-model:show="drawerOpen" placement="left" :width="288" :auto-focus="true" :trap-focus="true" :close-on-esc="true" @after-leave="restoreMenuFocus">
            <n-drawer-content title="读书会整理" closable :native-scrollbar="false">
              <div id="mobile-navigation"><NavBar @navigate="drawerOpen = false" /></div>
            </n-drawer-content>
          </n-drawer>
        </div>
      </n-message-provider>
    </n-dialog-provider>
  </n-config-provider>
</template>
