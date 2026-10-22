# 语音输入：听写、上传、编辑和复制

从网页「工具 → 语音输入」进入。桌面 Chrome / Edge 可在普通页面或紧凑小窗中使用；内网 IP 访问麦克风也需要 HTTPS，本机 `localhost` / `127.0.0.1` 可以使用 HTTP。内网部署与一次性证书信任见 [HTTPS.md](HTTPS.md)；本机已启用 `https://192.168.0.252:8443/dictation`，原8080 HTTP保留。

## 开始使用

1. 点击「开始听写」，允许麦克风权限。等状态显示「正在听音」再讲话。
2. 下方临时区显示当前句预览；停顿后正文先显示 Qwen 最终识别，再提交整理结果。
3. 点击「停止」立即释放麦克风。页面继续完成已收到的尾部；整理失败时原始识别仍可编辑、复制和下载 TXT。
4. 「设置」提供少量术语和整理开关。术语只作为最终识别与整理参考，不强制替换，也不继承读书会词表或书名。
5. 「上传录音」跳过实时预览，使用同一套分段、最终识别和整理。默认最多 100 MB、30 分钟；超限或损坏文件明确失败。
6. 「历史结果」只列本浏览器拥有的会话。删除需确认，先停止接收、推理和整理，再删除音频与文字。

「紧凑小窗」打开同一个会话的结果视图，使用相同浏览器凭据。原窗口继续拥有麦克风；小窗可以查看、编辑、复制或停止。不要在录音时关闭原录音窗口；断开超过宽限期后服务端会结束会话。没有会话时也可先开小窗，再直接在小窗开始听写。

复制是点击当时的**已完成正文快照**，不包含临时预览。整理尚未完成时，复制使用已经完成的原始识别。

## 三个模型各管一件事

- CPU：`csukuangfj/sherpa-onnx-streaming-paraformer-bilingual-zh-en`，INT8，sherpa-onnx 真正增量识别。与批处理 SeACo Paraformer 是不同模型。
- GPU：注册表中的 Qwen3-ASR 0.6B 固定 revision，每个会话加载一次。只对完整短句识别，不加载 ForcedAligner，不用第一遍文字提示第二遍。
- HTTP 网关：`gpt-6-luna`，显式 `reasoning.effort=none`，只整理最终 ASR。整句 JSON 接收完整并校验后才提交；不执行转录中的指令，不自动切供应商。

LLM 仍可能出错，`needs_review` 是建议而不是置信度。数字、否定、主体、条件和中英混说应人工核对。固定规则要求保留中英混说，不把英文翻译成中文。跨句改口不会静默重写较早正文。

## 安装与模型预下载

基础 Web 环境继续使用项目 `.venv`。Qwen 复用已有隔离环境（见 [ASR_BACKENDS.md](ASR_BACKENDS.md)）。新增 CPU 环境独立安装，不升级 Web / Whisper 的依赖：

```bash
.venv/bin/python scripts/setup_dictation.py --install --download
```

这一步显式安装 `.venv/dictation-py312`，下载约 226 MB INT8 文件及官方示例音频，并复制项目 faster-whisper 附带的 Silero v6 ONNX 资源到 `data/models/dictation/`。文件大小不代表运行内存。CPU 模型 revision 固定为 `8e40c43232a1c5c66c82111efc5820d3accca11b`。

Qwen 环境默认 `.venv/asr-py312/bin/python`，也可由 `TRANSCRIPT_ASR_PYTHON` 指定；模型缓存沿用当前 profile 的 `cache_dir/asr-models/hf`。首次使用前按既有 ASR 文档准备 Qwen 0.6B 的固定 revision。听写启动使用 `local_files_only=True` / `HF_HUB_OFFLINE=1`，不会边录音边下载。

网关地址与鉴权通过现有网页运行设置或配置指定的环境变量解析；临时测试密钥可只放进启动进程的环境，勿写入源码或测试产物。听写冻结网关地址和模型配置，不继承阶段 6 的 `high/xhigh` 或校对系统指令。两个推理 worker 不需要访问远端 LLM。

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
  "max_upload_mb": 100,
  "max_upload_minutes": 30
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

`enabled=false` 阻止新会话，不影响读书会流水线。还可配置 `cpu_python`、`final_python`、`model_dir`、`cleanup_model`、`cleanup_timeout_seconds`、`pre_ms`、`silence_ms`、`tail_ms`、`target_seconds`、`hard_seconds`。默认分别为 250ms 前留、700ms 静音、150ms 尾留、20s 软目标、25s 硬上限。硬切分标记需复核，不当作自然句界。

首版受测的生产整理配置是 Luna `none`；Gemini 对照脚本不自动改运行默认值。不要把其他模型直接填进 `cleanup_model` 并假定它支持 `none`。

## 状态、数据与唯一正文

会话：`created → preparing → listening → draining → completed`，另有 `interrupted / failed / cancelled`。句子分为临时识别、最终识别、整理完成，以及最终识别失败、整理失败但原稿可用。

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

- `POST /sessions`、`GET /sessions`、`GET /sessions/{id}`。
- `WS /sessions/{id}/stream`：双向音频与事件。二进制头 `<uint32 sequence, uint64 start_sample>`，后接 16 kHz、单声道、小端 PCM16；正常网络包 80ms。
- `POST /uploads?filename=...&terms=[...]&cleanup=true`：原始文件请求体。
- `PATCH /sessions/{id}/utterances/{uid}`：`{revision,text,discard_document?}`。
- `PATCH /sessions/{id}/draft`：`{revision,text}`。
- `POST /sessions/{id}/cleanup`：`scope=failed|utterance|document|apply`。
- `POST /sessions/{id}/stop`、`POST /sessions/{id}/cancel`、`DELETE /sessions/{id}`。
- `GET /sessions/{id}/draft.txt`。

