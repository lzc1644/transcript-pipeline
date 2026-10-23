---
kind: issue
title: 网页双遍语音输入
type: feature
status: open
created: 2026-10-21
---

# 网页双遍语音输入

## 目标和授权

用户批准独立语音输入模块 A→B→C→D，持续推进到完整交付。CPU INT8 Streaming Paraformer 中英预览，GPU Qwen3-ASR 0.6B 完整短句最终识别（不加载 ForcedAligner），GPT-6 Luna `none` 轻度整理。普通页和紧凑视图共用会话；录音上传、历史、人工编辑、复制、TXT 下载、重试整理及删除。不同于现有 SeACo Paraformer，不改旧流水线默认模型、阶段 6 提示词和中间 JSON。

范围不含系统输入法、全局快捷键、自动粘贴、说话人区分、摘要/会议纪要、模型训练或读书会阶段扩展。不自动关闭本事项、提交、推送或重启生产服务。

## 现状与接入

现有 Web `api_server.create_app` → `src/web/*` → 阶段进程；Qwen SDK 在可选 `.venv/asr-py312`，模型注册固定 revision，GPU 互斥位于 `data/jobs/_asr-gpu.lock`。本机 Web 基础 `.venv` 无 torch/sherpa，生产容器内有 Qwen 环境和模型缓存。新增 `src/dictation/`、独立 Web 路由与 Vue 页面，按进程隔离推理，复用 Qwen 加载/生成检查及网关客户端，不调用再取同一 GPU 锁的批处理入口。

没有 `codestable/spec/` 或相关 Epic；已检索现有 ASR、混合语言、生成恢复、网关及上传事项。此独立 Issue 不初始化/重组其他制度记忆。

## 质量承诺与证据

- 可靠性：连续采样位置、重复包幂等、停止尾部、失败原稿可用、迟到结果不覆盖人工编辑；接口/状态机与故障测试、真实回放验证。
- 信息安全性：同源 WebSocket、会话能力凭据、服务端密钥、受限上传/解码、路径隔离及删除等待 worker 退出；拒绝路径/超限/越权测试。
- 交互能力：临时与已完成文本分开；原稿可复制；状态文字和音量明确；停止立即释放麦克风；普通/紧凑共享逻辑；真实浏览器检查。
- 兼容性：旧入口/配置/输出默认不变；全量 `.venv/bin/python -m pytest` 及前端构建。
- 性能效率：预热单会话目标首字 P50≤0.8/P95≤1.5 秒，句尾最终 ASR P95≤2.5 秒、整理 P95≤5 秒；1x 回放记录分项时间，10 分钟积压不增长。目标不是已测结果。

## 方案和推进

A：固定 CPU 模型 revision `8e40c43232a1c5c66c82111efc5820d3accca11b`，独立 CPU 依赖环境；真实中文/夹英文回放；无对齐 Qwen；Luna none / Flash-Lite MINIMAL / Flash LOW 对照，通道不可用明确记录，不自动换供应商。

B：单实时会话；分段器唯一拥有 VAD 边界；样本轴 16k PCM16，80ms 网络包；250ms 前留、700ms 静音、150ms 尾留、20秒软目标/25秒硬限；有界队列、持久接收 ACK、版本提交、独立产物，stop/cancel/delete/restart 语义明确。

C 目标布局（沿用工作台 tokens；紧凑模式只缩减导航和边距，不新建会话）：

```text
语音输入                         [紧凑视图] [设置]
[实时听写] [上传录音] [历史结果]
┌ 已完成正文（按句编辑，人工修改不被后台覆盖） ┐
│ 原稿 / 整理状态；失败可仅重试整理             │
└──────────────────────────────────────────┘
临时预览（不进入复制正文）
状态文字 · 音量 · 时长
[开始 / 停止] [复制已完成文字] [下载 TXT]
```

控制区在独立布局行，不悬浮覆盖正文；窄屏自动换行；设置展开不遮住正文；删除二次确认。无麦克风权限先报错，不创建假录音。

D：100MB/30分钟默认保护；完整分段覆盖、共享 Qwen/cleanup，历史恢复、显式整稿差异预览后应用、取消及删除；真实上传/浏览器和旧流水线回归。

## 执行结果（2026-10-21）

A→D 的工程实现已完成。入口 `/dictation`，使用说明与当前契约见 [docs/DICTATION.md](../../docs/DICTATION.md)。本事项保持 open：未获关闭授权，且物理麦克风/人工质量验收和5秒整理目标仍有缺口。

### 已落地

- CPU 环境 `.venv/dictation-py312` 与既有版本约束的 Qwen 环境 `.venv/asr-py312` 独立安装，两个环境 `uv pip check` 均通过；Web 基础环境仅增加必需的 wsproto。固定 CPU 模型预下载；Qwen 缓存复用既有镜像权重，不增加模型菜单或下载 1.7B/SenseVoice 候选。
- 新增 `src/dictation/` 的音频/VAD、流式识别、无对齐最终识别、worker IPC、整理、状态/配置和存储，以及 `src/web/dictation.py`。旧 Qwen 构造只增加默认保持不变的可选 `timestamps/local_files_only`，生成触顶护栏继续共用。没有改旧批量入口、阶段6指令和产物主结构。
- 前端共用 `useDictation` 与版本化选择入口，提供普通页/紧凑小窗、术语、临时预览、已完成编辑、复制、上传、全文建议预览/应用、TXT 和历史/删除。新增探测与准备脚本、独立 CPU requirements、可选 Docker 构建项、使用文档。
- 正文统一由 `Storage.render_draft` 投影；Service 唯一提交版本，历史会话也共享缓存状态；全文建议绑定 content_revision。迟到结果不能覆盖人工编辑，旧建议不能借用最新 revision 应用。
- 上传接收/验证/启动由 Service 拥有；删除 tombstone 与关闭护栏拒绝新工作并等待现有任务。GPU 锁传给最终 worker，实际退出后才释放；重复取消等待同一次关闭，不再次打断清理。
- 空闲只用 VAD 样本轴；首连接、无音频、IPC 写锁+drain、CPU 停止收尾、上传接收和远端请求均有期限。CPU/GPU/LLM 失败保留音频/原稿，不换模型、不冒充最终成功。
- 网关地址/鉴权走现有网页设置和环境接口。用户提供的临时 key 仅进入测试进程环境，未写源码、配置或结果。通过既有 transport observer 记录实际回报模型 ID 与 usage，不持久化原始 HTTP 响应。

