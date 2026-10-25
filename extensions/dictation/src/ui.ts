import browser from "webextension-polyfill";
import { message, serviceOrigin, hostPermission } from "../../../shared/dictationExtensionProtocol";
import "./ui.css";

const app = document.querySelector<HTMLElement>("#app")!;
app.innerHTML = `
  <h1>网页语音输入</h1>
  <p class="intro">S1 开发验证：可录音，尚不回填文字。Firefox 需保持采音侧栏可见。</p>
  <p id="status" role="status"></p>
  <label for="origin">服务地址（含端口）</label>
  <input id="origin" type="url" placeholder="https://192.168.0.252:8443">
  <button id="connect">连接并授权此服务</button>
  <div class="actions">
    <button id="open">打开录音页</button>
    <button id="sidebar" hidden>打开 Firefox 采音侧栏</button>
    <button id="toggle">开始 / 结束</button>
    <button id="cancel">取消回填并停止</button>
    <button id="discard">放弃当前操作</button>
  </div>
  <pre id="state"></pre>`;
const input = document.querySelector<HTMLInputElement>("#origin")!;
const status = document.querySelector<HTMLElement>("#status")!;
const phaseLabels: Record<string, string> = {
  preparing: "正在准备", recording: "正在听写", draining: "正在处理 · 麦克风已请求关闭",
  completed: "本轮已完成 · 放弃当前操作后可开始下一轮", cancelled: "本轮已取消",
  failed: "本轮失败", interrupted: "本轮中断",
};
async function refresh() {
  const value = await browser.runtime.sendMessage(message("UI_STATE")) as {
    origin: string; operation: { phase?: string; error?: string; closeIntent?: { pagePending: boolean; capturePending: boolean } } | null;
  };
  input.value ||= value.origin;
  const op = value.operation;
  document.querySelector("#state")!.textContent = op
    ? (phaseLabels[op.phase ?? ""] ?? "等待状态") +
      (op.closeIntent?.pagePending || op.closeIntent?.capturePending ? "\n关闭尚待确认 · 保留原绑定，可重试关闭" : "") + (op.error ? "\n" + op.error : "")
    : "尚无操作。请先在服务录音页启用控制。";
}
function action(fn: () => Promise<unknown>) {
  return () => {
    status.textContent = "";
    void fn().then(refresh).catch(cause => { status.textContent = cause instanceof Error ? cause.message : "连接失败"; });
  };
}
document.querySelector("#connect")!.addEventListener("click", action(async () => {
  const origin = serviceOrigin(input.value); input.value = origin;
  // Request from the extension's user gesture, never from a page message.
  if (!await browser.permissions.request({ origins: [hostPermission(origin)] })) throw new Error("网站权限未授予");
  await browser.runtime.sendMessage(message("CONNECT", { data: { origin } }));
}));
for (const [selector, type] of [["#open", "OPEN_RECORDER"], ["#toggle", "TOGGLE"], ["#cancel", "CANCEL"], ["#discard", "DISCARD"]] as const)
  document.querySelector(selector)!.addEventListener("click", action(() => browser.runtime.sendMessage(message(type))));

const sidebarButton = document.querySelector<HTMLButtonElement>("#sidebar")!;
if (browser.runtime.getManifest().sidebar_action && browser.sidebarAction) {
  sidebarButton.hidden = false;
  // Call directly in the click handler: Firefox requires the real user gesture.
  sidebarButton.addEventListener("click", action(() => browser.sidebarAction.open()));
}
void refresh();