变更请求和 WebSocket 校验 Origin；默认同源，可用 `TRANSCRIPT_DICTATION_ORIGINS` 添加精确来源。反向代理需保留 Host，支持 WebSocket upgrade 与长连接；远程浏览器使用 HTTPS/WSS。Vite 开发代理已启用 WebSocket 并保留 Host。

ACK 表示 PCM 与接收位置已持久化。128 包有限恢复窗口内重复包必须字节相同，不能补接有缺口音频。队列超过2秒告警，超过5秒停止新增采音。IPC 写锁与 drain 合用3秒期限；stop 后 CPU 收尾另有10秒期限。卡死会结束本会话并保留音频，不让 GPU 永久占用。Qwen 与旧批量 ASR 共用 `_asr-gpu.lock`，worker 真正退出后才释放；远端整理不持 GPU。上传接收另有300秒总期限。删除/关闭会先占有生命周期，拒绝新工作，取消并等待已登记上传、推理和有界远端请求退出。

## 本机实测与尚未完成的验收

2026-10-21，i5-12400 / RTX 3060 Ti，真实录音按1倍速度回放600秒。接收9,600,000个样本、7,500包；84句全部生成独立 ASR 与整理产物，区间有序无重叠，最大音频积压16ms，未持续增长。以下为**服务端样本轴到结果提交**的计时，未测物理麦克风到浏览器绘制的完整延迟：

| 指标 | 样本数 | P50 | P95 |
|---|---:|---:|---:|
| VAD 前留起点到首个增量文字 | 81 | 0.687s | 0.726s |
| 最后语音样本到最终 ASR | 84 | 0.967s | 1.431s |
| 最后语音样本到 Luna 整理稿 | 84 | 3.998s | **5.828s** |

另3句很短，靠句尾 flush 后出字，没有纳入首个增量文字统计，但最终识别均保留。Luna 的 P95 超过建议的5秒目标。

CPU worker 峰值 RSS 约390 MiB；4线程预览累计约2334 CPU秒/600墙钟秒，不承诺低 CPU 占用。Qwen 的 PyTorch 峰值 allocated 约1.75 GiB，不是全部显存用量；实测 `aligner_loaded=false`。84句中40句被整理模型建议复核，该标记不能代替人工判定。没有人工参考稿，未计算 CER。

同源六条关键文本的 HTTP 对照：Luna `none` P95约5.79秒，Flash-Lite `minimal` 约2.73秒；六例的数字、否定和条件均保留，Flash-Lite 有两例缺末尾句号。它已能通过当前网关的结构化输出请求，但样本不足以建立完整保真结论，本轮保持用户指定的 Luna 默认。Gemini 3.8 通道仅公布 `-high` 别名，请求 `low` 能返回，原生 LOW 是否被转发仍未证实。三种模型的实际回报 ID 与 token usage 已通过既有 transport observer 真实读取；不保存原始 HTTP 响应或密钥。

已用 Chrome 虚拟麦克风加载真实录音，实际跑通 AudioWorklet、短断线补传、CPU/GPU/LLM、停止释放、编辑/复制、全文建议应用、紧凑小窗和上传/历史/删除；检查了390px截图与动态展开边界。它验证网页采音路径，不替代物理设备和本人口音验收。

本地证据：`tmp/dictation-ten-minute-real-02/{summary.json,artifact-check.json,001.json}`、`tmp/dictation-browser-05/`、`tmp/dictation-llm-compare-02/results.json`、`tmp/dictation-llm-usage-real-01/results.json`。测试录音及证据不纳入 Git。

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

`--candidate final` 走上传式 Qwen 路线，不运行 Paraformer。`--cleanup` 显式启用远端整理、会产生网关调用。`--realtime` 才按1倍样本时钟回放；快速文件处理不能作为实时延迟。长静音会触发产品的自动停止，报告应看实际收到的秒数，不能当十分钟连续讲话验收。运行时仍遵守 GPU 锁，不抢占既有 ASR。

LLM 同源文本对照：

```bash
.venv/bin/python scripts/probe_dictation_cleanup.py --manifest texts.json \
  --output tmp/dictation-cleanup-comparison
```

清单为 `[{"text":"预算一千五，不是一万五","expected":"预算一千五，不是一万五。"}]`。脚本记录请求模型/通道、完整耗时、结构或通道错误，不自动切默认。通过既有客户端的 transport observer 记录网关回报的模型 ID、input/output tokens；供应商未给出的字段保留 null，不估算。早期对照文件的这些字段为 null；不按展示名假定原生思考档已生效。

真实浏览器集成测试：

```bash
node frontend/tests/dictation.mjs
```

默认在独立本机 8021 端口启动当前代码。也可设置 `DICTATION_TEST_BASE_URL=http://127.0.0.1:8080` 直接验证已部署容器，脚本此时不启动/关闭服务。HTTPS 测试还需让 Node 通过 `NODE_EXTRA_CA_CERTS`、Chromium 通过受信任证书库接受同一 CA，不能用忽略证书错误代替。

它使用本地真实录音作 Chrome 虚拟麦克风，实际调用 CPU/GPU/网关，检查停止释放、编辑、复制、全文预览/应用、紧凑布局和上传；使用独立浏览器凭据，只编辑/删除自己新建的会话。不是物理麦克风验收，也不应与占用同一 GPU 的测试同时运行。

约100条人工对照、个人口音/设备、噪声和独立留出样本仍需实际材料。六个提示词例子或自动化通过不能代表全部整理质量；P95 也必须附带样本数和通道条件。
