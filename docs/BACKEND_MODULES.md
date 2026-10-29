# 独立后端能力怎么用

只转换文件、转写音频、识别一本 PDF 或整理一段文字时，直接调用下面的能力，不必建立 job、启动 Web 或加载完整 YAML。需要沿用读书会的目录、配置、结果发布和重跑规则时，使用 `src.pipeline.run_stage` 或原 CLI。

- `src.media`：音视频转换、限长 PCM 解码。
- `src.asr`：文件／长音频、流式预览、最终短句识别；`src.asr.results` 单独保存结果类型、校验与发布逻辑。
- `src.pdf`：单 PDF 逐页识别、检查点恢复、页序合并与完整 TXT 发布。
- `src.llm.gateway`：连接与 HTTP/SSE；`src.llm.tasks`：读书会／对谈校对、听写逐句／全文整理。

原 `ffmpeg_utils`、`asr_utils`、`reference_utils`、`pdf_book_ocr`、`refine_utils`、`dictation.cleanup` 保留配置和历史导入适配，并真实使用这些能力。阶段核心在 `src/pipeline/stages.py`；`scripts/run_pipeline.py` 保留原 CLI 参数、退出码、导入入口和显式操作替换接缝。

## 准备运行环境

从仓库根目录执行 Python 示例，使用 `.venv/bin/python`。开发验证依赖在 `requirements-dev.txt`；实际识别仍需要对应的已安装后端和模型资源，见 [ASR_BACKENDS.md](ASR_BACKENDS.md) 与 [DICTATION.md](DICTATION.md)。媒体转换需要 ffmpeg，PDF 渲染需要 `pdftoppm`（Poppler）。

新开发环境可按以下步骤复现验证依赖；已有 `.venv` 时跳过创建：

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
```

Linux 可用系统包 `ffmpeg`、`poppler-utils`。本次验证使用 `static-ffmpeg==3.0` 的固定 Linux 资产，并把 ffmpeg / ffprobe 链接到 `.venv/bin`：

```bash
.venv/bin/python - <<'PYTHON'
from pathlib import Path
import static_ffmpeg.run as run

# 同一仓库的 canonical Git LFS media URL；保持 TLS 校验。
run.PLATFORM_ZIP_FILES["linux"] = "https://media.githubusercontent.com/media/zackees/ffmpeg_bins/main/v8.0/linux.zip"
executables = run.get_or_fetch_platform_executables_else_raise()
for name, executable in zip(("ffmpeg", "ffprobe"), executables):
    destination = Path(".venv/bin") / name
    if not destination.exists():
        destination.symlink_to(executable)
PYTHON
export PATH="$PWD/.venv/bin:$PATH"
```

这一步只准备媒体工具，不下载 ASR 模型。`pdftoppm` 另由系统 Poppler 提供。

## 组合一：文件转换后直接转写

把下面代码保存为仓库根目录的脚本，用 `.venv/bin/python 脚本名.py` 运行；替换输入和缓存路径。转换目标的后缀决定音频格式，已有输出默认跳过。

```python
from pathlib import Path
from src.media import convert_audio
from src.asr.file import transcribe_file
from src.asr.options import AsrResources, FileAsrOptions

wav = Path("output/example/audio.wav")
convert_audio(Path("recording.mp4"), wav, sample_rate=16000, channels=1)
item = transcribe_file(
    wav, Path("output/example/asr"),
    options=FileAsrOptions(
        model_size="large-v3-turbo", device="cpu", compute_type="int8",
        candidate="whisper-existing",
    ),
    resources=AsrResources(cache_dir=Path("/path/to/model-cache")),
)
print(item.output_paths.json_path, item.output_paths.txt_path)
```

批量输入调用 `transcribe_files([wav1, wav2], output_dir, options=..., resources=...)`，同一候选复用模型／worker。显式选择 `secondary_candidate` 时仍串行处理两个候选，保留独立产物与完成配对门禁。

CUDA 文件识别须在 `AsrResources` 中提供与听写相同的 `gpu_lock_path=Path("data/jobs/_asr-gpu.lock").resolve()`；它会阻塞等待锁，并把锁 fd 传入子进程。切换候选时使用新输出目录，不覆盖其他候选的旧结果。

## 一次创建网关连接，再传给 PDF 或文字任务

在调用边界从获准的配置来源读取连接；下面示例使用环境变量。连接冻结 URL、认证、路由和代理，不在任务执行期间修改进程环境。不要把连接中的 API Key 写入 job 快照或日志。

```python
import os
from src.llm.gateway import GatewayConnection, GatewayClient, ProxySettings

