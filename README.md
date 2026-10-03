# transcript-pipeline

中文读书会录屏整理流水线。

这个项目的目标是把录屏中的语音内容整理为适合人工校对的 Markdown 文稿。整体设计从一开始就考虑两种运行环境：

- Ubuntu 24 本地开发环境，CPU 运行
- Windows + WSL2 + NVIDIA 3060Ti 环境，后续可切换到 GPU 后端

当前仓库已实现前九个阶段的最小可运行版本：

- 第一阶段：配置读取与音频抽取
- 第二阶段：ASR 抽象接口与第一版转录
- 第三阶段：参考原文统一提取
- 第四阶段：块级对齐与分段增强版（保留为辅助调试链路）
- 第五阶段：块级内容候选分类（保留为辅助调试链路）
- 第六阶段：AI 后端整篇整理，直接读取 `asr.txt + reference.txt` 产出最终 Markdown 草稿（默认 `codex_api`，`agy` / `codex_cli` 可显式指定）
- 第七阶段：将阶段 6 的 Markdown 结果写入最终输出目录，并同步生成 TXT
- 第八阶段：单任务 job 入口，支持显式指定视频、参考源和输出目录
- 第九阶段：批量 job 入口，支持按 manifest、basename 配对和共享参考三种模式批量运行

当前仍不包含最终分类定稿或自动发布能力。

## 当前阶段已实现

- 项目目录骨架
- `config/settings.yaml` 配置读取
- `local_cpu` / `wsl2_gpu` profile 机制
- 扫描 `data/input/videos/` 下的视频文件
- 使用 `ffmpeg` 抽取为 `wav`、单声道、16000 Hz 音频
- 扫描 `data/input/audio/` 下的音频文件
- 使用 `faster-whisper` 生成转录中间结果
- 输出 `data/intermediate/asr/` 下的 JSON 和 TXT
- 扫描 `data/input/reference/` 下的参考原文文件
- 支持 `.txt` / `.md` / 可提取文本的 `.pdf`
- 输出 `data/intermediate/extracted_text/` 下的 JSON 和 TXT
- 读取 ASR 与参考文本中间结果并生成 `data/intermediate/aligned/` 对齐结果
- 对齐阶段支持更细粒度分段、文本 normalization 和 top-k 匹配候选
- 基于 aligned 结果输出保守的候选分类到 `data/intermediate/classified/`（可选辅助）
- 基于 `asr.txt + extracted_text.txt + 提示词` 直接输出整篇 Markdown 草稿到 `data/intermediate/refined/`
- 阶段 6 默认使用 `codex_api`，也支持本地 `codex` 与 `gemini` CLI 双后端整篇比较后生成单一 `final_markdown`
- 阶段 7 将阶段 6 的 `final_markdown` 写入 `data/output/final/`
- 十个最小 CLI 入口
- 最基本单元测试

## 当前阶段未实现

- 参考原文结构化分析
- 复杂对齐路径搜索
- 最终分类定稿
- `quote / lecture / qa` 分类
- docx / html 等其他导出格式

## 目录结构

```text
transcript-pipeline/
├── README.md
├── .gitignore
├── .env.example
├── requirements.txt
├── config/
│   ├── settings.yaml
│   └── prompts/
│       ├── classify_and_correct.md
│       └── final_cleanup.md
├── data/
│   ├── input/
│   │   ├── videos/
│   │   ├── audio/
│   │   └── reference/
│   ├── jobs/
│   ├── intermediate/
│   │   ├── asr/
│   │   ├── ocr/
│   │   ├── extracted_text/
│   │   ├── chunks/
│   │   ├── aligned/
│   │   ├── classified/
│   │   └── refined/
│   └── output/
│       ├── review/
│       ├── final/
│       └── logs/
├── scripts/
│   ├── 00_run_main_pipeline.py
│   ├── 01_extract_audio.py
│   ├── 02_transcribe.py
│   ├── 03_prepare_reference.py
│   ├── 04_align.py
│   ├── 05_classify.py
│   ├── 06_refine.py
│   ├── 07_export_markdown.py
│   ├── 08_run_job.py
│   ├── 09_run_batch_jobs.py
│   └── run_pipeline.py
├── src/
│   ├── __init__.py
│   ├── align_utils.py
│   ├── asr_utils.py
│   ├── classify_utils.py
│   ├── config_loader.py
│   ├── ffmpeg_utils.py
│   ├── job_runner.py
│   ├── refine_utils.py
│   ├── reference_utils.py
│   ├── runtime_utils.py
│   └── schemas.py
└── tests/
    ├── helpers.py
    ├── test_config.py
    ├── test_extract_audio.py
    ├── test_align.py
    ├── test_classify.py
    ├── test_export_markdown.py
    ├── test_refine.py
    ├── test_job_runner.py
    ├── test_prepare_reference.py
    └── test_transcribe.py
```

说明：

- `config/settings.yaml` 是当前唯一运行配置入口
- `data/` 目录下的叶子目录使用 `.gitkeep` 保留骨架
- 第六阶段会读取 `config/prompts/classify_and_correct.md`
- 第七阶段会直接写出 `data/intermediate/refined/*.json` 中的 `final_markdown`

## 运行要求

1. Python 3.12
2. 系统已安装 `ffmpeg`
3. 如需 PDF Codex API OCR，系统需安装 `pdftoppm`（通常来自 `poppler-utils`）
4. 如使用远程 Cloudflare 反代的 `codex-lb`，建议系统安装 `curl`

