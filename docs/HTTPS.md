# 一个镜像、一个容器：内网 HTTPS 听写

应用、Vue 前端、ASR 依赖和 Caddy HTTPS 已合进 **`transcript-pipeline:local` 一个镜像**。只启动 `trans`，不需要第二个 HTTPS 容器或 overlay。

当前地址：**https://192.168.0.252:8443/dictation**。原 `http://192.168.0.252:8080` 保留；内网 HTTP 的麦克风受浏览器限制，听写请使用 HTTPS，并在访问设备信任一次 CA。

## 日常启动和升级

本机 `.env` 保留两个非密钥参数，把示例 IP 换成固定的宿主内网地址：

```dotenv
HTTPS_ADDRESSES="https://192.168.0.252:8443 https://localhost:8443"
HTTPS_DEFAULT_SNI=192.168.0.252
```

不再需要 `COMPOSE_FILE=compose.yaml:compose.https.yaml`。确认已有任务结束后：

```bash
docker compose build trans && docker compose up -d --no-build --wait trans
```

如果镜像已经构建/导入，只需 `docker compose up -d --no-build --wait trans`。更新不重新生成 CA，不删除数据与模型缓存。默认端口8080 HTTP、8443 HTTPS；手工隔离部署可通过 `APP_PORT`、`HTTPS_PORT` 分别选择不同宿主端口，容器内仍是8000/8443。

Caddy 的站点配置已放在镜像 `/app/docker/Caddyfile`，无需再挂配置文件。它负责本地 CA 与证书自动续期，保留 Host、转发 HTTPS scheme 并支持 WSS。API 仍只有一个 Uvicorn worker，Caddy 经 loopback 转发；不放宽 forwarded-header 信任。

普通内网没有公网信任链，本地 CA 方案需要每台设备信任一次根证书。删除前端检查、点击证书警告“继续访问”、合并 Docker 镜像都不能代替信任。受管设备禁止用户 CA 时，应由管理员配置，或改用内部域名与有效公网证书。

## 访问设备只需导入一次根证书

服务器已导出公开证书：`data/https/transcript-lan-root.crt`。也可重新导出：

```bash
mkdir -p data/https
docker compose cp trans:/data/caddy/pki/authorities/local/root.crt data/https/transcript-lan-root.crt
```

仅把这个 `.crt` 文件通过可信方式传到访问设备，**不要传私钥**。浏览器完全退出并重开，再访问 HTTPS 地址并允许麦克风。

- **Windows Chrome / Edge**：双击证书 → 安装证书 → 当前用户 → 指定存储“受信任的根证书颁发机构”。也可在证书目录执行 `certutil -user -addstore Root .\transcript-lan-root.crt`。
- **Ubuntu**：浏览器证书管理中导入受信任 CA。若管理员明确希望全系统信任，可将证书安装到 `/usr/local/share/ca-certificates/transcript-lan-root.crt` 后运行 `sudo update-ca-certificates`。部分浏览器仍需单独导入。
- **Firefox**：若未沿用系统信任库，在“隐私与安全 → 证书 → 查看证书 → 证书颁发机构”导入并信任网站证书。
- **macOS**：钥匙串访问中导入 CA，在“信任”中选“始终信任”。
- **iOS/iPadOS**：安装描述文件后，还需在“通用 → 关于本机 → 证书信任设置”对该根证书开启完全信任。
- **Android**：在安全/凭据设置中选择“安装 CA 证书”。设备管理策略可能禁止用户 CA。

本次没有擅自修改宿主系统或用户现有浏览器的信任库。隔离 Chromium 的真实 HTTPS 验证不等于其他设备已安装证书。

IP 网址通常不发送 TLS SNI，`HTTPS_DEFAULT_SNI` 必须与公布的内网 IP 一致；建议固定 IP / DHCP 地址保留。默认方案只公布一个内网 IP。多个无 SNI 的 IP 需要同一张覆盖多个 IP 的证书，不能只增加站点别名。

## 用镜像文件导入，不必再次构建

已导出的 Linux amd64 镜像文件：

```text
tmp/releases/transcript-pipeline-single-20261022.tar.gz
```

在 Docker 中导入，并将其设为 Compose 使用的本地镜像：

```bash
docker load -i tmp/releases/transcript-pipeline-single-20261022.tar.gz
docker tag transcript-pipeline:single-20261022 transcript-pipeline:local
docker compose up -d --no-build --wait trans
```

镜像包含前端、API、ASR依赖与HTTPS运行组件；**不包含用户录音、API Key、CA私钥和模型缓存卷**。本机继续复用现有数据/模型。迁往新机器时按 [DICTATION.md](DICTATION.md) 准备模型，或安全迁移原数据与模型卷；不能只搬镜像就假定旧权重和会话也已迁移。

不用 Compose 时，等价的 `docker run` 必须保留关机期限与持久卷，例如：

