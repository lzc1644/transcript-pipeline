#!/usr/bin/env bash
# Native Ubuntu only. Reuse the application image/Compose; never touch GPU drivers.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOCKET_PATH=/var/run/docker.sock
ENDPOINT=unix:///var/run/docker.sock
CHECK_ONLY=0
ASSUME_YES=0
INCOMPLETE=0
MISSING=0
ENGINE_MISSING=0
USE_SUDO=0
SERVER_ID=''

usage() {
  cat <<'USAGE'
用法：bash scripts/deploy_docker_linux.sh [--check | --yes]
  --help   显示帮助
  --check  只读预检；不提权、安装、改配置、启动服务、拉镜像或创建目录/容器
  --yes    明确批准安装缺失依赖、配置/按需重启共享 Docker、构建及更新 trans
默认须交互输入 DEPLOY；更新已有 trans 前必须自行确认任务结束，healthy 不代表空闲。
仅支持原生 Ubuntu 22.04/24.04/26.04、x86_64、systemd、NVIDIA、本机 rootful Docker Engine。
不支持 WSL、Docker Desktop、远程或 rootless Docker；WSL 请用 deploy_docker_wsl2.sh。
环境变量：APP_UID、APP_GID、APP_PORT、DOCKER_BIND_IP、TRANSCRIPT_PROFILE、ASR_BACKENDS。
ASR_BACKENDS：qwen（默认，保留 Whisper）、whisper、funasr、all；默认模型不变。
--check 退出码：0 预检通过（不代表 GPU 推理验收）；2 信息不足；1 明确阻塞/缺少依赖。
不安装驱动/宿主 CUDA，不升级系统、卸载软件、改用户组/防火墙、chown data 或删除历史资料。
USAGE
}
fail() { printf '[BLOCKED] %s\n' "$*" >&2; exit 1; }
info() { printf '[INFO] %s\n' "$*"; }
unknown() { printf '[INCOMPLETE] %s\n' "$*"; INCOMPLETE=1; }
missing() { printf '[MISSING] %s\n' "$*"; MISSING=1; }

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
[[ $EUID -ne 0 ]] || fail '请以普通 Ubuntu 用户运行；必要步骤才使用 sudo'
[[ $(uname -sr) != *[Mm]icrosoft* && $(uname -sr) != *WSL* ]] \
  || fail '检测到 WSL；请使用 scripts/deploy_docker_wsl2.sh'
[[ $(uname -m) == x86_64 ]] || fail '本轮仅支持 x86_64'
# shellcheck disable=SC1091
source /etc/os-release
[[ ${ID:-} == ubuntu ]] || fail '本轮仅支持原生 Ubuntu'
case "${VERSION_ID:-}:${VERSION_CODENAME:-}" in
  22.04:jammy|24.04:noble|26.04:resolute) ;;
  *) fail '未验证此 Ubuntu 版本/代号；不会替换发行版代号绕过软件源限制' ;;
esac
command -v systemctl >/dev/null && [[ $(ps -p 1 -o comm=) == systemd ]] \
  || fail '需要 systemd 管理的本机 Docker Engine'
engine_unit="$(systemctl show docker --property=LoadState --value)" || fail '无法读取 Docker systemd 服务'
case "$engine_unit" in
  loaded) ;;
  not-found) ENGINE_MISSING=1; missing 'Docker systemd 服务未安装；批准后才安装 Engine' ;;
  *) fail 'Docker systemd 服务不可用；先人工修复，不覆盖现有服务' ;;
esac
command -v nvidia-smi >/dev/null && nvidia-smi -L \
  || fail '宿主 nvidia-smi 不可用；先由管理员核实 Linux NVIDIA 驱动，本脚本绝不安装或替换驱动'
[[ -f "$ROOT/Dockerfile" && -f "$ROOT/compose.yaml" ]] || fail '缺少 Dockerfile/compose.yaml'
[[ -d "$ROOT/data" && -w "$ROOT/data" && -x "$ROOT/data" ]] \
  || fail '现有 ./data 不可写/遍历；请人工核对权限，不自动 chown 或创建目录'