安装依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

新电脑的 WSL2 项目环境可以直接使用安装脚本：

```bash
bash scripts/install_wsl2_env.sh
```

详细步骤见 `docs/INSTALL_NEW_MACHINE.md`。该脚本只处理 WSL2 Linux 内部环境，不处理 Windows 宿主机安装。

## Docker 部署

本项目提供单服务 `trans` 的 FastAPI + Vue Docker 部署（宿主默认端口 8080），两个入口共用现有 Dockerfile / Compose：

| 宿主平台 | 只读预检 | 部署与验收文档 |
| --- | --- | --- |
| 原生 Ubuntu 22.04/24.04/26.04 + NVIDIA，x86_64/systemd、本机 rootful Engine | `bash scripts/deploy_docker_linux.sh --check` | [`docs/DOCKER_LINUX.md`](docs/DOCKER_LINUX.md) |
| WSL2 Debian/Ubuntu + NVIDIA，WSL 内原生 Engine | `bash scripts/deploy_docker_wsl2.sh --check` | [`docs/DOCKER.md`](docs/DOCKER.md) |

预检后，由管理员确认风险再运行对应脚本（不带 `--check`），输入 `DEPLOY` 才安装缺失依赖并后台部署。Ubuntu 入口不经过 WSL2 / Docker Desktop，不提供完全不用 Docker 的安装方案；`--check` 退出码 `2` 表示信息不足，不是通过。首次建议 `DOCKER_BIND_IP=127.0.0.1`，默认无认证/HTTPS，不应暴露公网。旧 `app` 须在任务结束后人工停止/移除；已有 `trans` 更新可能中断内存任务，healthy 不代表空闲。现有 GPU profile 的历史 WSL2 名称可用于 Ubuntu，但须检查 Web 保存设置；构建成功不等于 Whisper/Qwen GPU 推理验收通过。

## Codex API 模式

当前默认 AI 后端已经切到 `codex_api`。下列环境变量示例用于原有本地脚本开发；Docker 部署的 codex-lb 位于外部互联网，优先在现有 Web「运行设置」中配置 HTTPS Base URL、API Key 和直连开关，无需在本机启动 codex-lb、也无需填写 `.env`。本地脚本方式使用前请确保目标 codex-lb 服务已启动/可达，再在当前 shell 配置 API 地址和 key：

本地 `codex-lb`：

```bash
export CODEX_LB_BASE_URL="http://127.0.0.1:2455"
export CODEX_LB_API_KEY="你的 codex-lb API key"
```

远程反代 `codex-lb`：

```bash
export CODEX_LB_BASE_URL="https://你的反代域名"
export CODEX_LB_API_KEY="你的 codex-lb API key"
```

注意：`CODEX_LB_BASE_URL` 填项目根地址，不要带 `/v1`。程序会按配置自动拼接 `/v1/responses` 和 `/backend-api/codex/responses`。

阶段 3 参考原文准备会按配置默认使用 Codex API 做 PDF OCR：

```bash
.venv/bin/python scripts/03_prepare_reference.py
```

## 独立 PDF 书籍 OCR

如果只需要把扫描版或图片型 PDF 书籍识别成文本，不需要放入录屏整理流水线，可直接运行独立命令：

```bash
.venv/bin/python scripts/10_ocr_pdf_books.py "/path/to/book.pdf"
```

也可以把一个书籍目录一次性处理；会递归扫描其中的 PDF，并在输出目录中保留原有子目录层级：

```bash
.venv/bin/python scripts/10_ocr_pdf_books.py \
  "/path/to/books" \
  --output-dir "/path/to/ocr-text"
```

默认输出目录是 `output/pdf/ocr/`，每本 PDF 对应一个同名 `.txt`。该命令不读取
`data/input/reference/`，也不会生成录屏整理流水线的中间 JSON。它沿用当前的 Codex API
逐页 OCR：每页单独识别、按页码重排，单页失败不会中止其余页面；只有全部页面齐全后才写出
该书的新 TXT。独立 Web 工具还会持久化成功页，并可反复“重试缺失页”；页面可为每个任务单独
设置图片投递间隔和最大并发数，补页时沿用原任务设置。使用前仍需按上文设置
`CODEX_LB_BASE_URL` 和 `CODEX_LB_API_KEY`，并安装 `pdftoppm`。

可临时覆盖书籍 OCR 模型：

```bash
.venv/bin/python scripts/10_ocr_pdf_books.py \
  "/path/to/book.pdf" \
  --ocr-model "gpt-6-luna" \
  --ocr-reasoning-effort "high"
```

阶段 6 精修默认读取 `llm.backends`，当前默认也是 `codex_api`：

```bash
.venv/bin/python scripts/06_refine.py
```

如果想显式指定 API 后端：

```bash
.venv/bin/python scripts/06_refine.py --backend codex_api
```

统一入口也可以显式指定：

```bash
.venv/bin/python scripts/run_pipeline.py --stage refine --backend codex_api
```

单任务入口：

```bash
.venv/bin/python scripts/08_run_job.py \
  --video "/path/to/video.mp4" \
  --reference "/path/to/reference.pdf" \
  --output-dir "/path/to/output" \
  --backend "codex_api"
```

批量入口：

```bash
.venv/bin/python scripts/09_run_batch_jobs.py \
  --profile "wsl2_gpu_high_accuracy" \
  --manifest "/path/to/jobs.yaml" \
  --remote-concurrency 2 \
  --backend "codex_api" \
  --model "gpt-6.1-sol" \
  --reasoning-effort "high" \
  --ocr-model "gpt-6-luna" \
  --ocr-reasoning-effort "high"
```

