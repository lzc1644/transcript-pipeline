# 语音输入：听写、上传、编辑和复制

从网页「工具 → 语音输入」进入。桌面 Chrome / Edge 可在普通页面或紧凑小窗中使用；内网 IP 访问麦克风也需要 HTTPS，本机 `localhost` / `127.0.0.1` 可以使用 HTTP。内网部署与一次性证书信任见 [HTTPS.md](HTTPS.md)；本机已启用 `https://192.168.0.252:8443/dictation`，原8080 HTTP保留。

## 开始使用

1. 点击「开始听写」，允许麦克风权限。等状态显示「正在听音」再讲话。
2. 文字框内显示当前句预览和已完成正文；停顿后先显示 Qwen 最终识别，再提交逐句整理结果。框内滚动，录音/复制按钮不随文字增长下移；浏览旧句时暂停自动跟随，可点「回到最新」。
3. 录音由应用级唯一控制器持有，切到任务列表、设置或历史不会中断。底部常驻面板可「暂停听写 / 继续听写」「结束听写」「复制正文」「正文」；收起后仍保留状态和暂停/继续入口。暂停立即关闭麦克风、保留尾包，不插入静音；只有服务端确认后显示「已暂停」。
4. 「结束听写」立即释放麦克风，继续完成已收到的尾部。暂停时仍占用模型和 GPU 锁；排队识别/整理可继续，暂停不是释放识别资源。关闭/刷新页面不自动恢复麦克风；断线超过会话宽限期（默认10秒）后结束已接收内容。
5. 「设置」分别选择 Qwen 识别后的逐句快速处理模型、全文整理模型及各自的思考强度，并提供少量术语和整理开关。选项随新会话冻结；术语只作为最终识别与整理参考，不强制替换，也不继承读书会词表或书名。
6. 三个入口分别为「实时听写」「录音转写」「导入音频」，另有服务器历史。「录音转写」先用同一采音引擎录到本浏览器 IndexedDB，不创建服务端会话、不启动 worker、不占 GPU；暂停后继续追加同一草稿，结束后可试听、下载 WAV 或提交既有上传链路。
7. 「导入音频」显示实际上传字节并可取消；上传完成后可暂停/继续文件转写。默认最多 512 MiB、4 小时；无网络断点续传。解码无假百分比；转写进度按实际已处理音频位置计，整理按当前应整理/已结束任务计，失败单列。
8. 「历史结果」只列本浏览器拥有的会话。删除需确认，先停止接收、推理和整理，再删除音频与文字。
9. 停止并处理完成后，点击「整理全文」使用当前会话选定的全文整理模型处理正文（含人工修改）。先展示差异，确认后才应用；已应用/编辑的全文也可再次整理。失败时保留原稿，可重试。

「紧凑小窗」打开同一个会话的结果视图，使用相同浏览器凭据。原窗口继续拥有麦克风；小窗可以查看、编辑、复制或停止。暂停/继续须返回原录音窗口，小窗不会抢占麦克风。不要在录音时关闭原录音窗口；断开超过宽限期后服务端会结束会话。没有会话时也可先开小窗，再直接在小窗开始听写。

复制是点击当时的**已完成正文快照**，不包含临时预览。整理尚未完成时，复制使用已经完成的原始识别。

## 本地草稿与文件暂停

本地录音以约1秒 PCM 块保存，块和累计样本位置在同一 IndexedDB 事务提交；界面分开显示「已录制」「已保存」。写入积压上限5秒，空间不足或写入失败会停止采音，保留已成功保存部分供试听/下载。结束等尾块事务完成后才提供完整 WAV；默认4小时，16kHz/PCM16/单声道原始音频约460.8 MB（439.5 MiB）。导出由DraftStore分批读取，每批约4 MiB，转成Blob后再组合，不把整段PCM保留在JavaScript数组中；试听、下载、提交共用这一边界，同一草稿不能并发组装。浏览器仍需要足够的IndexedDB、Blob临时磁盘和进程内存，资源失败时保留完整已保存草稿并提示重试。导出下载是恢复路径；转写提交另核对当前服务器时长/大小限制，不静默裁剪。