### 回归与自评

最终 `.venv/bin/python -m pytest`：**746 passed / 40.80s**（新增34个用例/参数化情况），日志 `tmp/dictation-full-pytest-delivery-02.log`。另一次同候选 Python 验证为746 passed / 39.06s。前端 `vue-tsc --noEmit && vite build` 通过；构建有大 chunk 提示（不阻塞）。`node --experimental-strip-types frontend/tests/dictation-state.mjs` 通过；`git diff --check` 通过。

新增测试覆盖本轮审阅提出的全部所有权问题：单一正文投影、apply/save/retry/edit/GET/TXT一致、重启会话并发编辑、旧全文建议失效、VAD而非partial空闲、删除/关闭与新请求竞争、阻塞上传取消、初始连接期限、永不drain的写入、stop已写但永不drained、重复取消，以及前端旧会话/旧revision/旧socket拒绝。还验证短回答/尾部/硬切连续覆盖、音频幂等与缺口、上传保护、ASR失败不发布预览、整理失败保留原稿、严格JSON与长稿完整分块、usage读取。

按 `docs/REVIEW_CHECKLIST.md` 自评：批准范围内，使用项目 `.venv`，旧中间 JSON 兼容，无上游回写；已真实推理、上传、检查产物和浏览器截图。没有以测试通过代替语义准确度结论。新 Docker 可选层未重新构建/部署，生产容器未重启；未 commit/push。

### 真实运行证据

1. 无 ForcedAligner 的 Qwen 在4段真实中英录音推理成功，约0.19–0.61秒/段；`tmp/dictation-qwen-spike.{json,log}`。本机后续两个短录音按1倍速度双遍回放成功，`tmp/dictation-dual-clean-real-01/`。
2. 十分钟连续回放：原录音 `data/jobs/f30050364963/input/audio/source.wav` 的 `[0,600]` 秒只读复制到独立验证目录，**600秒、9,600,000样本、7,500包、84句，全部 cleaned**，无重复语句ID，时间区间有序无重叠，ASR/cleaned文件齐全、draft一致。最大音频积压0.016秒，未增长。证据 `tmp/dictation-ten-minute-real-02/{summary.json,artifact-check.json,001.json}`，实际产物 `data/dictation/5730247b47764cc4b7d7a1b9a2c7fb65/`。
3. 该回放的服务端延迟：首个增量文字 P50/P95=0.687/0.726秒（81句，另3个短回答经flush保留）；最终ASR=0.967/1.431秒（84句）；Luna整理=3.998/5.828秒（84句）。CPU峰值RSS约390 MiB，4线程累计约2334 CPU秒；Qwen PyTorch peak allocated约1.75 GiB、aligner_loaded=false。不是总显存或物理麦克风到DOM延迟承诺。
4. Chrome真实录音虚拟麦克风：`tmp/dictation-browser-05/report.json` 全部7项通过、0页面异常、无ASGI error。实际Worklet重采样/WS短断线重传/双遍/整理、立即释放麦克风、编辑/复制/GET/TXT一致、全文建议应用、术语跨小窗冻结、390px布局及设置展开、真实WAV上传、历史删除确认、权限拒绝无假会话均通过；截图已读取检查。
5. LLM六条关键文本同源对照：`tmp/dictation-llm-compare-02/results.json`。Luna none P50/P95=2.725/5.789秒；Flash-Lite minimal=1.835/2.730秒，均保留六例关键语义，Lite有两例缺末句号。早期未要求结构化输出的Lite试验返回格式无效；加与生产一致的schema后6/6可解析，不把早期失败当通道不可用。Gemini3.8仅有-high别名，六例成功但原生LOW转发未证，不作默认。`tmp/dictation-llm-usage-real-01/results.json` 记录三个候选实际回报ID与tokens；生产整理的usage也已从真实浏览器产物核查。
6. 首次十分钟材料选到长段非语音，VAD只检测到一个短词并按60秒空闲规则停止；该次只计为空闲停止验证，不计为十分钟连续成功。诊断 `tmp/dictation-ten-minute-real-01.log` 与对应独立会话保留；更换为已确认含约549秒语音的真实材料后，获得上述完整600秒结果。

### 已知缺口与继续条件

- Luna整理P95 **5.828秒超过建议5秒目标**。Flash-Lite在当前网关明显更快，但小样本保真与标点不够支撑完整默认切换，本轮保留用户指定Luna；不随机或失败时切供应商。
- 84句中40句被模型建议人工复核；没有人工参考稿，不报告CER或专名准确率。预览到原始最终稿的字符编辑量697/2336（包括标点、空格、大小写）只说明回改量，不等于识别错误率。
- 实际浏览器链路通过的是**真实录音虚拟麦克风**，未使用用户的物理设备，也没有约100条独立人工对照/留出集。因此工程交付完成，整体人工质量验收未完成。下一步是由用户真实麦克风录音并提供人工对照，优先检查数字、否定、条件与中英术语；同源扩大Luna/Lite保真对照后再决定是否改默认。
- 关闭时应毕业到独立语音输入能力文档/Project Spec（项目当前无 spec 树，勿擅自初始化）；不扩展到系统输入法、自动摘要、多人识别、训练或读书会未来阶段。

## 已部署：8080 与内网 HTTPS（2026-10-22）