相关默认配置：

- 阶段 6：`llm.backends = ["codex_api"]`
- 阶段 6 默认模型：`llm.model = gpt-6.1-sol`（GPT-6.1 Sol）；读取旧 Web 保存的 `gpt-6-sol` 默认值时映射到新 ID，不改写历史任务配置
- 阶段 6 reasoning 支持 `low`、`medium`、`high`、`xhigh`、`max`；默认值为 `llm.reasoning_effort = high`
- PDF OCR：`reference.ai_ocr_backend = codex_api`
- PDF OCR 模型：`reference.codex_ocr_model = gpt-6-luna`（GPT-6 Luna）
- PDF OCR reasoning：`reference.codex_ocr_reasoning_effort = high`
- PDF OCR 最大并发：`reference.codex_ocr_max_concurrency = 40`；只限制同时在途请求，必须为正整数，可在独立 PDF OCR 页面按任务调整
- PDF OCR 投递间隔：`reference.codex_ocr_submit_interval_seconds = 5.0` 秒；每次只新增一张图片，不等待前一页完成，可在独立 PDF OCR 页面按任务调整，最终仍按原始页码拼接
- PDF OCR 等待时间：`reference.ocr_timeout_seconds = 480`
- `--backend both` 保持旧语义，只同时运行 `codex_cli` 和 `agy`

临时指定阶段 6 模型和 reasoning：

```bash
.venv/bin/python scripts/06_refine.py \
  --backend codex_api \
  --model "gpt-6.1-sol" \
  --reasoning-effort "high"
```

临时指定 PDF OCR 模型和 reasoning：

```bash
.venv/bin/python scripts/03_prepare_reference.py \
  --ocr-model "gpt-6-luna" \
  --ocr-reasoning-effort "high"
```

完整主链里也可以同时覆盖：

```bash
.venv/bin/python scripts/08_run_job.py \
  --video "/path/to/video.mp4" \
  --reference "/path/to/reference.pdf" \
  --output-dir "/path/to/output" \
  --backend "codex_api" \
  --model "gpt-6.1-sol" \
  --reasoning-effort "high" \
  --ocr-model "gpt-6-luna" \
  --ocr-reasoning-effort "high"
```

## WSL2 Debian GPU 部署清单

下面这份清单面向“全新 Debian WSL2 + NVIDIA 3060Ti”环境，目标是把当前项目跑到 `wsl2_gpu` / `wsl2_gpu_max_accuracy` profile。

### 1. Windows 宿主机准备

在 Windows PowerShell 中执行：

```powershell
wsl --install -d Debian
wsl --update
wsl --shutdown
wsl -l -v
```

如果 Debian 还不是 WSL2，继续执行：

```powershell
wsl --set-version Debian 2
```

然后处理显卡侧：

- 在 Windows 宿主机安装最新 NVIDIA Windows 驱动
- 不要在 WSL 里的 Debian 再安装 Linux NVIDIA 显卡驱动
- 更新完成后重新进入 Debian

### 2. Debian 基础环境

建议把仓库放在 Linux 文件系统中，例如 `~/code/transcript-pipeline`，不要放在 `/mnt/c/...`。

在 Debian 中执行：

```bash
sudo apt update
sudo apt install -y git ffmpeg poppler-utils curl python3 python3-venv python3-pip
```

检查 Python 版本：

```bash
python3 --version
```

如果这里不是 `Python 3.12.x`，建议先补齐 3.12 再继续，因为项目当前运行要求是 Python 3.12。

### 3. 克隆项目并创建虚拟环境

```bash
git clone <你的仓库地址> ~/code/transcript-pipeline
cd ~/code/transcript-pipeline

python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip wheel
pip install -r requirements.txt
```

### 4. 安装 GPU 运行库

`faster-whisper` 当前 GPU 运行依赖 `CUDA 12 + cuDNN 9` 对应的运行库。对这个项目，最直接的方式是在虚拟环境里安装 Python wheels：

```bash
. .venv/bin/activate
pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
```

然后为当前 shell 设置动态库路径：

```bash
export LD_LIBRARY_PATH="$(
  .venv/bin/python - <<'PY'
import importlib.util

paths = []
for name in ("nvidia.cublas.lib", "nvidia.cudnn.lib"):
    spec = importlib.util.find_spec(name)
    if spec and spec.submodule_search_locations:
        paths.extend(spec.submodule_search_locations)

print(":".join(paths), end="")
PY
)${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
```

如果你希望以后登录 Debian 自动生效，可以把上面这行追加到 `~/.bashrc`。

### 5. 先验证 WSL GPU 是否通

先看 WSL 是否已经识别到 3060Ti：

```bash
nvidia-smi
```

如果 `nvidia-smi` 不在默认 PATH，可以尝试：

```bash
/usr/lib/wsl/lib/nvidia-smi
```

如果这一步失败，先回到 Windows 宿主机检查 WSL 更新和 NVIDIA 驱动，不要继续折腾项目依赖。

### 6. 运行项目测试

```bash
.venv/bin/python -m pytest
```

### 7. 做一次 faster-whisper GPU 冒烟测试

```bash
.venv/bin/python - <<'PY'
from faster_whisper import WhisperModel

model = WhisperModel("small", device="cuda", compute_type="float16")
print("GPU model load OK")
PY
```