同一草稿用浏览器 Web Locks 保证跨窗口独占操作权；录音、暂停和提交期间均持有锁。另一窗口打开时只读，不改变活动草稿状态，也不能继续、结束、删除或重复提交；原窗口仍保留麦克风和控制面板。原窗口结束或关闭后，点击「重新检查草稿」才取得可操作视图。写入、删除和提交入口都检查独占权，不只依赖按钮禁用；浏览器不支持 Web Locks 时明确报错，不降级为无锁录音。

刷新/意外关闭只恢复已保存草稿，不自动重新开麦；只有取得锁、确认没有活动拥有者后，才把遗留的recording标为意外中断，把遗留提交标为结果未知。升级此版前请先结束旧版所有窗口的本地录音，再刷新页面；旧版页面没有跨窗口锁，不能与新版同时录同一草稿。提交前冻结音频样本版本，并在独占锁内重新读取、检查提交状态；其他窗口留下uploading或恢复unknown时，旧页面也不能绕过历史确认，拒绝提交时同步新状态但不发起上传。上传失败不删草稿；不明确的响应需先检查服务器历史再确认重试。提交成功记录对应样本版本及服务器会话ID，不自动删除本地草稿。「删除本地草稿」与「删除服务器录音」分别确认、互不代删。本地存储可能被浏览器清理，请下载备份。

实时与本地采音共享一个具有明确 live/local lease 的 PcmCapture；只有所属生命周期能释放该 lease。旧实时会话的轮询/迟到完成不会关掉新的本地录音。文件识别占用GPU时仍可单独本地录音；活动实时听写（包括暂停）不能同时开启本地第二路麦克风。

文件暂停统一约束生产者、新最终 ASR 和新逐句整理请求；已经开始的解码/推理/HTTP在原期限内退出后才显示「已暂停」。暂停保留文件读取位置、分段状态和待处理队列，不制造人工句界；EOF后还有识别/整理时仍可暂停；CPU输入只关闭一次，暂停后点击「结束」会解除派发等待并完成剩余工作，不再次向已退出的CPU worker发送stop。保留模型和GPU锁，但等待恢复不占空闲的全局HTTP整理槽；取消/结束可以解除等待，不因满队列互锁。服务重启仍标记中断，本阶段不承诺精确跨重启续跑。

## 设备、回听和常用术语

「设置 → 授权并列出设备」可选择麦克风并保存到本浏览器；后续按该exact设备申请，缺失时要求重新选择，不静默切换。录音中不能切换，请先暂停。持续5秒无明显输入会提示检查静音/连接/音量。「本地试音」进入录音草稿，试听仅在本地完成，不调用识别或整理。

每句「听这一句」播放该句真实样本范围；失败分句也可回听。全文稿只提供「原始分句音频」列表，不假装全文/逐字对齐。任一时刻只有一个应用内播放器；页面或面板继续采音前，由应用级协调器统一停止原句/草稿回放，避免录入回放声。

常用术语库保存在本浏览器。主动增删、勾选并「用于下次识别」，或人工改词后通过「存为常用术语」明确输入正确词；不会自动收集全部编辑，不强制替换正文，不修改已有会话冻结词表。数量/长度沿用50项、每项80字限制。

准确率证据和可复现评分方法见 [DICTATION_QUALITY.md](DICTATION_QUALITY.md)。当前设备/回听/术语和评分工程已完成；约100条人工参考、独立留出及个人设备质量验收未完成，**没有宣称准确率已提高**，原默认模型/分段/语言/供应商不变。

## 三个模型各管一件事

- CPU：`csukuangfj/sherpa-onnx-streaming-paraformer-bilingual-zh-en`，INT8，sherpa-onnx 真正增量识别。与批处理 SeACo Paraformer 是不同模型。
- GPU：注册表中的 Qwen3-ASR 0.6B 固定 revision，每个会话加载一次。只对完整短句识别，不加载 ForcedAligner，不用第一遍文字提示第二遍。
- HTTP 网关：逐句快速处理和手动全文整理默认各用 `gpt-6-luna`，新会话可分别选择 `gemini-3.5-flash-lite`、`gemini-3.8-flash-high` 或 `gpt-6.1-sol`，并分别设定思考强度。默认逐句 `low`、全文 `high`。两者只处理最终识别/当前正文，不重跑 ASR。完整 JSON 校验后才提交，不执行转录中的指令，不自动切供应商。历史会话重试仍使用创建时冻结的模型和强度。

