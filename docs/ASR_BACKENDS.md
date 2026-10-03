# ASR 候选、环境与验证边界

本页只说明 ASR 转录接入，不改变流水线其他阶段。执行记录见
[`codestable/issues/005-o-asr-multi-backend-integration.md`](../codestable/issues/005-o-asr-multi-backend-integration.md)。

## 候选与默认行为

| 候选 ID | 实际模型 | 时间戳 | 已验证运行精度 |
|---|---|---|---|
| `whisper-existing` | 当前 profile 的 faster-whisper 模型 | 原生 segment | 沿用 profile |
| `qwen3-asr-1.7b` | Qwen3-ASR-1.7B + ForcedAligner-0.6B | 字/词对齐边界聚合文本段 | CUDA FP16 / CPU FP32 |
| `qwen3-asr-0.6b` | Qwen3-ASR-0.6B + ForcedAligner-0.6B | 同上 | CUDA FP16 / CPU FP32 |
| `paraformer-zh` | 官方 SeACo Paraformer `v2.0.4` + ct-punc | 原生毫秒字/词边界转秒、聚合文本段 | FP32；标点模型 CPU |
| `fun-asr-nano` | Fun-ASR-Nano-2512 | Silero VAD 音频区间，最长 30 秒 PCM 子块 | CUDA encoder FP32/decoder BF16；CPU FP32 |

当前 Web 选择器只展示 Whisper 和两种 Qwen；Paraformer、Fun-ASR-Nano 暂时隐藏，未删除实现，CLI 显式候选和已有任务快照仍可使用。已保存的隐藏候选不再作为 Web 新任务默认选择（回到当前可见配置默认，若配置也隐藏则展示 Whisper），不会改写磁盘设置或历史任务。

Nano **不是字级或句级时间戳**，不能把整个音频区间误读为精确文字边界。普通 HF Paraformer checkpoint 在穿刺中不返回时间戳，所以候选明确绑定官方 SeACo checkpoint，而非静默使用无时间戳模型。

所有新模型和辅助模型在代码中固定 revision；Paraformer 同时校验权重 SHA256。
SDK 不运行远程 Python：Nano 显式注册所固定 FunASR wheel 中的实现，`trust_remote_code=False`。
Web 不接受用户自选仓库、revision 或任意 worker Python。

- 未配置 `asr.candidate` 时继续使用 Whisper。没有自动多模型识别、回退或投票。
- `backend`/`model` 仍属于现有 LLM，不能用来选择 ASR。
- 新任务：显式候选 > Web 保存默认（仅 Web 请求）> YAML 配置 > Whisper 兼容行为。
- 历史任务：显式同候选重试 > 任务 YAML 快照。后来的 Web 默认/profile 不重选旧任务模型。转录阶段另存有效 ASR 配置快照，阶段/文件阶段重试沿用保存的 config/profile/candidate。
- 不同候选必须新建任务或使用独立阶段文件工作区；同一工作区切换候选会报错。
- profile 仍决定 device；Whisper model/compute type/beam 不传给新后端。
- 新候选首版只验收 `language=zh`，其他语言配置会明确拒绝。
- Qwen 生成时仅对最后一个位置计算词表 logits，避免固定 SDK 对长音频/术语上下文的全部输入位置分配无用的大张量。优化只作用于 ASR generation，ForcedAligner 和其他完整 forward 不变；不减少术语、不改变分块/精度、不自动换模型或 CPU。8 GB GPU 仍需为桌面和其他进程预留显存，不能据一次短片段通过保证所有输入可用。真实 OOM 对照记录见 [`2027-o-ff-qwen-generation-vram.md`](../codestable/issues/2027-o-ff-qwen-generation-vram.md)。

## 安装：基础环境不变，可选 worker 隔离

不要把下面的 SDK 直接安装进已经工作的 Web/Whisper `.venv`：Qwen 要求的 Transformers/huggingface-hub 与原环境版本不同，PyTorch 2.8 的 CUDA wheels 也不同。

本次验证环境为 Linux x86_64 / Python 3.12.13，四个候选共用一个**按阶段启动、结束释放**的 worker 环境；它不是常驻服务。项目 Python 命令仍从项目 `.venv` 发起。

