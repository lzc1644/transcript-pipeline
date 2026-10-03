---
kind: issue
title: Qwen generation 避免长上下文无用 logits 的显存峰值
type: ff
status: open
created: 2026-10-03
---

# Qwen generation 避免长上下文无用 logits 的显存峰值

用户 Docker 任务 `fce08a52af66` 使用 Qwen 1.7B，在原音频 `[35.344,65.344]` 秒分块失败：SDK 的 `lm_head(hidden_states)` 为全部 prefill 位置投影词表，申请 736 MiB 时 CUDA OOM。不是驱动/Toolkit 缺失。任务包含 564 个术语（2489 字符），未删减术语或改用较小模型/CPU。

- 做了什么：在现有 generation 包装中，临时给 ASR thinker 的 `lm_head` 注册 pre-hook，只投影最后一个 token；generation 完成或异常后自动移除。自回归 next-token 使用的信息不变，ForcedAligner/完整 forward、token 上限检查、FP16、30 秒分块与术语保留。metadata 的 `resolved_parameters` 增加 `generation_logits=last_token`。
- 改了哪些：`src/asr/qwen3_backend.py`、`tests/test_asr_backends.py`、`docs/ASR_BACKENDS.md`、本文。
- 怎么验证：`.venv/bin/python -m pytest` 437 passed；8 个新增用例覆盖两款 Qwen、仅最后位置投影与数值一致、正常/异常后 hook 清理、token 截断拒绝及 EOS 边界、独立 aligner/完整 forward 不变。真实同一 30 秒失败片段使用现有应用镜像、同一模型/cache/术语/profile 和 GPU 锁，网络禁用、模型卷只读：旧代码退出 1，复现同一 736 MiB OOM；只覆盖临时容器 backend 文件后退出 0，CUDA FP16、3 段/150 字，JSON/TXT 一致、边界有效，PyTorch 阶段累计分配峰值 6073.70 MiB（不是总 GPU 占用）。有 ForcedAligner 零时长字/词聚合告警，仍需人工复核。证据：`data/output/logs/qwen-vram-fce08a52af66-59HCKa/{logs/baseline.log,logs/fixed.log,logs/pytest.log,validation-summary.json,data/intermediate/asr/failed-block.json,data/intermediate/asr/failed-block.txt}`。初次精简验收配置缺少必填字段，未运行模型；失败日志另保留，补齐公共配置后进行上述有效对照。
- 对 codestable/ 的影响：无 Project Spec 可同步；补充 `005-o-asr-multi-backend-integration.md` 的 Qwen 工程边界：既有容器短音频验收不能排除长术语上下文的显存峰值。没有宣称普遍消除所有 OOM 或完成整项任务。
- 待上线：当前 `transcript-pipeline-trans-1` 未更新/重启，原任务状态仍 failed；没有自动重跑全片、写回任务状态、覆盖原 ASR 或调用 OCR/精修 API。临时验收容器已退出，只保留隔离日志与产物；未重建生产镜像、commit 或 push。下一步在任务结束后获准重建服务，再由用户从 transcribe 同候选重试并检查整片结果。

编号沿本树（包含历史日期目录）最大数字前缀 2026 加一；不调整历史编号。