两套提示词分开维护：逐句只轻度修复当前句，全文结合上下文恢复断句、段落和高把握的同音错字；均保留数字、否定、条件、主体、专名与中英混说，不总结或补造事实。疑似听错无法唯一判断时保留原表达并标记复核。

逐句和全文整理默认均为3600秒（1小时）**总期限**；这是允许的最长等待，不会强制等待满1小时。全文输出预算仍为16K–32K（包含推理与正文），逐句为4096输出token。普通正文不超过12000字符时一次整体请求；更长正文由 Cleanup 明确分块，界面注明分块及段数，所有块共享同一个3600秒期限。任一块超时、截断或缺少completed事件时不发布部分建议。超时返回504，网关/格式错误返回502并附中文原因；原稿和TXT不变。相同会话的全文整理 single-flight，主窗/小窗重复点击返回409，原请求实际退出前不会放行；不同会话仍共享全局并发2。

LLM 仍可能出错，`needs_review` 是建议而不是置信度。数字、否定、主体、条件和中英混说应人工核对。固定规则要求保留中英混说，不把英文翻译成中文。跨句改口不会静默重写较早正文。

## 安装与模型预下载

基础 Web 环境继续使用项目 `.venv`。Qwen 复用已有隔离环境（见 [ASR_BACKENDS.md](ASR_BACKENDS.md)）。新增 CPU 环境独立安装，不升级 Web / Whisper 的依赖：

```bash
.venv/bin/python scripts/setup_dictation.py --install --download
```

这一步显式安装 `.venv/dictation-py312`，下载约 226 MB INT8 文件及官方示例音频，并复制项目 faster-whisper 附带的 Silero v6 ONNX 资源到 `data/models/dictation/`。文件大小不代表运行内存。CPU 模型 revision 固定为 `8e40c43232a1c5c66c82111efc5820d3accca11b`。

Qwen 环境默认 `.venv/asr-py312/bin/python`，也可由 `TRANSCRIPT_ASR_PYTHON` 指定；模型缓存沿用当前 profile 的 `cache_dir/asr-models/hf`。首次使用前按既有 ASR 文档准备 Qwen 0.6B 的固定 revision。听写启动使用 `local_files_only=True` / `HF_HUB_OFFLINE=1`，不会边录音边下载。

网关地址与鉴权通过现有网页运行设置或配置指定的环境变量解析；临时测试密钥可只放进启动进程的环境，勿写入源码或测试产物。听写冻结网关地址、两个处理模型与各自的思考强度，不继承阶段 6 的配置或校对系统指令。两个推理 worker 不需要访问远端 LLM。

完成前端构建后，按现有方法启动服务，或本机单进程运行：

```bash
npm --prefix frontend run build
.venv/bin/python -m uvicorn api_server:app --host 127.0.0.1 --port 8000 --workers 1
```

访问 `http://127.0.0.1:8000/dictation`。不应对同一个数据目录同时启动多个 Web 实例或多个 Uvicorn worker；当前设计是单进程管理、一个活动听写/上传会话。

Docker Compose 默认传入 `ASR_BACKENDS=qwen` 和 `DICTATION=1`，分别安装 Qwen 与 CPU worker 的独立环境；直接 `docker build` 仍需显式指定这两个 build-arg。镜像复制源码时设置 `app` 所有者，宿主新文件即使为 `0600/0700` 也能由非 root 服务读取。确认已有任务结束后部署：

```bash
docker compose build trans
docker compose up -d --no-build --wait trans
# 首次准备 CPU 模型；已有 data/models/dictation 时无需重复下载
docker compose exec -T trans /app/.venv/bin/python scripts/setup_dictation.py --download
```

