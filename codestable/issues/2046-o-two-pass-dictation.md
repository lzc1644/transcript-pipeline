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