connection = GatewayConnection(
    base_url=os.environ["CODEX_LB_BASE_URL"],
    api_key=os.environ["CODEX_LB_API_KEY"],
    proxy=ProxySettings.capture(),
)
gateway = GatewayClient(connection, timeout_seconds=480, require_stream_completed=True)
```

已有 `codex_lb` 配置对象可用 `connection_from_settings(settings)` 转换。新 Web 产品接线使用 `gateway_connection_for_frontend(codex_settings, frontend_settings)`，然后显式把同一连接传过后台线程、批量线程、阶段与能力。旧 `codex_lb_environment` 仅为兼容调用保留，新产品接线不使用它；旧 `CodexLBClient(settings)` 在每个完整请求起点读取并冻结配置，显式连接与 `GatewayClient` 则始终沿用传入快照。`None` 继承旧配置；前端空值、布尔显式关闭与历史快照的含义仍由原产品适配器处理。

## 组合二：单 PDF → 带物理页标记的 TXT

接上面的 `gateway` 示例：

```python
from pathlib import Path
from src.pdf import (
    PDFOCROptions, build_pdf_ocr_run_identity,
    build_pdf_ocr_checkpoint_namespace, ocr_pdf,
)

source = Path("book.pdf")
options = PDFOCROptions(
    model="gemini-3.5-flash-lite", reasoning_effort="medium",
    max_concurrency=4, submit_interval_seconds=5,
)
identity = build_pdf_ocr_run_identity(source, options.model, options.reasoning_effort)
checkpoints = build_pdf_ocr_checkpoint_namespace(Path("output/book-pages"), identity)
result = ocr_pdf(
    source, Path("output/book.txt"), options,
    gateway=gateway, checkpoint_dir=checkpoints,
)
print(result.status, result.state.completed_pages, result.state.page_count)
if result.success:
    print(result.output_text_path)
else:
    print(result.error, result.state.page_errors)
```

重复同一调用会复用身份匹配的成功页，只识别缺页。身份仍由 PDF 内容、主模型、推理强度和原协议版本决定；不增加提示词等新身份字段。参考提取与独立书籍工具继续使用各自检查点根目录。

结果有明确终态：

- `success`：全部物理页完成，TXT 已原子发布。
- `partial`：还有缺页；保留成功页检查点，不发布新的整本 TXT。
- `failed`：准备、检查点校验或产物发布失败；查看 `error`。

`resumable` 只描述页是否齐全，不代表成功。空白页是合法成功页；合并仍保留 `----- OCR_PAGE_BREAK: 1 -> 2 -----`，页内段落不改，`.source.json` 保留新成功页的真实模型来源。

默认使用参考提取提示词；独立书籍传 `prompt_builder=build_pdf_book_ocr_image_prompt`，保留中外文与注释要求。首轮不自动切模型。只有调用方显式创建并传入 `build_pdf_ocr_retry_plan(identity, previous.state.page_errors)` 时，匹配身份的 GPT 输出过滤／截断失败页才使用原有 Gemini 补页规则；普通超时、TLS 或鉴权错误不新建切模计划。Web 仍只在“重试缺失页”入口创建计划。

## 组合三：读书会／对谈文字 → 校对结果 → 现有导出

只整理文字时，接网关示例直接调用任务：

```python
from pathlib import Path
from src.llm.tasks.refinement import RefinementOptions, refine_document

result = refine_document(
    Path("asr.txt").read_text(encoding="utf-8"),
    title="本次读书会",
    prompt=Path("config/prompts/final_cleanup.md").read_text(encoding="utf-8"),
    reference_text=Path("reference.txt").read_text(encoding="utf-8"),
    options=RefinementOptions(model="gpt-6.1-sol", reasoning_effort="high"),
    gateway=gateway, logs_dir=Path("output/refine-logs"),
)
if result.selected_result is not None:
    Path("output/draft.md").write_text(result.selected_result.final_markdown, encoding="utf-8")
else:
    print(result.comparison_summary, result.backend_results)