访问 `http://localhost:8080/dictation`。CPU 权重在 `./data` 挂载中，Qwen 权重在原有 `model-cache` 卷；容器重建不删除。`DICTATION=0 docker compose build trans` 可显式省去 CPU 环境，但此镜像不能运行听写，须配合下文只读配置禁用该功能。仅重启旧容器不会补齐依赖。

## 服务端配置

可选 `config/dictation.json` 使用 `src/dictation/schemas.py` 的默认值；只需写要调整的字段，例如：

```json
{
  "enabled": true,
  "idle_seconds": 60,
  "disconnect_seconds": 10,
  "max_upload_mb": 512,
  "max_upload_minutes": 240,
  "cleanup_timeout_seconds": 3600,
  "document_cleanup_timeout_seconds": 3600,
  "decode_timeout_seconds": 900
}
```

Docker 默认不复制宿主的 `config/dictation.json`，没有此文件就使用上述代码默认值。自定义时选用**只读单文件挂载**：在仓库外准备实际存在、容器 UID 可读的 JSON，再在本机 `compose.override.yaml` 中加入：

```yaml
services:
  trans:
    volumes:
      - type: bind
        source: ${DICTATION_CONFIG_FILE:?set an absolute path to an existing JSON file}
        target: /app/config/dictation.json
        read_only: true
        bind:
          create_host_path: false
```

运行 `export DICTATION_CONFIG_FILE=/absolute/path/dictation.json` 后，`docker compose up -d --no-build --wait trans` 才会应用挂载。不要挂整个 `config/` 覆盖镜像默认配置。原生 Linux 部署脚本显式只使用基础 Compose，不读取本机 override；自定义配置应使用这里的手工 Compose 命令部署。配置在会话创建时读取，新会话才使用新参数；既有会话配置保持冻结。

`enabled=false` 阻止新会话，不影响读书会流水线。还可配置 `cpu_python`、`final_python`、`model_dir`、`cleanup_model`、`cleanup_reasoning_effort`、`cleanup_timeout_seconds`、`document_cleanup_model`、`document_reasoning_effort`、`document_cleanup_timeout_seconds`、`decode_timeout_seconds`、`pre_ms`、`silence_ms`、`tail_ms`、`target_seconds`、`hard_seconds`。默认分别为 250ms 前留、700ms 静音、150ms 尾留、20s 软目标、25s 硬上限。硬切分标记需复核，不当作自然句界。

未选择模型和强度时，逐句为 Luna `low`、全文为 Luna `high`（早期 `none` 实测仅保留作历史基线）。旧会话自动逐句处理仍用创建时冻结的期限；手动逐句重试及「整理全文」使用当前服务器配置的期限（默认3600秒），模型和强度继续使用会话快照，不回写旧配置。实际期限记在每次结果的 metrics，网页也显示当前校对期限。

## 状态、数据与唯一正文

实时会话：`created → preparing → listening → pausing → paused → listening`（可循环）；只有确认暂停后才能继续。`listening / pausing / paused → draining → completed`，另有 `interrupted / failed / cancelled`。暂停不触发60秒静音或15秒无音频自动结束，但断线宽限期仍生效。服务重启将 pausing/paused 与其他活动会话一样标为 interrupted，保留已有产物，不自动重新开麦。句子分为临时识别、最终识别、整理完成，以及最终识别失败、整理失败但原稿可用。

保存位置：

```text
data/dictation/<session_id>/
  session.json          配置快照、版本、句子、人工编辑、当前正文
  audio/source.pcm      按样本轴持久接收的 PCM16（无 WAV 头）
  audio/<uid>.wav       最终识别使用的真实样本范围
  upload.<ext>          原始上传文件（上传模式）
  audio/decoded.wav     受限转换后的上传音频（上传模式）
  asr/<uid>.json        原始最终识别
  cleaned/<uid>.json    逐句整理结果
  cleaned/document-*.json  全文建议的独立记录
  utterances.jsonl      有界预览之外的状态事件
  draft.txt             当前正文投影
  metrics.json          启动、延迟、队列和资源信息
  cpu.log / final.log   本会话 worker 诊断
```

