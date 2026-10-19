---
kind: issue
title: GPT 输出失败页在手动重试时使用 Gemini 补页
type: ff
status: open
created: 2026-10-18
---

# GPT 输出失败页在手动重试时使用 Gemini 补页

用户批准独立 PDF OCR 保持 GPT 为主模型，在点击重试时仅让明确上游输出截断/过滤的页面改用 `gemini-3.5-flash-lite`。代码实现和回归已完成；真实补页请求到达 CPA，但返回上游 Gemini/Antigravity 授权缺失 `project_id`，尚未验收线上成功补齐。

- 规则与计划：`src/pdf_ocr_workflow.py` 统一分类、不可变计划和页模型选择。`response.incomplete`、content_filter、safety_violation/regurg 及 HTTP 正常结束但缺 completed 的协议截断可选入；纯 TLS、timeout、IncompleteRead、普通 stream disconnected、限流、鉴权失败不新建切模计划。已有同父身份计划在 Gemini 临时失败后保留。
- 页执行：`src/reference_utils.py` 消费计划、使用原父检查点目录；仅选中缺页请求 Gemini/medium、不发 priority，不修改 LoadedSettings。每个新成功页在提交 TXT 前原子保存 `.source.json`，含真实模型、reasoning、父模型、fallback 标记和文本 SHA-256；旧 TXT 无 sidecar 继续兼容，已有 sidecar 必须与文本一致。
- 统一模型/状态：`src/pdf_book_ocr.py` 按来源和父 identity 绑定计划并上报成功补页模型；`src/web/pdf_book_ocr.py` 仅从历史失败与已有计划导出重试计划；`src/web/tasks.py` 在进度覆盖前持久化计划。首轮不追加 Gemini，不循环重试，普通缺页用原模型，多 PDF 页码隔离。旧任务缺少身份时首次按不可变上传内容与历史主配置绑定。
- 完成门禁：`src/codex_lb_client.py` 为 OCR 启用 completed 检查；流内 error/incomplete 即使已有部分文字也失败，无 completed 的 OCR 流不写成功页。共享客户端其他调用仍保留原默认兼容模式。
- 展示与文档：`frontend/src/api/client.ts`、`frontend/src/views/PDFBookOCRView.vue` 添加可选来源字段、成功 Gemini 补页标记及重试提示；`README.md` 说明边界。保留此前 2042 的提示词修改，不改阶段 6 整稿校对、阶段 3 的 fallback 行为、全局模型默认或原上传/任务。
- 验证：`.venv/bin/python -m pytest` **688 passed / 32.96s**；新增 `tests/test_pdf_ocr_retry_fallback.py` 27 项覆盖分类、纯传输边界、父身份/来源隔离、首轮无额外调用、缺页混合路由、旧成功页不动、来源持久化/损坏检查、来源写失败、Gemini 失败后跨服务重启继续、默认值变化不污染历史任务、partial/空白页门禁。`tests/test_prepare_reference.py` 两个成功 SSE 夹具补完整 completed。前端 `npm --prefix frontend run build` 和 `git diff --check` 通过，构建只有既有 bundle 体积提示。
- 真实运行（一次性验证脚本已在提交前清理，结果和 trace 保留）：在隔离项目复制真实失败单页状态，通过实际 `/api/pdf-book-ocr/{id}/retry` 路由执行。只发送 1 次 `gemini-3.5-flash-lite / medium`，父配置仍 `gpt-6-luna / xhigh`，计划与父身份保持，未重复 GPT 请求。CPA HTTP 400：`antigravity auth missing project_id: no project_id in response`。任务正确停留 partial，无页 TXT/成功来源提交。原任务和 PDF 哈希未变；未重试鉴权错误，也未把 mocked 回归说成线上成功。完整证据在同目录 `summary.json`、`trace/` 和隔离项目状态。
- 提交前清理：删除提示词实验阶段新增的 4 项逐字断言/重复回归，使 `tests/test_pdf_book_ocr.py` 恢复原有测试；保留补页功能的 27 项关键回归及真实 completed SSE 夹具。清理 `tmp/` 下 9 个一次性 API/离线验证脚本，仅保留图片、状态、JSON、原始响应和日志；这些诊断产物不纳入提交。最终 `.venv/bin/python -m pytest` **684 passed / 33.22s**，`git diff --check` 通过，未新增远端请求。
- codestable：无现有 Project Spec；README 为当前行为说明。此处是用户明确授权的手动补页例外，不是恢复旧版的静默整书 fallback。记录保持 open，等待上游修复 Gemini 授权后做一次真实补页验收；不自动 commit、部署或关闭。
- 下一步：CPA 管理方检查 Gemini/Antigravity 通道授权与 project_id，修复后在隔离任务或原 GPT 缺页任务点击一次重试，核验英文注释末尾及来源 sidecar。当前 5173 后端仍需重启加载新代码，未擅自中断已有服务。
