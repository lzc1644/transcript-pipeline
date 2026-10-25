# 浏览器语音输入扩展：S1 侧栏接入与验证记录

日期：2026-10-24。原计划：[BROWSER_DICTATION_EXTENSION_DESIGN.md](BROWSER_DICTATION_EXTENSION_DESIGN.md)。

## 暂停检查点（2026-10-25）

**用户已要求暂时停止扩展组件开发，并授权保存一次本地 commit。** 本检查点保存当前源码、测试、计划与进度；现有网页服务和 ZIP 下载入口保持原状。事项 `2048`、`2049` 均保留 open，不以暂停代替完成或关闭。后续执行者须等用户明确恢复，不自行继续实机验收、开发或发布。

### 现在保留到哪里

- **S1 工程**：Chrome 网页采音、Firefox 可见原生侧栏采音，经受信服务网页接原同源 HTTP/WS；后台不直连后端、不持有麦克风。快捷键录音、尾包、后台重建已有真实浏览器证据。
- **P1 已修复并通过 Astra 复核**：停止/取消/故障关闭意图先持久化，重建按原 document/lease 补送；不重新 PREPARE/START。原文档销毁且无法取得回执时，仍须人工确认停麦后恢复。该修复不再追加开发。
- **开发包 0.1.1 已部署**：`https://192.168.0.252:8443/dictation` → “扩展下载与安装”，提供 Chrome / Firefox ZIP、Ubuntu / Windows 加载说明及当前服务地址复制；实际下载包已在本机隔离 Chrome 155 / 原生 Firefox 154 安装成功。
- **有效验证基线**：最近一次 `.venv/bin/python -m pytest` 为 **819 passed / 52.33s**；扩展测试 **43 项**、桥接回归 **8 组**、本机隔离与真实 HTTPS 安装引导各 **5 组**通过。真实 LAN POST/WSS 已在前轮通过。暂停保存只修改交接文档，复用这些记录，不重跑浏览器、录音或部署。
- **S1 尚未完成**：物理麦克风实际释放、Firefox 原生/系统指示器、真正使用电脑的 CA/网络策略和 Windows 运行仍待验收。Firefox 154 曾报告 `tabbrowser.updateBrowserSharing is not a function`。S2 目标捕获/固定文字回填及 S3 识别/校对结果回填均未实现；S4 仅提前交付开发 ZIP 下载/说明，整套 S4 与 S5 商店发布仍未完成。
- **依赖风险**：扩展工程发布前审计为 0 项；既有前端依赖有 10 项 high 告警，详见下文 ZIP 发布记录，未在扩展工作中扩大升级。

### 用户恢复后从这里继续

