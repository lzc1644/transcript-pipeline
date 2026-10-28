---
kind: issue
title: 修复听写模块导致普通 HTTP 入口白屏
type: ff
status: closed
created: 2026-10-28
---

# 修复听写模块导致普通 HTTP 入口白屏

`dictationDocument.ts` 随 App 全局导入，顶层调用仅安全上下文提供的 `crypto.randomUUID()`，使保留的内网 HTTP 旧任务页也白屏。新增共用 UUID helper：优先原生 UUID，普通 HTTP 使用 `crypto.getRandomValues()` 生成同格式随机 UUID；窗口恢复标识、正文操作和本地录音草稿统一使用，不改变麦克风安全检查。

- 改动：`frontend/src/composables/{dictationId,dictationDocument,dictationDrafts}.ts`、`frontend/tests/{dictation-notebook.mjs,README.md}`。
- 验证：前端类型检查／build、`dictation-state.mjs` 通过；真实 Chromium notebook **20 项通过、0 页面异常**。`http://dictation-http.test` 实测 `isSecureContext=false`、原生 UUID／采音 API 缺失，旧任务页、历史编辑／保存／刷新／TXT 下载正常；上传检查真实 XHR 字节，响应为 fixture，无 ASR／付费调用。安全 localhost 完整编辑器／API／TXT 路径同轮通过；HTTP、桌面与手机截图已实际查看。证据 `tmp/voice-merge-review/http-fix-notebook-2/`，构建与状态日志同父目录。
- 汇总测试由主评审运行：`.venv/bin/python -m pytest` **864 passed / 58.95s**，`tmp/voice-merge-review/pytest.log`；保留既有 httpx 弃用与前端大 chunk 提示。
- codestable：无稳定规格变更，恢复 `docs/HTTPS.md` 的既定 HTTP 合同；原事项 `2046-o-two-pass-dictation.md` 保持 open。未部署、提交或推送；真实采音／识别质量继续由原事项验收。
