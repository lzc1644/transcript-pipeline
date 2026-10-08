---
kind: issue
title: "API 网关从 codex-lb 迁移到 CPA 标准 Responses 接口"
type: ff
status: open
created: 2026-10-08
---

# CPA 标准 Responses 接口适配

校对与已有 PDF OCR 默认统一调用 `/v1/responses`；Base URL 支持服务根地址、`/v1`、尾斜杠及反代前缀。适配集中在既有 client/settings；校对 trace 继续复用同一 URL 拼接函数，不增加业务模块网关判断。

- 改动：`src/codex_lb_client.py`、`src/schemas.py`、`config/settings.yaml`、`frontend/src/views/SettingsView.vue`；更新连接文档及配置/校对/OCR/trace 测试，新增 `tests/test_api_gateway.py`。
- 兼容：保留 `codex_lb` 配置段、Web 字段、`CODEX_LB_*` 环境变量及显式旧端点；不覆盖保存的地址、密钥、模型或历史任务，不改变中间 JSON。
- 验证：`.venv/bin/python -m pytest`：592 passed；`npm --prefix frontend run build` 通过（既有包体积提示）；`git diff --check` 通过。按 `docs/REVIEW_CHECKLIST.md` 自查范围、环境、产物兼容与验证边界。
- 本地运行：`tmp/cpa-format-validation/run_validation.py` 启动本地 HTTP 测试网关，实际执行 `scripts/06_refine.py`，并对有效单页 PDF 使用真实 pdfinfo/pdftoppm 后运行 OCR 调用；校对/OCR 均请求 `/v1/responses`，Bearer、SSE、输出落盘及 trace 通过。证据：`tmp/cpa-format-validation/summary.json`、`data/intermediate/refined/sample.json`（均在该验证目录）。返回文本是测试网关固定数据，不代表真实模型识别质量。
- 待确认：未调用远端 CPA 或付费模型；需用户更新 Web 地址/key，确认 CPA `/v1/models` 的模型 ID，并获准后做小样本端到端验收。保留 open，不宣称实际部署已验收。
- codestable：无现有 project spec 需要同步；历史 codex-lb 排错记录保留，不回写为 CPA 事实。无新阶段、并发/重试变更或部署操作。