用户后续明确授权 Docker 构建、更新8080服务，并要求解决内网HTTP无法使用麦克风的问题。本节更新前文“未部署”的历史状态；事项仍不关闭，不 commit/push。

### 启动故障与最小修复

- 原容器 `a98b25cd58e6` 反复退出，日志确定为 `PermissionError: /app/src/web/dictation.py`。宿主新增源码为0600/目录0700；普通COPY将其变成root所有，非root `app` 无法读取。
- `Dockerfile` 的运行时代码、配置、脚本、前端dist和听写requirements使用 `COPY --chown=app:app`，保留非root进程，不放宽data权限、不改为root运行。
- `compose.yaml` 显式传入 `DICTATION: ${DICTATION:-1}`，正常Compose构建即具备CPU worker环境，不在运行中容器手工补依赖。容器CPU环境的 sherpa-onnx 1.12.39 / ONNX Runtime 1.23.2 可导入，Qwen固定revision缓存存在。
- 自定义 `config/dictation.json` 选定文档化的**只读单文件挂载**入口（不进入镜像）；本次不使用该文件，按代码默认参数运行。文档用 `create_host_path:false` 防止不存在的文件变成目录。

### HTTPS 部署与更新所有权

- 新增 `compose.https.yaml` 和 `docker/Caddyfile`，使用固定digest的官方Caddy镜像、本地CA签发续期，非root/只读根fs。通过 `network_mode:service:trans` + `127.0.0.1:8000` 转发，保留单应用进程与默认loopback代理信任，不放宽forwarded-header信任。
- 本机忽略的 `.env` 持久指定完整Compose文件组、内网IP和IP客户端的default_sni。公开入口 `https://192.168.0.252:8443/dictation`；原 `0.0.0.0:8080 → 8000` 不变。没有修改宿主daemon、驱动或防火墙。
- CA私钥只在 `transcript-pipeline_https-data` 卷内；仅导出公开 `data/https/transcript-lan-root.crt` 供客户设备自行信任。该目录已忽略，不将CA私钥/真实配置打进镜像。
- Linux安装脚本原来只用基础Compose，会在下次重建时移除8443并遗留代理旧网络命名空间。现增加最小拒绝检查：shell COMPOSE_FILE、本机.env启用本overlay，或本项目任何https容器存在时，拒绝base-only操作并给出完整 `-f compose.yaml -f compose.https.yaml ... up ... trans https` 命令。6种入口/模式组合回归验证拒绝发生在修改宿主/构建之前，不泛化任意overlay。
- 官方Caddy二进制自带NET_BIND_SERVICE file capability，初次cap_drop ALL导致exec EPERM；明确仅保留该bounding capability后，非root代理正常执行。验证阶段发现并修复，未通过root/privileged运行绕过。

### 实际验收

- Docker新镜像构建成功；`trans` healthy、重启次数0；容器里原 `scripts/check_docker_gpu.py` 确认3060Ti、CTranslate2 CUDA count=1与float16。非root dictation导入和CPU环境均通过。
- `frontend/tests/dictation.mjs` 新增显式外部服务目标，测试不再另启宿主服务。对Docker `http://127.0.0.1:8080` 的真实录音虚拟麦克风7项通过，`tmp/dictation-docker-browser-01/report.json`。
- `curl --cacert` 真实校验证书链与内网IP，HTTPS health和页面200；没有使用 `-k`。隔离Chromium NSS库导入本CA后，未忽略TLS错误，内网HTTP `secure=false/media=false`，HTTPS `true/true`。宿主系统和用户现有浏览器证书库未改变，证据 `tmp/dictation-https-context.json`。
- 可信HTTPS浏览器7项也全部通过，实际WSS/双遍/网关、释放麦克风、编辑/复制、全文、小窗、上传、历史删除；`tmp/dictation-docker-https-browser-02/report.json`。初次Node请求未加CA时报issuer错误，随后只为测试进程通过NODE_EXTRA_CA_CERTS提供同一公开CA；不关闭TLS验证。
- HTTPS单独检查cookie的Secure/HttpOnly均为true，`tmp/dictation-https-cookie-check.json`。完整重建 `trans https` 后用**原CA**再次curl校验成功，证明部署更新保留CA且新代理连到新应用网络。
- 最终 `.venv/bin/python -m pytest`：**754 passed / 40.55s**（`tmp/dictation-https-pytest.log`）。Docker构建内前端类型检查/生产build通过；selection测试与diff检查通过。已按REVIEW_CHECKLIST核对范围、真实部署/产物、旧JSON兼容及验收缺口。

### 交付与剩余依赖

文档 `docs/HTTPS.md` 给出当前地址、公开根证书位置、Windows/macOS/Ubuntu/iOS/Android信任方法和每次保留overlay的升级命令；README、Docker/Linux、听写文档同步。

客户端负责人仍需将公开CA导入自己的访问设备并信任，再做物理麦克风验收；本机隔离测试不能证明另一台设备已完成证书安装或LAN防火墙验收。原有约100条人工质量对照和Luna延迟目标缺口不因部署成功而消失。

## 单镜像交付与 Git 保存（2026-10-22，后续授权）

用户要求不再维护两个镜像/容器，合并后导出镜像文件，并明确授权保存一个Git commit；不授权push或关闭事项。此轮将上节的HTTPS sidecar方案替换为单镜像，旧方案仅作历史记录。

### 当前唯一部署入口

