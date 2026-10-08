# Docker 单容器部署

## 选择宿主平台

| 平台 | 部署入口 | 详细文档 |
| --- | --- | --- |
| 原生 Ubuntu + NVIDIA（不经过 WSL2 / Desktop） | `bash scripts/deploy_docker_linux.sh --check`，确认后运行同一脚本部署 | [DOCKER_LINUX.md](DOCKER_LINUX.md)：Ubuntu 支持范围、权限/提权边界、迁移和分层验收 |
| WSL2 Debian/Ubuntu + NVIDIA，WSL 内原生 Engine | `bash scripts/deploy_docker_wsl2.sh --check`，确认后运行同一脚本部署 | 本页后续内容 |

两者共用现有 Dockerfile、Compose、`trans` 服务与数据/模型缓存结构，不使用 Docker Desktop。Ubuntu 入口不代表完全不用 Docker 的 Linux 安装方案。以下驱动与网络说明仍针对 **WSL2**，不要在 Ubuntu 套用 Windows portproxy / Hyper-V 配置。

## WSL2 原生 Docker 部署

一个 Compose 服务 `trans`：Node 22 多阶段构建 Vue，Python 3.12 slim 非 root 运行一个 Uvicorn worker；FastAPI 在容器 `8000` 同时提供 `/api/*`、Vue 页面和静态资源。不使用 Docker Desktop、Nginx、数据库或本机 codex-lb 容器；原来的 `./start_web.sh` / Vite 开发方式不变。**仅适用于可信局域网，默认无认证、无 HTTPS，不要映射公网。**

## 前置条件和危险操作边界

Windows NVIDIA 驱动 → WSL2 `/dev/dxg` → WSL 内原生 Docker Engine + NVIDIA Container Toolkit/CDI → 容器 CUDA 12/cuDNN 9 wheels → CTranslate2。不要在 WSL 或容器安装 Linux 显卡驱动，不挂载宿主驱动目录或 Docker socket。构建不需要 GPU、不会下载 ASR 模型或发起业务 API 请求。

```bash
uname -a                         # 应含 microsoft-standard-WSL2
docker context show
docker context inspect "$(docker context show)"  # 核对 unix:///var/run/docker.sock，不是 Desktop/远端
docker info --format '{{.OperatingSystem}} {{.ServerVersion}}'
/usr/lib/wsl/lib/nvidia-smi      # WSL 驱动可见；单独不足以证明容器可用
docker compose version
```

**宿主机操作须由操作者明确确认影响：** 安装 Docker Engine / NVIDIA Container Toolkit、配置 daemon、按需重启共享 Docker，可能中断其他容器。`scripts/deploy_docker_wsl2.sh` 默认要求交互输入 `DEPLOY`（或操作者显式传 `--yes`）才执行；`--check` 只读。该脚本只支持 WSL2 的 Debian/Ubuntu x86_64 + systemd，不安装 Windows/Linux 显卡驱动，不改 Windows 防火墙或 WSL 网络。Docker Hub、Debian、NVIDIA 和 PyPI/npm 需可达；Debian 13 并非 NVIDIA Toolkit 当前已测试发行版，现场须验收。之前此机 Toolkit 缺失而失败；之后用户自行安装并验证 GPU 和真实任务成功，不能把旧故障描述为当前状态。构建镜像本身不需要 GPU。

## 首次部署与日常运维

从本仓库根目录执行。容器默认 UID/GID `1000:1000`；安装脚本会按当前宿主用户导出 `APP_UID` / `APP_GID`。只映射可信 LAN；若只允许本机，先 `export DOCKER_BIND_IP=127.0.0.1`。`APP_PORT` 默认为 8080。默认启动**要求 GPU**，不能用 CPU 测试冒充 GPU 验收。

```bash
bash scripts/deploy_docker_wsl2.sh --check   # 只读预检，尤其检查旧 app 容器
bash scripts/deploy_docker_wsl2.sh           # 人工确认后安装缺失的 Docker/Toolkit，构建、启动 trans
# 由管理员事先授权时也可显式使用 --yes；不在自动化里无意跳过确认
```

