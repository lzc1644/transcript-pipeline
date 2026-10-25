# 浏览器语音输入：S1 开发验证包

**开发已按用户要求暂停（2026-10-25）**，当前版本 0.1.1；现有下载服务保留。计划、进度、未验收项与恢复顺序见[暂停检查点](../../docs/BROWSER_DICTATION_EXTENSION_IMPLEMENTATION.md#暂停检查点2026-10-25)。以下步骤供使用现有开发包及用户恢复开发后参考，S1 尚未验收完成。

Firefox 可保持目标输入框焦点，用原生侧栏采音，再经服务网页送入原 WebSocket。Chrome 继续使用网页采音。**当前版本能验证录音通路，尚不回填文字。**

本地录音、尾包和恢复证据见 [执行记录](../../docs/BROWSER_DICTATION_EXTENSION_IMPLEMENTATION.md)。Firefox 154 原生麦克风指示更新存在浏览器异常；物理设备及关闭侧栏后的系统指示尚未验收，当前不建议作为日用版本。

## 从网页下载并加载（Ubuntu / Windows）

打开服务的 `/dictation`，点击 **“扩展下载与安装”**。下载对应 ZIP，展开“安装与连接步骤”；页面可复制当前服务根地址。两个系统使用同一份对应浏览器的包。

现有 LAN 服务已提供 [下载与安装页](https://192.168.0.252:8443/dictation/extension#extension-install)。实际下载的两包已在本机隔离 Chrome 155 / 原生 Firefox 154 成功加载，版本均为 0.1.1；发布与恢复证据见 `tmp/deploy/browser-extension-zip-20261025T064222Z/`。

- Chrome：完整解压 ZIP，在 `chrome://extensions` 打开开发者模式，点击“加载已解压的扩展程序”，选择包含 `manifest.json` 的目录；安装后保留该目录。
- Firefox：完整解压 ZIP，在 `about:debugging#/runtime/this-firefox` 点击“临时载入附加组件”，选择 `manifest.json`。**重启 Firefox 后需重新载入**；未签名测试包不能通过普通“从文件安装附加组件”入口安装。

ZIP 根目录还包含 `INSTALL.txt`。当前版本 **0.1.1**，最低 Chrome 155 / Firefox 154.0。Windows 提供相同步骤，尚无 Windows 实机结果；S1 物理停麦验收仍按本文后半部分执行。

## 从源码构建开发包

使用 Node 24 和项目 `.venv`：

```bash
npm --prefix extensions/dictation ci
npm --prefix extensions/dictation run typecheck
npm --prefix extensions/dictation test
npm --prefix extensions/dictation run package:dev
npm --prefix frontend run build
npm --prefix frontend run test:extension-install
```

- Chrome：在独立测试配置的 `chrome://extensions` 启用开发者模式，加载 `extensions/dictation/dist/chrome`。
- Firefox：在独立配置的 `about:debugging#/runtime/this-firefox` 临时加载 `extensions/dictation/dist/firefox/manifest.json`。不需另装 carrier 夹具。

`package:dev` 从两端新构建生成 `frontend/public/dictation-extension/{dictation-chrome-dev.zip,dictation-firefox-dev.zip,packages.json}`，前端构建将其复制到 `dist`。归档不带外层目录；版本、最低浏览器版本及大小由实际 manifest / ZIP 生成。Docker 构建也运行同一命令，不依赖宿主残留 ZIP；修改源码后须重新打包、构建前端，不能只重建其中一层。

`test:extension-install` 默认启动本机隔离夹具，验证页面入口、实际下载、说明切换、复制地址、缺包重试和窄屏布局，证据保存到新建的 `tmp/browser-dictation-extension/install-*`。测试不创建会话、不申请麦克风、不调用模型。已获部署授权时可用 `DICTATION_INSTALL_URL` 指向真实服务，它会阻断所有非 GET/HEAD API 请求；`DICTATION_INSTALL_BROWSER_HOME` 可指定独立可信 CA 配置。

已加载后重新构建：先结束录音，在扩展管理页重新加载扩展，再刷新服务录音页并启用控制。Chrome 重启浏览器可能仍运行旧 worker，不能代替开发包 reload。

已测环境：Linux X11，Chrome for Testing 155、Firefox 154。manifest 下限与实测版本一致；临时加载不是 Firefox 正式签名安装，未发布商店版本。

## 先用隔离服务试两轮

夹具保留 HTTP/WS 协议，不运行 ASR、校对或用户历史：

```bash
.venv/bin/python extensions/dictation/tests/serve_fixture.py \
  --root tmp/browser-dictation-extension/my-sidebar-run --port 8031
```

`--root` 必须是新目录。构建前端后再启动夹具。音频是本次测试的录音，会以每会话 `.pcm` 保存在该目录，`protocol.json` 记录包序号、样本位置和停止总量；不要对敏感语音运行此调试夹具。

### Firefox

1. 打开扩展工具栏面板，填 `http://127.0.0.1:8031`，点击“连接并授权此服务”，在浏览器提示中确认主机权限。
2. 在自动打开的服务录音页点击“启用服务端控制”。此按钮不申请网页麦克风。
3. 回扩展面板点击“打开 Firefox 采音侧栏”。在侧栏点击“授权麦克风并试音”，通过浏览器原生提示允许麦克风。看到“就绪 · 麦克风已关闭”后继续。
4. **保持同一窗口的侧栏可见**。打开 `http://localhost:8031/fixture-target`，在 textarea 内放好光标，按实际注册的开始/结束快捷键；默认 `Ctrl+Shift+Space`。目标框应保留焦点，侧栏显示正在听写。再次按键结束；也可随时点击侧栏“结束本次听写”。
5. 等本轮结束，在扩展面板点击“放弃当前操作”，再做第二轮。当前 S1 尚无结果提交路径，不能用重复开始键丢弃未结束操作。

侧栏只持有采音设备和有界转接队列，不直接连接后端、不读 Cookie。网页必须同时保持打开。侧栏使用浏览器授予它的设备；网页中的设备设置仍只控制网页采音，未与侧栏共用设备选择 UI。

### Chrome

完成相同的服务连接，在网页点击“启用扩展控制并试音”，授权后切到目标标签页，按同一快捷键开始/结束，不需要侧栏。录音页始终提供独立停止入口。

### 故障时先看哪里

- 录音页刷新/导航、侧栏关闭、主机权限撤回：原操作失效，不自动重新开麦。先检查服务历史中的已接收内容，再放弃旧操作；恢复相应页面/权限并重新显式启用。
- 侧栏转接失联或积压接近 5 秒：关闭新增采音并报错，不能带缺口继续录制。已接收内容留在服务端。
- 服务处理失败：保留原录音页检查；不要把扩展按钮返回成功当作后端完成。
- 显示“关闭尚待确认”：后台保留原 document/lease，重建或再次点击开始/结束、取消时补送关闭；确认前不能放弃绑定或开始新操作。先用原网页/侧栏独立停止，再重试。原文档已销毁且无法取得回执时，确认采音已停止后重新加载扩展、录音页与侧栏，再显式启用。
- 快捷键冲突：按浏览器扩展快捷键设置修改；侧栏显示实际绑定，不依赖固定键位。

### 回归后台关闭恢复

构建前端和 Chrome 包后，在有显示环境的独立测试配置运行：

```bash
npm --prefix extensions/dictation run test:recovery
```

该脚本启动隔离 HTTP/WS 夹具，用虚拟麦克风实际录音，将关闭命令阻断在意图落盘之后，再真正终止 worker，检查停止、故障、取消三条恢复路径。音频和报告保存到新建的 `tmp/browser-dictation-extension/close-recovery-*`；不调用模型。

首次出现服务主机权限提示时，在浏览器中明确允许。脚本通过普通扩展管理 UI 启用开发者模式，仅操作它启动的测试配置。可用 `CHROME_TEST_EXECUTABLE` 指定已安装的 Chrome 155+，`DICTATION_TEST_PROFILE` 复用**专用测试**配置，`DICTATION_TEST_OUTPUT` 指定未存在的输出目录；Linux 需提供 `DISPLAY`。麦克风采用自动测试许可，不算原生设备权限或物理停麦验收。

独立 X11 自动执行可通过 `DICTATION_TEST_XDOTOOL` 提供 xdotool 路径，在出现主机许可提示时用普通键盘操作确认；不要指向用户正在操作的桌面显示。

## 真实 LAN 页面已部署

真实录音页是 **`https://192.168.0.252:8443/dictation/extension`**，当前 S1 网页改动已获授权部署。连接扩展时填写根地址 `https://192.168.0.252:8443`。上面的 localhost 仅为同电脑运行的隔离夹具；客户端电脑的 localhost 不是 LAN 服务器。

本机隔离 Chrome 155 / 原生 Firefox 154 已通过可信 HTTPS、同源 POST、带凭据 WSS、连续静音 PCM 与停止确认。报告在 `tmp/deploy/browser-extension-s1-20261025T053910Z/`。测试关闭本次会话校对、音源为虚拟设备；不代表另一台电脑已信任 CA 或物理设备已验收。

在真正使用的电脑按 [HTTPS.md](../../docs/HTTPS.md) 信任公开根证书，再按 [设计第 3.4 节](../../docs/BROWSER_DICTATION_EXTENSION_DESIGN.md) 确认服务页面无证书警告。扩展不能通过直连 API、读取 Cookie、忽略证书或放宽 CORS 替代网页通路。

## S1 实机交接

由有物理麦克风的执行者，在**普通 Firefox 配置**加载当前开发包；不要复用自动化的虚拟设备配置。为避免校对费用，采音关闭验证优先使用本文的隔离协议夹具，且夹具必须运行在这台 Firefox 电脑上。真实 LAN 页面仍需在实际使用电脑确认 CA 与网络策略。

开始前记录 OS、Firefox 版本和实际设备，确认没有其他应用占用麦克风。每种故障用一次新操作：

1. 普通原生许可下试音：收到声音采样，结束后实际设备和系统/浏览器采音指示均恢复关闭。
2. 保持目标 textarea 焦点，用快捷键做两轮短录音并结束；另测侧栏独立结束按钮。每轮设备释放，不能仅根据页面“已停止”判通过。
3. 录音中关闭侧栏：确认设备与指示关闭；重新打开不能自动开麦，必须显式启用。
4. 新操作录音中撤回扩展的服务主机权限，检查实际停麦；再检查浏览器麦克风权限撤回路径。原指示器异常应单独记录为失败，不用轨道 ended 代替指示器结论。

提供每项结果及必要截图/系统观察。原文档销毁、取不到关闭回执时，先人工确认设备已停再按故障恢复步骤操作；该次不记为自动恢复成功。物理停麦、原生指示和真实使用电脑的访问策略验收通过后，才进入 S2。

## 早期承载对照夹具

`tests/carrier/` 保留不联网的扩展标签页/侧栏可行性夹具：

```bash
node extensions/dictation/tests/carrier/build.mjs firefox sidebar
```

它只统计采样，不接服务；不要与主扩展同时启用，避免快捷键冲突。其自然休眠证据与已接入服务的主包证据分别记录在执行文档中。
