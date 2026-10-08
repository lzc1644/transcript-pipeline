#!/usr/bin/env bash
# Install the WSL-native Docker Engine and NVIDIA Container Toolkit when needed,
# then build and start the single Compose service. Never install a Linux GPU driver.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_ONLY=0
ASSUME_YES=0

usage() {
  cat <<'USAGE'
用法：bash scripts/deploy_docker_wsl2.sh [--check | --yes]
  --check  只读检查环境和旧容器，不安装、重启、构建或启动
  --yes    明确同意安装宿主软件、配置 daemon 并按需重启共享 Docker（供人工批准后使用）
环境变量 ASR_BACKENDS：qwen（默认，Whisper + Qwen）、whisper、funasr 或 all。
默认交互式确认，部署 Compose 服务 trans；更新前须确认任务已结束，healthy 不代表空闲。
默认 TRANSCRIPT_PROFILE=wsl2_gpu_high_accuracy（Web 保存设置可能优先）。
只支持 WSL2 内原生 Docker Engine；不改 Windows 驱动、网络或防火墙。
USAGE
}
fail() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }
info() { printf '[INFO] %s\n' "$*"; }

while (($#)); do
  case "$1" in
    --check) CHECK_ONLY=1 ;;
    --yes) ASSUME_YES=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; fail "未知参数: $1" ;;
  esac
  shift
done
((!CHECK_ONLY || !ASSUME_YES)) || fail '--check 与 --yes 不能同时使用'
export ASR_BACKENDS="${ASR_BACKENDS:-qwen}"
case "$ASR_BACKENDS" in
  whisper|qwen|funasr|all) ;;
  *) fail 'ASR_BACKENDS 必须是 whisper、qwen、funasr 或 all' ;;
esac
[[ $EUID -ne 0 ]] || fail '请以普通 WSL 用户运行，本脚本只在必要步骤使用 sudo'
[[ -r /proc/sys/kernel/osrelease ]] && grep -qi 'microsoft.*WSL2' /proc/sys/kernel/osrelease \
  || fail '只支持 WSL2；不能在 Windows、Docker Desktop 或普通 Linux 上运行'
[[ $(uname -m) == x86_64 ]] || fail '当前 Docker GPU wheels 仅针对 x86_64 验证'
# shellcheck disable=SC1091
source /etc/os-release
[[ ${ID:-} == debian || ${ID:-} == ubuntu ]] || fail '当前仅支持 Debian/Ubuntu WSL2'
[[ -n ${VERSION_CODENAME:-} ]] || fail '无法确定 apt 发行版代号'
command -v systemctl >/dev/null && [[ $(ps -p 1 -o comm=) == systemd ]] \
  || fail '需要 WSL2 systemd 管理的原生 Docker Engine'
[[ -x /usr/lib/wsl/lib/nvidia-smi && -e /dev/dxg ]] \
  || fail 'WSL 内未发现 GPU；先在 Windows 配置 NVIDIA 驱动与 WSL2，勿在 WSL 安装 Linux 驱动'
[[ -f "$ROOT/Dockerfile" && -f "$ROOT/compose.yaml" ]] || fail '缺少 Dockerfile/compose.yaml'
[[ -d "$ROOT/data" && -w "$ROOT/data" ]] || fail '现有 ./data 不可写：请人工核对 UID/权限，不自动 chown 用户数据'