Compose 和部署脚本默认 `ASR_BACKENDS=qwen`：保留 Whisper，并在独立 Python 环境安装两款 Qwen3-ASR 的依赖，不改变默认转录模型。首次构建会下载较大的 PyTorch 等依赖，但不下载模型权重；首次转录仍可能下载权重。可用 `ASR_BACKENDS=whisper bash scripts/deploy_docker_wsl2.sh` 构建精简版，或选择 `funasr` / `all`。脚本在使用 sudo 时也会保留此选项。仅设置宿主环境或重启旧容器不会安装依赖，必须重新构建并创建容器；先确认没有运行中任务。

脚本不会执行 `apt upgrade`、给用户加入 Docker 组、递归 chown、启动付费任务或删除数据；当前用户无 Docker socket 权限时仅对 Docker 命令使用 sudo。镜像拉取/编译需联网。Web 设置页的 Key 不作为部署脚本输入；安装/启动/health/GPU 设备检查不调用远端业务或下载模型。若已有 `transcript-pipeline-app-1`，脚本**会停止执行而不会中断任务或自动删除旧容器**；先确认旧任务结束，再人工 `docker stop transcript-pipeline-app-1 && docker rm transcript-pipeline-app-1`（只移除旧容器，不带 `-v`，不要执行 `down -v`），随后重跑脚本。改名后的新容器为 `transcript-pipeline-trans-1`，保留同一 `./data` bind 和 `transcript-pipeline_model-cache` 卷；不要在两个服务上同时运行同一任务。

若环境已准备好，手工部署等价于：

```bash
export APP_UID="$(id -u)" APP_GID="$(id -g)"
stat -c '%u:%g %a %n' ./data  # 不自动更改现有资料权限
docker compose config
docker compose build trans
docker compose up -d trans
docker compose ps               # 等待 (healthy)，启动瞬间的 curl connection reset 是竞态
curl --retry 8 --retry-delay 2 --retry-connrefused --fail http://127.0.0.1:8080/api/health
```

后续：`docker compose up -d trans`；查看 `docker compose logs -f trans`；更新 `docker compose build trans && docker compose up -d trans`；正常停止 `docker compose down`。**不要用 `docker compose down -v`**（会删模型缓存卷）。`restart: unless-stopped` 只处理容器进程异常退出，`unhealthy` 不会自动触发重启；也不负责启动 Windows、WSL 或 Docker daemon。关终端后进程可能继续运行，系统重启后能否启动取决于 WSL/Docker 的宿主启动配置。

如果 GPU 尚未就绪，可以仅在独立临时目录做 **CPU 接口验收**：通过一次性 Compose override 清除设备 reservation、设 `TRANSCRIPT_PROFILE=local_cpu` 并将数据挂到独立目录；勿改默认 `compose.yaml` 以伪造 GPU 成功。镜像构建、无 GPU 的健康/上传测试不等于 GPU 验收。

镜像的 Node stage 执行 `npm ci && npm run build`，运行镜像不包含 Node 开发依赖。基础镜像锁定 Node 22 和 Python 3.12 bookworm 的 digest；`docker/constraints.txt` 固定 CTranslate2 4.8.2、cuBLAS-cu12 12.9.2.10、CUDA NVRTC-cu12 12.9.86、cuDNN-cu12 9.26.0.51；这些是本次构建验证的 Linux x86_64 Python wheels，不代表当前硬件已完成推理验收。基础 Whisper 环境不装 PyTorch；Compose 默认启用的 Qwen worker 在独立环境安装 PyTorch。镜像不装 Linux NVIDIA 驱动或完整 CUDA 工具链。运行镜像包含 `ffmpeg`、`poppler-utils`、`curl`、`ca-certificates`、`libgomp1`。日志 `json-file` 每文件最多 10 MiB、保留 3 个；`stop_grace_period: 30s`，`init: true`。视频/PDF 临时工作文件在容器可写层或项目路径，需为 Docker 存储预留空间；不默认使用大容量 tmpfs。

## 存储与权限

| 位置 | 内容 | 生命周期 |
| --- | --- | --- |
| 宿主 `./data` ↔ `/app/data` | 上传、任务、中间结果、输出，包含 `data/jobs/frontend-settings.json` | bind mount；`down`/重建不删除 |
| Compose 命名卷 `transcript-pipeline_model-cache` ↔ `/home/app/.cache/transcript-pipeline` | `profile.cache_dir` 指定的目录；其下 `faster-whisper/` 是 `asr.model_cache_subdir`，保存模型 | `down`/重建不删除；`down -v` 删除 |
| `/home/app` | 容器固定 HOME，进程以非 root `app` 运行 | 未单独挂载，非模型缓存内容不保证持久 |

