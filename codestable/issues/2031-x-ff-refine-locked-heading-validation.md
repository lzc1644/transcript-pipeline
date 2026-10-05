---
kind: issue
title: 阶段六锁定标题被 Markdown 转换误删导致校验失败
type: ff
status: closed
created: 2026-10-05
---

# 阶段六锁定标题校验修复

任务 `9a0768426d09` 的四次请求均 HTTP 200、`response.completed`，但被拒为 `locked_quote_changed`。模型把三个锁定标题排成 Markdown 标题，既有 `markdown_to_plain_text` 丢弃标题行，制造原文缺失；不是 PDF 换页标记导致失败。

- 做了什么：锁定原文比较保留标题文字，只去 Markdown 标记；普通转换默认行为不变，仍拒绝实际实词删除、改写、插入及段内调序。未修改预替换阈值或 `job_runner`，不加 PDF 特判。
- 改动：`src/refine_utils.py` 保留锁定标题，并把 `locked_quote_changed` 传入交付原因和针对性重试提示；`src/request_trace.py` 消费 `refinement_reason`，归类为 `locked_quote_validation` 并指向既有校验证据，不再误报 backend。
- 测试：新增 `tests/test_refine_locked_quotes.py`，覆盖六级标题、引用、标点、真实内容修改、一次接受、重试成功、持续失败及诊断归因；定向 76 passed；`.venv/bin/python -m pytest` 全量 576 passed；`git diff --check` 通过。
- 真实验证：原任务 ASR、参考原文及四份原始 SSE 通过本地 HTTP 服务回放，执行 `scripts/06_refine.py`，均 exit=0、单次 accepted、retry=0；12 个锁定段全部保留，输出 Markdown 与对应历史模型结果逐字相同。没有新模型调用、ASR 或 OCR。证据、配置、CLI 日志及独立输出在 `tmp/refine_locked_quote_replay/`，未改原任务 state 或上游 TXT（哈希确认）。
- 质量边界：最新稿有 46 处 `needs_review_sections`，问答及讲解仍存在明显 ASR 损坏，需要人工核听；只证明程序误拒绝已修复，不宣称可直接发布。线上 Docker 尚未部署或重试任务。
- codestable：无现有 spec 需要同步；补充本条修复记录。不改中间 JSON 主结构、重试次数、网络协议、PDF/OCR 规则，不自动回填历史诊断。