- `transcript-pipeline:local` 内含前端、API、Qwen/流式ASR依赖、固定digest Caddy静态二进制与Supervisor。只有一个 `trans` 容器，8080/8443照旧；删除未提交的 `compose.https.yaml`，本机.env只加载基础Compose。旧https容器已移除，不删卷。
- Supervisor统一管理API/Caddy，任一启动失败/退出由约20行官方eventlistener协议适配触发整体关闭，交给Docker重启；不自写进程调度器，不启第二个ASGI进程。Caddy仍经loopback转发，CA/模型/会话沿用原持久卷。
- Caddy复制保留了上游file capability，`cap_drop ALL`预检发现exec EPERM；镜像构建时改变二进制所有者明确清除此capability，高端口无需特权。镜像还初始化app所有的空data目录，直接docker run不会因/app/data不存在而启动失败。
- 镜像与Compose健康检查均调用同一 `docker/healthcheck.sh`：HTTP与用原CA验证的HTTPS同时通过才healthy，不能被Compose旧HTTP-only覆盖掩盖。

### 关机期限与所有权

- Uvicorn显式 `--timeout-graceful-shutdown 5`，避免300秒慢上传在lifespan前耗尽停机预算。
- Service.close先取消全部后台任务与runtime，再统一等待真实外部线程和worker退出。避免先等background再取消runtime导致两个120秒请求窗口串行叠加；添加回归断言runtime在慢手工整理结束前已收到取消。
- Supervisor API.stopwaitsecs=145，Compose.stop_grace_period=160s；文档docker run也固定 --stop-timeout 160。5+120+2×(3+5)=141秒在API预算内，TLS停止和Docker余量另计。
- 隔离真实容器同时运行35秒本地HTTP整理请求与迟迟不结束的HTTP上传，发TERM后约**35.063秒**正常退出（code0）。HTTP上传5秒后取消；实际HTTP线程已结束，Uvicorn写入shutdown complete，没有Supervisor killing，原稿与partial上传保留，GPU文件锁可重获。见 `tmp/dictation-single-term/{report.json,container.log,run_probe.py}`。该验证不发付费请求。
- 隔离单镜像实测可信HTTP/TLS健康通过后主动终止Caddy，Supervisor观察到退出并正常关闭API/整个容器；`tmp/dictation-single-lifecycle.log`。

### 镜像文件、验证与当前偏差

- 导出 `tmp/releases/transcript-pipeline-single-20261022.tar.gz`，约6.0 GiB，tag=`transcript-pipeline:single-20261022`。**已真实docker load导入成功**，`tmp/dictation-single-image-load.log`。不把用户录音、API key、CA私钥或模型卷塞进镜像；跨机器仍需迁移/预下载权重与持久数据。
- 当前生产单容器healthy、API/TLS均RUNNING、原CA仍能验证，未修改驱动、daemon、防火墙或系统信任库。Docker构建中的前端类型检查/build通过；`.venv/bin/python -m pytest` **755 passed / 44.11s**（`tmp/dictation-single-image-pytest-final.log`），selection测试和diff检查通过。
- 单镜像可信HTTPS浏览器复测 `tmp/dictation-single-image-browser-01/`：真实录音→Worklet→WSS短重连→双遍识别、立即释放、人工编辑/复制/GET/TXT通过；**全文整理未通过**，网关 api.redworker.org 的TLS连接超时/SSL_ERROR_SYSCALL。其后容器与宿主均无法连接该网关，容器访问其他HTTPS（Hugging Face）为200，应用本地HTTP/TLS仍正常。此为当前外部依赖阻塞，未改供应商/代理或静默跳过，也未把之前双容器7项成功写成本镜像全套通过。失败逐句保留原ASR。
- 文档已收口到“一个镜像、一个trans、一次up”，提供docker load、等价docker run、原双容器一次迁移、关机预算和证书信任；不再要求维护overlay。按用户本轮授权保存Git commit，事项保持open。

当前后续动作：访问设备信任原公开CA后试录；网关负责人恢复该地址连通性后再重跑全文整理。物理麦克风、100条人工标注与Luna延迟目标仍是原有验收缺口。

## 修复本地SSE滴流突破关机期限（2026-10-22）

完成审阅发现：旧 `SessionGatewayClient` 沿用批处理策略，本地/`.local`网关走urllib，socket inactivity timeout无法限制不断发送SSE但不完成的请求。35秒后自然结束的验证没有证明总期限，145/160秒预算在该配置下可能强杀API。

最小修复仅在 `src/dictation/cleanup.py` 的 SessionGatewayClient 覆盖SSE发送：构造相同请求，直接复用 `read_http_response_with_curl` 与严格SSE提取器，所有听写网关均应用curl `--max-time`总期限，禁止None/0关闭期限。不增加新传输，不改 `src/codex_lb_client.py` 或旧批处理默认策略；原始ASR/失败保留和provider usage保持不变。

新增6项回归：localhost和127.0.0.1持续合法delta、永不completed时在0.35秒期限附近退出，实际服务端连接断开；Service关闭等待真实HTTP线程返回而非只取消await，并保留原稿；正常completed结果及usage保留；None/0期限拒绝。定向41 passed；全量 `.venv/bin/python -m pytest` **761 passed / 43.04s**，日志 `tmp/dictation-curl-deadline-{tests,full-pytest}.log`。

真实隔离Docker验证使用localhost网关每50ms滴流（不发送completed），配置8秒；同时发送未结束上传并对Supervisor容器发TERM。**8.090秒退出、code0、160个SSE块、Uvicorn shutdown complete、无Supervisor killing**，原稿/partial上传保留，GPU锁可重获，见 `tmp/dictation-drip-term/{report.json,container.log}`。该网关进程与测试容器同时结束，TERM报告中的peer-disconnected观测未落盘；实际socket断开与线程返回由独立回归明确断言，不拿该缺失观测冒充通过。

使用同一单镜像入口更新运行服务和导出文件，保留上一镜像tag/归档供恢复；按用户既有授权追加修复commit，不改写原提交，不push或关闭事项。使用说明同步明确：最大120秒是所有听写网关的传输总期限，而非仅公网curl-first地址的条件性保证。

## 全文500、low/high提示词与限高文字框（2026-10-22）

用户反馈实时听写的全文整理500及长语句挤走操作按钮；后续明确要求优化两套提示词，逐句从none改为low，全文使用high。仍在本事项中完成，不重跑用户原音频、不扩展旧读书会流水线。