只设置 `HF_HOME` 不会持久化本项目的模型：真实路径由当前 profile 的 `cache_dir` 加 `asr.model_cache_subdir` 决定。**自定义 profile 若改了缓存路径，须同步修改 Compose 卷挂载**。设置页保存的 profile 优先于环境 `TRANSCRIPT_PROFILE=wsl2_gpu_high_accuracy`；启动后检查 `/api/frontend-settings` 返回的 `profile`，默认模型为 `large-v3-turbo`。不要把曾在宿主 `~/.cache/transcript-pipeline` 下载的模型直接当作容器命名卷已缓存；需要时先评估只读迁移或重新下载，避免误覆盖。

初次部署先检查 `stat -c '%u:%g %a %n' ./data`，按容器 UID/GID 创建**新的**验收数据目录并限定权限，例如 `mkdir -m 700 /tmp/transcript-docker-accept-data` 并在仅用于验收的 Compose override 中挂到 `/app/data`（确保它由构建使用的 UID 持有，勿放到 Docker 构建上下文内）。现有 `./data` 属于用户资料，不自动递归 `chown`；若不可写，管理员应针对必要目录授予该 UID 的 ACL/权限，并检查其上级目录可遍历。新卷通常继承镜像中 `/home/app/.cache/transcript-pipeline` 的属主；如既有卷无法写入，先备份/核对卷名，再针对**卷根**初始化属主（例如用一次性 root 容器挂载该命名卷 `chown UID:GID /cache`，不执行递归修改用户 data）。定期只备份实际需要的 `./data` 和模型卷；设置文件及备份属于敏感数据。

镜像内默认 `config/settings.yaml` 不依赖宿主 config。需要自定义时在本机创建 `compose.override.yaml`（不要提交真实凭据），只读挂载单个设置文件；额外资料也应显式只读挂到 `/media/...`，任务使用**容器路径**。示例：

```yaml
services:
  trans:
    environment:
      TRANSCRIPT_SETTINGS_PATH: /app/config/custom.yaml
    volumes:
      - ${CUSTOM_SETTINGS_FILE:?set an absolute path outside this repository}:/app/config/custom.yaml:ro
      - /mnt/d/reading-material:/media/reading-material:ro
```

先 `export CUSTOM_SETTINGS_FILE=/absolute/path/outside/repository/settings.yaml`；对应文件必须事先存在且容器用户可读，不放入 Docker 构建上下文。切勿把整个宿主项目挂到 `/app` 覆盖镜像。历史 `data/jobs` 中保存的宿主绝对路径在容器内通常不可重用；使用隔离验收目录新建任务，不改写旧状态/上游产物。

## 互联网 CPA / API 网关与局域网访问

**CPA / API 网关部署在外部，本机不随此项目部署它。** 浏览器只请求同源 `/api`，后端经普通 bridge 出站 HTTPS。打开现有网页「运行设置」填写远端 **HTTPS Base URL（服务根地址或带 `/v1` 的地址）**、CPA 客户端 API Key（不是管理密钥），按需切换已有「API 直连（绕过代理）」开关并保存；数据位于 `/app/data/jobs/frontend-settings.json`。不必填 `.env`。阶段 6 和 OCR 默认均使用标准 `/v1/responses`，模型 ID 须匹配 CPA 提供的模型；配置段和 Web 字段保留旧名，迁移时手动更新已保存的连接和模型。可选使用宿主传入的 `CODEX_LB_BASE_URL`/`CODEX_LB_API_KEY` 环境变量（名称保留以兼容旧部署），但避免 shell 历史、Compose 展开输出和日志暴露密钥；网页保存的值会覆盖环境默认。本地地址（包括默认示例 `http://127.0.0.1:8317` 和旧地址 `http://127.0.0.1:2455`）在容器中指向容器自己，不能用于远端服务；可选 `codex_cli`、`agy` 等依赖额外 CLI 登录的后端未默认打包，默认远端路径是 `codex_api`。不加 host 网络、`host.docker.internal`、codex-lb 端口映射或默认宿主代理。