```bash
uv python install 3.12
uv venv --python 3.12 .venv/asr-py312
# 两组可分别安装；以下命令安装全部新增候选。
uv pip install --python .venv/asr-py312/bin/python \
  -r requirements-asr-qwen.txt -r requirements-asr-funasr.txt
uv pip check --python .venv/asr-py312/bin/python
.venv/bin/python -m pytest
```

`requirements-asr-constraints.txt` 是已实测 worker 环境的传递依赖版本快照，不用于基础 Web 环境；不宣称支持所有 OS、架构或 Python 版本。普通 pytest 不下载模型或运行 GPU。

配置例子：

```yaml
asr:
  engine: faster-whisper             # 保留旧字段；新候选内部解析实际引擎
  candidate: qwen3-asr-0.6b         # 省略即可保留 Whisper 默认
  worker_python: .venv/asr-py312/bin/python
  backend_cache_subdir: asr-models
  language: zh
  terms: [卷土重来, 书名]
  max_new_tokens: 2048
  chunk_seconds: 30.0               # 新后端 VAD 区间的实际 PCM 子块上限
```

`TRANSCRIPT_ASR_PYTHON` 可在进程环境覆盖 worker Python 路径。路径保留 venv 的 executable symlink，不能解析成系统 Python。
模型缓存位于 profile 的 `cache_dir/asr-models/{hf,ms}`；首次运行需联网下载固定权重。代理请使用可用的 HTTP(S) 配置；若 `ALL_PROXY` 与小写 `all_proxy` 指向 SOCKS 而缺少 SOCKS 客户端，下载可能失败，不能冒充模型不可用或自动回退。

## CLI / Web

```bash
.venv/bin/python scripts/02_transcribe.py \
  --config path/to/independent-settings.yaml \
  --profile wsl2_gpu_high_accuracy \
  --asr-candidate qwen3-asr-0.6b
```

pipeline、主流水线、单任务和批量任务 CLI 同样支持 `--asr-candidate`。
API 请求字段为 `asr_candidate`。Web 设置页保存默认候选；单任务、批量和转录阶段页复用一个选择器，不提供引擎/模型的重复选择。

`GET /api/config` 的候选列表提供 `dependency_status`。这只说明必要包的存在性检查：
**不是 CUDA、缓存完整性或真实推理验收**。`cache_status`、`runtime_validation` 当前明确为 `not_checked`。
任务状态显示已保存候选；有实际产物时优先显示产物里的实际 engine/model。

## 输出与风险护栏

- JSON 保留 `source_file`、`engine`、`model_size`、`device`、`compute_type`、`language`、`segments`、`full_text`；新增可选 `metadata`。下游仍读取同目录 JSON/TXT。
- 时间戳统一为秒，来自实际 SDK 对齐或真实 PCM/VAD 区间，不按文字长度均分。
- 新候选非空文字但无对齐、文字和对齐 lexical content 不一致、解码触顶无结束 token、检测到语音但返回空文，均明确失败。
- 新候选先做 CPU Silero VAD。无检测到语音时发布带 `inference_performed=false` 的空结果，不加载识别模型；VAD 本身可能漏检弱语音，人工仍需复核。
- 新候选只处理记录的 VAD 区间并按最长 30 秒切块，保留原音频绝对偏移。长连续语音可能被硬切，metadata 提醒复核漏字/重复；没有自动补字或合并猜测。
- Qwen 对每个子块使用固定 SDK 的识别和 ForcedAligner，记录实际区间/推理 padding。SDK 自身对齐分块目标为 180 秒，但接入层使用更小的上限，避免已实测 0.6B 在较长块上触顶。个别零时长字/词只能与相邻实际边界聚合并告警；全部对齐零时长仍失败。Qwen 分块若人为切出不足 1 秒的末尾块，会在同一 VAD 区间内平衡最后两块（仍不超过配置上限、无重叠/漏采样），避免几十或几百毫秒残片缺少对齐和识别上下文。天然的极短 VAD 区间不丢弃、不虚构 padding 或跨静音合并，仍须真实校验。
- Nano 显式使用 greedy decoding（`do_sample=false`、`repetition_penalty=1.1`），不沿用 checkpoint 的随机采样默认。先前随机采样/仅 greedy 的真实失败日志均保留；有界解码仍可能失败，不自动换模型或发布截断文字。
- 工程推理 batch=1、分块有界；目前解码/VAD仍将单个音频的 PCM 读入内存，不是流式处理，超长文件仍需足够主存。
- JSON/TXT 先校验再写临时文件，逐个 rename 发布；第二次发布的 OSError 会回滚旧产物。**不是两个文件跨进程崩溃的事务**，掉电或 SIGKILL 发生在两个 rename 之间仍可能需要人工复核。
- 工作区锁和项目 `data/jobs/_asr-gpu.lock` 使用 Linux 文件锁。worker 继承锁 fd，父进程被杀时仍保有 GPU 互斥；不因取消而让第二个模型同时挤入 GPU。不同项目根目录/主机不共享此锁。
- Whisper CUDA runtime 预加载仍只在 Whisper 路径，worker 不继承其 wheel library 路径。
- Whisper 保留原模型参数和时间戳，允许旧行为的尾段小幅越界（最多 2 秒），超过音频 0.1 秒时记录告警；新候选容差 0.1 秒。不可将尾部幻觉称作正确正文。

