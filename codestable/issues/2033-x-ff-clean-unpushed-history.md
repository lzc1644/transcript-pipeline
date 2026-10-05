---
type: ff
status: closed
---

# 清理未推送历史中的大文件

- 用户授权备份后合并本地 3 个未推送提交，避免普通 main 推送携带曾加入后删除的影视视频/音频；不推送、不改远端历史。
- 已 fetch origin，确认 origin/main 是旧 HEAD 的祖先；旧历史备份为 `backup/pre-push-cleanup-20261005-102022`，从 origin/main 重新整理一个提交。
- 保留最终业务代码、测试和清理记录；取消跟踪 `tmp/refine_locked_quote_replay/` 与 `frontend/test-results/.last-run.json`，本地文件保留。
- `.gitignore` 增加 `tmp/` 与 `frontend/test-results/`，防止临时产物再次误入提交；未清理远端早已跟踪的其他 tmp 样例。
- 验证：`.venv/bin/python -m pytest`，576 passed；工作区与索引 diff 检查通过。提交后检查 main 新增对象和本地模拟推送包，不执行真实 push。
- 对 `codestable/` 的影响：无 spec 变化，仅增加本记录。备份分支仍保留大对象，禁止使用 `git push --all` 将备份一并推送。