`Storage.render_draft` 是正文唯一投影：保存、GET、复制、TXT 下载及全文整理输入一致。整篇建议绑定 `content_revision`；正文改动立即使旧建议失效，不能用最新 session revision 套用旧建议。确认应用后全文稿成为正文，普通保存或后台逐句重试不会撤回它；这时使用「编辑全文」。API 如需返回逐句稿，必须显式 `discard_document=true`，不得无提示撤回全文。

会话状态由 Service 缓存并统一提交；人工修改与后台结果按版本合并，迟到整理保留为建议，不覆盖已保存编辑。前端也以选择代次、会话 ID、revision / event sequence 统一接收快照，旧轮询和旧 socket 不能切回旧会话。

识别失败时，已接收的完整音频仍在 `audio/source.pcm`；在已有 ffmpeg 的运行环境可转成新的 WAV 后再上传，不覆盖原始 PCM：

```bash
ffmpeg -nostdin -n -f s16le -ar 16000 -ac 1 -i /path/to/source.pcm recovered.wav
```

原始音频与文本按项目规则保留，不承诺零留存。浏览器会话凭据使用 HttpOnly、SameSite cookie；清除该 cookie 会丢失网页对旧会话的访问凭据，但不会自动删除服务器文件。本机维护者仍可在数据目录处理旧数据。

## 接口与部署边界

路由均在 `/api/dictation`：

- `POST /sessions`、`GET /sessions`、`GET /sessions/{id}`。创建请求可分别带 `cleanup_model`、`document_cleanup_model`（`gpt-6-luna` / `gpt-6.1-sol` / `gemini-3.8-flash-high` / `gemini-3.5-flash-lite`）和 `cleanup_reasoning_effort`、`document_reasoning_effort`（`low` / `medium` / `high` / `xhigh` / `max`）；省略时保持各自的服务端默认。
- `WS /sessions/{id}/stream`：双向音频与事件。二进制头 `<uint32 sequence, uint64 start_sample>`，后接 16 kHz、单声道、小端 PCM16；正常网络包 80ms，暂停/结束尾包可不足80ms。
  - 暂停：先关闭本地新增采音并 flush 尾包，再在同一 WS 发送 `{type:"pause",next_sequence,samples}`。服务端校验接收边界，CPU 收尾 `boundary=pause`、重置 VAD/预览但不退出，提交 `session.paused`。不提供可能越过音频的独立 HTTP 实时暂停。
  - 继续：主动申请麦克风但先不发布音频，发送 `{type:"resume",next_sequence,samples}`，收到最新 listening 确认后开启新采音 lease。样本/包号/句号继续递增；迟到权限、Worklet、连接或终止后的回调不能恢复采音。
  - `pause_boundaries` 为新增可选字段；旧会话没有该字段也可读取。延迟通过各段采音的样本时钟映射，暂停空档不计入后续语句延迟；`metrics.paused_wall_seconds` 单独记录已结束的暂停时长。
- `POST /uploads?filename=...&terms=[...]&cleanup=true`：原始文件请求体，可附加与创建请求相同的两个模型参数。
- `GET /sessions/{id}/utterances/{uid}/audio`：仅按有权限会话和已存在句子读取WAV，拒绝路径/符号链接，支持音频范围读取。
- `PATCH /sessions/{id}/utterances/{uid}`：`{revision,text,discard_document?}`。
- `PATCH /sessions/{id}/draft`：`{revision,text}`。
- `POST /sessions/{id}/cleanup`：`scope=failed|utterance|document|apply`。
- `POST /sessions/{id}/pause`、`POST /sessions/{id}/resume` 仅用于文件转写（同源/所有权保护）；实时暂停继续仍只走有序WS。
- `POST /sessions/{id}/stop`、`POST /sessions/{id}/cancel`、`DELETE /sessions/{id}`。
- `GET /sessions/{id}/draft.txt`。

变更请求和 WebSocket 校验 Origin；默认同源，可用 `TRANSCRIPT_DICTATION_ORIGINS` 添加精确来源。反向代理需保留 Host，支持 WebSocket upgrade 与长连接；远程浏览器使用 HTTPS/WSS。Vite 开发代理已启用 WebSocket 并保留 Host。