### 原因与实现

- 生产日志明确：该次全文整理沿用逐句20秒期限，curl (28)在收到231020字节后超时，CodexLBClientError未映射HTTP，直接变成500；不是可据此推断的模型识别失败。
- `Cleanup.__call__` 为逐句Luna low / 4096输出预算；`Cleanup.document` 为全文Luna high / 独立16K–32K输出预算、默认120秒总期限。两套专用提示词分别界定逐句轻改和整体断句分段；允许有充分上下文证据的同音错字修复，保留数字、主体、否定、条件、专名、中英混说，不总结、猜造或执行转录指令。当前实测仍有疑似听错未修，high不是ASR准确率保证。
- 普通正文≤12000字符一次整体请求；超长才在Cleanup内分块并注明strategy/chunk_count，所有块共享**同一个总期限**，不是每块120秒。任一截断/错误不返回部分全文。Service仅提供Storage.render_draft快照、版本检查、生命周期与建议提交，不塞入高推理payload/分块策略。
- 全文超时返回504、网关/格式错误返回502和中文原因，保存document_cleanup_error；重试成功清除错误，原稿和TXT不改。已应用/手工编辑的全文也可再次预览；应用后明确消费建议，结果与原文相同时也不留下旧预览。
- 完成审阅发现主窗/小窗可并发两次全文。Service新增按sid的single-flight集合：在首次await前占有，直到真实外部线程退出后的finally才释放；第二次409。不同sid仍保留全局并发2。新增旧请求失败/取消但线程未退出时的竞争回归，独立会话可先成功，新建议不被旧失败污染。
- 前端正文和临时预览合入限高内部滚动框，下面录音/复制/全文按钮不随文字长度下移。接近底部时跟随最新，向上浏览或编辑时不抢滚动，可主动回到最新；差异预览也限高内部滚动。增加Luna high处理中反馈、中文失败/重试及已应用全文再次预览。

### 验证与实际产物

- `.venv/bin/python -m pytest` **771 passed / 44.99s**（`tmp/dictation-high-full-pytest-final.log`）；补充同文应用边界后的document定向10 passed；前端类型检查/build、selection与diff检查通过。
- 新测试断言实际全文payload包含合并当前正文及人工修改、high、独立预算；逐句payload为low；合法JSON但response.incomplete不发布；网关/超时HTTP错误可重试；分块共享总deadline；同sid单飞直到真实线程返回。
- 真实脚本 `probe_dictation_cleanup.py --document` 读取原失败的1866字符正文副本，不修改原会话，单次high **43.35秒**完成，strategy=whole、reported_model=gpt-6-luna、needs_review=true。两条语义样例high为3.83/4.30秒、low为2.95/3.15秒，关键数字、否定、条件、顺序、英文和引用指令保持。没有据此报告CER或延迟P95。`tmp/dictation-{high,low}-real-02/results.json`；首轮提示词验证记录在real-01，仅作历史对照。
- 真实隔离容器的全文high入口，localhost网关每50ms持续滴流且不完成，配置8秒总期限，与未结束上传并发TERM：**8.083秒正常退出/code0**，Uvicorn shutdown complete，无Supervisor强杀，原稿/partial上传保留，GPU锁可重获。`tmp/dictation-high-term/report.json`。网关与容器同停，未用未落盘的peer-disconnected标记证明连接退出；独立滴流回归仍明确检查真实socket和线程返回。
- Playwright合成长会话（60句+超长partial）验证内部滚动、向上阅读不抢滚动、回到最新、下方按钮位置、失败重试及已应用全文再次整理；390px手机和540px紧凑窗无水平溢出，已查看实际截图。`tmp/dictation-layout-high-04/`。此测试不冒充真实ASR/LLM。
- 部署前确认0活动听写、0运行批处理，再更新唯一trans容器；原CA/数据/模型卷保留。新镜像可信HTTPS真实浏览器 **7项全部通过，0页面异常**（WSS重连、双遍+low、编辑复制、high全文预览/应用、共享小窗、上传历史删除、权限拒绝）；`tmp/dictation-high-docker-browser-01/report.json`。
- 单镜像归档已更新并实际docker load验证：`tmp/releases/transcript-pipeline-single-20261022.tar.gz`，约6.0GiB，仍用single-20261022标签；旧版归档和before-luna-low-high标签保留供恢复。未把录音、会话或凭据加入Git/镜像。按此前授权追加补丁commit，不push、不关闭事项。

本轮两个用户问题与所提并发边界已修复。当前仍需人工确认实际录音中的疑似错词/专名；物理设备、约100条人工对照和原实时延迟目标仍是后续质量验收，不由此次小样本和虚拟麦克风替代。

## 语音输入优化 A → B → C（2026-10-23，实施中）

用户批准按三个独立验收批次连续推进，仍禁止自动 commit/push、部署/重启、模型下载、付费整理、新跑旧用户录音或关闭事项。基线 HEAD 为 `8dd129d`，开始时工作区无改动。本节的 A/B/C 与最初模块选型阶段编号不同。

- **A（工程/公开音频验收通过）**：应用级唯一采音拥有者；查看历史与活动会话分离；WS 有序音频边界暂停、CPU 收尾但不退出；用户主动继续；站内底部常驻面板。
- **B（工程/公开音频与格式验收通过）**：复用采音的 IndexedDB 本地草稿、WAV 试听/下载/转写；上传进度/取消，文件转写暂停生产者及新 ASR/整理派发。
- **C（工程完成，人工质量材料/准确率验收未完成）**：设备、试音、句子回听与术语；人工参考基线、单变量对照；分别报告工程完成、质量评测与准确率改善。

### A 的实现约束与验收

