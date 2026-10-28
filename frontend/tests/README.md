# 工作台浏览器检查

使用项目 `.venv` 和已安装的前端依赖。以下命令分别在终端运行，`TEST_ROOT` 必须是新建的临时目录，不能指向真实任务数据。

```bash
# 项目根目录；TEST_ROOT 指向会话 scratch 下的新目录
.venv/bin/python -m tests.frontend_workbench_server --data-dir "$TEST_ROOT" --port 8765
TRANSCRIPT_API_PROXY_TARGET=http://127.0.0.1:8765 npm --prefix frontend run dev -- --host 127.0.0.1 --port 5179
```

连接一个已有的、支持真实布局的 CDP 浏览器，不自动安装或启动浏览器：

```bash
CDP_URL=http://127.0.0.1:9339 WORKBENCH_OUTPUT="$TEST_OUTPUT" npm --prefix frontend run test:workbench
# 只检查配色与动画，避免重复全套
WORKBENCH_FILTER='soft light|reduced motion' CDP_URL=http://127.0.0.1:9339 WORKBENCH_OUTPUT="$TEST_OUTPUT" npm --prefix frontend run test:workbench
```

`TEST_OUTPUT` 应在 scratch 内。`WORKBENCH_FILTER` 为测试名称正则；搜索定向检查需先运行 `real upload`，以生成单阶段样例。可使用 `real upload|task search` 一次完成。

脚本先检查隔离标记，拒绝在真实服务上执行。服务仅允许隔离设置、上传、已有 Markdown 导出和 fixture 删除；ASR、OCR、精修执行全部拒绝。成功/失败/运行中任务是 UI 样例，批量搜索使用拦截响应，不能作为模型运行证据。导出检查实际上传 JSON、调用原有导出阶段并下载 ZIP。

本次使用 Chrome 154 验证。Moli 1.1.10 对该页面出现 DOM 存在但布局为零的兼容问题，不应在失败时用强制点击跳过可见性检查。

检查完关闭自己启动的服务和临时浏览器；不要终止用户已有进程。

## 连续听写记事本

```bash
npm --prefix frontend run test:dictation-notebook
# 使用本机已经安装的其他 Chromium（可选，不下载浏览器）
DICTATION_TEST_CHROMIUM=/path/to/chrome-headless-shell npm --prefix frontend run test:dictation-notebook
```

此用例自行启动隔离的 Python API（8027）与 Vite（5177），数据和截图写入新的 `tmp/dictation-notebook-<timestamp>/`；可用 `DICTATION_TEST_OUTPUT` 指定新目录。使用真实 CodeMirror、HTTP 和文件保存；语音／整理结果为确定性 fixture。启动／布局检查使用 Chromium 虚拟麦克风，不访问物理设备、GPU 或远端模型，不连接／重启当前服务。`dictation-layout.mjs` 保留为该用例的兼容入口，无需重复执行。

同一轮使用 `http://dictation-http.test` 检查普通内网 HTTP 兼容性：浏览器实际处于非安全上下文，未注入 `randomUUID` 或伪装为 localhost。覆盖旧任务页渲染、历史稿转换与编辑、自动保存／刷新／TXT 下载，以及拒绝采音后仍可导入音频；上传检查实际 XHR 字节和参数，响应使用 fixture，不启动 ASR。`http-capabilities.json` 记录浏览器能力和操作 ID；其余 localhost 场景继续检查安全上下文与原生 UUID 路径。

覆盖听写默认模型／思考强度、恢复默认及已保存的手动选项刷新保留；正文输入／emoji／撤销、在途编辑与后台插入合并、双 pending 冲突不污染恢复副本、失败刷新恢复、双窗口同步、不抢选区和滚动、同区整理候选的编辑／应用／撤销／过期拒绝，以及1159×756和390px深色布局。空白编辑区直接启动、听写前后正文高度、浮动面板原尺寸及拖曳后编辑器高度不变，也在同一场景内检查。临时识别使用超长中文和无空格URL，检查两行限高、栏内纵向滚动，以及375×667手机上正文和页面都没有横向溢出。

`dictation.mjs` 和 `dictation-proofreading.mjs` 的编辑／回听入口已跟随新 UI 更新；前者仍需显式费用授权，后者会使用实际ASR。它们不是上面这项免模型回归的一部分；真实短录音、中文输入法和本人设备效果由使用者另行验收。

评审回归另覆盖历史稿首次转换、过期保存响应／输入法缓冲快照不恢复已放弃的预览、逐句修正后的两步撤销、应用期间继续修改预览不丢稿，以及开始准备时只读／继续听写在麦克风权限等待后再次保存定位。跨窗口过期预览用例控制第二窗口的轮询时序，避免依赖恰好赶在一秒轮询前点击。

单光标回归在同一脚本中覆盖连续语音推进真实编辑光标、当前落点键入／Enter 也建立音频边界、保存阻塞期间旧尾句与恢复后新句的顺序、鼠标／方向键自动换位、快速移动合并、旧轮次迟到结果、选区末尾保留原文及撤销／重做；还覆盖输入法组词事件与后台更新缓冲、继续确认／worklet 启动期间再移位、展开／收起面板主动暂停取消自动恢复、保存／暂停／恢复拒绝、旁窗／历史／整理预览不夺取录音位置，以及自动准备麦克风时结束后迟到授权关闭全部轨道。定位逻辑使用真实 CodeMirror，音频／控制状态使用确定性 fixture，不调用模型。`unified-cursor-desktop.png` 和 `unified-cursor-recoverable-error.png` 留存单光标与可恢复失败状态。

只复验单光标与录音生命周期的场景可加 `DICTATION_TEST_UNIFIED_ONLY=1`；普通 HTTP、历史保存／整理等完整回归保持默认入口。
