---
kind: issue
title: 开放8路试用与8并发文字整理
type: ff
status: closed
created: 2026-10-25
---

用户同意保存并部署8路试用配置：识别容量8、CPU预览每会话2线程、远端整理全局8并发，不push。合并已验证的[2线程调整2050](2050-x-ff-preview-cpu-threads.md)；Qwen仍共享单进程、会话公平调度和已有生命周期不变。

- 改动：`src/dictation/{schemas,service,streaming_asr,worker}.py`、`src/web/dictation.py`、`tests/test_dictation{,_file_pause,_streaming,_capacity}.py`、`docs/DICTATION.md`。识别默认8但保留自定义容量；整理统一常量8用于真实请求槽和手动请求准入，不只扩大其中一道限制。配置接口新增实际预览/整理预算。
- 验证：`.venv/bin/python -m pytest` **856 passed / 58.27s**，`tmp/dictation-eight-pytest-final.log`；新增8路默认准入/第9人拒绝、显式2路旧配置、8个手动整理/第9个拒绝、实际loopback HTTP/curl最多8在途且第9个等待。暂停文件回归改为验证全部8个槽可供其他请求使用；同一会话全文single-flight和有界HTTP退出保持。
- 真实证据：复用2050的2线程、8路1x公开音频试验（`tmp/dictation-preview-2threads-trial/report.json`）：每路60.31875秒，96句全部完成，最大积压0.416秒，最终识别P95=4.85秒。预览和Qwen实现未再改变，试验有效容量同为8；不重复运行相同音频、不调用付费网关。新增真实loopback覆盖文字整理8并发，但网关实际承载与10分钟多设备使用仍由用户验收。
- codestable：无Project Spec需同步，事项2046保持open，历史2路/4线程及2整理并发基线保留。发布前确认无活动会话/worker/HTTP进程；旧镜像`transcript-pipeline:rollback-before-eight-20261025t125710z`保留，发布前快照、实际提交号、健康/配置/有效源码验收及独立回退步骤写入`tmp/deploy/dictation-eight-20261025T125710Z/`，最终发布结果见`report.json`。
