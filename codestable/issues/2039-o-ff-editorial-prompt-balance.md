---
kind: issue
title: 校对提示词兼顾自然编辑与语义保真
type: ff
status: open
created: 2026-10-09
---

# 校对提示词兼顾自然编辑与语义保真

依据 fcb933b77a18 单 ASR、90ed921097cc 双 ASR 与用户未人工修改的 Chat 稿对比，吸收自然分段、机械重复清理和主动纠错，避免否定反转、漏细节、抹平调侃、重排朗读与讲解。没有将本次录音的具体答案写进通用模板；实现与离线链路验证完成，真实模型编辑效果待用户运行验收。

- 改动：`config/prompts/final_cleanup.md`、`conversation_cleanup.md`；新增 `tests/test_cleanup_prompts.py`。业务政策仍由两个现有 Markdown 模板拥有，不新增 Python 分支，不改 JSON 接口或模型设置。
- 核心边界：高置信音近＋句法＋全文证据可以主动采用候选，不要求逐字唯一还原；歧义才局部留缺并记录来源。纠错、重复清理及参考/OCR 纠错仅限未锁定输入，`locked_quote` 保持实词与顺序，冲突进入复核，不能自行解锁；工程误锁 OCR 问题本轮未修。
- 验证：`.venv/bin/python -m pytest`：650 passed，新增 22 个测试覆盖两模板、三输入模式、三个后端的政策传递与原始证据保留、无样例答案硬编码及锁定实词校验；`git diff --check` 通过。
- 真实材料离线运行：`tmp/prompt-editorial-validation/` 隔离复用上述两个任务的 ASR／参考，执行真实 `scripts/06_refine.py`，接收端是本机历史固定响应。单、双稿各一次请求、exit=0，新模板逐字进入请求，锁定和 JSON 兼容；原任务文件哈希未变。0 次新 ASR/OCR 或远端模型调用，不据回放输出声称语义质量改善。
- codestable：现有能力边界及文档仍成立，无 spec 需要同步；仅新增本条快改记录，不关闭既有事项。不部署、不重启、不改历史输出、不自动 commit/push；下一步由用户手动只重跑阶段 6 检查新的编辑效果。