暂停先关闭本地麦克风、flush 尾包，再在同一 WS 发送 `{type:pause,next_sequence,samples}`。服务端核对持久接收位置，进入 pausing；CPU 提交 `boundary=pause` 并重置非连续 VAD/预览状态后确认 paused。继续先申请麦克风但不发布，收到服务端 resume 确认后按连续样本位置采音。暂停不插静音、不重置句号或包号，保留模型/GPU 锁；断线宽限期仍适用。停止/取消/删除高于迟到继续。

目标布局：站内所有页面底部停靠 `[状态 · 有效时长 · 音量 · 收起] / [暂停或继续] [结束] [复制正文] [正文]`；收起仍保留状态和暂停操作。内容按面板实际高度留白；390px 换行并考虑安全区。语音页和面板共享控制器，不产生第二路采音。小窗可查看/复制/结束，不能冒充原窗口采音拥有者。

关键风险与证据：
1. 音频完整性/可靠性：尾包、短句、多次暂停、重复命令、暂停/结束/取消/删除竞争、70秒暂停及断线超时；检查 source.pcm、样本轴、句子 WAV，不以识别词语代替。
2. 采音生命周期/可靠性：权限迟到、拒绝、设备失效、旧 Worklet/WS 回调；跨三站内页面不换 session、不重建 mic/socket；暂停不自动恢复。
3. 交互能力：真实桌面/390px/长正文/收起态布局与点击；正文投影、人工编辑和复制回归。
4. 兼容性/信息安全性：ACTIVE 与重启中断涵盖新增状态；旧字段缺省；同源、所有权、重复包、GPU 锁与唯一正文保持。

验收命令：`.venv/bin/python -m pytest`、前端 build、dictation-state、隔离真实浏览器/模型样例。证据放 `tmp/dictation-optimization/`，不提交私人材料。

### A 执行结果

- PcmCapture 唯一管理硬件/lease、尾包协议；App provide/inject 唯一控制器拥有 WS、重连、活动任务和独立历史选择。浮动面板只消费同一控制器，ResizeObserver 提交实际面板高度留白。路由卸载不关闭采音；pagehide 不自动恢复。
- Runtime/CPU 新增 pausing/paused 和非EOF pause；验证下一包/样本边界，提交尾句后清理VAD/预览上下文，位置与句号不重置。重复命令幂等；暂停不自动完成、不触发无音频/静音检查，断线仍有宽限期限。停止/取消/删除及重启中断覆盖新增活动状态。
- 每段采音独立墙钟映射，前后结果均按原始所属样本段计时，暂停空档不污染后续延迟。模型/GPU锁在暂停期间保留。原始音频/ASR/整理与Storage正文投影保持。
- 两项架构反馈已落地：上传成功登记不依赖当前历史选择；创建代次与采音控制代次分开，已完成历史无权取消新上传。stop/cancel 必须显式传会话ID，页面历史动作不隐式操作另一活动任务。

验证：全量 `.venv/bin/python -m pytest` **787 passed / 47.57s**，`a-pytest-01.log`；新增暂停协议/CPU/短尾/20循环/竞争/重启/延迟/WS共16项。前端build、dictation-state通过。`a-ownership-02/report.json` 的四项所有权状态测试与 `a-layout-01/report.json` 的长正文/编辑/全文建议/390px/540px回归通过；这些为合成状态证据。

真实免付费运行 `a-real-02/report.json` 通过：公开官方0.wav经Chrome虚拟麦克风、Worklet、WS、实际CPU/GPU；三路由切换保持mic/context/socket/session不变；70.5秒暂停零新增音频；20次继续/暂停；拒绝和迟到权限；暂停中短断线及超宽限期中断。主测试148475样本/123个唯一包，全部去重包PCM与source.pcm逐字节相同，句子WAV也与实际样本范围相同；21个暂停边界，实际ASR1句（短循环主要覆盖采音压力，不冒充20句语义测试）；final延迟0.587秒，暂停累计73.27秒，最大积压16ms。未调用远端整理、未碰用户历史音频、生产容器未修改。

首轮 `a-real-01` 已通过70.5秒暂停，但在切换窄屏后立即量布局，早于ResizeObserver/Vue提交，检查失败；等待真实尺寸/留白稳定后 `a-real-02` 通过。截图中桌面路由过渡尚未结束，已补针对目标组件过渡结束的等待，继续保留真实候选的最终截图验证。未将失败轮记为全通过。

按REVIEW_CHECKLIST自评：批准A范围、项目.venv、旧JSON可读、无上游回写；自动化和真实公开音频证明暂停及跨页控制。个人物理麦克风/设备拔插和本人口音仍需人工验收，虚拟麦克风不替代该证据。最终 `a-real-03` 再次完整通过同一路径（含70.5秒、20循环、错误恢复），桌面与390px展开/收起截图已读取，正文和操作区不被面板遮挡；`a-ownership-03` 和state测试再次通过。A工程与公开真实音频链路验收通过，个人物理设备仍为独立人工依赖。按既有授权进入B，C尚未开始。

### B

仅复用A的PcmCapture与现有上传/Runtime链路；新增IndexedDB本地草稿的独立生命周期，不把草稿ID冒充服务器会话ID。录音时不调用模型；本地保存/读取/WAV导出互相有明确完成边界。文件转写采用Runtime统一派发门控；在途操作完成后才确认暂停，保留文件游标/分段状态/队列；EOF/满队列/取消不互锁。界面区分实际上传字节、解码阶段、实际转写位置与整理任务计数。B完成与真实样例验收后才进入C。

### B 执行结果

本地草稿独立保存在IndexedDB，约1秒块与样本位置事务一致，内存/写入积压5秒有界；暂停释放麦克风、保存尾块，结束等事务完成后提供PCM16 WAV；刷新只打开已保存部分。提交先single-flight占有并冻结样本版本，再走现有受限上传；失败/未知响应保留草稿，后者须检查历史再确认重试。明确本地/服务器删除范围。模型忙可本地录音，录音自身没有推理生命周期。

