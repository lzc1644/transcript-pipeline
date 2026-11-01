---
kind: issue
title: 指定页、历史页和 TXT 零 OCR 通路
type: feature
status: open
created: 2026-11-01
---

# 先证明三条关键通路

实现指定原PDF物理页的共享执行，保持旧整本成功契约；从现有历史任务确认可靠逐页来源；以两书不连续材料验证TXT进入原参考阶段且零OCR。

PDF worker拥有共享执行和针对测试；书库worker提供历史读取/组合骨架；主代理拥有TXT/任务接入及集成验收。接口遵循[活规格](../spec.md)，不重新分配书库状态。

验证：首尾/单页/越界、空白、部分失败、检查点复用与停止投递；原PDF身份不变；新旧历史副本身份/来源检查；实际pypdf组合与TXT零OCR。整合后 `.venv/bin/python -m pytest`，真实外部调用单独记录。

## 执行与证据

- 共享按页执行和原整本适配已实现；PDF/调度/旧工具针对回归141 passed。
- 单任务固定快照、实际TXT准备、分片对齐及旧材料删除后的任务重跑集成测试7 passed；OCR入口调用即失败，实际未触发。
- 首轮 `.venv/bin/python -m pytest -q`：1007 passed，70.42秒；日志 `tmp/library-batch1-pytest.log`。后续批次加厚后另跑最终候选。
- 两份真实历史资料副本导入9+1页，组合顺序A1/A2/A4/B1；人工修订、旧版不漂移和原PDF页内容对应通过。原始证据 `/tmp/transcript-library-acceptance-ji1bgr7s/acceptance.json`，后续浏览器验收使用 `/tmp/transcript-library-real-browser-20261101/`。
- 此处为离线接线/真实文件证据，不计为真实模型调用。Issue保持open，待用户授权关闭。
