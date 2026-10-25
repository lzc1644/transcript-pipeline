---
kind: issue
title: "扩展后台重建不得遗忘关闭意图"
type: bug
status: open
created: 2026-10-24
---

# 扩展后台重建不得遗忘关闭意图

执行状态：2026-10-25 随扩展开发暂停并保存本地检查点。P1 修复及 Astra 复核已完成，本事项保持 open，等待用户另行授权收尾；后续按[暂停交接](../../docs/BROWSER_DICTATION_EXTENSION_IMPLEMENTATION.md#暂停检查点2026-10-25)恢复。

## 修复目标与范围

Astra 评审发现：停止或故障已落盘、关闭命令尚未送达时终止后台，重建会用网页的 recording 覆盖 draining，或只保留 failed 而不补送关闭；取消则过早清空原绑定。用户已要求原实现修复，再做 S1 实机验收。

停止、取消、故障关闭的意图必须独立持久保存，按原 operation / document / lease 幂等重送。关闭交接确认前保留绑定；迟到状态和请求不能撤销意图或重新开麦。范围限于这项 P1 及其回归，不推进 S2、不部署、不调用模型。

## 责任与验证

- 由扩展后台统一保存关闭意图及各采音端待确认状态；网页桥提供明确的操作关闭收据。外部 RPC 不占状态写入队列。
- 覆盖停止落盘、故障落盘、取消未送达三个重建窗口，以及丢回执、迟到 recording / PREPARE / START、旧操作回执和后继隔离。
- 复用现有内存 harness 加载实际后台；用隔离浏览器虚拟麦克风检查真实 worker 终止后的关闭，不替代物理设备和 LAN 验收。
- 实施与验收入口：[扩展执行记录](../../docs/BROWSER_DICTATION_EXTENSION_IMPLEMENTATION.md)。原 `2046` 事项排除扩展，本修复独立记录。

完成后保留事项 open，等待用户授权收尾；S1 仍须物理设备及实际 8443 通路验收。

## 实现与验证（2026-10-24）

`background.ts` 独立保存 closeIntent 与原网页/侧栏各端待确认位；关闭前落盘，重建先补送，逐端确认后才允许清空取消/放弃的操作。旧 draining/faulted 记录在核对前升级。加载绑定与外部恢复分开，原 lease 尾包/STOP 回执不等待恢复完成。网页桥新增 `closedOperationId` 收据并封存旧操作；UI 显示未确认关闭，不允许用放弃绕过。

- 扩展内存测试 **39 passed**（新增 11 项），覆盖三类中断与实际侧栏分支、逐端回执、终态放弃收据、旧记录、旧回执及尾包；桥接浏览器 **8 组通过**。
- 生产 Chrome 155 实际 worker 在关闭落盘、RPC 尚未送达时被终止，CDP 确认 stopped；三种意图均在重建后关闭原录音，所有轨道 ended、开麦及会话数不增加。样本分别 6,826 / 13,141 / 6,400；故障保留 failed，取消在交接后清空。音源为虚拟设备，HTTP/WS 为协议夹具。
- `.venv/bin/python -m pytest` **817 passed / 52.04s**；扩展类型检查、Chrome/Firefox 构建、前端类型检查/构建及 dictation-state 通过。既有 httpx 弃用和大 chunk 提示保留。
- 证据：`tmp/browser-dictation-extension/close-intent-fix/{extension-tests.log,pytest.log,bridge-01/report.json,browser-07/report.json}`。驱动 01–06 的失败及原因记录在执行文档，不重标成功。
- 已按 `docs/REVIEW_CHECKLIST.md` 自评；无后端、上游产物、模型或未来阶段变更。本次未重跑原生 Firefox，物理设备/指示器及 LAN 验收仍未完成，不宣布 S1 完成。

## Astra 复核通过，修复工作结束

用户提供的 Astra 只读复核确认：原 P1 已解决，本次修复范围内无新的阻断问题，三类针对性复现与 `browser-07` 实际恢复证据一致。该项不再继续修改实现，事项保持 open，未获关闭授权。

原文档销毁、无法取得回执时，仍由执行者人工确认停麦后显式恢复；不能计作自动恢复成功。后续工作转入 S1 的物理设备与 LAN 验收，部署授权及结果记入扩展执行记录，不扩大本 bug 的修复范围。

后续 S1 网页部署与本机两端真实 POST/WSS 已在单独授权下通过，见执行记录及 `tmp/deploy/browser-extension-s1-20261025T053910Z/report.json`；这不改变本事项的 open 状态。P1 修复轮次未重跑原生 Firefox，后续 LAN 轮次已运行原生 Firefox 154；物理设备及原生指示器仍待实机执行者验收。