如果这里输出 `GPU model load OK`，说明 WSL2 + CUDA runtime + `faster-whisper` 这条链已经打通。

### 8. 开始跑项目

平衡档：

```bash
.venv/bin/python "scripts/08_run_job.py" \
  --video "/path/to/video.mp4" \
  --reference "/path/to/reference.pdf" \
  --output-dir "/path/to/output" \
  --profile "wsl2_gpu"
```

最高精度档：

```bash
.venv/bin/python "scripts/08_run_job.py" \
  --video "/path/to/video.mp4" \
  --reference "/path/to/reference.pdf" \
  --output-dir "/path/to/output" \
  --profile "wsl2_gpu_max_accuracy"
```

### 9. 常见问题

- `nvidia-smi` 不通：优先检查 Windows 驱动和 `wsl --update`
- 报 `libcublas.so` / `libcudnn.so` 找不到：通常是 `LD_LIBRARY_PATH` 没生效
- `pytest` 通过但 GPU 仍跑不动：用上面的 GPU 冒烟测试单独排查
- 不要在 `/mnt/c/...` 下长期跑项目，I/O 会明显更慢

### 10. 官方参考

- Microsoft WSL 安装: https://learn.microsoft.com/en-us/windows/wsl/install
- Microsoft WSL 基本命令: https://learn.microsoft.com/en-us/windows/wsl/basic-commands
- NVIDIA CUDA on WSL User Guide: https://docs.nvidia.com/cuda/wsl-user-guide/index.html
- faster-whisper README: https://github.com/SYSTRAN/faster-whisper

## 配置

默认配置文件为 `config/settings.yaml`。

当前支持五个 profile：

- `local_cpu`
- `local_cpu_high_accuracy`
- `wsl2_gpu`
- `wsl2_gpu_high_accuracy`
- `wsl2_gpu_max_accuracy`

可以通过以下方式切换：

- 配置文件中的 `runtime.profile`
- 环境变量 `TRANSCRIPT_PROFILE`
- CLI 参数 `--profile`

优先级从高到低依次为：CLI 参数、环境变量、配置文件默认值。

## Web UI 使用方式

当前仓库已经包含一个 Web UI，默认只允许本机访问，也可以显式开启局域网访问：

- 后端：FastAPI，默认监听 `127.0.0.1:8000`
- 前端：Vue 3 + TypeScript + Naive UI，默认监听 `127.0.0.1:5173`

### 1. 首次安装前端依赖

```bash
cd "frontend"
npm install
cd ".."
```

### 2. 一键启动 Web UI

推荐在项目根目录使用一键启动入口，同时拉起后端和前端：

```bash
./start_web.sh
```

如果希望启动后自动打开浏览器：

```bash
./start_web.sh --open-browser
```

如果要让同一局域网内其他人访问，在主机上执行：

```bash
./start_web.sh --lan
```

脚本会监听 `0.0.0.0` 并打印可分享的局域网访问地址，例如：

```text
http://192.168.1.20:5173
```

如果项目运行在 **Windows 的 WSL2** 中，需要额外注意：WSL2 默认 NAT 网络下，
`./start_web.sh --lan` 只是让服务在 WSL 内监听 `0.0.0.0`，局域网其他电脑通常不能直接访问
WSL 内部 IP。脚本检测到 WSL 后会额外打印一组 Windows 管理员 PowerShell 命令，格式类似：

```powershell
netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=5173 connectaddress=<WSL_IP> connectport=5173
netsh advfirewall firewall add rule name="Transcript Pipeline Web 5173" dir=in action=allow protocol=TCP localport=5173
```

执行后，局域网其他电脑应访问 **Windows 主机的局域网 IP**，例如：

```text
http://192.168.1.20:5173
```

说明：

- `<WSL_IP>` 可以用 WSL 内的 `hostname -I` 查看，启动脚本也会尽量自动打印。
- WSL 重启后内部 IP 可能变化；如果访问失效，重新执行启动脚本打印的 `portproxy` 命令。
- Web UI 只需要暴露前端端口 `5173`，页面里的 `/api`、上传和下载会由前端开发服务器代理到 WSL 内部后端。
- 如果你已启用 WSL mirrored networking，局域网直连会更接近普通 Linux 主机，但仍需要确认 Windows / Hyper-V 防火墙允许入站端口 `5173`。

其他人在浏览器打开这个地址后，点击“选择并上传本机视频 / 参考源”时，打开的是**使用者自己电脑**的文件选择器；选中文件后会上传到主机的 `data/uploads/`，流水线再从上传后的服务器路径执行处理。

在 Web UI 中，输出目录是主机上的服务器保存位置，普通局域网使用者不需要填写或进入这个目录。任务完成后进入 `任务列表`：

- 单任务：点击任务卡片上的 `下载结果`，选择下载 Markdown 或 TXT。
- 批量任务：点击 `下载全部结果` 获取成功子任务的 ZIP 包；展开批量任务后，也可以在子任务列表中分别下载每个成功子任务的结果。

也可以直接使用 Python 脚本：

```bash
.venv/bin/python "scripts/start_web.py"
```

一键启动后可访问：

- `http://127.0.0.1:5173`：Web UI 页面
- `http://127.0.0.1:8000/docs`：FastAPI Swagger 文档
- `http://127.0.0.1:8000/api/config`：当前 profile / backend 配置

如需单独调试后端，也可以在项目根目录执行：

```bash
.venv/bin/python "api_server.py"
```

### 3. 启动前端开发服务器

