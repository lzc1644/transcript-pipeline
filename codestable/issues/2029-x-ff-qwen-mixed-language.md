---
kind: issue
title: Qwen 混合语言文字保留与粗粒度对齐降级
type: ff
status: closed
created: 2026-10-04
---

# Qwen 混合语言文字保留与粗粒度对齐降级

- **做了什么**：用户确认“中文主语言、允许混合文字、对齐失败有明确降级”。两种 Qwen 已返回非空且非纯标点的完整文字时，不因缺失/零时长/不匹配/无效细粒度对齐阻断任务，保留文字并使用该真实 PCM 块起止时间。优先保留已有 0.6B Japanese 重对齐；不增加识别次数，不按字数猜时间，不自动切语种。空文、解码未结束、SDK 异常和 OOM 仍失败。
- **改了哪些**：`src/asr/qwen3_backend.py`；更新 `tests/test_asr_backends.py`，新增 `tests/test_qwen_mixed_language.py`；同步 `docs/ASR_BACKENDS.md`。段结构不变；新增 `metadata.chunk_timestamp_fallbacks` 定位段 ID、实际区间、原因与需复核标记，文件级来源/粒度明确区分全粗粒度或混合精度，日志与告警提示不是词/句边界。
- **怎么验证的**：`.venv/bin/python -m pytest` **493 passed / 30.29s**；针对性测试 111 passed；`git diff --check` 通过。测试覆盖两种 Qwen 的多种中外文、9 类无效对齐、绝对 PCM 偏移、保留原文、无额外推理、单文件/跨文件隔离、Japanese 重对齐结果无效、解码及 SDK/OOM 护栏，并实际写 JSON/TXT、用既有下游读取。按 `docs/REVIEW_CHECKLIST.md` 自评：范围内、使用项目 .venv、主结构兼容、未回改上游，已检查真实输出，没有新增后续 AI 功能。
- **真实运行**：当前生产镜像启动临时隔离容器，只读挂载本轮后端/分块源码和模型缓存、禁用网络、沿用项目 GPU 锁，公共 `scripts/02_transcribe.py` 重跑六个 0.6B 样例（两个原失败片段、邻近上下文、各 120 秒样本），全部退出 0，segments 与前轮一致；含日文和 `Japan` 外语词。另跑 1.7B 同一日文短片段，原长词表配置 OOM，未发布 JSON/TXT；独立空词表配置退出 0，保留日文。不是运行时自动减术语或显存修复。
- **证据与限制**：`data/output/logs/qwen-mixed-language-20261004/` 保存配置、音频、日志、退出码、源码哈希、`pytest.log` 与 `validation-summary.json`。7 个成功自然样例的 JSON/TXT、时间戳与下游读取通过；它们未触发新增粗粒度分支，该分支目前由故障输入单元测试和真实文件读写验证，不冒充自然音频触发验收。`Pox` / 原 Whisper `BOX` 仍需听校；没有人工稿/CER 或全文质量结论，1.7B 长词表 GPU 验收仍受 OOM 阻塞。
- **对 codestable 的影响**：当前稳定契约已同步上述 ASR 文档“Qwen 混合语言文字与粗粒度时间戳”。项目无 `codestable/spec/`，不擅自初始化；`2028-x-qwen06-short-chunk-recovery.md` 保留原关闭时历史策略，其“无效对齐仍失败”的边界由本轮用户确认的新契约替代。其他事项不关闭。
- **范围边界**：没有改其他后端、默认候选、纯外语配置或 1.7B 显存策略；没有新增 OCR/LLM/最终稿拼装、重跑全片、更新生产容器或 push。验证完成后用户另行授权保存本轮 Git 提交。生产容器 image、启动时间和源码仍为旧版本；部署需另行批准。
