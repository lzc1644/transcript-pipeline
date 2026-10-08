---
kind: issue
title: WSL2 部署依赖可用性与入口同步
type: ff
status: open
created: 2026-10-08
---

# WSL2 部署依赖可用性与入口同步

- 做了什么：用户 WSL 构建日志显示 Qwen/Gradio 安装时固定 Ruff 没有可用发行包；0.16.9 满足 Gradio 的版本下限，官方 PyPI/本机原环境均有该版本，不能认定为数学上的版本冲突或确认远端网络根因。仅将非推理工具 Ruff 改为 `>=0.9.3,<0.17`，保留核心依赖锁定，optional SDK 安装增加 10 次连接重试/60 秒超时。WSL 入口统一普通/sudo 的默认 GPU profile，提醒已有 trans 更新前确认空闲，健康等待与 Linux 一致为 150 秒，构建失败明确停止而不执行 up。
- 改了哪些：`requirements-asr-constraints.txt`、`Dockerfile`、`scripts/deploy_docker_wsl2.sh`、`tests/test_docker_deploy.py`、`docs/DOCKER.md`、`docs/ASR_BACKENDS.md`。
- 怎么验证：`.venv/bin/python -m pytest` 601 passed；部署定向测试 90 passed；bash 语法、Compose config、diff 检查通过。临时 Docker 容器仅提供 Ruff 0.16.10 wheel 时，旧固定约束复现 ResolutionImpossible/no matching distributions，新范围下载成功；另一临时容器实际安装 0.16.10，基于已有 SDK 的 Qwen requirements dry-run 和两套 venv pip check 通过。证据目录 `tmp/wsl2_deploy_validation/`。首次安装验证因镜像默认非 root 无法写构建期 venv 失败，改为临时容器 `--user 0` 后通过，未修改宿主 venv。
- 对 codestable/ 的影响：补充 002 Docker / 007 Qwen 部署记录；无 Project Spec 可同步。部署默认依赖仍是 Whisper + Qwen，默认识别模型和中间产物结构不变。已按 `docs/REVIEW_CHECKLIST.md` 自评。
- 验证边界：本机为原生 Linux，真实 WSL 脚本 `--check` 正确拒绝；没有全新镜像完整构建、WSL 完整部署、GPU 推理或模型下载，没有重启/更新现有 trans、修改 daemon/驱动/网络、删除 data/缓存或调用付费 API。待用户 WSL 现场重新构建确认；事项保持 open，不将本机依赖验证等同现场部署成功。
