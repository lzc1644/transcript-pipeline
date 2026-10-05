---
type: ff
status: closed
---

# 清理已移除的影视转录模块残留

- 用户要求删除影视转录模块的全部相关改动；检查发现当前源码、API、前端入口及文档已无该模块实现，不回滚共用 ASR 功能。
- 清理 `.gitignore` 中两个专用数据目录的规则；保留工作区原有 `data/film_jobs/`（244 文件）、`data/film_uploads/`（43 文件）删除状态，未恢复这些数据。
- 删除模块专用缓存、前端测试截图，以及 `data/output/logs/film-*` 下的调试/验证产物；重新构建 `frontend/dist/`，移除旧构建中的影视转录入口。
- 验证：`.venv/bin/python -m pytest`，576 passed；`npm --prefix frontend run build` 成功（仅包体积警告）；`git diff --check` 通过。
- 运行当前应用冒烟检查：`/api/health`、`/api/config`、`/` 均返回 200，无 film API 路由；未重跑 ASR 或已删除模块的样例。
- 对 `codestable/` 的影响：无相关 spec 需要同步，仅增加本清理记录。未改写 Git 历史，未自动暂存、提交或推送。
