# 新电脑安装入口

- **原生 Ubuntu + NVIDIA Docker：** 先运行 `bash scripts/deploy_docker_linux.sh --check`，按 [DOCKER_LINUX.md](DOCKER_LINUX.md) 核对信息不足/阻塞项，再批准安装及部署。Docker Engine 直接运行在 Ubuntu 上，不使用 WSL2 / Docker Desktop，本轮不提供无 Docker 的 Ubuntu 安装方案。
- **WSL2 Docker：** 见 [DOCKER.md](DOCKER.md)，保留原 `deploy_docker_wsl2.sh` 入口。
- **WSL2 本地开发环境：** 以下内容及 `install_wsl2_env.sh` 仅适用于 WSL2，勿直接用于原生 Ubuntu。

## WSL2 项目环境安装

以下只说明 WSL2 Linux 内部的项目环境安装。

Windows 宿主机侧的 WSL2、显卡驱动、系统功能启用暂不由本项目脚本处理。进入本步骤前，请先确保已经能打开 WSL2 发行版。

默认推荐发行版是 `Ubuntu-24.04`，原因是项目要求 Python 3.12，Ubuntu 24.04 可以直接通过系统包满足。

## 1. WSL2 Linux

首次打开 `Ubuntu-24.04` 后，按提示创建 Linux 用户。

建议把仓库放在 Linux 文件系统中，不要放在 `/mnt/c/...`：

```bash
mkdir -p ~/code
git clone <你的仓库地址> ~/code/transcript-pipeline
cd ~/code/transcript-pipeline
```

然后执行：

```bash
bash scripts/install_wsl2_env.sh
```

脚本会处理：

- 安装 `git`、`ffmpeg`、`poppler-utils`、`curl`、`nodejs`、`npm`、Python venv 等系统工具
- 创建 `.venv`
- 安装 `requirements.txt`
- 安装 `nvidia-cublas-cu12` 与 `nvidia-cudnn-cu12`
- 将 CUDA/cuDNN wheel 动态库路径写入 `~/.bashrc`
- 执行基础命令检查
- 执行 Python 包导入检查
- 执行 WSL `nvidia-smi` 与 CTranslate2 CUDA 检查
- 执行 `.venv/bin/python -m pytest`
- 执行 `npm run build`

没有 NVIDIA GPU 或只想安装 CPU 环境：

```bash
bash scripts/install_wsl2_env.sh --cpu-only
```

常用参数：

```bash
bash scripts/install_wsl2_env.sh --skip-tests
bash scripts/install_wsl2_env.sh --skip-frontend-build
bash scripts/install_wsl2_env.sh --skip-gpu-check
bash scripts/install_wsl2_env.sh --no-bashrc
```

## 2. 安装后启动

在 WSL 项目根目录执行：

```bash
./start_web.sh
```

浏览器打开：

```text
http://127.0.0.1:5173
```

如果要给同一局域网内其他设备访问：

```bash
./start_web.sh --lan
```

WSL2 NAT 网络下，启动脚本会打印 Windows 管理员 PowerShell 中需要执行的 `netsh portproxy` 和防火墙命令。执行后，局域网其他设备访问 Windows 主机的局域网 IP 和前端端口。

## 3. API 配置

阶段 3 PDF OCR 和阶段 6 精修默认使用 `codex_api`。安装脚本只检查本地运行环境，不会替你写入 API key。

在 WSL shell 中配置：

```bash
export CODEX_LB_BASE_URL="http://127.0.0.1:2455"
export CODEX_LB_API_KEY="你的 codex-lb API key"
```

远程反代时：

```bash
export CODEX_LB_BASE_URL="https://你的反代域名"
export CODEX_LB_API_KEY="你的 codex-lb API key"
```

`CODEX_LB_BASE_URL` 填项目根地址，不要带 `/v1`。

## 4. 判断安装成功

安装完成后至少应满足：

- `ffmpeg -version` 正常
- `pdftoppm -v` 正常
- `.venv/bin/python -m pytest` 通过
- `npm run build` 通过
- GPU 环境下 `nvidia-smi` 正常
- GPU 环境下 CTranslate2 能发现 CUDA 设备
- `./start_web.sh` 能启动 Web UI

如果只是 CPU 环境，则 GPU 两项不要求通过。
