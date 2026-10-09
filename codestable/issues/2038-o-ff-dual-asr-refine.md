---
kind: issue
title: 同源双 ASR 联合校对与语义保真提示词
type: ff
status: open
created: 2026-10-09
---

# 同源双 ASR 联合校对与语义保真提示词

实现就绪，本地验证通过；按用户要求不运行新 ASR 或真实 GPT，等待用户手动运行与质量验收。

- 做了什么：增加可选 `asr.secondary_candidate`，默认单 ASR 不变；批处理复用 registry/worker/GPU 锁串行运行，主产物仍在顶层，辅助在 `secondary/<candidate>/`。配对先 pending，全部成功后原子发布 complete，记录音频及两个 JSON/TXT 的 SHA256；辅助失败/中断/旧配对/哈希变化均不能静默进入联合校对。
- 校对：`refine_inputs` 校验同源配对并按共同分钟窗装配全部原始候选；双 ASR 不做单稿 OCR 预替换，单 ASR 引用锁定不变。沿用 final_markdown 与旧 JSON 字段，新增来源数组、模式与配对运行身份。编辑政策由 Markdown 提示词唯一拥有，装配层只负责输入协议和接口契约，移除虚构跨模型审核及旧后置编辑限制。
- 改动：`src/asr_utils.py`、`src/asr/pairing.py`、`src/refine_inputs.py`、`src/refine_utils.py`；schemas/overrides/job 快照、CLI、API/Web 默认值/任务/产物及 ASR 选择器；两份 cleanup 提示词、文档和回归测试。单 TXT refine 文件模式显式关闭全局第二候选，显式双 ASR 请求报错；历史任务沿用组合。
- 验证：`.venv/bin/python -m pytest`：628 passed；`npm --prefix frontend run build` 与 `git diff --check` 通过。新增 24 个双 ASR 测试，覆盖串行隔离、辅助失败＋旧文件、pending/中断、文件篡改、同源/候选、来源记录、快照、文件模式边界与提示词单一所有者。
- 真实材料离线验证：`tmp/dual-asr-validation/` 复用任务 f30050364963/85f154653a51 的真实 ASR，确认音频同 SHA256，调用真实 `scripts/06_refine.py`，接收端为本机固定响应 API。一个请求包含完整两份原稿（42500 字符）并输出 dual_asr 来源；pending 重跑 exit=1 且无新增 API 请求。原历史 ASR 未修改，0 次新推理/远端模型调用。该验证不证明新校对的语义质量或 GPU 双模型运行已验收。
- codestable：无现有 project spec；已同步 README、`docs/ASR_BACKENDS.md`、`docs/PIPELINE_PLAN.md` 的已实现能力和待验收边界，历史事项不回写。没有部署、重启、改默认组合、自动融合、复杂对齐、新阶段、commit/push。
