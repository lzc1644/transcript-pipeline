# 原生 Ubuntu + NVIDIA Docker 部署

本页的“原生 Linux”指 **Docker Engine 直接运行在 Ubuntu 宿主机，不经过 WSL2 / Docker Desktop**。复用仓库现有 Dockerfile 和 `compose.yaml`，仍然只有一个服务 `trans`，不提供完全不用 Docker 的 Ubuntu 安装方案。WSL2 用户请看 [DOCKER.md](DOCKER.md)。

## 支持范围与依赖链

本入口只支持 Ubuntu 22.04 / 24.04 / 26.04、x86_64、systemd（标准 `docker.service`）、NVIDIA GPU、**本机 rootful Docker Engine**。不宣称支持其他发行版、ARM、AMD GPU、rootless 或远程 daemon。

```text
Ubuntu Linux NVIDIA 驱动
→ Docker Engine
→ NVIDIA Container Toolkit / Docker NVIDIA runtime
→ 容器 CUDA 12 / cuDNN 9 运行库
→ Whisper / 独立 Qwen worker 推理
```

原生 Ubuntu 需要宿主 Linux NVIDIA 驱动；WSL2 依赖 Windows 驱动，不应在 WSL 安装 Linux 显卡驱动。两者的应用容器都不安装宿主驱动，也不需要在宿主安装完整 CUDA Toolkit。已有 `nvidia-smi` 正常时优先复用驱动；兼容性失败应停止排查，脚本不会擅自替换驱动。

### Ubuntu 26.04 的软件源