ACK 表示 PCM 与接收位置已持久化。128 包有限恢复窗口内重复包必须字节相同，不能补接有缺口音频。队列超过2秒告警，超过5秒停止新增采音。IPC 写锁与 drain 合用3秒期限；stop 后 CPU 收尾另有10秒期限。卡死会结束本会话并保留音频，不让 GPU 永久占用。Qwen 与旧批量 ASR 共用 `_asr-gpu.lock`，worker 真正退出后才释放。实时会话最终识别退出后释放GPU，再等待远端整理；文件会话按暂停资源契约保留锁至整理收尾结束。HTTP整理请求本身不使用GPU。上传接收另有1800秒总期限，浏览器等待比它多10秒。文件解码使用独立的`decode_timeout_seconds`，默认900秒、可配1–3600秒，不与Luna校对期限共用。听写整理对所有网关（含localhost/127.0.0.1/`.local`）强制使用既有curl `--max-time`：逐句、全文默认及配置上限均为3600秒；全文多块共用该总期限，持续SSE滴流也不能无限延长。缺少curl时明确失败，不回退urllib；旧批处理HTTP策略不变。删除/关闭会先占有生命周期，拒绝新工作，取消并等待已登记上传、推理和有界远端请求退出。校对卡住时，该等待最长可接近1小时；镜像Supervisor/Compose的优雅关闭预算已同步放宽，避免145/160秒提前强杀。容量验证阶段仅改源码与部署模板，后续经用户授权的发布记录见事项2046。

## 优化 A：暂停与跨页面控制（2026-10-23）

应用级控制器分别持有查看中的会话与本窗口创建的活动任务；浏览/删除旧历史不会丢失新上传任务的控制权。页面与面板共用同一控制器；复制面板正文始终取活动任务的已完成正文，不包含临时预览，也不受当前历史选择影响。面板按实际高度给页面底部留白，390px可换行并考虑安全区。

新增免付费真实链路测试 `frontend/tests/dictation-pause.mjs`：公开录音 → Chrome虚拟麦克风 → 实际Worklet/WS/CPU/GPU。已验证70.5秒暂停无新样本、20次暂停/继续、三路由不重建采音、权限拒绝/迟到、暂停中短重连及超期中断；直接比较发送PCM、source.pcm和句子WAV样本范围，不以识别几个词判完整性。证据 `tmp/dictation-optimization/a-real-03/`（最终截图已检查，02轮的样本完整性证据也保留）。这是实际公开录音链路测试，个人物理设备/口音仍需人工试录；不据此宣称准确率提升。

上传等待期间查看历史、删除上一条已完成实时任务的竞争与显式停止目标，见 `dictation-ownership.mjs`；长正文、编辑、全文建议、390px/540px布局回归见 `dictation-layout.mjs`。两类为合成状态测试，不能替代上述真实音频证据。服务端全量测试787通过，新增暂停协议16项；模型/供应商/默认分段参数均未改变，运行容器未更新。

## 优化 B：录音草稿和文件暂停（2026-10-23）

`tmp/dictation-optimization/b-real-03/` 已实际录制三分钟、暂停三次、试听/下载，保存2,894,335样本（约180.90秒）并证明录音阶段零服务端会话/worker创建；上传后解码PCM与下载WAV数据逐字节一致。实际Qwen处理36个唯一分句，失败0；文件暂停后无新音频输入或ASR产物，继续完成；GPU忙及文件完成时第二条本地录音仍持续。刷新恢复也通过。该脚本随后在格式导入环节遇到Chrome inspector响应体被逐出，**03轮整体标为failed**，不掩盖失败；前述独立检查与产物仍有效。

修正测试驱动为读取已拥有的历史API、等待文件输入真正可用后，`b-formats-05/report.json` 已分别真实导入 WAV/MP3/M4A并完成实际ASR，无整理调用。`b-drafts-03` 覆盖限长/WAV、同一渲染轮重复提交、上传失败、配额回滚、5秒写入积压、刷新恢复；`b-ownership-02` 覆盖旧实时轮询/迟到完成不释放local lease，以及两项上传所有权竞争。上述合成上传/状态用例不冒充真实转写。

