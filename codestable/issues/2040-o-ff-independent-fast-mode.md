---
kind: issue
title: PDF OCR 与 AI 精修独立快速模式及前端布局
type: ff
status: open
created: 2026-10-10
---

# PDF OCR 与 AI 精修独立快速模式及前端布局

实现与验证已完成；现有 Web 后端仍是旧进程，待用户重启后使用。请求快速服务已验证，不代表实际上游加速已验收。

- 做了什么：独立 `fast_mode` / `ocr_fast_mode` 开关默认关闭，仅对应 API 请求添加 `service_tier: priority`；不改模型、推理强度、提示词、并发。显式 false 优先、任务快照/补页沿用原值，旧任务默认关闭，成功页内容指纹不变。
- 前端：单/批任务开关放到配置方案下；配置方案增加准确的 Whisper 参数提示；移除服务选择器并显式提交 codex_api；设置页保存两个默认值，PDF/单阶段仅显示相关开关，历史任务网页重跑明确走 API。
- 改动：schemas/settings_overrides、web models/frontend_settings/tasks/pdf_book_ocr、api_server、reference_utils/refine_utils、config/settings.yaml；前端 client/composable、五个视图、ProfileSelector/JobStatusCard、新 FastModeSwitch、main.css；README 与相关测试。
- 验证：新增 7 个关键测试用例；`.venv/bin/python -m pytest` 657 passed；前端 vue-tsc/Vite 构建、`git diff --check` 通过（保留既有 >500 kB 包体积提示）。按 `docs/REVIEW_CHECKLIST.md` 自查范围、环境、产物兼容与证据。
- 页面：实际检查用户已打开的 localhost:5173，桌面 1440px/窄屏 390px，位置、开关独立、对谈隐藏 OCR、PDF 页只显示 OCR、无横向溢出/运行错误通过；未提交网页任务或保存用户设置。证据 `tmp/fast-mode-validation/ui-summary.json` 与两张截图。
- 真实样例：沿用 f30050364963 的真实 PDF 第 1 页、360 字符已有 ASR 和参考摘录，经现有 CPA 共 4 次模型调用，0 次新 ASR/自动重试。普通 OCR 18.47s 成功；普通精修在样例 120s 上限超时；快速 OCR 40.25s 成功；快速精修实际 `scripts/06_refine.py` 173.84s 成功（样例上限 240s），trace 确认 priority。输出已读取，有需要人工复核的精修标注。证据 `tmp/fast-mode-validation/sample-summary.json`、`fast/page.txt`、`fast/workspace/refined_dir/sample.json`。不能据此证明加速，未继续做性能测试。
- 待确认：CPA 模型列表可访问且现有两模型可用，但未获取版本/管理配置，无法核实全局 payload.override；后台 8000 尚未加载新字段，需要重启。应用没有改写网关配置、重启服务、提交或推送。
- codestable：无现有 project spec 需要同步；README 已补使用契约，历史事项不回写。留 open，等待现场使用及上游快速服务效果确认。
