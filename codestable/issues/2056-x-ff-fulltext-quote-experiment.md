---
kind: issue
title: 整篇原始转录与结构化引用隔离试验
type: ff
status: closed
created: 2026-10-31
---

# 整篇原始转录与结构化引用隔离试验

- 做了什么：用户批准后，对 `abf389f69a4b` 用同一 Gemini 3.8 Flash High/high/priority、原 ASR 和参考 TXT 完成一次真实整篇请求，去掉预替换锁定，返回带来源的 quote/speech/heading 后程序渲染。试验完成，但候选未通过交付验收；关闭本记录不表示新流程已验收或上线。
- 改了哪些：新增 `scripts/probe_fulltext_refinement.py` 与 `tests/test_fulltext_refinement_probe.py`；所有真实材料、旧/新稿、实际提示词、脱敏响应及检查报告位于 `tmp/fulltext-quote-experiment-20261031/`，详细结论见该目录 `report.md`。未改生产校对链、配置、原任务或导出接口。
- 怎样验证：`.venv/bin/python -m pytest -q` 920 passed，1条已有弃用警告；新增20项实验测试通过，`git diff --check`通过。真实请求约323秒，SSE完整完成；79段中35引用/42讲话/2标题。三处目标引用边界、插话及“突厥还没平定”均改善；仍把李石/长安京兆尹无依据改成裴延龄、李齐运，并有未复核数字冲突。来源记录缺359行“我们。”，结构检查主动拒绝（exit=2），未篡改候选补过校验。网关total_tokens 80920，对照历史稿44909，不能宣称更便宜/快。
- 对 codestable/ 的影响：现有阶段规格保持不变，本轮仅实验，无稳定能力变更。后续建议固定本次输入/协议，仅换原模型做对照；结构化新协议若进入主流程需单独授权、保持最终 Markdown 消费接口。没有新增 ASR/OCR、部署、重启、提交或推送。
