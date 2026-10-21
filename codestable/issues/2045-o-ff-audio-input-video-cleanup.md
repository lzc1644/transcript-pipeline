---
kind: issue
title: 支持音频输入并在任务成功后清理视频
type: ff
status: open
created: 2026-10-21
---

# 支持音频输入并在任务成功后清理视频

- 做了什么：单任务、批量目录和音频抽取文件模式支持配置中的视频与音频格式，统一经现有 ffmpeg 生成 WAV。保留 `video` / `videos_dir` / `--video` 等旧字段和路径；`/api/config` 向后兼容新增 `media_extensions`，原 `video_extensions` 含义不变。
- 清理边界：同一 helper 覆盖 CLI、Web 单任务、批量子任务及 Web 成功重跑；最终产物复制成功且 job 内非空音频存在后，只删除当前 job 的规范视频副本及 `data/uploads/videos/` 中的对应上传源。不删除外部本地原件、音频、参考源或文字；失败/partial/交付失败不清理，清理 OSError 只警告，重复清理可用。解析路径防止跟随符号链接删除外部原件；不追溯清理历史任务。保留音频可供 transcribe 及以后阶段重跑，重新抽取需再上传视频。
- 改了哪些：`src/ffmpeg_utils.py`、`src/job_runner.py`、`src/web/{uploads,tasks,stage_file_runs}.py`、`api_server.py`；前端配置类型/组合函数、单任务/批量/阶段页面与状态卡；三个入口脚本帮助、README 和阶段计划；新增 `tests/test_media_inputs.py`，更新 API 配置断言。手机实测发现模式按钮组固定高度遮挡第二行，局部改为自适应高度。
- 怎么验证：最终 `.venv/bin/python -m pytest` **712 passed / 33.91s**（新增 28 项，覆盖上传、混合目录、路径归属、符号链接、各入口成功/失败/partial/交付失败/清理告警及成功重跑）；前端类型检查/生产构建和 `git diff --check` 通过。真实已有录音只读截取 4 秒，制作 MP3 与 MP4，经真实上传 API 和 `scripts/01_extract_audio.py` 得到 16 kHz/单声道/16-bit WAV（4.000 / 4.032 秒）；显式交付文本夹具后验证视频两份删除、音频保留。宿主无 ffmpeg，隔离容器仅借用现有镜像 ffmpeg，未重启服务。真实浏览器验证 MP3 上传、手机直接选择对谈模式、混合目录上传/预检计数 2 与桌面/手机无横向溢出；已读取截图。证据：`tmp/media-input-validation-FR2O7v/{report.json,ui-report.json,*-extract.log,*.png}`。0 次 ASR / OCR / 远端模型调用，不将交付夹具说成真实转录。
- 对 codestable 的影响：无现行 Project Spec / Epic 需要回写；仅新增本快改记录，阶段 1 边界同步 `docs/PIPELINE_PLAN.md`。实现验证完成，待用户实际使用确认；本次按用户要求提交 Git commit，不部署、不推送。