1. 先读本节、[原设计](BROWSER_DICTATION_EXTENSION_DESIGN.md)和[扩展 README 的 S1 实机交接](../extensions/dictation/README.md#s1-实机交接)，检查届时工作区与实际浏览器/服务版本；不要覆盖新的工作。
2. 由有物理设备的执行者补齐 Firefox 普通许可下试音、两轮目标框快捷键录音、独立停止、关闭侧栏、撤回服务/麦克风权限后的实际设备释放和指示。真正使用电脑另确认可信 CA 与访问策略；Windows 声明仍须对应实机证据。已有同一候选的有效验证可复用，发生版本或实现变化时再补相应检查。
3. S1 验收通过后再做 S2：先用明确标记的固定文本验证 input/textarea、受控输入、基础 contenteditable 等回填，不调用 ASR/校对。之后才进入 S3 结果接入，再补完整 S4。不要将当前录音包当作自动回填已完成。
4. 实际供应商校对、商店账号/签名/发布、费用、后续部署和 push 均按操作取得对应授权；本次只授权本地 commit。事项关闭也需另行授权。

### 源码、证据与回退在哪里

计划与本执行记录、扩展/网页/共享协议源码、锁文件、测试及构建入口纳入本地检查点提交。安装与重建命令在 [扩展 README](../extensions/dictation/README.md#从源码构建开发包)，ZIP 可从源码重新生成。

以下原始日志、截图、测试浏览器配置及生成包留在本机忽略目录，**不纳入 Git**；文档保留结果与定位信息。迁移到别的电脑时，不应假定这些本机证据或镜像也随 commit 迁移。

- 最新 ZIP 发布：`tmp/deploy/browser-extension-zip-20261025T064222Z/`，入口 `report.json`，当前部署指针 `tmp/browser-dictation-extension/current-deploy-path.txt`。
- POST/WSS：`tmp/deploy/browser-extension-s1-20261025T053910Z/report.json`；P1 恢复：`tmp/browser-dictation-extension/close-intent-fix/browser-07/report.json`；侧栏集成：`tmp/browser-dictation-extension/sidebar-integrated-final/acceptance-summary.json`。
- 回退操作入口：最新 ZIP 发布目录的 `RECOVERY.md`；回退镜像 `transcript-pipeline:rollback-before-extension-zip-20261025t064223z`。该镜像保留 S1 录音页、不含本轮 ZIP 入口。只有需回退且获得操作授权时，先确认任务/听写/在途校对全部结束，再按该文档执行；本次暂停不回退、不重启服务。
- 数据、模型、公开 CA 与 CA 私钥仍由原持久目录/卷管理，不随源码提交；不把本机配置、凭据、录音或历史产物放进 Git。

## 结论：Firefox 侧栏已接入原服务通路，完整扩展尚未完成

Chrome 原网页路线可从目标标签页用快捷键重新开麦、停止并恢复后台 worker。Firefox 154 的后台网页重新开麦会等待页面前台；即使放在独立、未最小化的网页窗口，失焦后仍等待。这两条 Firefox 网页采音路线均不能满足目标框内快捷键体验。

用户先选择“保留原体验，先穿刺新的录音承载方案”，随后接受尝试原生侧栏。现已将 Firefox 侧栏 PCM 接入受信服务网页，由原 `DictationController` 使用原同源 HTTP/WS；没有改成扩展直连后端。

**采用 Firefox 原生侧栏，不采用普通扩展标签页或独立录音窗口。** 侧栏必须保持可见，约占 340px 横向空间；两轮真实快捷键验证中目标 textarea 保持焦点，worklet PCM（含尾包）与服务网页 WS 接收字节完全相同。Chrome 继续用网页采音，普通网页不增加侧栏前提。

侧栏采音接入、隔离通路验证及获单独授权的 S1 网页部署已完成。本机隔离 Chrome/Firefox 已在真实 `https://192.168.0.252:8443` 完成 POST/WSS 验证。**S1 整体尚未验收**：物理麦克风、Firefox 原生指示器及异机访问策略仍有缺口。S2 固定文本回填、S3 识别/校对结果接入、S4 正式引导均未完成。用户最新授权先交付开发 ZIP 下载/加载说明并更新现有服务；商店发布与签名未授权。

Astra 随后发现的 P1“后台重建遗忘关闭请求”已修复并通过复核，修复工作结束，`2048` 保持 open。原文档已销毁且无法确认关闭时仍需人工确认停麦再恢复；S1 的物理设备和 LAN 验收条件不变。

## 已有代码与责任

- `extensions/dictation/`：共用 TypeScript、webextension-polyfill、两端 MV3 构建；Chrome service worker、Firefox event page。主包标明 S1 开发验证，没有假装支持插入文字。
- `src/background.ts`：服务可选权限、准确 origin/路径、可信扩展 UI、录音 tab/document/operation/session 与侧栏 window/document/lease 绑定、短串行状态写入。关闭意图与观察状态分开保存，原网页和采音 lease 各有待确认位；重建先补送未确认关闭，再查询原文档。临时转接 PCM，不存音频、不发后端 HTTP/WS。
- `src/content/recorder.ts` 与网页桥：仅在授权服务路径建立桥；首次加载双向 ready/HELLO 解决 SPA 与 document_idle 先后顺序。普通页不能开始录音；离开专用页的旧操作仅可关闭原 lease、排尾包与终态。目标网站没有网页控制桥，BFCache 后旧绑定失效。
- `useDictation.start`：第七参数追加 `{ signal?, capture? }`，返回 `started(sessionId)` / `rejected(reason)`；原六个参数、普通网页设备设置、local/probe 路径和忽略返回值的调用保持。侧栏代理只实现原采音边界，不复制服务会话状态机。取消只使本代准备失效，迟到权限/会话不转移所有权。
- `/dictation/extension`：独立停止按钮；Chrome 前台试音，Firefox 仅启用服务参与，不申请网页麦克风。普通页录音经 SPA 进入此页时，独立停止仍使用明确 session ID；即使新的扩展开始因忙碌被拒，也不能遮盖这个停止入口。
- `src/sidebarCapture.ts` / `src/sidebar.ts`：原 `PcmCapture` / worklet 的硬件 lease、前台短试音、独立停止、最多 62 包的串行转接和有限重试。停止先关轨道再确认尾包，丢回执只重试同一序号，网页去重后才调用原 sendPCM。后台休眠不靠假心跳；故障禁用侧栏资格，迟到 PREPARE/START 不能重新开麦。
- `tests/carrier/`：同一份 `PcmCapture` 的扩展标签页/Firefox 侧栏对照夹具。音频只计样本，不发送、不落盘，不调用模型；观察端只返回计数、轨道/上下文状态与可见性。

运行方法见 [extensions/dictation/README.md](../extensions/dictation/README.md)。没有改动后端、模型/提示词/期限、已有中间 JSON 或历史产物。

## 新增实测：侧栏 → 服务网页 → 原同源 WS

目录：`tmp/browser-dictation-extension/sidebar-integrated-final/`。真正临时安装生产扩展，HTTP/WS 对端为独立协议夹具，未调用模型。Firefox 原生麦克风提示通过正常 UI 明确允许；音源仍为虚拟设备。

| 场景 | 结果与证据 |
|---|---|
| 首次加载及授权 | 页面自动识别侧栏承载；网页不申请麦克风；侧栏试音后轨道 ended。`probe.json`、`microphone-native-prompt.png` |
| 两次目标框快捷键录音 | 21,674 / 21,504 样本，尾包 1,194 / 1,024 样本。原始 PCM 与服务 WS 保存的 `.pcm` 逐字节相等；后端序号和样本位置连续、无重复；目标框保持焦点。`cycles-report.json` |
| 录音中重建后台 | 实际终止 Firefox event page，context 172 → 173；恢复原 operation/session/lease，getUserMedia 次数不增加，最终 23,552 样本，字节仍完全相同。`rebuild-report.json`。这是主动终止测试；早期独立承载夹具另有自然休眠证据 |
| 刷新录音页、撤回服务主机权限 | 原操作 failed；侧栏资格清零、轨道全部 ended，不能自动重开。`faults-checkpoint.json` |
| SPA 离开专用录音页 | 侧栏轨道 ended、后端明确会话 cancelled；扩展操作保留导航导致的 failed，而不伪装为可回填结果。`spa-exit-diagnostic.json` |
| 关闭原生侧栏 | 文档已销毁，扩展操作 failed；见 `close-report.json`（partial）。跨文档 track 包装失效，关闭时的物理设备释放尚未验收 |

集成中修复了首次握手竞态、刷新后新操作误被认作旧文档重载、失败准备阻挡显式重试，以及 SPA 卸载过早拆掉采音回执监听的问题。旧 lease 的停止/收尾不得关闭、禁用或改写新 lease 及新试音授权；后台迟到失败同样不得影响后继操作。

**Firefox 原生指示器仍有平台风险。** 干净配置在第一次侧栏试音时已出现 `WebRTCParent.sys.mjs: tabbrowser.updateBrowserSharing is not a function`，不能再归因于反复装卸测试包。侧栏自身状态、独立停止和停止后的 track 状态可用；实际设备的系统麦克风指示、关闭侧栏和撤权须由有物理设备的执行者验收后，才能宣布 Firefox 日用支持。

无效观察已区分：早期测试从 privileged frame 直接遍历 TypedArray 被 Xray 阻止，改用 DataView 读取后完成字节比较；父进程及其他文档的 media manager 在采音时也返回 0，不能用其 0 值证明关闭。关闭时的审计未取到有效轨道状态，保留 partial，而不是记通过。SPA 测试最初预期 cancelled；按导航失效契约保留扩展 failed，并分别验证后端 cancelled、轨道 ended，没有放宽采音释放要求。

已查看原生侧栏、桌面和 390px 网页截图。侧栏截图中半透明辅助文字/禁用按钮不可见，已改用实色并检查实际预览；权限说明、快捷键和禁用状态现在可读。最新侧栏预览为 `sidebar-readable-ui.png`。

## 真实浏览器证据

总目录：`tmp/browser-dictation-extension/`。音源均为**合成虚拟麦克风**；没有物理麦克风或语音语义质量验收。

环境：Linux X11，独立 Xvfb；Chrome for Testing **155.0.8059.12**；原生 Firefox **154.0**，Geckodriver **0.37.1**。Firefox 使用原生发行二进制，经 WebDriver 真正安装扩展，`focusmanager.testmode=false`、普通 autoplay 默认策略、`acceptInsecureCerts=false`。下列原生权限证据没有自动授予麦克风、关闭证书验证或 autoplay 绕过参数。manifest 下限暂与实测版本一致，不宣称更早浏览器或其他 OS 通过。

### Chrome：原路线通过录音控制

- `s1-chrome-report.json`：两次目标标签页快捷键开始/结束；前台试音释放麦克风；停止尾包；录音中终止 worker 后恢复，不重复创建会话。首轮 388,778 样本，尾包 938 样本；第二轮 23,040 样本。
- `s1-chrome-denied.png`：真实麦克风拒绝后的失败反馈；随后使用原生权限 UI 允许，未伪装启动成功。
- `chrome-final-worker-report.json`：上一轮协调器实际装载，录音中停止 worker，新实例查询原录音页，再停止同一 operation/session，最终 `completed`。权限通过正常网站权限设置 UI 明确允许。
- `sidebar-integrated-final/chrome-regression-report.json`：本轮新构建另完成两轮录音控制与实际 worker 终止/重建，仍使用 webpage capture、原 operation/session。该轮通过受信扩展 UI TOGGLE 触发，使用自动测试麦克风许可；原生快捷键/权限证据来自上面两份原生测试。开发配置复用旧授权，先执行标准扩展 reload，避免旧 service worker 缓存被当作新构建。
- `sidebar-integrated-final/chrome-lost-receipt-report.json`：让实际网页先开麦，再注入错误 START 回执。后台将原操作记 failed，同时明确停止原文档；所有轨道 ended，原后端会话 completed，已接收 12,800 样本。不会只留下失败 badge 而继续录音；该故障测试同样使用自动测试许可。
- 同源 HTTP/WS 对端是 `serve_fixture.py` 的本地协议夹具，`completed` 不代表真实 ASR/校对完成。

### Firefox：原网页与独立窗口失败

- `s1-firefox-foreground-gate.json`：麦克风已许可；录音页隐藏时，`getUserMedia` 在 10 秒观察窗内未完成，服务会话数未增加。恢复前台后完成开麦，随即开始送样本。
- `firefox-popup-focus-gate.json`：干净 Firefox 配置、真实安装主扩展，把服务录音页移至独立 popup 窗口。该页 `hidden=false`、`focus=false`，且持久 microphone 权限为 `ALLOW`，仍不能从另一窗口开始。恢复录音窗口焦点后才开始。
- 普通扩展标签页也观察到前台等待，未证实可用；没有把扩展 origin 直接当作解决方法。

因此不采用“每次后台快捷键后偷偷切回录音页”，也不靠持续占用麦克风避开重新申请。

### Firefox：早期无网络侧栏承载穿刺

`carrier-firefox-sidebar-final-report.json`：

| 检查 | 实际结果 |
|---|---|
| 前台明确授权、短试音 | 试音后轨道 `ended` |
| 第一次目标框快捷键开麦/结束 | 573,781 个 16k 样本，449 包，尾包 341 样本 |
| 第二次重新开麦/结束 | 13,312 个样本，11 包，尾包 512 样本 |
| 输入焦点 | 两次开始后，目标 `document.hasFocus()` 均为 true，焦点仍在 textarea |
| 麦克风与上下文 | 两次停止后所有轨道 `ended`；各 AudioContext 都经历 `suspended → running → closed` |
| 尾包 | 每轮包样本之和等于最终样本总数，无静默漏尾 |
| 后台生命周期 | 首轮等 35 秒，Firefox event page 实际变为 `stopped`；快捷键唤醒后停止原采音，没有再次开麦 |

`carrier-firefox-sidebar-ui.png` 是 WebDriver 捕获的原生侧栏界面，已查看。早期 XWD 转码图存在颜色/比例伪影，不用作视觉验收。

关闭侧栏后确认录音文档被销毁，见 `carrier-firefox-sidebar-close-diagnostic.json`。跨文档保留的测试轨道包装随之成为 dead object，不能据此断言物理设备释放。独立停止的轨道释放已实际断言；物理设备的关闭/指示器检查仍待本人环境。

该指示器异常后来在干净配置中复现，最新结论见上面的集成验证；旧载体夹具只用于历史对照。

## 关闭意图恢复修复：重建后仍须完成用户要求的关闭

追踪：[2048-o-extension-close-intent.md](../codestable/issues/2048-o-extension-close-intent.md)。Astra 复现的根因是用同一 `phase` 表达用户意图和网页观察：恢复时 recording 可覆盖 draining，faulted 只保留失败状态而不重发 STOP；取消则在外部关闭前删掉操作。

当前实现：

- 在第一个关闭 RPC 前，将 `closeIntent.kind`、`pagePending`、`capturePending`、放弃标记及原绑定一起持久保存。故障同时保存禁用侧栏资格。旧 draining/faulted 记录在读取时升级，先落盘再补送。
- 原网页确认 `closedOperationId`，表示该操作已封存，原控制器已接手关闭；侧栏独立确认原 lease 的 STOP。分别落盘确认，只补送尚未确认端。取消/放弃在交接完成前保留全部绑定，迟到 recording 和 PREPARE/START 均不能撤销关闭；旧关闭回执不能清除后继操作。
- 正常停止保持尾包 flush；取消/故障的侧栏直接关闭使用 `flush:false`，网页仍按原 STOP/CANCEL 契约处理，Chrome 故障 STOP 保留尾包。恢复分开“绑定已加载”和“外部核对已完成”，允许原操作的 STOP 回执、STATE、正常停止尾包通过，避免恢复等待自己阻塞的尾包。页面不匹配旧操作时封存该 ID 并确认，不影响另一个操作的麦克风。
- 回执丢失或原文档不可达时，保留待确认位并显示“关闭尚待确认”，不宣称麦克风已停。重试与原文档已销毁时的显式恢复方法见扩展 README。

新增内存回归加载实际后台代码，覆盖停止落盘、故障落盘、取消尚未送达的三个重建窗口，以及旧记录升级、两端各自丢回执、无效收据、迟到状态/开麦请求、恢复尾包和旧回执隔离。

**真实 Chrome 155 worker 中断验证通过。** `tmp/browser-dictation-extension/close-intent-fix/browser-07/report.json` 使用生产构建、原网页 `PcmCapture` 和虚拟麦克风，在已落盘、命令尚未送达时阻断外部关闭，随后实际终止 worker。CDP 确认 `runningStatus=stopped`，唤醒后 JS 全局拦截器消失，三条路径均补送原 document 的关闭：

| 持久意图 | 重建后操作 / 协议夹具会话 | 接收样本 |
|---|---|---:|
| stop | completed / completed | 6,826 |
| fault | failed / completed，保留原故障 | 13,141 |
| cancel | 关闭交接后清除操作 / cancelled | 6,400 |

三次中断前均观测到实际 live 轨道，重建后所有轨道 ended，getUserMedia 次数和服务会话数不增加。截图中的“关闭尚待确认”与恢复后界面已读取检查。这里的麦克风采用自动测试许可，HTTP/WS 是隔离协议夹具，不是物理设备、Firefox 原生指示或 LAN 验收。Firefox 侧栏端的原 document/lease 补送及恢复尾包由本次实际后台内存回归覆盖；此前真实侧栏硬件/PCM 证据保留复用，本次未重跑原生 Firefox。

驱动失败记录 `browser-01` 至 `browser-06` 保留 failed：开发者模式未开启使 reload 后扩展被禁用、原生主机权限提示未及时处理，以及错误等待 Playwright 的新 Worker 包装事件。最终驱动通过正常扩展管理 UI 启用开发者模式、正常授权主机权限，改为核对 CDP 已停止状态及重建后的新 JS 全局。产品侧未加入权限或生命周期绕过。

## 自动化与原网页回归

| 入口 | 结果与覆盖 |
|---|---|
| `.venv/bin/python -m pytest` | 发布轮 **818 passed / 51.68s**；新增共享协议 Docker 构建依赖回归；1 条既有 Starlette/httpx 弃用警告 |
| `npm --prefix frontend run build` | 类型检查与生产构建通过；保留既有大 chunk 提示 |
| `node --experimental-strip-types frontend/tests/dictation-state.mjs` | 选中会话、worklet 尾包、采音 lease、播放互斥、设备、术语通过 |
| `npm --prefix extensions/dictation run typecheck` | 主包与承载夹具通过 |
| `npm --prefix extensions/dictation test` | **39 passed**：原 28 项，加三类关闭中断、旧记录升级、逐端回执、无效收据、终态放弃仍需关闭收据、恢复尾包、终态及旧回执隔离 |
| `npm --prefix extensions/dictation run test:bridge` | **8 组通过**：原 7 组，加关闭收据封存未送达 START 且不影响当前操作 |
| `npm --prefix extensions/dictation run test:recovery` | Chrome 155 三类真实 worker 中断恢复通过，证据见上节 |
| `frontend/tests/dictation-ownership.mjs` | 复用此前通过证据：原历史选择/上传注册、明确 ID 停止、旧 live 终态不关闭 local、慢上传与旧会话删除 |
| `build:chrome` / `build:firefox` | 两端主包构建通过；都是 S1 开发产物，不是商店包 |

P1 修复轮的构建、pytest（817 passed）、状态与扩展测试日志在 `tmp/browser-dictation-extension/close-intent-fix/`，汇总为该目录的 `acceptance-summary.json`；桥接报告及桌面/390px 截图在其 `bridge-01/`，真实 worker 中断报告在 `browser-07/`。后续发布轮的 818 passed 和镜像构建日志见下节发布目录。侧栏集成总览仍为 `sidebar-integrated-final/acceptance-summary.json`，此前桥接和所有权证据保留原目录。

`test:bridge` 是合成 HTTP/权限故障回归，使用自动测试麦克风许可，**不计入上面的原生权限证据**。所有服务状态均来自独立协议夹具，ASR/付费调用为 0。

失败记录保留：`bridge-final-01` 的 headless-shell 在只配置 context permission 时返回 `Not supported`；故障测试采用项目既有 fake UI 许可方式后通过，未据此改动产品权限策略。早期 Firefox 153/Playwright 构建含默认 `focusmanager.testmode=true`，不用于结论，已改为原生 154/普通焦点设置。重复装卸后的旧测试配置出现挂起，独立窗口反例改用干净配置复现；没有把该挂起判为窗口方案失败。

## 已授权部署：两端真实 POST/WSS 通过

用户在 Astra 复核通过后明确授权部署当前 S1 网页改动并验证 LAN，不调用付费模型。发布目录：`tmp/deploy/browser-extension-s1-20261025T053910Z/`，入口 `report.json`，独立恢复命令 `RECOVERY.md`。

- 切换前两次确认服务空闲：99 份任务/听写状态无活动项，无识别 worker 或在途 HTTP 进程。保留旧镜像 `transcript-pipeline:rollback-before-extension-s1-20261025t053910z` 后，按原 `docker compose build trans` / `up -d --no-build --wait` 更新现有单容器。
- 补齐前端 Docker 构建阶段的 `COPY shared/ /shared/`，并从构建上下文排除扩展 node_modules/dist；新增部署回归防止共享协议文件遗漏。应用后端、默认模型、提示词与协议未改。
- 原数据、模型缓存、CA 卷、端口、非 root 身份与 GPU 配置保持；HTTP 和使用原根证书验证的 HTTPS 健康通过，API/Caddy 均 RUNNING，容器 healthy、重启次数 0。生效前端含专用路径、关闭收据及侧栏桥接代码；已检查实际页面与侧栏截图。

真实浏览器均使用独立配置，**只在各自测试证书库导入公开 CA**，不忽略 TLS 错误；浏览器运行在服务器主机上，录音页请求的 origin 为实际 IP 与 8443，不是 localhost。目标焦点页为另一 HTTPS origin `https://localhost:8443`。

| 浏览器 / 采音端 | POST / WSS | 服务确认的音频 | 停止与凭据 |
|---|---|---|---|
| Chrome 155.0.8059.12 / 原网页 | POST 200；握手 101，Origin 与 Host 保留 `192.168.0.252:8443` | 17 包、20,992 样本，尾包 512 | completed；轨道 ended；新匿名上下文读本次会话 401 |
| 原生 Firefox 154.0 / 侧栏 | POST 200；WSS open 并收到实际会话快照 | 17 包、20,650 样本，尾包 170；网页 getUserMedia 为 0 | completed；原 page/lease 关闭均确认；轨道 ended；省略凭据读本次会话 401 |

两端后端样本数等于原控制器发包样本之和，包序号及位置连续，持久 PCM 长度正确。浏览器正常管理 Cookie，扩展仍不直接访问后端，也不读取 Cookie。Chrome 使用受信扩展 UI TOGGLE；Firefox 使用真实 Ctrl+Shift+Space 开始/结束，检查目标 textarea 焦点。Chrome 原生快捷键证据复用前轮。

本次专门验证网络：测试拦截器把本次新会话 `cleanup` 置为 false，并在网页原 WS 发送边界将 PCM 内容置静音、保留包头。两个成功会话均无语句、无校对调用；生产默认设置未改，未重跑用户历史。Chrome 自动测试麦克风许可；Firefox 使用普通原生许可提示，音源均为虚拟设备。没有语音语义或物理设备验收结论。

Chrome 证据 `lan-chrome-report.json`；Firefox 证据 `lan-firefox-02-report.json`。Firefox 第一轮 `lan-firefox-report.json` 保留 failed：测试的 WebDriver 沙箱以 `instanceof ArrayBuffer` 判断网页缓冲区，跨 realm 判定为 false 后误入文本分支，导致音频回执失败。按实际二进制/文本发送边界修正测试后通过，最终报告的 `binaryRealmMatches:false` 确认了原因；未修改生产采音实现。

本轮原生 Firefox 仍记录 `tabbrowser.updateBrowserSharing is not a function`，原生指示器问题继续保留。测试结束后服务再次确认空闲；新增两份 completed 静音会话、一份首次失败后取消的测试会话，均可由报告中的 ID 追踪。

## 恢复后的第一步：有物理设备的执行者完成 S1 实机验收

Astra 已通过 `2048` 的 P1 复核，修复工作结束，事项保持 open。原文档销毁且无回执时，仍须人工确认停麦后显式恢复。

有物理设备的执行者负责 Firefox 停止、关闭侧栏、撤权后的实际设备释放及原生/系统指示，并在真正的使用电脑上确认 CA、网络和设备策略。操作与记录要求见 [扩展 README 的实机交接](../extensions/dictation/README.md#s1-实机交接)。这些通过后再进入 S2。

完整功能仍未实现目标捕获、框架输入同步、撤销/回填收据、结果就绪判断、正文投影与识别/校对结果回填、正式商店安装引导或商店配置；没有付费供应商语义验收。

## 最新选择：网页下载开发 ZIP，再按步骤加载

用户最初要求 Ubuntu / Windows 从网页点击安装，随后明确选择“把安装包的下载放到网页上，然后下载 ZIP 安装，同时告诉怎么安装”，并授权直接部署到现有 8443 服务。本轮按最新选择交付，不再以发布者账号、商店或签名阻塞开发包下载。

`2049` 的实现包括：

- `package:dev` 重建两端源码，生成版本 **0.1.1** 的 ZIP 和目录清单；`manifest.json` 位于根目录，含全部运行资源和中文 `INSTALL.txt`。`fflate 0.8.3` 只用于构建，不进入扩展运行依赖。
- 普通语音输入页提供入口；专用录音页在原停止控件之后显示两个下载链接及可展开说明。Chrome 使用开发者模式加载解压目录，Firefox 临时载入 manifest、重启重载；显示当前服务地址并支持复制。下载版本来自实际清单，版本查询参数避免升级后复用旧包。
- ZIP 与清单作为前端静态文件发布；Docker 每次从源码生成，排除宿主残留包，不修改运行容器。没有改动后台关闭意图、网页桥、采音引擎或后端。
- 缺失/无效清单隐藏下载链接并提供重试；复制失败保留地址和下载入口。说明按普通文档流展开，保留停止控件与正文边界，兼顾键盘操作和窄屏。

安装和维护入口见 [扩展 README](../extensions/dictation/README.md)。该交付不包含商店安装或 Windows 运行验收；Firefox 指示器与物理设备验收继续保留。

**本轮已部署并验证**：`https://192.168.0.252:8443/dictation` 的“扩展下载与安装”进入实际下载页。发布目录为 `tmp/deploy/browser-extension-zip-20261025T064222Z/`：

- `.venv/bin/python -m pytest`：**819 passed / 52.33s**；扩展 **43 项**测试、类型检查、前端及 Docker 构建通过，原桥接回归 **8 组**通过。
- 隔离与真实 HTTPS 安装引导各 **5 组**通过。真实按钮下载两包，检查根 manifest / 资源 / `INSTALL.txt` / 版本；切换说明、复制地址及拒绝反馈、缺包/坏清单重试可用。已查看桌面与 390px 展开图，停止控件和正文未被覆盖。
- 将**实际 LAN 下载包**安装到独立 Linux 浏览器配置：Chrome 155.0.8059.12 解压加载、原生 Firefox 154.0 临时安装均成功，设置页可用、实际扩展版本均为 0.1.1。结果见 `package-install-report.json`。本轮不重新执行采音或 POST/WSS，既有相同实现的有效证据保留。
- 更新前两次确认服务空闲，保留旧 S1 镜像与 `RECOVERY.md`；新容器 healthy，HTTP / 原 CA 可信 HTTPS、API/Caddy 正常。持久卷、端口、GPU 和原模型配置一致。LAN 下载/界面验证无写 API、录音或付费调用。
- 新增打包依赖发布前修正至 `fflate 0.8.3`，扩展工程审计 0 项。既有前端依赖仍报告 10 项 high（Vue SSR、开发工具及 lodash 等），未在本轮扩大升级；详见发布目录的 `npm-audit-frontend.json` 与事项 `2049`。

早期 CRX 打包试验在 Chrome 启动阶段遇到 Ubuntu `No usable sandbox`，未进入网页安装环节；原记录保存在 `tmp/browser-dictation-extension/direct-install-01/pack.log`。当前 ZIP 路线不依赖该试验，也不继续调整系统安全策略。

## 文件清单与自评

修改：`.gitignore`、`frontend/src/App.vue`、`frontend/src/router/index.ts`、`frontend/src/composables/{useDictation,dictationCapture}.ts`、原设计文档。

S1 网页发布另修改 `Dockerfile`、`.dockerignore`、`tests/test_docker_deploy.py`，用于共享协议构建依赖及对应回归。

ZIP 下载交付另修改上述部署文件、`.gitignore`、`frontend/package.json`、`frontend/src/views/{DictationView,DictationExtensionView}.vue`、扩展包版本/依赖锁和 README；新增 `extensions/dictation/{package.mjs,tests/package.test.mjs}`、`frontend/src/components/DictationExtensionInstall.vue`、`frontend/tests/dictation-extension-install.mjs`、`codestable/issues/2049-o-extension-zip-download.md`。下载 ZIP / 清单为构建产物，不入 Git。

新增：

- `shared/dictationExtensionProtocol.ts`。
- `frontend/src/composables/{dictationExtensionBridge,dictationExtensionAudio}.ts`、`frontend/src/views/DictationExtensionView.vue`、`frontend/tests/dictation-extension-bridge.mjs`。
- `extensions/dictation/{package.json,package-lock.json,tsconfig.json,build.mjs,README.md,popup.html,options.html,sidebar.html}`。
- `extensions/dictation/manifests/{common,chrome,firefox}.json`。
- `extensions/dictation/src/{background,ui,sidebar,sidebarCapture}.ts`、`src/{ui,sidebar}.css`、`src/content/recorder.ts`。
- `extensions/dictation/tests/{background.test.mjs,protocol.test.mjs,sidebar-audio.test.mjs,close-recovery.mjs,serve_fixture.py}`、`tests/carrier/{background.ts,capture.ts,build.mjs,recorder.html}`。
- `codestable/issues/2048-o-extension-close-intent.md`：Astra P1 修复与验证，保持 open 待授权收尾。
- 本执行记录。

已按 `docs/REVIEW_CHECKLIST.md` 自评：范围限于 S1、用户确认的侧栏接入及单独授权的网页部署/LAN 验证，另含用户最新选择的开发 ZIP 下载与加载说明；各轮对应全量 pytest、镜像构建、实际产物/页面检查已执行。上游结构未改。物理设备/指示器、Windows 和异机策略仍待验收，S1 与整套扩展尚未完成。