```

对谈可省略 `reference_text`，并传入原对谈提示词。任务保留引用锁定、结果校验和单／双 ASR 差异。多个后端成功时保留候选供人工选择，不能擅自挑出一个当作最终稿。

要沿用完整产品产物和导出规则，运行现有组合入口：

```bash
.venv/bin/python scripts/00_run_main_pipeline.py --help
.venv/bin/python scripts/08_run_job.py --help
.venv/bin/python scripts/09_run_batch_jobs.py --help
```

程序内可调用同一阶段核心：

```python
import logging
from src.config_loader import load_settings
from src.pipeline import run_stage

loaded = load_settings()
for stage in ("prepare-reference", "refine", "export-markdown"):
    code = run_stage(stage, loaded, logging.getLogger("example"), gateway_connection=connection)
    if code:
        raise SystemExit(code)
```

该组合消费现有 ASR 输入；完整音视频任务由原 job／主流程入口运行。退出码不变：0 成功、1 失败、2 参考 OCR 部分完成。`align`／`classify` 与旧两步校对仍保留。

## 组合四：听写预览／最终短句 → 逐句与全文整理

流式预览与最终短句是同一 ASR 能力域中的不同操作，不能用文件批处理替换流式生命周期。模型目录已准备好时可直接运行：

```python
from pathlib import Path
import numpy as np
from src.asr.streaming import StreamingASR
from src.asr.utterance import UtteranceASR
from src.asr.file import gpu_asr_lock
from src.llm.tasks.dictation import DictationCleanup

preview = StreamingASR(Path("/path/to/preview-model"), num_threads=2)
print(preview.feed(np.zeros(1600, dtype=np.float32)))  # 实际使用传入 16kHz float32 PCM
preview.reset()

# GPU 资源由调用方持有；产品内仍由原 scheduler 以非阻塞方式排队。
with gpu_asr_lock("cuda", Path("data/jobs/_asr-gpu.lock").resolve()):
    final_asr = UtteranceASR(Path("/path/to/model-cache"), local_files_only=True)
    try:
        text = final_asr.transcribe(Path("utterance-16k-mono-pcm16.wav"), terms=["术语"])
    finally:
        del final_asr

cleanup = DictationCleanup(
    gateway, model="gemini-3.5-flash-lite", reasoning_effort="medium",
    document_model="gemini-3.8-flash-high", document_reasoning_effort="high",
)
line = cleanup(text, context=[], terms=["术语"])
full = cleanup.document(line["text"], context=[], terms=["术语"])
print(full["text"], full["needs_review"])
```

能力不接管会话 owner、人工编辑、正文版本、暂停／结束、历史和共享排队。使用网页听写时继续通过原 service／scheduler，暂停保留 GPU 资源、停止等待真实 worker／HTTP 退出等行为不变。全文整理的所有分块共享同一总期限，任一失败不发布半篇建议。

## 本地验证与证据

统一测试：

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest
```

可复现的 PDF／产品编排离线样例：

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python scripts/validate_backend_modules.py
```

脚本每次建立独立 `tmp/backend-modules-validation/offline-*` 目录，生成三页合成 PDF（含一页空白），实际用 `pdftoppm` 渲染，执行首轮缺页、显式 Gemini 补页、检查点复用、页来源保存和带标记合并。随后实际执行 `prepare-reference → refine → export-markdown`，检查 MD／TXT。`summary.json` 给出阶段退出码、调用次数和产物路径，`rendered-page-1.png` 可查看实际渲染。

这条样例使用固定本地模型响应，不发远程请求，不执行 ASR 推理或下载模型。它验证模块接线、状态与产物发布；模型准确率和远端服务质量仍需使用者另行验收。

本次本地运行证据：

- `tmp/media-asr-validation/report.json`：真实 ffmpeg 转换与 ffprobe 检查，视频转 16kHz 单声道 PCM16 WAV、音频转 22.05kHz 单声道 FLAC，以及 PyAV 限长解码；ASR 只固定模型边界，复用一次模型处理两份合成文件。
- `tmp/backend-modules-validation/offline-hbe979p2/summary.json`：三页 PDF 首轮完成 2 页、显式补 1 页、完整检查点再次合并不请求模型；真实阶段编排输出 MD 和 TXT。
- 连接隔离与文字任务的本地 HTTP 验证见 `tests/test_llm_capabilities.py`，不访问远端服务。
