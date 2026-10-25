<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { NButton } from 'naive-ui';

type Browser = 'chrome' | 'firefox';
type Catalog = { version: string; browsers: Record<Browser, { filename: string; minimumVersion: string }> };
const selected = ref<Browser>(navigator.userAgent.includes('Firefox/') ? 'firefox' : 'chrome');
const catalog = ref<Catalog | null>(null), loading = ref(false), error = ref(''), copied = ref(false), copyError = ref('');
const origin = location.origin;
async function load() {
  loading.value = true; error.value = ''; catalog.value = null;
  try {
    const response = await fetch('/dictation-extension/packages.json', { cache: 'no-store' });
    if (!response.ok) throw new Error();
    const value = await response.json();
    if (value?.kind !== 'development' || typeof value.version !== 'string' || !/^\d+(\.\d+){1,3}$/.test(value.version)) throw new Error();
    for (const browser of ['chrome', 'firefox']) {
      const entry = value.browsers?.[browser];
      if (entry?.filename !== `dictation-${browser}-dev.zip` || typeof entry.minimumVersion !== 'string' || !/^\d+(\.\d+)*$/.test(entry.minimumVersion)) throw new Error();
    }
    catalog.value = value;
  } catch { error.value = '当前服务的扩展包暂不可用，请稍后重试。'; }
  finally { loading.value = false; }
}
async function copyOrigin() {
  copyError.value = '';
  try { await navigator.clipboard.writeText(origin); copied.value = true; }
  catch { copied.value = false; copyError.value = '复制未成功，请选中并复制上方服务地址。'; }
}
onMounted(load);
</script>

<template>
  <section id="extension-install" class="extension-install" aria-label="扩展下载与安装">
    <div class="install-heading"><h3>下载浏览器扩展</h3><span v-if="catalog">开发测试版 {{ catalog.version }}</span></div>
    <p>Ubuntu、Windows 使用同一份 ZIP。当前版本可控制录音，尚不回填文字。</p>
    <div v-if="catalog" class="install-downloads">
      <n-button v-for="browser in (['chrome', 'firefox'] as const)" :key="browser" tag="a"
        :href="`/dictation-extension/${catalog.browsers[browser].filename}?v=${catalog.version}`" :download="catalog.browsers[browser].filename"
        @click="selected = browser">下载 {{ browser === 'chrome' ? 'Chrome' : 'Firefox' }} ZIP</n-button>
      <span class="install-versions">Chrome {{ catalog.browsers.chrome.minimumVersion }}+ · Firefox {{ catalog.browsers.firefox.minimumVersion }}+</span>
    </div>
    <p v-if="loading" role="status">正在读取下载包…</p>
    <div v-if="error" class="install-feedback"><p role="status">{{ error }}</p><n-button v-if="!catalog" size="small" :loading="loading" @click="load">重试</n-button></div>
    <details>
      <summary>安装与连接步骤</summary>
      <div class="install-steps">
        <div class="install-tabs" role="group" aria-label="选择安装说明">
          <button :aria-pressed="selected === 'chrome'" aria-controls="install-guide" @click="selected = 'chrome'">Chrome</button>
          <button :aria-pressed="selected === 'firefox'" aria-controls="install-guide" @click="selected = 'firefox'">Firefox</button>
        </div>
        <div id="install-guide">
          <ol v-if="selected === 'chrome'">
            <li>下载 Chrome ZIP，将文件完整解压到固定文件夹。</li>
            <li>在 Chrome 地址栏输入 <code>chrome://extensions</code>，打开“开发者模式”。</li>
            <li>点击“加载已解压的扩展程序”，选择包含 <code>manifest.json</code> 的解压目录。安装后保留这个目录。</li>
          </ol>
          <template v-else>
            <ol>
              <li>下载 Firefox ZIP，将文件完整解压。</li>
              <li>在 Firefox 地址栏输入 <code>about:debugging#/runtime/this-firefox</code>。</li>
              <li>点击“临时载入附加组件”，选择解压目录内的 <code>manifest.json</code>。</li>
            </ol>
            <p class="install-notice">Firefox 重启后需重新载入。本包未正式签名，请使用上述临时载入入口。</p>
          </template>
          <h4>安装后连接此服务</h4>
          <div class="install-origin"><code>{{ origin }}</code><n-button size="small" @click="copyOrigin">{{ copied ? '已复制' : '复制服务地址' }}</n-button></div>
          <p v-if="copyError" role="status">{{ copyError }}</p>
          <ol>
            <li>从浏览器工具栏的扩展菜单打开“网页语音输入”，填入上方地址，点击“连接并授权此服务”。</li>
            <li v-if="selected === 'chrome'">在自动打开的录音页点击“启用扩展控制并试音”，允许麦克风。</li>
            <li v-else>在录音页启用服务端控制；从扩展面板打开 Firefox 采音侧栏，在侧栏授权并试音。听写时保持侧栏可见。</li>
            <li v-if="selected === 'chrome'">回到目标输入框，默认按 <kbd>Ctrl+Shift+Space</kbd> 开始 / 结束；若快捷键冲突，在 <code>chrome://extensions/shortcuts</code> 查看或改绑。</li>
            <li v-else>回到目标输入框，用侧栏显示的快捷键开始 / 结束。首次使用先确认停止后麦克风释放；Firefox 原生指示器仍待实机验收。</li>
            <li>本轮结束后，在扩展面板点击“放弃当前操作”，再开始下一轮。</li>
          </ol>
        </div>
      </div>
    </details>
  </section>
</template>

<style scoped>
.extension-install { padding: 18px 20px; border: 1px solid var(--border-strong); border-radius: 8px; background: var(--surface-raised); flex: none; font-size: 14px; line-height: 1.65; scroll-margin-top: 20px; }
.install-heading, .install-downloads, .install-origin, .install-feedback { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.install-heading h3 { margin: 0; font-size: 17px; }
.install-heading span, .install-versions { color: var(--text-secondary); font-size: 13px; }
.extension-install p { margin: 8px 0 12px; }
.extension-install summary { margin-top: 14px; cursor: pointer; font-weight: 600; }
.install-steps { padding-top: 14px; }
.install-tabs { display: flex; gap: 8px; }
.install-tabs button { font: inherit; padding: 6px 14px; border: 1px solid var(--border-strong); border-radius: 6px; color: var(--text-primary); background: transparent; cursor: pointer; }
.install-tabs button[aria-pressed="true"] { color: var(--primary); border-color: var(--primary); font-weight: 600; }
.install-tabs button:focus-visible, summary:focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; }
.install-steps ol { margin: 12px 0; padding-left: 24px; }
.install-steps li + li { margin-top: 6px; }
.install-steps h4 { margin: 18px 0 8px; font-size: 14px; }
code { overflow-wrap: anywhere; font-size: 13px; }
.install-notice { border-left: 3px solid var(--primary); padding-left: 12px; color: var(--text-secondary); }
@media(max-width: 600px) { .extension-install { padding: 16px; } .install-versions { width: 100%; } }
</style>
