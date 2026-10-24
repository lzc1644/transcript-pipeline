---
kind: issue
title: 接入 Gemini 模型并分开选择语音文字处理模型
type: ff
status: closed
created: 2026-10-24
---

# 接入 Gemini 模型并分开选择语音文字处理模型

阶段 6 校对默认改用 `gemini-3.8-flash-high`、PDF OCR 默认改用 `gemini-3.5-flash-lite`（`medium`）；现有网页运行设置也同步为这组模型，阶段 6 思考强度为 `high`。语音输入保持 Paraformer/Qwen 识别不变，新会话分别选择逐句快速处理与全文整理的模型、思考强度；省略时仍是 Luna `low` / Luna `high`。发现原 OCR 清洗会丢弃英文-only 页及行首英文，最小修复后保留可见文字。

- 改动：两份 YAML、`src/schemas.py`、设置/阶段运行/语音输入前端、`src/dictation/{schemas,service,cleanup}.py`、`src/web/dictation.py`、`src/reference_utils.py`，以及对应测试和使用文档；语音输入跟踪事项仍为 `2046-o-two-pass-dictation.md`。
- 验证：`.venv/bin/python -m pytest` 817 passed；前端类型检查/build、浏览器桌面与 390px 模型选择和布局检查通过。真实网关中 Gemini 3.8 `high` 完成阶段 6 小样本，Gemini 3.5 `medium` 完成一页 PDF OCR；语音输入逐句 Lite `medium` / 全文 3.8 `high` 均得到可解析结果。样本留在 `tmp/model-choice-*`。
- codestable：同步 `docs/PIPELINE_PLAN.md` 和 `docs/DICTATION.md`；不关闭语音输入事项。未部署、提交或推送，已运行服务需在获授权后更新才显示新控件。