服务端文件暂停测试覆盖解码在途、EOF、满队列、取消/结束、全局整理槽公平性、实际loopback HTTP滴流退出后才确认暂停。付费供应商整理未重新调用；HTTP契约和有界退出不等于整理语义质量验收。当前默认模型、分段及供应商保持。

## 本机实测与尚未完成的验收

2026-10-21，以下为早期逐句 Luna none 的历史基线：i5-12400 / RTX 3060 Ti，真实录音按1倍速度回放600秒。接收9,600,000个样本、7,500包；84句全部生成独立 ASR 与整理产物，区间有序无重叠，最大音频积压16ms，未持续增长。以下为**服务端样本轴到结果提交**的计时，未测物理麦克风到浏览器绘制的完整延迟：

| 指标 | 样本数 | P50 | P95 |
|---|---:|---:|---:|
| VAD 前留起点到首个增量文字 | 81 | 0.687s | 0.726s |
| 最后语音样本到最终 ASR | 84 | 0.967s | 1.431s |
| 最后语音样本到 Luna 整理稿 | 84 | 3.998s | **5.828s** |

另3句很短，靠句尾 flush 后出字，没有纳入首个增量文字统计，但最终识别均保留。Luna 的 P95 超过建议的5秒目标。

CPU worker 峰值 RSS 约390 MiB；4线程预览累计约2334 CPU秒/600墙钟秒，不承诺低 CPU 占用。Qwen 的 PyTorch 峰值 allocated 约1.75 GiB，不是全部显存用量；实测 `aligner_loaded=false`。84句中40句被整理模型建议复核，该标记不能代替人工判定。没有人工参考稿，未计算 CER。

同源六条关键文本的 HTTP 对照：Luna `none` P95约5.79秒，Flash-Lite `minimal` 约2.73秒；六例的数字、否定和条件均保留，Flash-Lite 有两例缺末尾句号。它已能通过当前网关的结构化输出请求，但样本不足以建立完整保真结论，该轮保持 Luna none；后续按用户要求改为逐句low/全文high，不能沿用这组none计时代表当前延迟。Gemini 3.8 通道仅公布 `-high` 别名，请求 `low` 能返回，原生 LOW 是否被转发仍未证实。三种模型的实际回报 ID 与 token usage 已通过既有 transport observer 真实读取；不保存原始 HTTP 响应或密钥。

已用 Chrome 虚拟麦克风加载真实录音，实际跑通 AudioWorklet、短断线补传、CPU/GPU/LLM、停止释放、编辑/复制、全文建议应用、紧凑小窗和上传/历史/删除；检查了390px截图与动态展开边界。它验证网页采音路径，不替代物理设备和本人口音验收。

2026-10-22 更新为逐句low / 全文high后，复用此前失败的1866字符正文快照（不改原会话）做单次整体请求，43.35秒成功完成，needs_review=true；疑似听错的词仍需人工复核。另两条语义样例的high用时3.83/4.30秒，low用时2.95/3.15秒，数字、否定、条件、英文和引用指令均保留。仅为小样例，不代表延迟分位数或转录准确率。证据 `tmp/dictation-{high,low}-real-02/results.json`。

更新后的单容器可信HTTPS浏览器实测7项通过（含真实录音虚拟麦克风、WSS、low逐句整理、high全文预览/应用和上传）。60句加超长临时预览的布局测试通过，正文/差异均内部滚动，翻看旧句不被自动滚动抢走，390px手机与540px小窗的下方操作按钮保持可用。证据 `tmp/dictation-high-docker-browser-01/report.json`、`tmp/dictation-layout-high-04/`。

本地历史证据：`tmp/dictation-ten-minute-real-02/{summary.json,artifact-check.json,001.json}`、`tmp/dictation-browser-05/`、`tmp/dictation-llm-compare-02/results.json`、`tmp/dictation-llm-usage-real-01/results.json`。测试录音及证据不纳入 Git。

## 可重放验证

测试与构建：

```bash
.venv/bin/python -m pytest
npm --prefix frontend run build
node --experimental-strip-types frontend/tests/dictation-state.mjs
```