if command -v docker >/dev/null 2>&1; then
  endpoint="$(docker context inspect "$(docker context show)" --format '{{.Endpoints.docker.Host}}' 2>/dev/null)" \
    || fail 'Docker context 不可读取；先人工确认 daemon 位置'
  [[ $endpoint == unix:///var/run/docker.sock ]] \
    || fail "当前 Docker context 是 $endpoint；拒绝修改 Desktop/远端 daemon"
  if docker info >/dev/null 2>&1; then
    os_name="$(docker info --format '{{.OperatingSystem}}')"
    [[ $os_name != *Docker\ Desktop* ]] || fail '当前连接到 Docker Desktop；不会修改它'
    info "现有本地 Docker: $os_name"
  elif systemctl is-active --quiet docker; then
    if [[ -S /var/run/docker.sock && ! -w /var/run/docker.sock ]]; then
      info '本用户暂不能访问 Docker socket；批准后将仅通过 sudo docker 执行，不自动授予 docker 组权限'
    else
      fail 'Docker 服务正在运行却无法连接：先排查 socket/daemon，勿盲目安装'
    fi
  else
    info 'Docker CLI 存在，但本地 daemon 未运行；经确认后将启动 systemd 服务'
  fi
else
  info "将为 $ID $VERSION_CODENAME 安装官方 Docker Engine/Compose"
fi
if command -v docker >/dev/null 2>&1; then
  if docker compose version >/dev/null 2>&1; then
    info "Compose: $(docker compose version --short)"
  else
    info 'Compose plugin 缺失，将在确认后安装'
  fi
fi
if command -v nvidia-ctk >/dev/null 2>&1; then
  info 'NVIDIA Container Toolkit 已安装'
else
  info 'NVIDIA Container Toolkit 缺失，将在确认后安装'
fi

# Renaming app -> trans must not launch a second server on port 8080 or interrupt
# a live in-memory job. Never remove or stop the legacy service automatically.
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  legacy="$(docker ps -a --filter 'label=com.docker.compose.project=transcript-pipeline' \
    --filter 'label=com.docker.compose.service=app' --format '{{.Names}} {{.Status}}')"
  if [[ -n $legacy ]]; then
    printf '[BLOCKED] 检测到旧 app 服务：%s\n' "$legacy" >&2
    fail '先确认没有运行中任务，再由用户自行停止/移除旧 app 容器；绝不自动停止、删除或使用 --remove-orphans。之后重新运行脚本。'
  fi
fi
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  existing="$(docker ps -a --filter 'label=com.docker.compose.project=transcript-pipeline' \
    --filter 'label=com.docker.compose.service=trans' --format '{{.Names}} {{.Status}}')"
  if [[ -n $existing ]]; then
    info "已有 trans：$existing"
    info '更新可能重建容器并中断内存中的任务；请先确认任务已结束，healthy 不代表空闲'
  fi
fi
info "环境: $ID $VERSION_CODENAME / WSL2; 目标 Compose 服务: trans"
info "ASR 依赖: $ASR_BACKENDS（所有选项均保留 Whisper；不会改变默认转录模型）"
info '将使用可信局域网端口 8080（默认 0.0.0.0）；不提供登录或 HTTPS，不应暴露公网'
if ((CHECK_ONLY)); then
  info '只读检查结束；没有安装软件、修改 daemon 或启动容器'
  exit 0
fi

info '请先确认已有 trans 的任务已结束；部署可能重建容器，也可能重启共享 Docker'
if ((!ASSUME_YES)); then
  [[ -t 0 ]] || fail '非交互环境必须由操作者显式传入 --yes，不能默许重启共享 Docker'
  printf '可能安装软件、修改 /etc/docker/daemon.json 并重启共享 Docker（影响其他容器）。确认后输入 DEPLOY：'
  read -r answer
  [[ $answer == DEPLOY ]] || fail '未确认；没有执行宿主更改'
fi

command -v sudo >/dev/null 2>&1 || fail '缺少 sudo'
cd "$ROOT"
# Bootstrap download tools only after explicit approval, even on a fresh distro.
if ! command -v curl >/dev/null 2>&1 || ! command -v gpg >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl gnupg
fi
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

if ! command -v docker >/dev/null 2>&1; then
  info '安装官方 Docker Engine + Compose plugin（不会安装 Docker Desktop）'
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl gnupg
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o "$tmp_dir/docker.gpg"
  sudo install -m 0644 "$tmp_dir/docker.gpg" /etc/apt/keyrings/docker.asc
  if [[ ! -e /etc/apt/sources.list.d/docker.sources && ! -e /etc/apt/sources.list.d/docker.list ]]; then
    printf 'Types: deb\nURIs: https://download.docker.com/linux/%s\nSuites: %s\nComponents: stable\nArchitectures: amd64\nSigned-By: /etc/apt/keyrings/docker.asc\n' "$ID" "$VERSION_CODENAME" \
      | sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null
  fi
  sudo apt-get update
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  sudo systemctl enable --now docker
fi
if ! systemctl is-active --quiet docker; then
  sudo systemctl start docker
fi

DOCKER=(docker)
USE_SUDO=0
if ! docker info >/dev/null 2>&1; then
  if sudo docker info >/dev/null 2>&1; then
    DOCKER=(sudo docker)
    USE_SUDO=1
    info '使用 sudo docker 访问本地 socket；没有修改用户组'
  else
    fail '本地 Docker daemon 不可访问；检查 Docker 服务，不打开远程 socket'
  fi
fi
[[ $("${DOCKER[@]}" info --format '{{.OperatingSystem}}') != *Docker\ Desktop* ]] || fail '实际 daemon 是 Docker Desktop，停止'
if ! "${DOCKER[@]}" compose version >/dev/null 2>&1; then
  info '安装缺失的 Docker Compose plugin'
  sudo apt-get update
  sudo apt-get install -y docker-compose-plugin docker-buildx-plugin
fi
# Recheck after Docker startup as the daemon may have been stopped during preflight.
legacy="$("${DOCKER[@]}" ps -a --filter 'label=com.docker.compose.project=transcript-pipeline' \
  --filter 'label=com.docker.compose.service=app' --format '{{.Names}} {{.Status}}')"
[[ -z $legacy ]] || fail "旧 app 容器仍存在：$legacy。确认任务完成后由用户手动停止/移除，再重跑脚本"

export APP_UID="${APP_UID:-$(id -u)}" APP_GID="${APP_GID:-$(id -g)}"
# Match the sudo path and native Linux entrypoint: a copied .env example must
# not silently select local_cpu for this GPU deployment.
export TRANSCRIPT_PROFILE="${TRANSCRIPT_PROFILE:-wsl2_gpu_high_accuracy}"
dc() {
  if ((USE_SUDO)); then
    # Only non-secret Compose knobs cross sudo; configure codex-lb in the Web UI.
    sudo env APP_UID="$APP_UID" APP_GID="$APP_GID" ASR_BACKENDS="$ASR_BACKENDS" \
      APP_PORT="${APP_PORT:-8080}" DOCKER_BIND_IP="${DOCKER_BIND_IP:-0.0.0.0}" \
      TRANSCRIPT_PROFILE="${TRANSCRIPT_PROFILE:-wsl2_gpu_high_accuracy}" docker compose "$@"
  else
    docker compose "$@"
  fi
}

if ! command -v nvidia-ctk >/dev/null 2>&1; then
  info '安装 NVIDIA Container Toolkit（不是 Linux 显卡驱动）'
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl gnupg
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey -o "$tmp_dir/nvidia.gpg"
  sudo gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg "$tmp_dir/nvidia.gpg"
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    -o "$tmp_dir/nvidia-container-toolkit.list"
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    "$tmp_dir/nvidia-container-toolkit.list" \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y nvidia-container-toolkit
fi

if ! "${DOCKER[@]}" info --format '{{json .Runtimes}}' | grep -q '"nvidia"'; then
  info '注册 NVIDIA runtime；先备份已有 daemon.json'
  if [[ -f /etc/docker/daemon.json ]]; then
    sudo cp -a /etc/docker/daemon.json "/etc/docker/daemon.json.before-transcript-$(date +%Y%m%d%H%M%S)"
  fi
  sudo nvidia-ctk runtime configure --runtime=docker
  info '即将重启共享 Docker daemon（可能中断其他容器）'
  sudo systemctl restart docker
fi

"${DOCKER[@]}" info --format '{{json .Runtimes}}' | grep -q '"nvidia"' \
  || fail 'Docker 未注册 NVIDIA runtime；停止，勿以 CPU 模式冒充 GPU 部署'
info '官方 CUDA 容器 GPU 可见性测试（不下载 ASR 模型）'
"${DOCKER[@]}" run --rm --runtime=nvidia --gpus all nvidia/cuda:12.8.1-base-ubuntu24.04 nvidia-smi

dc config --quiet
info '构建镜像并后台启动 trans（不调用 codex-lb、不下载模型）'
dc build trans || fail 'trans 镜像构建失败；请检查上方 pip/软件源/网络错误。未更新现有容器，不会回退为 Whisper 或 CPU'
dc up -d --no-build trans
container="$(dc ps -q trans)"
[[ -n $container ]] || fail 'trans 容器未创建；检查 docker compose ps -a'
healthy=0
status=unknown
for ((attempt=0; attempt<75; attempt++)); do
  status="$("${DOCKER[@]}" inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$container")"
  if [[ $status == healthy ]]; then healthy=1; break; fi
  if [[ $status == unhealthy || $status == no-healthcheck ]]; then break; fi
  sleep 2
done
if ((!healthy)); then
  dc ps -a
  fail "trans 健康检查未通过（$status）；查看 docker compose logs --tail 80 trans。不要用 down -v"
fi
info '检查容器 CTranslate2 CUDA 能力（不会推理或下载模型）'
dc exec -T trans /app/.venv/bin/python scripts/check_docker_gpu.py
dc ps
info '部署完成：访问 http://<可信局域网主机>:${APP_PORT:-8080}；真实 GPU 转录和远端 API 仍须单独验收'
