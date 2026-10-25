---
kind: issue
title: CPU预览改为2线程并验证8路回放
type: ff
status: closed
created: 2026-10-25
---

将每会话Streaming Paraformer预览计算线程从4改为2，CPU ready元数据增加`preview_threads`，便于识别后续试验实际配置。Qwen共享进程、生产识别容量2及整理并发2不变；未提交或部署。承接[事项2046](2046-o-two-pass-dictation.md)的容量调参。

- 改动：`src/dictation/streaming_asr.py`、`src/dictation/worker.py`、新增`tests/test_dictation_streaming.py`及`docs/DICTATION.md`。只增加实际线程预算记录，不新增配置平台/兜底/模型切换。
- 验证：`.venv/bin/python -m pytest` **852 passed / 57.41s**，新增3项验证构造参数2线程和preview开/关元数据；日志`tmp/dictation-preview-2threads-pytest.log`。
- 真实试验：`tmp/dictation-preview-2threads-trial/run.py`，独立数据root、原GPU锁、8个真实CPU worker及一个共享Qwen，公开0.wav同步1x循环6次，每路60.31875秒。**8路全部completed、96句全部raw、0接收/产物错误**；完整PCM一致、句ID/结果归属及样本区间已检查。未调用远端整理、未更改线上配置。
- 结果：60.32秒输入约62.87秒收尾；最大CPU音频积压0.416秒，各10秒窗口未持续增长。96句预览P95=1.442秒，Qwen排队P95=3.436秒，最后语音至最终识别P95=4.850秒。每路CPU约1.30 CPU秒/录音秒，8路预览合计约10.4逻辑核当量；输入为高语音占比的同步重复公开短片，不能据此给出相对前次用户录音的降幅。
- 证据：同目录`report.json`、`telemetry.json`、`run.log`和`project/data/dictation/`；GPU 50个离散样本平均10.48%、最大61%，单点采样可能漏掉短峰值，不作串行推理占时。源代码2线程版本尚未进入线上容器，网页仍为旧4线程版本。
- codestable：无Project Spec需要同步；本记录关联原事项2046，历史4线程基线保留原义。本次支持2线程下短时8路CPU/GPU可运行；下一步需要部署授权及更长、多设备且包含远端整理的验收。