检查实际容器环境中是否**意外**存在 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY`（仅显示变量名，勿打印密钥），以及 DNS、TLS 证书、出站策略。确需代理才单独显式配置；直连开关只对 API 网关 host 临时追加 NO_PROXY，不是关闭所有代理。不要用 `curl -k` 或关闭 TLS 验证。缺 Key 或远端不可达时页面及 `/api/health` 仍应正常，相关任务应报错；认证用实际获批业务请求验证，HTTP 401 不是认证成功、首页 200 不是 API 成功。**真实付费请求必须先取得许可，构建/启动/健康检查都不会调用远端。**

局域网 HTTP 输入的 Key 在传输中未加密，仅用于可信局域网；响应仍脱敏，但仓库 `data/jobs/frontend-settings.json` 存明文 Key，限制权限并勿提交到 Git。无公网部署、无内建认证或 TLS。Compose 默认将宿主端口发布到 `0.0.0.0`，**这不意味着另一台设备已能访问**：

- WSL NAT：Windows localhost 转发一般支持本机 `http://localhost:8080`，其他 LAN 设备可能还需 Windows `portproxy` 指向 WSL IP（IP 可能变化）及 Windows 防火墙规则。
- WSL mirrored：按实际 Windows/WSL 版本检查 Windows/Hyper-V 防火墙、入站规则与地址；不套用 NAT 的固定转发步骤。
- 两种模式均由管理员先评估暴露范围再设置网络/防火墙；项目不会自动修改。最终从**另一台局域网设备**访问 Windows 宿主可达地址的 `/`、`/api/health` 和一个已授权非敏感页面；localhost 不是 LAN 验收。

## GPU 与任务状态的现场验收顺序

1. WSL 中 `/usr/lib/wsl/lib/nvidia-smi` 看到 GPU。
2. Toolkit/CDI 修复后用官方 CUDA **devel 验收容器**实际运行计算核（不在应用镜像安装工具链）：

   ```bash
   docker run --rm -i --gpus all nvidia/cuda:12.8.1-devel-ubuntu24.04 bash -s <<'SH'
   cat >/tmp/add.cu <<'CU'
   #include <cstdio>
   #include <cuda_runtime.h>
   __global__ void add(int *p) { *p += 42; }
   int main() {
     int *p=nullptr;
     if (cudaMallocManaged(&p,sizeof(int)) != cudaSuccess) return 1;
     *p=0; add<<<1,1>>>(p);
     if (cudaDeviceSynchronize() != cudaSuccess) return 2;
     printf("GPU result=%d\n",*p);
     int ok=(*p==42); cudaFree(p); return ok?0:3;
   }
   CU
   nvcc -o /tmp/add /tmp/add.cu && /tmp/add   # 期望 GPU result=42
   SH
   ```
3. `docker compose exec trans /app/.venv/bin/python scripts/check_docker_gpu.py`：CTranslate2 报 CUDA device_count≥1，支持 float16；没有 GPU 明确失败，不回退 CPU。
4. 将**已获授权的短语音**单独挂到 `/media/sample.wav`，执行 `docker compose exec trans /app/.venv/bin/python scripts/check_docker_gpu.py --audio /media/sample.wav --model small`。脚本必须实际消费 faster-whisper segments 且检查非空文本；首次会下载 small 模型，预留网络/空间。不得拿只创建生成器当作转录成功。
5. 用同一短样例改 `--model large-v3-turbo` 做默认模型/profile 验收，并再次执行确认模型已在卷中、没有重复下载（比对缓存文件/下载日志）。若用户在前端另存了 profile，先核对实际 profile。
6. 使用项目现有转录入口对**隔离数据目录**运行（例如 `scripts/02_transcribe.py`，按其参数使用容器内路径/profile），检查 JSON 的 `segments`、`device=cuda`、`compute_type=float16` 和 TXT 实际内容；勿重跑用户完整资料或覆盖已有上游产物。

停止/异常退出：`ThreadPoolExecutor.shutdown(wait=False)` 不会杀死运行中的线程；30 秒仅是停止宽限，超时容器会被强制终止。原有任务状态修正会在下一次查询时把不在进程内的 `pending/running` 标成 `failed`（参考 OCR 可 `partial`），保留已有结果但**不保证自动续跑或自动重试远端/GPU 作业**。须在独立数据目录测试实际长任务的 SIGTERM 与强制退出、恢复后状态及输出；勿使用真实付费任务来造故障。`unhealthy` 本身不触发 Compose 的 restart policy。权限失败、GPU 缺失和远端不可达各自查看任务错误和容器日志，避免用 `/api/health` 成功来推断业务可用。