如需单独调试前端，另开一个终端，在项目根目录执行：

```bash
cd "frontend"
npm run dev -- --host 127.0.0.1
```

然后在浏览器打开：

```text
http://127.0.0.1:5173
```

### 4. 页面说明

- `单任务`：对应 `scripts/08_run_job.py`
- `批量任务`：对应 `scripts/09_run_batch_jobs.py`
- `单阶段`：对应 `scripts/run_pipeline.py --stage`
- `PDF OCR`：独立上传单本 PDF 或 PDF 目录，识别完成后逐本下载 TXT 或 EPUB；失败页会单独列出，任务历史可重试缺失页并沿用成功页检查点
- `任务列表`：查看 `data/jobs/` 下已有任务状态，下载单任务结果、批量结果 ZIP 和批量子任务结果
- `设置`：配置 Codex API 的 base URL、API key、阶段 6 模型和 PDF OCR 模型；“API 直连
  （绕过代理）”默认关闭，开启并保存后只把当前 Codex API 主机加入新任务的 `NO_PROXY`，不会清除或
  改写其他网站的代理设置，也不需要重启 Web 服务
- 单任务视频、参考源、术语词表会通过浏览器选择使用者本机文件并上传到主机
- 批量任务的视频目录和参考目录会通过浏览器选择使用者本机目录，并把受支持文件上传到主机

### 5. 使用注意

- Web UI 依赖后端 API，日常使用优先执行 `./start_web.sh`
- `./start_web.sh` 会在同一个终端内管理前端和后端，按 `Ctrl+C` 可同时停止两个服务
- Web UI 设置会保存到 `data/jobs/frontend-settings.json`，该路径默认被 `.gitignore` 忽略
- 上传文件会保存到 `data/uploads/`，该目录默认被 `.gitignore` 忽略
- PDF OCR 的上传文件会位于 `data/uploads/pdf-ocr/`；结果、任务状态和逐页检查点会隔离保存在 `data/jobs/pdf-ocr/`，不会写入录屏整理流水线的数据目录
- 局域网访问前，请确保主机防火墙允许前端端口 `5173` 和后端端口 `8000`
- 单阶段页面运行的是**当前配置文件绑定的数据目录**，不是临时单文件运行器
- 长任务使用 `state.json` 持久化状态，页面会通过轮询显示 `pending / running / success / failed`

## 运行方式

直接运行音频抽取：

```bash
.venv/bin/python scripts/01_extract_audio.py
```

指定 profile：

```bash
.venv/bin/python scripts/01_extract_audio.py --profile local_cpu
```

通过统一入口运行音频抽取阶段：

```bash
.venv/bin/python scripts/run_pipeline.py --stage extract-audio
```

直接运行转录阶段：

```bash
.venv/bin/python scripts/02_transcribe.py
```

通过统一入口运行转录阶段：

```bash
.venv/bin/python scripts/run_pipeline.py --stage transcribe
```

直接运行参考原文准备阶段：

```bash
.venv/bin/python scripts/03_prepare_reference.py
```

通过统一入口运行参考原文准备阶段：

```bash
.venv/bin/python scripts/run_pipeline.py --stage prepare-reference
```

直接运行对齐阶段：

```bash
.venv/bin/python scripts/04_align.py
```

通过统一入口运行对齐阶段：

```bash
.venv/bin/python scripts/run_pipeline.py --stage align
```

直接运行候选分类阶段：

```bash
.venv/bin/python scripts/05_classify.py
```

通过统一入口运行候选分类阶段：

```bash
.venv/bin/python scripts/run_pipeline.py --stage classify
```

直接运行阶段 6 精修：

```bash
.venv/bin/python scripts/06_refine.py
```

只运行 agy：

```bash
.venv/bin/python scripts/06_refine.py --backend agy
```

显式运行 Codex API：

```bash
.venv/bin/python scripts/06_refine.py --backend codex_api
```

指定 Codex API 模型和 reasoning：

```bash
.venv/bin/python scripts/06_refine.py \
  --backend codex_api \
  --model "gpt-6.1-sol" \
  --reasoning-effort "high"
```

同时运行旧 Codex CLI 和 agy：

```bash
.venv/bin/python scripts/06_refine.py --backend both
```

通过统一入口运行阶段 6：

```bash
.venv/bin/python scripts/run_pipeline.py --stage refine
```

阶段 6 说明：

- 默认按 `config/settings.yaml` 中的 `llm.backends` 运行后端，当前默认是 `codex_api`
- 可用 `--backend codex_api|codex_cli|agy|both` 临时覆盖
- `codex_api` 通过 `codex-lb` 调用，`CODEX_LB_BASE_URL` 和 `CODEX_LB_API_KEY` 必须在环境变量中可用
- 阶段 6 模型可用 `--model` 临时覆盖，reasoning 可用 `--reasoning-effort` 临时覆盖
- `both` 保持旧语义，只展开为 `codex_cli + agy`
- Gemini 主模型默认是 `Gemini 3.1 Pro (High)`
- 主输入是 `data/intermediate/asr/*.txt` 和同 basename 的 `data/intermediate/extracted_text/*.txt`
- 不再依赖 `classified.json` 作为阶段 6 的主输入
- 提示词贴近网页端单轮整理模式，直接要求输出最终 Markdown
- 单模型模式会继续写单一 `final_markdown`
- 非 fallback AI 后端会写主索引文件 `basename.json`，并按后端额外写侧车文件，例如：
  - `basename.codex_api.json`
  - `basename.codex_cli.json`
  - `basename.agy.json`