```bash
docker run -d --name transcript-pipeline --init --restart unless-stopped \
  --gpus all --stop-timeout 160 --cap-drop ALL --security-opt no-new-privileges:true \
  -p 8080:8000 -p 8443:8443 \
  -e TRANSCRIPT_PROFILE=wsl2_gpu_high_accuracy \
  -e 'HTTPS_ADDRESSES=https://192.168.0.252:8443 https://localhost:8443' \
  -e HTTPS_DEFAULT_SNI=192.168.0.252 \
  -v "$PWD/data:/app/data" \
  -v transcript-pipeline_model-cache:/home/app/.cache/transcript-pipeline \
  -v transcript-pipeline_https-data:/data \
  -v transcript-pipeline_https-config:/config \
  transcript-pipeline:single-20261022
```

先确认同名容器/端口没有冲突。GPU runtime、数据 UID/GID 规则与 Compose 相同，不在此安装宿主驱动或修改防火墙。

## 从原来的两个容器迁移

当前机器已完成迁移。其他旧安装先确认任务结束，再：

1. 停止并移除**旧的** `transcript-pipeline-https-1`，不带 `-v`，保留 CA 卷。
2. 从本机 `.env` 的 COMPOSE_FILE 去掉 `compose.https.yaml`，或删除该 COMPOSE_FILE 行；保留 HTTPS 地址参数。
3. 构建/导入本单镜像，运行 `docker compose up -d --no-build --wait trans`。
4. 用原根证书验证 HTTPS。原来的 `https-data` / `https-config` 卷名与挂载路径保持一致，设备无需再次信任一个新 CA。

Linux 安装脚本发现旧 HTTPS 容器或旧 overlay 配置时会拒绝直接升级并提示迁移，不自动删除用户容器。迁移后基础 Compose 和安装脚本即可管理完整能力，不再需要组合 overlay。

## 进程、健康和停止由同一个容器负责

Supervisor 管理 API 与 Caddy。任意服务意外退出或启动失败，退出观察器会让 Supervisor 关闭另一服务，Docker 的 `unless-stopped` 再重启整个容器；不留下“HTTP 健康但 TLS 已死”的半工作状态。复制 Caddy 后清除其上游 file capability，8443 不需要特权，因此仍为非 root、`cap_drop: ALL`。

Dockerfile 与 Compose 都使用 `/bin/sh /app/docker/healthcheck.sh`，检查 HTTP 和**按持久 CA、配置主机名验证的 HTTPS**，不使用 `-k`。`HTTPS_ENABLED=false` 可显式禁用 TLS，此时只检查 HTTP；默认开启。

关机预算统一为：

- Uvicorn 现有 HTTP 请求最多等待 **5秒**，随后取消慢上传等请求并进入 lifespan shutdown。
- Service 同时取消后台工作与 runtime，等待已经开始的真实 HTTP 线程退出；支持的远端请求期限最大 **120秒**。
- 两个 worker 各有 **3+5秒** 退出余量；GPU 锁实际退出后才释放。
- Supervisor 给 API **145秒**，Compose / `docker run --stop-timeout` 给整个容器 **160秒**。

超出预算才有强杀兜底；旧读书会任务仍不承诺自动续跑，不要为测试强停真实付费任务。

## 验证与排错

```bash
curl --noproxy '*' --cacert data/https/transcript-lan-root.crt \
  --fail https://192.168.0.252:8443/api/health
docker compose ps
docker compose logs --tail 60 trans
docker compose exec trans /app/.venv/bin/supervisorctl -c /app/docker/supervisord.conf status
```

- `ERR_CERT_AUTHORITY_INVALID`：设备尚未信任 CA，或浏览器使用独立证书库/旧进程。
- `ERR_CERT_COMMON_NAME_INVALID`：访问地址与证书/default SNI 不一致。
- 连接超时：检查宿主地址、端口、网络/防火墙；不通过改 VAD 或模型参数排查网络。
- 浏览器控制台应显示 `window.isSecureContext === true`，并存在 `navigator.mediaDevices.getUserMedia`，随后允许麦克风。

CA 私钥只在 `transcript-pipeline_https-data` 卷中。**不要 `down -v` 或删除 CA 卷**，否则已安装的信任会失效；卷备份含私钥，按凭据保护。HTTPS 没有增加登录认证，仅用于可信内网，禁止直接暴露公网。

2026-10-22 验证包括：原CA通过单容器HTTPS校验；真实录音虚拟麦克风与WSS；API/TLS生命周期联动；35秒慢HTTP请求与未结束上传并发时TERM约35秒后正常退出、原稿/部分上传保留、GPU锁可重获，无Supervisor强杀。证据 `tmp/dictation-single-term/report.json` 与 `tmp/dictation-single-lifecycle.log`。物理设备和人工转录质量验收仍按听写文档执行。