本轮核对的 [Docker 官方 Ubuntu 安装页](https://docs.docker.com/engine/install/ubuntu/)列出 **Resolute 26.04 LTS**；[NVIDIA Toolkit 支持表](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/supported-platforms.html)也列出 Ubuntu 26.04 x86_64。[Toolkit apt 安装说明](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)使用通用 `stable/deb` 源。

已有可用 Docker 时优先复用，不重新安装 Engine。需要补装时脚本检查官方源的**实际发行版** `dists/resolute/Release`，不会把 `resolute` 偷换为 `noble`。源不可达/缺包、已有冲突软件包，或发行版自带 Docker 缺少 Compose 时，停止并由管理员按原安装来源处理；不自动卸载 Docker/containerd/runc，不改用 convenience script。官方支持范围会变化，首次部署仍应核对上面两份文档；列入支持表不等于当前机器已通过 GPU 推理验收。

## 先只读检查，再批准部署

```bash
cd ~/code/transcript-pipeline
bash scripts/deploy_docker_linux.sh --help
bash scripts/deploy_docker_linux.sh --check
```

`--check` 不调用 sudo，不安装软件/改 daemon，不启动或重启服务，不拉镜像/创建容器或输出目录，不下载模型、不调用业务 API。检查平台、驱动、实际 Docker endpoint、Compose/daemon、Toolkit/runtime、`data` 权限与目标 UID/GID、旧 `app` 和已有 `trans`、ASR 构建选项。

| 退出码 | 含义 | 处理 |
| --- | --- | --- |
| `0` | 只读预检通过 | 仅说明可检查的先决条件具备，不是 GPU 计算/推理验收 |
| `2` | 信息不足，例如 socket 无权限、daemon 未运行、目标 UID/GID 不同 | 明确列出未完成项，不能声称全部通过 |
| `1` | 明确阻塞或缺少依赖，例如不支持的平台、远程 endpoint、驱动失败、旧 app、缺 Compose/runtime/Toolkit | 先处理阻塞；可补装的缺项需批准后部署 |

同时有缺项和未完成项时，返回 `2`，输出中仍列出已发现缺项；不支持的平台、远程 endpoint、旧 app 等明确危险条件直接返回 `1`。

当前机器只读预检：Ubuntu 26.04.1、RTX 3060 Ti 8 GB、宿主驱动 595.91.07 正常，Docker 服务运行，Compose 可用；普通用户无 socket 权限，**daemon/runtime/已有容器尚未核实，退出码应为 2**。PATH/常见路径未发现 `nvidia-ctk`，dpkg 未登记 `nvidia-container-toolkit`，但不能用此代替实际 runtime 检查。批准部署后才允许尝试 `sudo docker`；不自动加 docker 组。

确认影响后，首次建议仅绑定本机：

```bash
DOCKER_BIND_IP=127.0.0.1 \
TRANSCRIPT_PROFILE=wsl2_gpu_high_accuracy \
bash scripts/deploy_docker_linux.sh
# 阅读提示，确认已有任务结束后输入 DEPLOY

# 仅在管理员已明确批准时可改用 --yes；不要无意跳过确认
# DOCKER_BIND_IP=127.0.0.1 bash scripts/deploy_docker_linux.sh --yes
```

获批后才安装缺失的 Docker Engine/Compose、Toolkit，必要时备份 `/etc/docker/daemon.json`、注册 NVIDIA runtime 并重启共享 Docker，然后构建/更新 `trans`。其他容器可能受 daemon 重启影响。已有 runtime 时不反复重写配置/重启；已有 Engine 不自动升级。安装使用 `apt-get --no-remove ... --no-upgrade`，不执行 `apt upgrade`。

**不做：** 安装/升级/卸载宿主驱动、安装宿主 CUDA、卸载现有容器软件、改用户组、防火墙、递归 `chown data`、清理已有容器/卷/历史任务、启动付费任务。旧 `app` 容器即使已停止也阻止部署：先确认任务结束，再人工停止/移除旧容器（不带 `-v`），不要自动 `--remove-orphans`。已有 `trans` 更新可能重建并中断内存任务，**healthy 不代表空闲**，必须自行确认任务结束。

### Docker endpoint 与 sudo

脚本按 Docker CLI 优先级检查有效连接：`DOCKER_CONTEXT` 优先于 `DOCKER_HOST`，否则读取当前 context。只允许 `/var/run/docker.sock`（接受等价 `/run/docker.sock`），拒绝远程、Desktop、rootless；可访问 daemon 时还检查操作系统、rootless 标记和 daemon ID。

所有实际 Docker/Compose 操作通过同一个封装，显式固定 `--host unix:///var/run/docker.sock`，不让 sudo 的默认 context 指向其他主机；提权/重启后再次检查 daemon ID。Compose 固定项目名 `transcript-pipeline` 和仓库的基础 `compose.yaml`，不读取 `COMPOSE_FILE`、`COMPOSE_PROJECT_NAME` 或 `compose.override.yaml` 来改变目标。自定义/隔离验收使用下文的**人工 Compose 命令**，不要拿部署脚本直接更新生产数据。

脚本只运行 `compose config --quiet`，不打印可能包含 API Key 的完整展开配置。sudo 路径显式保留下面六个非密钥选项；API Key 推荐在 Web 设置页配置，不作为安装脚本输入。

| 变量 | 脚本默认值 |
| --- | --- |
| `APP_UID` / `APP_GID` | 当前普通用户的 UID/GID；容器仍为非 root |
| `APP_PORT` | `8080` |
| `DOCKER_BIND_IP` | `0.0.0.0`；首次建议显式 `127.0.0.1` |
| `TRANSCRIPT_PROFILE` | `wsl2_gpu_high_accuracy` |
| `ASR_BACKENDS` | `qwen`（Whisper + Qwen）；也支持 `whisper` / `funasr` / `all` |

`data` 检查是当前用户对目录根的写入/遍历检查，不代表所有历史子目录都可写。自定义 UID/GID 与当前用户不同时预检返回信息不足，部署前人工核实相应目录权限；脚本不会提权试写或递归修复。现有 `./data` bind、`model-cache` 命名卷、非 root 用户及默认转录模型不变。

### Profile 与默认模型

`wsl2_gpu_high_accuracy` 是历史命名，配置只有通用 `cuda`、`float16`、`large-v3-turbo`、`/tmp` 和用户缓存路径，无 WSL 专属依赖，可直接用于 Ubuntu。本轮不新增 Linux profile、不重命名历史快照。

- `.env.example` 包含 `TRANSCRIPT_PROFILE=local_cpu`，**不能照抄后把手工 Compose 启动称为 GPU 部署**。本脚本显式导出 GPU 默认，优先于 `.env`；显式传入其他 profile 仍会保留，不能冒充 CUDA 推理通过。
- Web 保存的 profile 可能优先于环境变量。启动后核对运行设置及 `/api/frontend-settings` 中实际 `profile`；不要把含 Key 的原始设置文件粘贴到日志。
- `ASR_BACKENDS=qwen` 只是安装可选依赖，不切换默认转录模型。改变构建选项后必须任务结束再重新构建/创建容器，只重启不会补装依赖，详见 [ASR_BACKENDS.md](ASR_BACKENDS.md)。

## 分层验收：启动成功不等于推理成功

脚本自动检查官方 CUDA base 容器的 `nvidia-smi`、`trans` health 和 CTranslate2 CUDA/float16；**不自动执行计算核、下载模型或转录**。以下真实验收要另行批准，使用授权短音频，保留日志和 ASR JSON/TXT，不重跑整段录屏，不调用 OCR/精修 API。3060 Ti 只有 8 GB，模型**串行**验收；OOM 应报告，不静默回退 CPU。

### 1. 宿主与 Docker GPU

宿主 `nvidia-smi` 成功后，用官方 CUDA devel 验收镜像完成实际计算（不是给宿主/应用装 CUDA 编译工具链）：

```bash
# 当前用户无 socket 权限时，下文 Docker 命令使用 sudo，并仍固定本机 socket
sudo docker --host unix:///var/run/docker.sock run --rm -i --runtime=nvidia --gpus all \
  nvidia/cuda:12.8.1-devel-ubuntu24.04 bash -s <<'SH'
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
nvcc -o /tmp/add /tmp/add.cu && /tmp/add
SH
```

期望退出码 `0`、`GPU result=42`。镜像里的 Ubuntu 24.04 是容器用户空间，不是把 Ubuntu 26.04 宿主的软件源换成 noble。

### 2. 隔离应用、Whisper 与 Qwen

在**应用镜像已构建且另外获批真实验收后**，创建独立数据目录、独立项目/模型卷和验收日志。不要复制含明文 API Key 的现有设置进去。示例在仓库根运行：

```bash
export ACCEPT_ROOT="$(mktemp -d /tmp/transcript-gpu-accept.XXXXXX)"
export ACCEPT_PROJECT=transcript-whisper-accept
export APP_UID="$(id -u)" APP_GID="$(id -g)"
export APP_PORT=18080 DOCKER_BIND_IP=127.0.0.1 TRANSCRIPT_PROFILE=wsl2_gpu_high_accuracy
mkdir -m 700 "$ACCEPT_ROOT/data" "$ACCEPT_ROOT/logs"
mkdir -p "$ACCEPT_ROOT/data/input/audio"
cp /absolute/path/to/approved-short.wav "$ACCEPT_ROOT/data/input/audio/sample.wav"
cat >"$ACCEPT_ROOT/accept.yaml" <<'YAML'
services:
  trans:
    volumes:
      - ${ACCEPT_ROOT}/data:/app/data
YAML
# override 按容器 target 合并，替换 /app/data；项目名也隔离 model-cache 卷。
# 单实例验收；若有其他验收项目，请选择不同项目名/端口。
dc_accept() {
  sudo env ACCEPT_ROOT="$ACCEPT_ROOT" APP_UID="$APP_UID" APP_GID="$APP_GID" \
    APP_PORT="$APP_PORT" DOCKER_BIND_IP="$DOCKER_BIND_IP" TRANSCRIPT_PROFILE="$TRANSCRIPT_PROFILE" \
    docker --host unix:///var/run/docker.sock compose --project-name "$ACCEPT_PROJECT" \
    --project-directory "$PWD" -f "$PWD/compose.yaml" -f "$ACCEPT_ROOT/accept.yaml" "$@"
}
dc_accept config --quiet
dc_accept up -d --no-build trans
# 等待 healthy；本机页面 / 和 /api/health 均须成功
# dc_accept ps
curl --fail http://127.0.0.1:18080/api/health
curl --fail --output /dev/null http://127.0.0.1:18080/

dc_accept exec -T trans /app/.venv/bin/python scripts/check_docker_gpu.py \
  >"$ACCEPT_ROOT/logs/whisper-runtime.log" 2>&1
# 首次可能下载 small 权重；必须实际消费 segments 并得到非空文本
# 若失败，先检查退出码和日志，不继续冒充通过。
dc_accept exec -T trans /app/.venv/bin/python scripts/check_docker_gpu.py \
  --audio /app/data/input/audio/sample.wav --model small \
  >"$ACCEPT_ROOT/logs/whisper-small.log" 2>&1
# 默认模型的阶段入口，实际落盘 ASR JSON/TXT；只处理隔离目录的短音频
dc_accept exec -T trans /app/.venv/bin/python scripts/02_transcribe.py \
  --profile wsl2_gpu_high_accuracy --asr-candidate whisper-existing \
  >"$ACCEPT_ROOT/logs/whisper-default.log" 2>&1
dc_accept logs --no-color --tail 100 trans >"$ACCEPT_ROOT/logs/container.log"
```

逐条核对命令退出码；运行库应报告 CUDA device_count≥1、float16。检查 `$ACCEPT_ROOT/data/intermediate/asr/sample.{json,txt}` 的非空内容、`device=cuda`、`compute_type=float16` 和时间戳；只有创建 faster-whisper 生成器不能证明推理已运行。

如要宣称 Qwen 可用，另起**不同数据目录与独立项目**，沿用上面的 override 隔离方法（不能在 Whisper 同一个阶段工作区切换候选），先结束 Whisper 推理，再检查 worker：

```bash
# 仍在同一个仓库根目录 shell，保留上面的 dc_accept 函数
WHISPER_ACCEPT_ROOT="$ACCEPT_ROOT"
export ACCEPT_ROOT="$(mktemp -d /tmp/transcript-qwen-accept.XXXXXX)"
export ACCEPT_PROJECT=transcript-qwen-accept APP_PORT=18081
mkdir -m 700 "$ACCEPT_ROOT/data" "$ACCEPT_ROOT/logs"
mkdir -p "$ACCEPT_ROOT/data/input/audio"
cp /absolute/path/to/approved-short.wav "$ACCEPT_ROOT/data/input/audio/sample.wav"
cp "$WHISPER_ACCEPT_ROOT/accept.yaml" "$ACCEPT_ROOT/accept.yaml"
dc_accept config --quiet
dc_accept up -d --no-build trans
# 等待 healthy；安装了 qwen 或 all 构建选项才有此解释器
dc_accept exec -T trans /app/.venv/asr-py312/bin/python -c \
  'import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.device_count()); assert torch.cuda.is_available()' \
  >"$ACCEPT_ROOT/logs/qwen-worker-cuda.log" 2>&1
dc_accept exec -T trans /app/.venv/bin/python scripts/02_transcribe.py \
  --profile wsl2_gpu_high_accuracy --asr-candidate qwen3-asr-0.6b \
  >"$ACCEPT_ROOT/logs/qwen-inference.log" 2>&1
```

仍须保留 Qwen 日志、检查独立 JSON/TXT 的实际 CUDA 精度、非空文字及对齐；worker 包存在、CUDA 可见或 Whisper 成功都不等于 Qwen 推理成功。1.7B 若需使用须单独串行验收，不以 0.6B 通过推断所有模型都通过。

### 3. 持久化与网络

确认验收任务结束，再对**各自隔离项目** `dc_accept up -d --no-build --force-recreate trans`（`ACCEPT_ROOT`、`ACCEPT_PROJECT` 和端口要指向同一个验收项目）；比较重建前后 ASR 文件内容/校验和、模型缓存文件与日志，再运行同一短音频检查没有重复下载权重。保留验收目录及命名卷以便复核。不要使用 `docker compose down -v`，不要用删卷来模拟重建。卷的权限与自定义缓存路径注意事项见 [DOCKER.md 的存储说明](DOCKER.md#存储与权限)。

首次本机验收成功后，再独立批准 LAN 配置。Ubuntu 不需要 Windows `portproxy`、Hyper-V 防火墙或 WSL mirrored/NAT 配置；其他设备访问 **Ubuntu 主机的局域网 IP** 和发布端口。默认服务没有登录认证或 HTTPS，Key 在 HTTP LAN 上传输未加密，**禁止直接暴露公网**。

Docker 发布端口可能绕过 UFW 的常规规则，不能仅凭一条 `ufw allow` 或 `ufw status` 宣称访问已受限。由管理员按 [Docker 的端口过滤/UFW 说明](https://docs.docker.com/engine/network/packet-filtering-firewalls/)评估绑定地址和转发规则，本脚本不修改防火墙。必须从另一台设备验证 `/`、`/api/health` 可达性，并从不允许的来源验证限制效果；localhost 成功不是 LAN 验收。

## WSL → Ubuntu 迁移

先确认旧任务结束、备份，再逐项迁移。容器重建会中断内存任务，项目不保证自动续跑。

| 内容 | 处理办法 |
| --- | --- |
| 仓库代码 | Git 克隆或复制；不迁移旧 WSL `.venv`，容器复用同一构建定义 |
| `data/` | 保留目录结构，先备份再迁移；核对 UID/GID/所需目录权限，不自动递归 chown |
| Web 设置 `data/jobs/frontend-settings.json` | 含明文 API Key、profile、旧 API 地址；限制权限、勿提交 Git/贴日志，启动后核对实际 profile |
| 模型命名卷 | 从 WSL 的原生 Docker daemon 单独备份/恢复，或接受重新下载；不是仓库文件 |
| 旧绝对路径 | `/mnt/c/...`、`/mnt/d/...` 不会自动变为 Ubuntu 路径；新任务使用容器实际可访问的路径 |

**拷贝仓库不等于迁移 Docker 模型卷。** 默认生产卷名为 `transcript-pipeline_model-cache`，实际模型路径取决于 profile 的 `cache_dir` 和模型子目录。需要卷迁移时，先人工核对源/目标 daemon、卷名和空间，通过仅挂模型卷的一次性容器导出 tar，再在新主机空卷中恢复并核对属主（见 [Docker 卷备份/恢复说明](https://docs.docker.com/engine/storage/volumes/#back-up-restore-or-migrate-data-volumes)）；不要误覆盖已有目标卷，也不要导出正在写入的缓存。`docker compose down -v` 会删卷，**不要使用**。

不自动批量改写历史任务 JSON，不覆盖旧 ASR/其他上游产物；旧任务的配置快照和绝对路径需要人工复核。额外资料可显式只读挂载到 `/media/...`，不要将整个宿主仓库挂到 `/app` 覆盖镜像。容器内 `127.0.0.1` 是容器自己，**不是 Ubuntu 宿主机**；旧本机 API/代理地址不能照搬，远端 codex-lb 应配置实际 HTTPS 地址。详细密钥、缓存、停止恢复边界仍沿用 [DOCKER.md](DOCKER.md)。

## 代码验证与当前验收状态

开发测试使用项目 Python 3.12 `.venv`；Ubuntu 26.04 的系统 Python 版本不改变应用镜像的 Python 3.12。初始化测试 venv 后执行：

```bash
bash -n scripts/deploy_docker_linux.sh
.venv/bin/python -m pytest
```

`tests/test_docker_linux_deploy.py` 用命令桩模拟平台、socket、sudo、安装、runtime、构建、健康与 GPU 失败；不会真的安装软件/重启 Docker。命令桩通过只能证明脚本控制逻辑，不证明本机 GPU 可推理。

本轮实际只运行只读预检，**没有批准/执行宿主安装、daemon 修改、容器构建/启动或模型推理**。下一步先批准固定本机 endpoint 下的 `sudo docker` 只读核查，确认 runtime、Toolkit、旧服务和正在运行的任务，再决定补装及隔离分层验收。不能用这份部署文档或已有 WSL 验收记录宣称当前 Ubuntu 已部署成功。