- 双模型模式下主索引文件不会自动选主结果，`final_markdown` 留空，需人工决定使用哪一份
- `final_markdown` 已直接包含：
  - 原文朗读引用块 `>`
  - 讲解普通段落
  - `## 提问环节`
- 模型输出会先校验为 JSON 对象并要求 `final_markdown` 非空；最终交付校验只拒绝程序回退、Unicode
  替换字符 `�` 和明确的 `# source` 占位标题，不再强制一级标题或固定的标题/正文空行样式
- 如果所有请求后端都失败，会回退到本地保守整理逻辑

阶段 6 的 `codex_api` 调用会保留可追踪诊断：

- 汇总文件默认位于 `data/output/logs/refine/diagnostics.json`；单任务目录中对应
  `data/jobs/<job_id>/output/logs/refine/diagnostics.json`，也可通过任务产物中的“阶段 6 调用诊断”读取
- 每次尝试的完整证据位于汇总文件所列的 `diagnostic_directory`，包括脱敏请求摘要、传输指标、
  完整或中断时已收到的 SSE、SSE 事件摘要、提取正文、JSON 解析结果、错误和最终校验原因
- 请求摘要不保存 API Key、Authorization 或 Prompt 正文，只保存模型、端点、代理地址以及长度和
  SHA-256；模型响应正文会完整保存，因此诊断目录仍应按任务数据的权限保护
- 为保证失败证据完整，当前不截断 SSE，也不自动清理历史诊断；长文稿和多次重跑会增加磁盘占用，
  需要由部署方根据实际用量制定备份或清理策略

直接运行阶段 7 写入最终 Markdown：

```bash
.venv/bin/python scripts/07_export_markdown.py
```

通过统一入口运行阶段 7：

```bash
.venv/bin/python scripts/run_pipeline.py --stage export-markdown
```

阶段 7 说明：

- 输入为 `data/intermediate/refined/*.json`
- 输出为 `data/output/final/*.md` 与 `data/output/final/*.txt`
- 当前生成的是最终校对稿，Markdown 供保留结构阅读，TXT 供纯文本下载和校对
- 阶段 7 只负责把阶段 6 已生成的 `final_markdown` 落盘
- 不再负责正文结构重组

当前推荐主链：

- `extract-audio`
- `transcribe`
- `prepare-reference`
- `refine`
- `export-markdown`

`align` / `classify` 当前保留为辅助调试链路，不再是默认成稿主链。
默认整链运行时，应优先按以上五个阶段串行执行，而不是把 `align` / `classify` 作为必经步骤。

一键运行当前推荐主链：

```bash
.venv/bin/python scripts/00_run_main_pipeline.py
```

推荐的单任务运行方式：

```bash
.venv/bin/python scripts/08_run_job.py \
  --video "/path/to/video.mp4" \
  --reference "/path/to/reference.pdf" \
  --output-dir "/path/to/output"
```

使用新的更激进 GPU 高精度预设：

```bash
.venv/bin/python scripts/08_run_job.py \
  --video "/path/to/video.mp4" \
  --reference "/path/to/reference.pdf" \
  --output-dir "/path/to/output" \
  --profile "wsl2_gpu_max_accuracy"
```

指定阶段 6 使用 Gemini：

```bash
.venv/bin/python scripts/08_run_job.py \
  --video "/path/to/video.mp4" \
  --reference "/path/to/reference.pdf" \
  --output-dir "/path/to/output" \
  --profile "wsl2_gpu_max_accuracy" \
  --backend "agy"
```

指定阶段 6 使用 Codex API：

```bash
.venv/bin/python scripts/08_run_job.py \
  --video "/path/to/video.mp4" \
  --reference "/path/to/reference.pdf" \
  --output-dir "/path/to/output" \
  --backend "codex_api"
```

指定阶段 6 与 PDF OCR 的 Codex API 模型：

```bash
.venv/bin/python scripts/08_run_job.py \
  --video "/path/to/video.mp4" \
  --reference "/path/to/reference.pdf" \
  --output-dir "/path/to/output" \
  --backend "codex_api" \
  --model "gpt-6.1-sol" \
  --reasoning-effort "high" \
  --ocr-model "gpt-6-luna" \
  --ocr-reasoning-effort "high"
```

同时运行旧 Codex CLI 和 agy：

```bash
.venv/bin/python scripts/08_run_job.py \
  --video "/path/to/video.mp4" \
  --reference "/path/to/reference.pdf" \
  --output-dir "/path/to/output" \
  --backend "both"
```

可选附加术语词表：

```bash
.venv/bin/python scripts/08_run_job.py \
  --video "/path/to/video.mp4" \
  --reference "/path/to/reference.pdf" \
  --book-name "家庭、私有制和国家的起源" \
  --chapter "第八章" \
  --glossary-file "/path/to/chapter_terms.txt" \
  --output-dir "/path/to/output"
```

单任务入口说明：

- 不再要求视频文件和参考文件同 basename
- 每次运行都会创建独立的 `data/jobs/<job_id>/`
- 参考源支持：
  - 本地 `txt`
  - 本地 `md`
  - 本地 `pdf`
  - 公开网页链接
