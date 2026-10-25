---
kind: issue
title: "网页下载扩展 ZIP 并指导安装"
type: feature
status: open
created: 2026-10-24
---

# 网页下载扩展 ZIP 并指导安装

执行状态：2026-10-25 用户要求暂停扩展开发并保存一次本地 commit。ZIP 下载/说明已交付，现有部署保留；本事项保持 open，暂停交接与恢复顺序见[执行记录](../../docs/BROWSER_DICTATION_EXTENSION_IMPLEMENTATION.md#暂停检查点2026-10-25)。

用户选择先用 ZIP 开发测试包，不等待商店或签名；Ubuntu 与 Windows 都提供安装说明。已另行授权更新现有 8443 页面，部署前确认空闲并保留回退，不调用校对、不提交商店。

## 交付与边界

普通语音输入页能找到扩展下载入口；扩展录音页提供两个实际 ZIP 下载、各浏览器的解压/加载步骤、当前服务地址和连接说明。Firefox 明确为临时载入，重启需重载；Chrome 使用开发者模式加载解压目录。S1 尚不回填文字及 Firefox 指示器待验收状态保留。

ZIP 来自同一扩展源码的两端构建，压缩包根目录包含 manifest 与必需资源。Docker 更新时自动重建归档并随前端发布，不手工修改运行容器。不改变录音、校对默认值或后台关闭状态机。

## 局部界面

```text
普通语音输入页标题                    [扩展下载与安装]
                                      ↓
扩展录音页标题 / 原停止控制（保留）
下载浏览器扩展 · 开发测试版 / 版本
[下载 Chrome ZIP] [下载 Firefox ZIP]
▸ 安装与连接步骤
  [Chrome] [Firefox] → 对应加载步骤
  服务地址 [复制] → 扩展连接 / Firefox 侧栏说明
原听写正文与录音准备
```

两端下载始终可见，浏览器识别只选择默认说明；包缺失时显示不可用及重试，不给坏链接。窄屏纵向排布，展开说明使用正常文档流，不覆盖停止控件。

## 验证

检查归档完整性、无构建依赖/私密文件、版本及资源引用；实际页面下载并解压，检查安装说明切换/复制地址/缺包反馈、桌面与窄屏渲染。使用项目 pytest、前端及 Docker 构建和真实 HTTPS 下载验收；原录音状态证据按未改变范围复用。完成不自动关闭本事项或 commit/push。

## 执行结果：0.1.1 已部署，可从页面下载

入口：`https://192.168.0.252:8443/dictation` → “扩展下载与安装”；也可直接打开 `/dictation/extension#extension-install`。

- 新增 `DictationExtensionInstall.vue`，两端下载、按浏览器切换的说明、动态服务地址和复制反馈；保留原停止控件。Chrome 的快捷键设置地址和第二轮前放弃旧操作的步骤也已写明。说明是原生可展开区，浏览器切换用可键盘操作的选择按钮。
- 新增 `extensions/dictation/package.mjs` 及 `package:dev`，生成两个 ZIP / `INSTALL.txt` / `packages.json`，核对必需资源与两端版本。Docker 从源码重建；宿主生成目录不进入 Git 或 Docker 上下文。
- 打包依赖初选 `fflate 0.8.2`，构建审计发现其 ZIP64 解压循环漏洞后，发布前升级至 **0.8.3**；扩展工程当前 `npm audit` 为 **0 项**，没有做强制批量升级。

### 验证证据

发布目录：`tmp/deploy/browser-extension-zip-20261025T064222Z/`；汇总 `report.json`，健康/配置验收 `health-report.json`，回退入口 `RECOVERY.md`。

| 检查 | 结果 |
|---|---|
| `.venv/bin/python -m pytest` | **819 passed / 52.33s**，1 条既有 Starlette/httpx 弃用警告 |
| 扩展类型检查、`npm test`、两端构建/打包 | 通过，**43 项**测试（含 4 项 ZIP 回归）：根 manifest、版本/资源、HTML 引用、文件清单、缺资源/版本不符/软链接拒绝 |
| 前端生产构建、Docker 构建 | 通过；本轮依赖修正后的镜像见 `final-build.log` |
| 原网页桥接回归 | **8 组通过**；`tmp/browser-dictation-extension/bridge-1792910475420/report.json` |
| 隔离页面安装引导测试 | **5 组通过**；`tmp/browser-dictation-extension/install-1792910474250/report.json` |
| 真实可信 HTTPS 页面 | **5 组通过**；`lan-ui/report.json`。实际按钮下载 Chrome **25,766** 字节、Firefox **33,849** 字节；两包 manifest 为 **0.1.1**，并检查桌面 / 390px 展开截图、复制拒绝和缺包重试 |
| 实际下载包安装 | **Linux Chrome 155.0.8059.12 / 原生 Firefox 154.0 均通过**；`package-install-report.json`、`chrome-installed.png`、`firefox-installed.png`。Chrome 解压后在独立开发配置加载，Firefox 用原生临时安装接口载入该 ZIP；设置页可用、实际版本一致 |

切换前两次确认 102 份任务/听写状态无活动项、无 worker 或在途 HTTP 进程；保留上个 S1 镜像后更新现有单容器。HTTP / 原 CA 验证的 HTTPS 均正常，API/Caddy RUNNING、容器 healthy，原数据/模型/CA 卷、端口、GPU 与服务配置一致。本轮 LAN 验证只发 GET，不创建录音/任务、不调用模型；POST/WSS 使用未改变实现的前轮有效证据。

构建另报告**既有前端依赖的 10 项 high 审计告警**，完整结果在 `npm-audit-frontend.json`。涉及 Vue SSR、开发工具链及 lodash 等；当前服务只发布静态前端，不运行 Vite dev server / Vue SSR。本轮没有更改该依赖锁或扩大到全站依赖升级，余下依赖风险需单独维护。

按 `docs/REVIEW_CHECKLIST.md` 自检：本轮下载与说明已达到交付目标，未改采音/后台状态机、校对默认值、上游产物或历史数据。Windows 安装/运行、物理停麦与 Firefox 指示器仍由有设备的执行者验收；S1 未因此完成，不进入 S2。

### 收尾候选

本事项保持 open。用户已授权本次暂停检查点执行本地 commit，未授权 push 或关闭事项。用户授权关闭后，可将“普通页入口 → ZIP 下载 / 对应浏览器开发加载 / 动态服务地址 / Docker 自动打包”的稳定使用与构建契约回写相应项目规格；商店发布和正式安装仍是另外授权的后续事项。
