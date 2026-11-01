---
kind: issue
title: 正式校对直接使用整篇原始ASR，取消预替换与锁定
type: ff
status: closed
created: 2026-11-01
---

# 整篇原始ASR校对入口

- 做了什么：按用户授权将试验中的无预处理方式接入正式单次校对。单ASR直接提交原始全文和参考，不先匹配、预替换、生成SEGMENT或锁定引用，也不再按规则锁定文字触发回退；双ASR继续提交已验证同源候选。旧式显式分段装配/锁定工具保留兼容，不在正式默认路径调用。新增的原始全文参数为可选，不改变旧调用方的原有契约；独立文字入口直接提供参考字符串也会正确传入。
- 改了哪些：`src/llm/tasks/refinement.py`、`config/prompts/final_cleanup.md`；更新`tests/test_cleanup_prompts.py`、`tests/test_refine.py`、`tests/test_refine_locked_quotes.py`、`tests/test_llm_capabilities.py`；同步README、`docs/PIPELINE_PLAN.md`、`docs/BACKEND_MODULES.md`和后端边界note。五个模型返回字段不变，单ASR策略标记改为single_pass_raw_asr；未运行的自动保真检查明确写checked=false/ passed=null，不冒充通过，旧诊断读取仍兼容。
- 怎么验证：`.venv/bin/python -m pytest -q`983 passed、1条已有依赖弃用警告。真实样本abf389f69a4b经正式入口做离线接线检查：17126字符ASR、5252字符参考完整传入，SEGMENT为0，预替换和锁定校验调用均0，后端fixture一次，远端模型调用0；仅验证输入线路，不是新模型校对结果。初次离线检查暴露独立入口无文件路径时漏传参考字符串，已修复并在本地协议测试补回归。证据在`tmp/production-raw-asr-20261101/`，模型质量按用户计划构建后手测。
- 对codestable/的影响：已同步默认输入契约，后端职责不变；ASR/OCR、输出主结构、双ASR配对门禁、多后端候选、格式校验/重试及导出保持。未回写上游、调用新模型、部署或混入已有网关/试验CLI调试改动。此记录关闭工程接线任务，不宣布语义质量或此前API断流已解决。