- 网页链接若目标是 PDF，会先下载 PDF，再按阶段 3 处理
- 最终 Markdown 与 TXT 会额外复制到 `--output-dir`
- `config/glossaries/marxism_common.txt` 会默认参与构造本次任务的 `asr.initial_prompt`
- `--book-name`、`--chapter`、`--glossary-file` 会追加到本次任务的 `initial_prompt`
- `--backend` 只覆盖本次任务的阶段 6 后端选择，可用值为 `codex_api`、`codex_cli`、`agy`、`both`
- 单任务入口同样支持 `--model`、`--reasoning-effort`、`--ocr-model`、`--ocr-reasoning-effort`
- 附加词表文件格式为一行一个词条
- `local_cpu` 与 `wsl2_gpu` 使用 `beam_size = 5`
- `local_cpu_high_accuracy` 与 `wsl2_gpu_high_accuracy` 使用 `beam_size = 8`
- `wsl2_gpu_max_accuracy` 使用 `large-v3-turbo`，并将 `beam_size` 提高到 `10`

推荐的 GPU 批量运行方式：

manifest 模式：

```bash
.venv/bin/python scripts/09_run_batch_jobs.py \
  --profile "wsl2_gpu_high_accuracy" \
  --manifest "/path/to/jobs.yaml" \
  --remote-concurrency 2 \
  --backend "codex_api" \
  --model "gpt-6.1-sol" \
  --reasoning-effort "high" \
  --ocr-model "gpt-6-luna" \
  --ocr-reasoning-effort "high"
```

`jobs.yaml` 示例：

```yaml
jobs:
  - video: /data/videos/session-01.mp4
    reference: /data/reference/session-01.txt
    output_dir: /data/output
    book_name: 家庭、私有制和国家的起源
    chapter: 第一章

  - video: /data/videos/session-02.mp4
    reference: https://example.com/session-02
    output_dir: /data/output
    glossary_file: /data/glossary/session-02.txt
```

basename 配对模式：

```bash
.venv/bin/python scripts/09_run_batch_jobs.py \
  --profile "wsl2_gpu_high_accuracy" \
  --videos-dir "/data/videos" \
  --reference-dir "/data/reference" \
  --output-dir "/data/output" \
  --book-name "家庭、私有制和国家的起源" \
  --chapter "第一编" \
  --remote-concurrency 2 \
  --backend "both"
```

目录约定示例：

```text
/data/videos/
  session-01.mp4
  session-02.mp4

/data/reference/
  session-01.txt
  session-02.md
```

共享参考模式：

```bash
.venv/bin/python scripts/09_run_batch_jobs.py \
  --profile "wsl2_gpu_high_accuracy" \
  --videos-dir "/data/videos" \
  --shared-reference "/data/reference/shared.txt" \
  --output-dir "/data/output" \
  --book-name "家庭、私有制和国家的起源" \
  --chapter "导读" \
  --remote-concurrency 2
```

如果共享参考是网页链接，可直接把 `--shared-reference` 换成公开 URL。

批量入口说明：

- GPU 高精度建议优先使用 `wsl2_gpu_high_accuracy`
- `prepare-reference` 与 `refine` 默认按 `--remote-concurrency 2` 并发运行
- `extract-audio` 与 `transcribe` 仍按单任务顺序执行，避免本地资源阶段过载
- `--backend` 只覆盖本次批量任务的阶段 6 后端选择，可用值为 `codex_api`、`codex_cli`、`agy`、`both`
- 批量入口同样支持 `--model`、`--reasoning-effort`、`--ocr-model`、`--ocr-reasoning-effort`
- 每次批量运行都会写出 `data/jobs/batches/<batch_id>/manifest.json`
- 每次批量运行都会写出 `data/jobs/batches/<batch_id>/summary.json` 与 `summary.md`
- 批量退出码：
  - `0` 表示全部成功
  - `2` 表示部分失败
  - `1` 表示全部失败或入口参数无效

运行测试：

```bash
.venv/bin/python -m pytest
```

## 音频抽取行为

- 扫描目录：`data/input/videos/`
- 支持扩展名：`.mp4`、`.mkv`、`.mov`、`.webm`
- 输出目录：`data/input/audio/`
- 输出格式：`wav`
- 声道数：单声道
- 采样率：16000 Hz
- 默认不覆盖已有输出，只有在配置中 `audio.overwrite: true` 时才覆盖

如果出现以下情况会给出明确错误：

- `ffmpeg` 不存在
- 输入目录不存在或没有可处理视频
- 配置文件不存在或 profile 无效

## 转录行为

- 扫描目录：`data/input/audio/`
- 支持扩展名：`.wav`、`.mp3`、`.m4a`、`.flac`
- 使用 `faster-whisper`
- 从 profile 读取 `device`、`asr_compute_type`、`asr_model_size`、`beam_size`
- 若 profile 未显式配置 `beam_size`，会回退到全局 `asr.beam_size`
- 输出目录：`data/intermediate/asr/`
- 每个音频输出：
  - `<basename>.json`
  - `<basename>.txt`

JSON 中间结果至少包含：

- `source_file`
- `engine`
- `model_size`
- `device`
- `compute_type`
- `language`
- `segments`
- `full_text`

如果出现以下情况会给出明确错误：

- 未安装 `faster-whisper`
- `device` 配置无效
- `cuda` profile 不能正常初始化
- 音频目录为空

## 参考原文准备行为

- 扫描目录：`data/input/reference/`
- 当前仅处理顶层文件，不递归子目录
- 支持扩展名：`.txt`、`.md`、`.pdf`
- 输出目录：`data/intermediate/extracted_text/`
- 每个参考文件输出：
  - `<basename>.txt`
  - `<basename>.json`