音频清单是 JSON 数组，例如 `[{"path":"path/to/16k-mono-pcm16.wav","reference":"人工参考，可省略"}]`。输出目录必须是新目录：

```bash
.venv/bin/python scripts/probe_dictation.py --manifest samples.json \
  --candidate dual --realtime --output tmp/dictation-replay
```

可用 `--project-root <已准备的隔离测试root>` 指定独立数据目录，root会解析为绝对路径；沿用离线模型与原GPU锁，不执行会话恢复。离线评分用 `scripts/score_dictation.py`，清单、规则和当前缺口见 DICTATION_QUALITY.md。

`--candidate final` 走上传式 Qwen 路线，不运行 Paraformer。`--cleanup` 显式启用远端整理、会产生网关调用。`--realtime` 才按1倍样本时钟回放；快速文件处理不能作为实时延迟。长静音会触发产品的自动停止，报告应看实际收到的秒数，不能当十分钟连续讲话验收。运行时仍遵守 GPU 锁，不抢占既有 ASR。

LLM 同源文本对照：

```bash
.venv/bin/python scripts/probe_dictation_cleanup.py --manifest texts.json \
  --output tmp/dictation-cleanup-comparison
```

清单为 `[{"text":"预算一千五，不是一万五","expected":"预算一千五，不是一万五。"}]`。脚本记录请求模型/通道、完整耗时、结构或通道错误，不自动切默认。通过既有客户端的 transport observer 记录网关回报的模型 ID、input/output tokens；供应商未给出的字段保留 null，不估算。早期对照文件的这些字段为 null；不按展示名假定原生思考档已生效。

真实浏览器集成测试：

```bash
# 免付费的暂停/跨页真实音频验收：独立数据 + 现有GPU锁 + 离线模型
node frontend/tests/dictation-pause.mjs
# 合成状态/布局回归，不调用模型
node frontend/tests/dictation-ownership.mjs
node frontend/tests/dictation-layout.mjs
node frontend/tests/dictation-drafts.mjs
# 多窗口草稿独占/原窗口关闭恢复、面板导航；真实浏览器存储/采音，HTTP为合成状态
node frontend/tests/dictation-boundaries.mjs
# 4h数据容量边界（不是连续采音4h），较耗磁盘；按需执行
node frontend/tests/dictation-capacity.mjs
# 三分钟录音/试听/下载/实际文件ASR；约需4分钟，无远端整理
node frontend/tests/dictation-recording.mjs
# 仅补验导入格式，可复用已有三分钟产物证据
DICTATION_RECORDING_FORMATS_ONLY=1 node frontend/tests/dictation-recording.mjs
# 设备/术语/原句回听与回放→采音互斥，实际CPU/GPU但不调用整理
node frontend/tests/dictation-proofreading.mjs
# 完整整理链路：取得实际远端调用费用授权后才启用
DICTATION_TEST_ALLOW_CLEANUP=1 node frontend/tests/dictation.mjs
```

测试输出目录必须新建；可通过 `DICTATION_TEST_OUTPUT` 指定。默认通过 `scripts/serve_dictation_test.py` 在独立数据目录启动当前代码，复用离线模型和原GPU文件锁，不恢复正在运行服务的数据。完整整理测试使用本机8021端口；暂停测试使用8023端口。也可设置 `DICTATION_TEST_BASE_URL=http://127.0.0.1:8080` 直接验证已部署容器，脚本此时不启动/关闭服务。HTTPS 测试还需让 Node 通过 `NODE_EXTRA_CA_CERTS`、Chromium 通过受信任证书库接受同一 CA，不能用忽略证书错误代替。

它使用本地真实录音作 Chrome 虚拟麦克风，实际调用 CPU/GPU/网关，检查停止释放、编辑、复制、全文预览/应用、紧凑布局和上传；使用独立浏览器凭据，只编辑/删除自己新建的会话。不是物理麦克风验收，也不应与占用同一 GPU 的测试同时运行。

约100条人工对照、个人口音/设备、噪声和独立留出样本仍需实际材料。六个提示词例子或自动化通过不能代表全部整理质量；P95 也必须附带样本数和通道条件。
