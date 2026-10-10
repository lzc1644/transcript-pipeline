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

脚本先检查隔离标记，拒绝在真实服务上执行。服务仅允许隔离设置、上传、已有 Markdown 导出、校对记忆离线操作和 fixture 删除；ASR、OCR、精修及记忆联网分析启动全部拒绝。成功/失败/运行中任务是 UI 样例，批量搜索使用拦截响应，不能作为模型运行证据。导出检查实际上传 JSON、调用原有导出阶段并下载 ZIP。

本次使用 Chrome 154 验证。Moli 1.1.10 对该页面出现 DOM 存在但布局为零的兼容问题，不应在失败时用强制点击跳过可见性检查。

校对记忆板块定向验证（包含真实隔离API上传/导入/审核/冻结下载，禁止真实模型执行）：

```bash
WORKBENCH_FILTER='proofreading-memory' CDP_URL=http://127.0.0.1:9339 WORKBENCH_OUTPUT="$TEST_OUTPUT" npm --prefix frontend run test:workbench
```

检查新页面的1440/390px深浅主题、Unicode修改窗口、未审核不可用、审核取消/确认、未保存范围隔离、同经验两请求出处积累不改approved上下文、停用后旧下载不漂移、坏响应失败和页面刷新恢复。生成 `memory-flow.json`、`memory-approved-context.json` 和展开态截图。输入是synthetic、响应是offline/replay，只证明工程链路；fixture对 `/analyses` POST 返回403，不会发起模型请求。

检查完关闭自己启动的服务和临时浏览器；不要终止用户已有进程。
