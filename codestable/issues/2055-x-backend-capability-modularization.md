---
kind: issue
title: 后端四大能力模块化与现有产品接线
type: refactor
status: closed
created: 2026-10-31
closed: 2026-10-31
---

# 后端四大能力模块化与现有产品接线

实现与工程验收已完成。用户在分支实测两天后，于 2026-10-31 明确要求创建 PR、合并并把成果写入 CS；本记录据此关闭模块化实施范围。用户反馈为“已实测两天”，未附具体材料或逐项指标。主线合并状态以本次 PR 记录为准。

## 结果与范围

把原先夹在配置适配器、阶段脚本和产品功能中的处理操作提取为媒体、本地 ASR、PDF、LLM 四类独立能力，由现有编排组合使用。新能力不依赖完整 `LoadedSettings`、job 或 Web，旧入口真实调用新核心，外部使用方式与中间产物合同保持兼容。

产品层继续拥有任务快照、阶段推进、目录和产物交付；听写继续拥有正文版本、人工编辑、会话权限、暂停／结束及排队状态。没有把这些状态机合并进能力层，也没有新增识别算法、模型组合或流水线阶段。

## 实现与兼容性处理

代码提交为 `e8d2f125fa1ce2ea214e118a447322e0d0c49a19`（`refactor: modularize backend processing capabilities`）。

- 新核心位于 `src/media/`、`src/asr/`、`src/pdf/`、`src/llm/`；`src/pipeline/` 承接阶段分派。旧配置／目录适配器、CLI、Web 单任务／批量／文件模式、参考提取、书籍工具与听写均已接线。
- 新网关连接显式传过线程，旧客户端请求起点解析配置和旧环境上下文的历史用法保留。审核修复了提前冻结旧配置、把旧上下文改成无作用，以及仓库外相对路径调用 worker 的兼容问题。
- PDF 保留页标记、空白页、检查点身份与来源、两种提示词和手动补页切模边界；完成事件及完整 TXT 发布是成功门禁，不从 `resumable` 推断成功。
- 保留共享 GPU 锁及 fd 继承、文件／听写资源差异、双 ASR 配对与发布门禁、引用锁定、多后端人工选择、听写全文总期限和退出顺序。
- README／阶段计划直接相关的旧说明已校准；收尾按用户要求补齐 README 的四模块总览、LLM 两层、组合流程、状态职责、扩展落点与实际目录树。[使用指南](../../docs/BACKEND_MODULES.md)提供真实接口与组合示例。实施设计、分工和审核结论保留在[方案文档](../../docs/BACKEND_MODULARIZATION_PLAN.md)。

## 验证与用户反馈

- 稳定代码候选使用项目 `.venv` 执行 `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest`，**900 passed / 61.52 秒**。原始日志：`tmp/backend-modules-validation/full-pytest-final.log`。
- 真实本地媒体样例经 ffmpeg 转换、ffprobe 核对，并执行 PyAV 限长解码；ASR 仅固定模型边界，验证两文件共一次加载、校验和 JSON/TXT 发布。证据：`tmp/media-asr-validation/report.json`。
- 合成三页 PDF 经真实渲染，验证首轮 2/3 页、一次显式补页、空白页、两条页标记、来源和完整检查点零模型调用复用。真实参考准备、精修、导出阶段均返回 0，生成并检查 MD/TXT；模型响应为本地固定数据。证据：`tmp/backend-modules-validation/offline-hbe979p2/summary.json`；可复现入口：`scripts/validate_backend_modules.py`。
- 读书会、对谈、听写逐句／全文使用四次本地 HTTP/SSE 请求，连接／认证／代理交错隔离另有合同测试。证据：`tmp/backend-modules-validation/llm-local/summary.json`及 `tests/test_llm_capabilities.py`。
- 初轮十项测试失败均已处理：八项迁到真实调用接缝；两项越权测试改为单条伪造 Cookie，继续要求 404，不改变产品鉴权。
- 2026-10-31 fetch 后确认 `origin/new-backend` 仍为上述代码提交，测试期间没有后续提交；本次只新增 CS 文档并补齐 README 总览，因此沿用同一代码候选的 900 项测试与样例，不重复运行。用户两天实测及合并授权按其反馈记录，未追加远端模型调用或 ASR 推理。

上述 `tmp/` 证据为当前工作区的本地验收产物，未纳入仓库；关键结论已保存在本记录与实施方案，可复现部分由脚本和测试承载。真实模型准确率、GPU 推理及具体材料的校对质量不在本轮工程验收结论内。

## 关闭与回写

稳定的能力组合、状态所有权、连接隔离及兼容契约已回写到[后端能力边界 note](../notes/001-backend-capability-boundaries.md)。本仓库此前没有项目级 spec，本次按授权只新增主题 note，不初始化整套 spec。

仅关闭本次模块化 refactor。现有 ASR、双 ASR、PDF 补页、网关与听写等 open 事项保持原状态，其待验收内容不由本次用户反馈代替。没有部署、重启服务、下载模型、改写用户材料或关闭无关事项。
