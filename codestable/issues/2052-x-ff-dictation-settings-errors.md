---
kind: issue
title: 听写前端改版与设置弹窗错误可见性修复
type: ff
status: closed
created: 2026-10-25
---

用户授权审核 agy 前端改动、修复发现的问题并保存一个 commit。本次保留设置弹窗、浏览器偏好持久化、编辑交互及响应式样式改版；将错误提示置于弹窗固定页脚，关闭弹窗后回到页面显示，避免错误被遮罩挡住或重复播报。

- 改动：`frontend/src/views/DictationView.vue`、`frontend/src/components/RecordingDraftPane.vue`；在既有 `frontend/tests/dictation-layout.mjs` 增加桌面/手机超长术语失败、错误实际无遮挡、修正后保存及关闭后错误保留检查。
- 验证：`.venv/bin/python -m pytest`：856 passed，1 条既有依赖弃用警告；`npm --prefix frontend run build` 通过，保留既有大 chunk 提示；浏览器布局回归全部通过，截图与报告在 `tmp/agy-settings-fix-01/`，已检查桌面/手机错误截图。按 `docs/REVIEW_CHECKLIST.md` 自评；浏览器会话为模拟数据，未调用 ASR/LLM。
- codestable：无稳定规格变更，原听写事项 2046 保持 open。本次仅提交前端、回归测试及本记录；工作区 `src/dictation/cleanup.py` 提示词修改保留、不纳入提交。未部署或 push。