JSON 中间结果至少包含：

- `source_file`
- `source_type`
- `output_text_file`
- `extraction_method`
- `success`
- `text_length`
- `warnings`

PDF 支持边界：

- 当前 PDF 默认优先尝试 `codex_api` OCR
- `codex_api` OCR 先读取 PDF 总页数，再由受限工作线程按需用 `pdftoppm` 渲染当前页，并通过 codex-lb `/v1/responses` 发送一个 `input_image`；页面完成后立即释放图片数据，不会一次性把整本书的 PNG/base64 保存在内存中
- 独立 PDF OCR 任务会把每个成功页原子写入任务目录；单页失败后继续处理其余页面并记录完整失败页码，点击“重试缺失页”只重新请求没有成功检查点的页面
- 最终 TXT 仍只在所有页检查点齐全后按页序生成，且不会在页边界额外插入换行；服务或任务进程重启不会删除已成功页面
- `codex_api` OCR 默认模型是 `gpt-6-luna`，reasoning effort 是 `high`
- 直接运行阶段 3 时，可用 `--ocr-model` 和 `--ocr-reasoning-effort` 临时覆盖 OCR 模型与 reasoning
- 如果 Codex API OCR 失败，但 PDF 自带文字层可提取，则回退到文字层提取
- 如果 Codex API OCR 失败，且文字层为空或接近空，并且 `reference.run_ocr_when_needed = true`，会继续尝试其他 AI OCR 后端，最后回退到 `ocrmypdf + tesseract`
- OCR sidecar 默认写入 `data/intermediate/ocr/*.codex_api_ocr.txt`
- 当前 OCR 路线面向中文扫描版 PDF
- 如果 OCR 结果仍为空或接近空，会提示当前 PDF 质量可能较差

## OCR 文本整合与 EPUB 导出

EPUB 导出采用两步流程：先把单页/片段 OCR TXT 按自然顺序整合为一个独立 TXT，再以这个整合后的 TXT 生成 EPUB。这样尾注是否可用取决于整合 TXT 是否包含全集尾注区；缺少定义的普通数字会保留原文，不会被猜测成链接。

独立 PDF OCR 后台使用专用 OCR 提示词，要求识别正文、脚注、尾注及其编号；提示词不在前端页面展示。校对流程中的参考文本准备仍使用原有的纯文本 OCR 提示词，不会套用独立书籍 OCR 的注释要求。

```bash
.venv/bin/python scripts/11_integrate_ocr_txt.py \
  /path/to/ocr-pages/ \
  --output /path/to/book.integrated.txt

.venv/bin/python scripts/12_export_epub.py \
  /path/to/book.integrated.txt \
  --output /path/to/book.epub \
  --title "书名" \
  --author "作者"
```

目录片段会按文件名中的数字自然排序，例如 `page-1.txt`、`page-2.txt`、`page-10.txt`。EPUB 使用 reflowable 排版，支持章节目录、圆圈数字脚注、全书普通数字尾注以及正文与注释之间的返回链接。

## 对齐与分段行为

- 读取目录：`data/intermediate/asr/` 与 `data/intermediate/extracted_text/`
- 当前只处理同 basename 可配对的文件
- ASR 分段：按相邻 segments 保守合并，受最小/最大字符数和时长阈值控制
- reference 分段：优先按空行切段，再按句号、问号、叹号、分号等细分
- 匹配前会做轻量 normalization：
  - 统一空白
  - 统一全角半角
  - 统一常见分隔符与标点差异
- 对齐方法：基于 `rapidfuzz` 的轻量组合评分
- 输出目录：`data/intermediate/aligned/`
- 每个配对文件输出：
  - `<basename>.json`

aligned JSON 至少包含：

- `source_asr_file`
- `source_reference_file`
- `alignment_method`
- `total_asr_blocks`
- `total_reference_blocks`
- `blocks`

每个 `block` 现在至少包含：

- `block_id`
- `source_segment_ids`
- `start`
- `end`
- `asr_text`
- `matched_reference_text`
- `match_score`
- `match_status`
- `top_matches`

当前能力边界：

- 仍然只是块级对齐增强版
- 不做一对多 / 多对一复杂对齐
- 不做全局最优路径搜索
- 不做分类
- 不做 LLM 校对

## 候选分类行为

- 读取目录：`data/intermediate/aligned/`
- 输出目录：`data/intermediate/classified/`
- 当前只做候选分类，不做最终定稿
- 当前输出标签包括：
  - `quote_candidate`
  - `mixed_candidate`
  - `lecture_candidate`
  - `qa_candidate`
  - `intro_candidate`

分类主要依据：

- `match_score`
- `match_status`
- top-1 与 top-2 的分差
- 问答关键词
- 讲解口语标记
- 开场播报关键词

每个 `classified_block` 至少包含：

- `block_id`
- `start`
- `end`
- `asr_text`
- `matched_reference_text`
- `match_score`
- `match_status`
- `top_matches`
- `classification`
- `classification_reason`
- `confidence`

当前能力边界：

- 这只是候选分类层，不是最终分类结果
- 不做 LLM 校对
- 不做最终 Markdown 拼装

## 下一阶段建议

下一步最适合优先做的事情是引入“LLM 校对精修阶段”：

- 基于 `data/intermediate/classified/` 的候选分类结果做保守精修
- 先只处理文本校对和分类确认，不急着做最终 Markdown 拼装
- 继续保持中间结果可追踪，避免一步到位变成黑箱
