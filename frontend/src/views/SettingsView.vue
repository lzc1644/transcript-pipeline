<script setup lang="ts">
import {
  NAlert,
  NButton,
  NCard,
  NForm,
  NFormItem,
  NGrid,
  NGridItem,
  NInput,
  NSelect,
  NSpace,
  NSwitch,
  NTag,
  useMessage,
} from "naive-ui";
import { computed, onMounted, reactive, ref } from "vue";

import { getFrontendSettings, saveFrontendSettings, type FrontendSettings } from "../api/client";
import AsrCandidateSelector from "../components/AsrCandidateSelector.vue";
import { useConfigOptions } from "../composables/useConfigOptions";
const { asrCandidates, loading: configLoading } = useConfigOptions();

const message = useMessage();
const loading = ref(false);
const saving = ref(false);
const loadedSettings = ref<FrontendSettings | null>(null);

const form = reactive({
  codex_lb_base_url: "",
  codex_lb_api_key: "",
  clear_codex_lb_api_key: false,
  codex_lb_bypass_proxy: false,
  model: "",
  asr_candidate: "",
  reasoning_effort: "high",
  ocr_model: "",
  ocr_reasoning_effort: "high",
});

const reasoningOptions = [
  { label: "低", value: "low" },
  { label: "中", value: "medium" },
  { label: "高", value: "high" },
  { label: "很高", value: "xhigh" },
  { label: "最高", value: "max" },
];

const modelOptions = [
  { label: "GPT-6.1 Sol（阶段 6 精修）", value: "gpt-6.1-sol" },
  { label: "GPT-6 Luna（PDF OCR）", value: "gpt-6-luna" },
];

const apiKeyStatus = computed(() => {
  if (form.codex_lb_api_key.trim()) {
    return "本次会写入新 API key";
  }
  if (form.clear_codex_lb_api_key) {
    return "保存后会清除已保存 API key";
  }
  if (loadedSettings.value?.has_codex_lb_api_key) {
    return "已保存或已通过环境变量提供";
  }
  return "尚未配置";
});

function applySettings(settings: FrontendSettings) {
  loadedSettings.value = settings;
  form.codex_lb_base_url = settings.codex_lb_base_url;
  form.codex_lb_api_key = "";
  form.clear_codex_lb_api_key = false;
  form.codex_lb_bypass_proxy = settings.codex_lb_bypass_proxy ?? false;
  form.model = settings.model;
  form.asr_candidate = settings.asr_candidate || "whisper-existing";
  form.reasoning_effort = settings.reasoning_effort;
  form.ocr_model = settings.ocr_model;
  form.ocr_reasoning_effort = settings.ocr_reasoning_effort;
}

async function loadSettings() {
  loading.value = true;
  try {
    applySettings(await getFrontendSettings());
  } catch (caught) {
    message.error(caught instanceof Error ? caught.message : "加载设置失败");
  } finally {
    loading.value = false;
  }
}

async function saveSettings() {
  saving.value = true;
  try {
    const settings = await saveFrontendSettings({
      codex_lb_base_url: form.codex_lb_base_url,
      codex_lb_api_key: form.codex_lb_api_key || null,
      clear_codex_lb_api_key: form.clear_codex_lb_api_key,
      codex_lb_bypass_proxy: form.codex_lb_bypass_proxy,
      model: form.model,
      asr_candidate: form.asr_candidate,
      reasoning_effort: form.reasoning_effort,
      ocr_model: form.ocr_model,
      ocr_reasoning_effort: form.ocr_reasoning_effort,
    });
    applySettings(settings);
    message.success("设置已保存");
  } catch (caught) {
    message.error(caught instanceof Error ? caught.message : "保存设置失败");
  } finally {
    saving.value = false;
  }
}

onMounted(loadSettings);
</script>

<template>
  <n-space vertical :size="20" class="settings-view">
    <section class="view-hero">
      <div>
        <p class="view-hero__eyebrow">运行设置</p>
        <h2 class="view-hero__title">连接与模型默认值</h2>
        <p class="view-hero__copy">这里维护全局连接和模型默认值；每次任务的配置方案、推理服务和 OCR 服务在任务页选择。</p>
      </div>
      <n-button type="primary" ghost :loading="loading" @click="loadSettings">刷新</n-button>
    </section>

    <n-alert type="info" title="API key 存储位置">
      API key 会保存到 {{ loadedSettings?.settings_path || "data/jobs/frontend-settings.json" }}。该目录已被 .gitignore 忽略。
    </n-alert>

    <n-grid :cols="2" :x-gap="18" :y-gap="18" responsive="screen" item-responsive>
      <n-grid-item span="2 m:1">
        <n-card title="CPA / OpenAI 兼容 API 连接" class="view-card settings-card">
          <n-form label-placement="top">
            <n-form-item label="Base URL">
              <n-input
                v-model:value="form.codex_lb_base_url"
                placeholder="http://127.0.0.1:8317/v1 或 https://你的网关域名/v1"
              />
            </n-form-item>
            <p style="font-size: 12px; color: var(--n-text-color-3, #8c8c8c); line-height: 1.5;">
              支持网关根地址或带 /v1 的地址；校对与 OCR 均调用 /v1/responses。模型名称须与 CPA 提供的一致。
            </p>
            <n-form-item label="API Key">
              <n-input
                v-model:value="form.codex_lb_api_key"
                type="password"
                show-password-on="click"
                placeholder="填写 CPA 客户端 API Key；留空则保持现有 key"
              />
            </n-form-item>
            <n-form-item label="API 直连（绕过代理）">
              <n-space vertical :size="4" style="width: 100%;">
                <n-switch v-model:value="form.codex_lb_bypass_proxy" />
                <div style="font-size: 12px; color: var(--n-text-color-3, #8c8c8c); line-height: 1.5;">
                  开启后仅 API 网关地址绕过系统代理；其他网站仍使用原代理。保存后对新任务生效。
                </div>
              </n-space>
            </n-form-item>
            <div class="settings-card__meta">
              <n-tag :type="loadedSettings?.has_codex_lb_api_key ? 'success' : 'warning'" round>
                {{ apiKeyStatus }}
              </n-tag>
              <n-button text type="error" @click="form.clear_codex_lb_api_key = !form.clear_codex_lb_api_key">
                {{ form.clear_codex_lb_api_key ? "取消清除" : "清除已保存 key" }}
              </n-button>
            </div>
          </n-form>
        </n-card>
      </n-grid-item>

      <n-grid-item span="2 m:1">
        <n-card title="模型默认值" class="view-card settings-card">
          <n-form label-placement="top">
            <n-form-item label="默认语音转文字模型">
              <AsrCandidateSelector v-model="form.asr_candidate" :options="asrCandidates" :loading="configLoading" />
            </n-form-item>
            <n-form-item label="阶段 6 模型">
              <n-select v-model:value="form.model" :options="modelOptions" />
            </n-form-item>
            <n-form-item label="阶段 6 推理强度">
              <n-select v-model:value="form.reasoning_effort" :options="reasoningOptions" />
            </n-form-item>
            <n-form-item label="PDF OCR 模型">
              <n-select v-model:value="form.ocr_model" :options="modelOptions" />
            </n-form-item>
            <n-form-item label="PDF OCR 推理强度">
              <n-select v-model:value="form.ocr_reasoning_effort" :options="reasoningOptions" />
            </n-form-item>
          </n-form>
        </n-card>
      </n-grid-item>
    </n-grid>

    <div class="settings-actions">
      <n-button type="primary" size="large" :loading="saving" @click="saveSettings">保存设置</n-button>
    </div>
  </n-space>
</template>