术语：Whisper 继续使用原 `initial_prompt`；Qwen 映射 `context`，SeACo 映射 `hotword`，Nano 映射 `hotwords`。新任务将书名/词表保存在结构化 `asr.terms`，不把 Whisper prompt 或整本参考书送给新模型。超过 4096 字符的术语配置明确拒绝，不静默截断。

## Docker 可选镜像

直接运行 `docker build` 默认仍只安装基础 Whisper；`docker compose build trans`、`scripts/deploy_docker_wsl2.sh` 和原生 Ubuntu 入口 `scripts/deploy_docker_linux.sh` 默认安装 Whisper + Qwen（`ASR_BACKENDS=qwen`），两种部署入口共用同一 Dockerfile/Compose，不改变默认转录模型。Ubuntu 支持范围、sudo 配置传递和隔离 GPU 验收见 [DOCKER_LINUX.md](DOCKER_LINUX.md)；沿用历史 `wsl2_gpu_high_accuracy` profile，不新增 Linux 专属 profile。Compose 可通过环境变量选择，例如 `ASR_BACKENDS=whisper docker compose build trans`；部署脚本同样支持此环境变量，包含 sudo 路径。已有容器须在任务结束后重新构建并用 `docker compose up -d --no-build trans` 更新，仅重启不会补装依赖。

直接构建可选环境：

```bash
docker build --build-arg ASR_BACKENDS=all -t transcript-pipeline:asr .
# ASR_BACKENDS: whisper（直接 docker build 默认）、qwen（Compose 默认）、funasr、all
```

新 SDK 安装到 `/app/.venv/asr-py312`，基础应用依赖不与 PyTorch SDK 混装。基础 decoder 单独固定 `av==16.1.0`，避免 PyAV 19 移除 `metadata_errors` API 导致 faster-whisper 1.2.1 无法打开音频。可选环境固定 pip 26.2.1 以支持大包断线续传和哈希校验，为 FunASR 的旧 sdist 预装 setuptools/wheel，再关闭临时 build isolation；构建仍执行 pip check。不绕过包哈希验证。
运行时需要 GPU device requests、可写独立 data/cache 挂载、明确的任务/profile 配置。
构建成功、`nvidia-smi`、包导入和实际模型推理是不同验收层级。本次可选 `all` 镜像已经通过五候选的真实 CUDA 短语音/静音转录；尚未在容器中重复 10 分钟样例或部署到现有服务。具体版本、日志与输出以 issue 的最新记录为准，不以本段命令作为通过证明。

## 接入不等于识别质量结论

先验证真实输出、时间戳、静音/短片段/跨分块和下游读取，再做同源 10–20 分钟代表性录音的人工对比。没有人工稿不能给出 CER，也不能据耗时或文本字数宣布某个候选更好。Nano 粗时间戳、分块边界词语和 ForcedAligner 零时长项需要特别复核。现有默认不自动切换。