Runtime文件派发gate约束audio/asr/cleanup，保留在途操作的期限；EOF和满队列时仍可暂停，取消/结束不互锁；实际ASR位置/失败与整理计数单独公开，不用入队进度冒充识别进度。文件暂停不插人工边界，恢复不重做已提交句子，重启仍中断而非承诺精确续跑。

两项后续架构反馈已修复并补测：
- PcmCapture lease显式owner=live/local；prepare/start/stop核对owner，旧live终止快照不能释放local采音。普通轮询与延迟终止快照期间本地硬件/界面都持续录音。
- 等待文件gate时不持全局cleanup slot；取得slot后无await复核gate、登记inflight再创建请求。paused文件等待期间两项独立手工整理可同时取得全部两个槽，且文件零新派发。模型/GPU锁仍按产品决策保留。

验证与真实证据：
- 全量`.venv/bin/python -m pytest` **794 passed / 48.22s**（`b-pytest-01.log`）；随后增加实际loopback HTTP滴流文件暂停用例，文件定向**8 passed**。真实curl在途退出后才paused，失败原稿保留。前端build/state通过，新增storage/ownership浏览器及既有长正文布局通过。
- `b-real-03`：实际公开音频虚拟麦克风录制**2,894,335样本/约180.90秒**、三次暂停；试听/下载成功、录音阶段服务器会话目录为空；WAV数据与上传decoded/source.pcm逐字节相同，实际Qwen **36个唯一分句/0失败**，无跳读或重复ID。文件暂停后无新增ASR产物/样本；继续完成。GPU忙时另一条本地录音不被文件完成状态释放。刷新恢复已保存数据，无自动开麦。
- 03轮接着测格式时遇到Chrome inspector响应体逐出，脚本整体failed；不重跑已成立的三分钟录音证据。补测驱动改为读取owned history API，等待disabled文件输入解禁（04轮因程序强制写disabled输入未触发上传而超时）；`b-formats-05` **WAV/MP3/M4A实际解码+Qwen均成功**。01轮宿主无ffmpeg预处理失败，改用已安装PyAV编码并停止自己的孤立测试进程，没有安装软件或碰运行容器。
- `b-drafts-03`：真实IndexedDB与采音的配额/积压/限长/刷新/WAV检查通过；上传为合成HTTP失败，双击提交只有一请求。此前02轮暴露IDB回调同步throw冒泡，现事务abort消化异常并保留有效前缀，0 pageerror。`b-ownership-02`五项通过（含上述旧live/local串扰），`b-layout-01`长稿/人工编辑/全文建议/390px/540px回归通过。已读取本地录音390px和恢复草稿截图。

B工程与公开真实音频/格式链路可用；未调用付费整理供应商，实际供应商语义与个人物理设备仍是人工/费用依赖，不以loopback模拟网关代替质量结论。按REVIEW_CHECKLIST：本阶段范围、.venv、原JSON兼容、新字段缺省、无用户历史回写、无部署/下载/commit/push。进入C工程切片；人工参考与准确率改善验收尚未具备材料。

### C 执行结果：工程完成，质量评测未完成

设备列表/本地试音/明确选择保存，exact设备申请、无输入提示和设备失效停止路径已补；录音中禁用切换。原句通过受会话所有权保护的WAV接口回听，支持Range，拒绝未知uid/任意路径/符号链接；全文显示原始分句列表，不虚构逐字对齐。常用术语本浏览器增删/主动选用、人工改词后明确加入，50项/80字去重与会话快照不变。

后续架构反馈：回放不能由各页面按钮包装独自保护。新增应用级DictationPlayback唯一播放所有者，所有播放入口claim互斥；PcmCapture.prepare/start调用beforeCapture统一stop，覆盖dock和页面的实时/本地继续。原句/草稿播放后点击dock继续的实际麦克风申请发生前，播放器已经paused；state检查也覆盖两个播放器互斥。

新增`score_dictation.py`只读probe产物、无需模型/网络。保留原始参考/输出、模型/词表/分段/整理配置、S/D/I/CER、字面术语/数字/否定/条件、失败/缺失/重复ID/静音、分项延迟/资源；数字小数点/负号不被过度归一化，1500不因包含于15000而通过数字检查。显式tuning/holdout及recording_group防近似片段泄漏，缺失产物不丢弃；整理语义留给人工而非CER判好坏。probe新增sample身份与可选隔离root，不改默认调用/供应商。

验证：全量`.venv/bin/python -m pytest` **799 passed / 50.25s**（`c-pytest-01.log`）；build/state通过。`c-real-03`真实公开音频/CPU/GPU验证设备选择、词表冻结、原句回听、人工修改、明确加词、两种回放→dock继续、刷新无自动开麦；390px截图已读取。`c-ownership-01`、`c-drafts-01`和`c-layout-01`全部通过，分别为合成HTTP/状态、真实存储故障和长稿布局证据。

`probe_dictation.py`真实1x公开0.wav：`c-probe-02`完成10.053125秒/160850样本/2句，最大积压16ms，无远端整理；原固定模型和默认分段不变。`c-score-02`明确samples=1、scored_samples=0、human_semantic_reviewed=0、holdout=0、CER=null（尚无人工参考），没有把评测脚本存在说成准确率提高。首轮相对root令worker找不到配置，已resolve绝对路径；失败轮`c-score-failed-start`保留missing_reports=1。早期C浏览器测试未展开details而不能填词表，修正驱动后03轮通过，不掩盖01/02失败记录。

质量报告与可复现用法：`docs/DICTATION_QUALITY.md`。按REVIEW_CHECKLIST自评：阶段工程及真实公开样例通过、范围和旧JSON兼容、未回写上游；付费整理/个人设备/人工参考约100条仍是明确依赖。**A/B工程和公开音频验收通过，C工程完成；人工质量评测未完成，准确率改善未证实。**不改默认模型/语言/分段/供应商，不下载/部署/重启/commit/push/关闭事项。

