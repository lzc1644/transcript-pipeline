---
kind: issue
title: 完整体验与备份恢复验收
type: feature
status: open
created: 2026-11-01
---

# 真实材料可用，恢复后仍可复用

主代理负责最终整合与验收，worker执行已定实现修复。桌面/手机关键操作、归档/引用保护、实际两书材料、停服备份恢复方法与用户文档全部纳入；不在开发中停止或重启用户现有服务。

复用已有历史OCR及ASR，仅少量真实缺页OCR，记录真实模型调用与离线证据。验证组合PDF原页、TXT/清单/修订、零重复OCR、旧快照稳定、隔离服务重建与备份恢复后的预览/导出/复用。运行完整pytest、前端构建、关键浏览器交互，按docs/REVIEW_CHECKLIST.md自评。

完成不自动关闭Epic/Issues、commit或部署；关闭时将稳定用户能力和恢复要求毕业到项目规格，实施证据留在本事项。

## 最终验收

- `.venv/bin/python -m pytest -q`：**1027 passed / 70.68s**；日志 `tmp/library-delivery-pytest.log`。仅既有Starlette/httpx弃用警告。
- `cd frontend && npm run build`：类型检查及Vite构建通过；仅既有大bundle提示。没有新增依赖。
- 浏览器：上传/预览/缓存清理、空白页、修订冲突保留输入、未保存离开、多书调序/发布、单任务固定版本选择、390px无横溢；报告 `/tmp/transcript-library-ui-final/screens/report.json`。清理失败重试另见同目录 `cleanup-report.json`。
- 手机长正文：整体粘性页码/视图/选页控件无重叠，文末可读；保存中锁定输入及导航，延迟PUT不串页；报告 `/tmp/transcript-library-mobile-sticky/screens/mobile-sticky-report.json`。主代理实际检查桌面/手机截图。
- 真实两书：9+1页历史导入、人工修订、不连续/跨书四页清单、旧版稳定、第二版更新；`/tmp/transcript-library-real-browser-20261101/acceptance.json`。主代理实际查看了非空原页预览；截图 `tmp/library-real-book-desktop.png`、`tmp/library-real-book-mobile.png`、`tmp/library-real-material-desktop.png`。
- 停服一致性备份恢复：仅停止自建127.0.0.1隔离服务；tar排除缓存/半成品，恢复两书10页及两个固定版本，重新渲染预览，浏览器下载PDF并重新选择旧版用于单任务；`tmp/library-backup-20261101/report.json`、`tmp/library-restored-single-job.png`。实际Uvicorn重启后的显式继续见 `restart-report.json`。
- 真实模型：单页OCR请求1次、复用新增0；原ASR摘录到真实双Terra并导出，参考OCR0、ASR不重跑。报告分别为 `tmp/library-real-ocr-20261101/report.json`、`tmp/library-real-refine-20261101/report.json`。

真实校对脚本首版把计数器挂在OCR的stream方法，而校对走codex_responses_text，导致收尾计数断言失败；模型与三个阶段已经成功。改用已有completed两步记录及draft accepted/polish passed校验恢复证据，未为计数重复付费。报告保留了该脚本错误说明。

## 自评和交付边界

按 `docs/REVIEW_CHECKLIST.md` 完成：批准范围内、使用项目.venv、真实运行和输出已查、原中间结构兼容、上游及用户现有服务未改写。未实现账户、检索、章节/印刷页识别、新后端、ASR重跑或新整理算法。现有服务未部署/重启。
原始PDF身份/逐页版本/旧任务快照均成立；真实样本用于接线验证，疑点继续人工听音校对。实现验收完成，等Astra检查和用户决定上线/关闭；不自动毕业Project Spec。

## 修改落点

- PDF：`src/pdf/ocr.py`、`src/pdf/__init__.py`、`src/ocr_scheduler.py`。
- 新领域：`src/library/{__init__,store,jobs,imports,materials}.py`；新适配 `src/library_reference.py`、`src/web/library.py`、`src/web/library_jobs.py`。
- 接入：`api_server.py`、`src/web/models.py`、`src/web/tasks.py`、`src/job_runner.py`、`src/schemas.py`、`src/align_utils.py`、`src/refine_utils.py`。
- 前端：`frontend/src/api/{client,library}.ts`、`App.vue`、`router/index.ts`、`components/{NavBar,JobStatusCard,LibraryBookDetail,LibraryMaterialPicker}.vue`、`views/{LibraryView,SingleJobView,PDFBookOCRView}.vue`；`frontend/tests/library.mjs`。
- 测试：`tests/test_library{,_imports,_jobs,_materials,_pdf_pages,_web,_job_integration}.py`、`tests/library_acceptance_server.py`。
- 文档/持久资料忽略：`.gitignore`、`README.md`、`docs/{PDF_LIBRARY,PDF_LIBRARY_IMPLEMENTATION,PIPELINE_PLAN,BACKEND_MODULES}.md`、本Epic及五个Issue。