export APP_UID="${APP_UID:-$(id -u)}" APP_GID="${APP_GID:-$(id -g)}"
export APP_PORT="${APP_PORT:-8080}" DOCKER_BIND_IP="${DOCKER_BIND_IP:-0.0.0.0}"
export TRANSCRIPT_PROFILE="${TRANSCRIPT_PROFILE:-wsl2_gpu_high_accuracy}"
[[ $APP_UID =~ ^[0-9]+$ && $APP_GID =~ ^[0-9]+$ ]] && ((10#$APP_UID > 0 && 10#$APP_GID > 0)) \
  || fail 'APP_UID/APP_GID 必须为非 root 的数字 UID/GID'
[[ $APP_PORT =~ ^[0-9]+$ ]] && ((10#$APP_PORT >= 1 && 10#$APP_PORT <= 65535)) \
  || fail 'APP_PORT 必须在 1..65535'
info "data 权限: $(stat -c '%u:%g %a %n' "$ROOT/data"); 目标 UID/GID: $APP_UID:$APP_GID"
if [[ $APP_UID != "$(id -u)" || $APP_GID != "$(id -g)" ]]; then
  unknown '目标 UID/GID 不同于当前用户；须人工核实 data 及所需子目录权限，脚本不提权试写/递归修改'
fi

# Resolve the CLI's effective endpoint BEFORE pinning it. DOCKER_CONTEXT takes
# precedence over DOCKER_HOST. No values from these settings are printed (URLs
# can contain credentials). Explicit --host keeps sudo's context out of all ops.
if command -v docker >/dev/null 2>&1; then
  if [[ -n ${DOCKER_CONTEXT:-} ]]; then
    endpoint="$(docker context inspect "$DOCKER_CONTEXT" --format '{{.Endpoints.docker.Host}}')" \
      || fail 'DOCKER_CONTEXT 不可读取'
  elif [[ -n ${DOCKER_HOST:-} ]]; then
    endpoint="$DOCKER_HOST"
  else
    context="$(docker context show)" || fail 'Docker context 不可读取'
    endpoint="$(docker context inspect "$context" --format '{{.Endpoints.docker.Host}}')" \
      || fail 'Docker endpoint 不可读取'
  fi
  [[ $endpoint == "$ENDPOINT" || $endpoint == unix:///run/docker.sock ]] \
    || fail '拒绝非本机 rootful socket：不支持远程 Docker、Desktop 或 rootless endpoint'
else
  [[ -z ${DOCKER_CONTEXT:-} && ( -z ${DOCKER_HOST:-} || ${DOCKER_HOST} == "$ENDPOINT" || ${DOCKER_HOST} == unix:///run/docker.sock ) ]] \
    || fail '缺少 Docker CLI，无法核实指定 context/endpoint；先人工处理连接设置'
  missing 'Docker CLI 不存在；批准后才安装官方 Docker Engine/Compose'
  if systemctl is-active --quiet docker; then
    unknown 'Docker 服务已运行但 CLI 缺失；daemon、runtime、已有容器检查未完成，须先人工修复 CLI'
  fi
fi

dkr() {
  local prefix=()
  ((USE_SUDO)) && prefix=(sudo)
  "${prefix[@]}" env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH \
    APP_UID="$APP_UID" APP_GID="$APP_GID" APP_PORT="$APP_PORT" DOCKER_BIND_IP="$DOCKER_BIND_IP" \
    TRANSCRIPT_PROFILE="$TRANSCRIPT_PROFILE" ASR_BACKENDS="$ASR_BACKENDS" \
    docker --host "$ENDPOINT" "$@"
}
dc() {
  # Keep service/volume identity stable; do not let COMPOSE_FILE or sudo select
  # another project. This entrypoint uses the base Compose, without overrides.
  dkr compose --project-directory "$ROOT" --project-name transcript-pipeline -f "$ROOT/compose.yaml" "$@"
}
verify_daemon() {
  local details daemon_id os_name security
  details="$(dkr info --format '{{.ID}}|{{.OperatingSystem}}|{{json .SecurityOptions}}')" \
    || fail '本机 Docker daemon 信息不可读取'
  IFS='|' read -r daemon_id os_name security <<< "$details"
  [[ -n $daemon_id ]] || fail 'Docker daemon ID 缺失'
  [[ $os_name != *Docker\ Desktop* && $security != *rootless* ]] \
    || fail '实际 daemon 是 Docker Desktop/rootless，停止'
  [[ -z $SERVER_ID || $SERVER_ID == "$daemon_id" ]] \
    || fail '提权/启动后 Docker daemon ID 改变，停止以免操作错误 daemon'
  SERVER_ID="$daemon_id"
  info "已核实本机 rootful Docker: $os_name"
}
inspect_services() {
  local legacy existing
  legacy="$(dkr ps -a --filter 'label=com.docker.compose.project=transcript-pipeline' \
    --filter 'label=com.docker.compose.service=app' --format '{{.Names}} {{.Status}}')" \
    || fail '旧 app 服务检查失败'
  [[ -z $legacy ]] || fail "检测到旧 app 服务：$legacy。先确认任务结束，再人工停止/移除；不自动处理、不使用 --remove-orphans"
  existing="$(dkr ps -a --filter 'label=com.docker.compose.project=transcript-pipeline' \
    --filter 'label=com.docker.compose.service=trans' --format '{{.Names}} {{.Status}}')" \
    || fail 'trans 服务检查失败'
  if [[ -n $existing ]]; then
    info "已有 trans：$existing"
    info '更新可能重建容器并中断内存中的任务，须先确认任务结束；healthy 不代表没有运行中任务'
  fi
}
find_toolkit() {
  CTK="$(command -v nvidia-ctk || true)"
  if [[ -z $CTK ]]; then
    for CTK in /usr/bin/nvidia-ctk /usr/sbin/nvidia-ctk /usr/local/bin/nvidia-ctk; do
      [[ ! -x $CTK ]] || return 0
    done
    CTK=''
    return 1
  fi
}
package_installed() {
  [[ $(dpkg-query -W -f='${Status}' "$1" 2>/dev/null || true) == 'install ok installed' ]]
}

if command -v docker >/dev/null 2>&1; then
  if dkr compose version >/dev/null 2>&1; then
    info "Compose: $(dkr compose version --short)"
  else
    missing 'Compose plugin 不可用；批准后检查安装来源并补齐'
  fi
  if dkr info >/dev/null 2>&1; then
    ((!ENGINE_MISSING)) || fail 'daemon 可访问但 docker.service 缺失；先人工核实运行方式，不安装第二套 Engine'
    verify_daemon
    inspect_services
    if dkr info --format '{{json .Runtimes}}' | grep -q '"nvidia"'; then
      info 'Docker 已注册 NVIDIA runtime（仍须单独验收 GPU）'
    else
      missing 'Docker 尚未注册 NVIDIA runtime；批准后备份并配置 daemon'
    fi
  elif systemctl is-active --quiet docker; then
    if [[ -S "$SOCKET_PATH" && ! -w "$SOCKET_PATH" ]]; then
      unknown 'Docker 服务已运行，但当前用户无权访问；daemon、runtime、已有容器检查未完成。批准部署后可使用 sudo docker，不自动加入 docker 组'
    else
      fail 'Docker 服务已运行但无法连接；先排查 daemon/socket，不盲目重装'
    fi
  else
    unknown 'Docker daemon 未运行；runtime、已有容器检查未完成，批准后才允许启动'
  fi
fi
if find_toolkit; then
  info "NVIDIA Toolkit 可执行程序: $CTK"
elif package_installed nvidia-container-toolkit; then
  unknown 'Toolkit 软件包已安装但 nvidia-ctk 不可发现；请核实安装路径，不重复安装冒充修复'
else
  missing '未发现 nvidia-ctk，dpkg 也未登记 Toolkit；批准后才安装（不等于已检查 Docker runtime）'
fi
info "环境: Ubuntu $VERSION_ID ($VERSION_CODENAME) / 原生 x86_64; 服务: trans"
info "ASR_BACKENDS=$ASR_BACKENDS; profile=$TRANSCRIPT_PROFILE（Web 保存设置可能优先）；默认模型不变"
info "端口 $APP_PORT，绑定 $DOCKER_BIND_IP；默认无登录/HTTPS，不应暴露公网"
if ((CHECK_ONLY)); then
  info '只读预检结束；没有提权、安装、修改配置、启动服务、拉取镜像或创建目录/容器'
  if ((INCOMPLETE)); then exit 2; fi
  if ((MISSING)); then exit 1; fi
  info '预检通过；不代表容器 GPU 计算、Whisper/Qwen 推理或 LAN 验收通过'
  exit 0
fi

info '部署可能安装缺失软件、备份/修改 /etc/docker/daemon.json 并重启共享 Docker（影响其他容器）'
info '若存在 trans，更新可能重建并丢失内存中的任务；请先确认任务已结束'
if ((!ASSUME_YES)); then
  [[ -t 0 ]] || fail '非交互环境须显式 --yes；未确认，没有执行宿主更改'
  printf '批准以上操作并确认任务已结束后，输入 DEPLOY：'
  read -r answer
  [[ $answer == DEPLOY ]] || fail '未确认；没有执行宿主更改'
fi
command -v sudo >/dev/null || fail '缺少 sudo'
cd "$ROOT"

# Refuse conflicts rather than automatically replacing installed container tools.
apt_install() { sudo apt-get --no-remove install -y --no-upgrade "$@"; }
prepare_downloads() {
  if ! command -v curl >/dev/null || ! command -v gpg >/dev/null; then
    sudo apt-get update
    apt_install ca-certificates curl gnupg
  fi
}
prepare_docker_repo() {
  prepare_downloads
  # Exact suite; a missing Release is a blocker, never substitute noble.
  curl -fsSL "https://download.docker.com/linux/ubuntu/dists/$VERSION_CODENAME/Release" \
    -o "$tmp_dir/docker-release" || fail "官方 Docker 软件源不提供 $VERSION_CODENAME；停止，不替换代号"
  if [[ ! -e /etc/apt/sources.list.d/docker.sources && ! -e /etc/apt/sources.list.d/docker.list ]]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o "$tmp_dir/docker.asc"
    sudo install -m 0755 -d /etc/apt/keyrings
    sudo install -m 0644 "$tmp_dir/docker.asc" /etc/apt/keyrings/docker.asc
    printf 'Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: %s\nComponents: stable\nArchitectures: amd64\nSigned-By: /etc/apt/keyrings/docker.asc\n' "$VERSION_CODENAME" \
      | sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null
  fi
}
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

if ((ENGINE_MISSING)) || ! command -v docker >/dev/null 2>&1; then
  for pkg in docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc containerd.io docker-ce; do
    ! package_installed "$pkg" || fail "已有 $pkg；不自动卸载/替换，请管理员修复现有 Docker CLI/服务"
  done
  ! systemctl is-active --quiet docker || fail 'daemon 已运行但 CLI 缺失；先人工修复 CLI，不重装 Engine'
  prepare_docker_repo
  sudo apt-get update
  apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  sudo systemctl enable --now docker
fi
if ! systemctl is-active --quiet docker; then
  sudo systemctl start docker
fi
if ! dkr info >/dev/null 2>&1; then
  USE_SUDO=1
  dkr info >/dev/null 2>&1 || fail 'sudo 仍无法访问固定的本机 Docker socket；停止，不重装 daemon'
  info '使用 sudo docker 访问同一本机 socket；没有修改用户组'
fi
verify_daemon
inspect_services
if ! dkr compose version >/dev/null 2>&1; then
  package_installed docker-ce || fail '已有非 docker-ce Engine，缺少 Compose；请按原安装来源人工补齐，不替换软件包'
  prepare_docker_repo
  sudo apt-get update
  apt_install docker-compose-plugin docker-buildx-plugin
  dkr compose version >/dev/null 2>&1 || fail 'Compose 安装后仍不可用'
fi
if ! find_toolkit; then
  ! package_installed nvidia-container-toolkit || fail 'Toolkit 已安装但 nvidia-ctk 不可发现；请人工修复路径'
  prepare_downloads
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey -o "$tmp_dir/nvidia.gpg"
  sudo gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg "$tmp_dir/nvidia.gpg"
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    -o "$tmp_dir/nvidia-container-toolkit.list"
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    "$tmp_dir/nvidia-container-toolkit.list" \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
  sudo apt-get update
  apt_install nvidia-container-toolkit
  find_toolkit || fail '安装后 nvidia-ctk 仍不可发现'
fi
if ! dkr info --format '{{json .Runtimes}}' | grep -q '"nvidia"'; then
  if [[ -f /etc/docker/daemon.json ]]; then
    backup="/etc/docker/daemon.json.before-transcript-$(date +%Y%m%d%H%M%S)-$$"
    sudo cp -a /etc/docker/daemon.json "$backup"
    info "daemon 配置备份: $backup"
  fi
  sudo "$CTK" runtime configure --runtime=docker
  info '重启共享 Docker daemon（可能中断其他容器）'
  sudo systemctl restart docker
  verify_daemon
fi
dkr info --format '{{json .Runtimes}}' | grep -q '"nvidia"' \
  || fail 'Docker 未注册 NVIDIA runtime；不静默回退 CPU'
info '官方 CUDA 容器 GPU 可见性检查（非计算/推理验收）'
dkr run --rm --runtime=nvidia --gpus all nvidia/cuda:12.8.1-base-ubuntu24.04 nvidia-smi \
  || fail 'Docker GPU 检查失败；停止，不替换宿主驱动、不回退 CPU'
dc config --quiet || fail 'Compose 配置无效（不打印可能含密钥的展开配置）'
info '构建并启动 trans；不下载 ASR 模型、不调用业务 API'
dc build trans || fail 'trans 镜像构建失败'
dc up -d --no-build trans || fail 'trans 启动失败'
container="$(dc ps -q trans)"
[[ -n $container ]] || fail 'trans 容器未创建；检查 Compose 日志'
healthy=0
status=unknown
for ((attempt=0; attempt<75; attempt++)); do
  status="$(dkr inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$container")" \
    || fail 'trans 容器状态不可读取'
  if [[ $status == healthy ]]; then healthy=1; break; fi
  if [[ $status == unhealthy || $status == no-healthcheck ]]; then break; fi
  sleep 2
done
if ((!healthy)); then
  dc ps -a
  fail "trans 健康检查未通过（$status）；请查看 Compose logs --tail 80 trans，不要使用 down -v"
fi
dc exec -T trans /app/.venv/bin/python scripts/check_docker_gpu.py \
  || fail '容器 Whisper CUDA/float16 运行库检查失败；不回退 CPU'
dc ps
info "部署启动检查完成：端口 $APP_PORT。真实 GPU 计算、Whisper/Qwen 推理、持久化及 LAN 仍须分层验收"