最终收口：`final-pytest.log` **799 passed / 49.52s**，前端类型检查/build、state通过；`final-a-regression`再次实际验证70.5秒与20循环，`c-real-final`再次通过设备/词表/原句及两种回放→dock继续。补充文件提前结束不能误报全文件100%的断言，数字字面检查区分负数/百分比；设备选择排除可漂移的系统default/communications别名。仍有既有frontend大chunk提示和FastAPI测试依赖弃用警告，不影响本轮功能验收。

下一步由用户/人工标注者提供新设备/口音录音及忠实参考，按recording_group约70%调参/30%留出，先定位采音/切分错误，再做少量单变量候选；远端整理或较大模型另需调用/资源授权。没有材料前保留原默认值，不为了完成指标制造改善。

### 本轮文件清单

- 应用/视图：`frontend/src/App.vue`、`frontend/src/views/DictationView.vue`；新增`frontend/src/components/DictationDock.vue`、`RecordingDraftPane.vue`。
- 控制/音频：修改`frontend/src/composables/useDictation.ts`、`frontend/public/dictation-worklet.js`；新增同目录`dictationCapture.ts`、`dictationDrafts.ts`、`dictationPlayback.ts`、`dictationTerms.ts`。
- 服务端：`src/dictation/{audio,service,storage,worker}.py`、`src/web/dictation.py`。
- 脚本：修改`scripts/probe_dictation.py`；新增`scripts/serve_dictation_test.py`、`scripts/score_dictation.py`。
- Python验证：修改`tests/test_dictation.py`；新增`tests/test_dictation_pause.py`、`tests/test_dictation_file_pause.py`、`tests/test_dictation_quality.py`。
- 浏览器/状态验证：修改`frontend/tests/dictation-state.mjs`、`dictation.mjs`；新增同目录`dictation-ownership.mjs`、`dictation-pause.mjs`、`dictation-drafts.mjs`、`dictation-recording.mjs`、`dictation-proofreading.mjs`。
- 文档：`docs/DICTATION.md`、`docs/PIPELINE_PLAN.md`、本事项；新增`docs/DICTATION_QUALITY.md`。测试音频、私人材料、凭据及模型不进入Git。

## 后续请求：录音4小时，Luna校对3600秒

用户先提出12h，随后明确收窄为4h，并要求不用过度测试。本轮仅调整语音输入容量/期限和直接关联的导出资源边界，不改模型、提示词、供应商，不部署/重启/提交/关闭事项。

- 本地录音/导入默认240分钟、512 MiB；4h PCM为460,800,000字节，WAV为460,800,044字节。Luna逐句及全文默认/允许上限3600秒，长文所有块仍共用一个总期限；旧自动任务保留冻结值，手动重试采用当前期限且不改旧快照，metrics记录实际期限。
- 文件解码归Runtime、用独立`decode_timeout_seconds`默认900秒/上限3600秒，替换固定120秒；修正解码回复恰在轮询间隔迟到时不能越过期限。上传接收独立1800秒、XHR多等10秒。前端显示实际服务器限制与校对期限。
- 架构反馈已落实在DraftStore.wav：约4 MiB PCM分批只读事务转Blob，最终只组合Blob部分；不一次累积所有PCM ArrayBuffer；组装single-flight，播放/提交前释放旧URL，失败保留完整已保存草稿。仍受浏览器Blob临时盘/存储配额约束，没有在各按钮添加4h特判。
- 未来部署模板的Supervisor3650秒/Compose3670秒优雅退出预算与3600秒远端期限匹配；停止/删除可能等待在途校对接近1小时。未修改正在运行的容器。

验证：`.venv/bin/python -m pytest` **810 passed / 50.58s**，前端类型检查/build、state通过。本地实际HTTP/curl验证新期限传递，不做1小时空等或付费调用。可控时钟验证独立解码期限与旧120秒边界、手动重试当前期限及旧快照保持。

容量真实数据边界`capacity-4h-01`：14,400个一秒PCM块/230,400,000样本；第二批Blob失败受控且完整草稿可重试；完整WAV组装、跨批/尾部标记、14400秒媒体时长、近EOF播放、460,800,044字节下载通过。后续完整HTTP上传时浏览器关闭，**整轮仍记录failed，原因未确认**；没有宣布完整4h上传/服务端PyAV4h解码/持续采音4h/ASR4h验收。按用户要求停止追加大文件/长时验证，后续完整HTTP测试改为显式opt-in，本轮不重跑。

本轮相关文件：`src/dictation/{schemas,cleanup,service}.py`、`src/web/dictation.py`；`frontend/src/composables/{dictationDrafts,useDictation}.ts`、`frontend/src/views/DictationView.vue`、`frontend/src/components/RecordingDraftPane.vue`；`compose.yaml`、`docker/supervisord.conf`；`tests/test_dictation.py`、`tests/test_dictation_document.py`、`tests/test_docker_deploy.py`，新增`tests/test_dictation_limits.py`；`frontend/tests/{dictation,dictation-layout}.mjs`，新增`dictation-capacity.mjs`；`docs/DICTATION.md`、`docs/DICTATION_QUALITY.md`及本事项。按REVIEW_CHECKLIST检查了批准范围、兼容及失败保留，下一步如要网页生效需用户授权部署。

## 发布授权（2026-10-23）

用户随后明确要求「先保存一个commit，然后部署」。本轮全部语音输入改动保存为一个commit后更新现有单容器，不push、不关闭事项、不调用付费服务、不追加长时测试。部署前只读核对：无pending/running/活动听写任务、无识别worker或在途校对HTTP，现有容器healthy、GPU profile和原HTTPS地址/持久卷保持。发布后的实际commit、构建日志及健康/配置结果记录在`tmp/deploy/dictation-4h-<commit>/`；部署不改变质量报告中的未完成验收结论。
