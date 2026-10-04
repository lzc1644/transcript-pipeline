---
kind: issue
title: PDF OCR 项目删除、批量 TXT 下载与换行修复
type: ff
status: open
created: 2026-10-04
---

# PDF OCR 项目操作与 TXT 输出

已实现并完成本地验证，等待用户界面/阅读效果验收。

- 做了什么：结果区 `[一键下载 TXT（ZIP）] [删除项目]`，历史行 `[查看] [删除]`；运行中禁用打包和删除，删除二次确认，只清理本任务状态/结果/页检查点，保留上传 PDF。ZIP 保留子目录，只包含完整书籍，`summary.json` 列出未完成书籍。TXT 保留页内段落，在非空页之间添加空行；移除 EPUB。
- 改动：`api_server.py`、`src/web/pdf_book_ocr.py`、`src/reference_utils.py`、`frontend/src/api/client.ts`、`frontend/src/views/PDFBookOCRView.vue`、`README.md` 及相关测试；删除 `src/epub_export.py`、`scripts/12_export_epub.py`、`tests/test_epub_export.py`。
- 验证：`.venv/bin/python -m pytest` 447 passed；前端构建通过（仅既有 bundle 大小警告）；`git diff --check` 通过。ZIP 压缩通过线程池运行，回归确认不阻塞其他请求。桌面/390px 手机浏览器确认 ZIP 下载、删除取消/确认和删除后空状态，无页面错误或横向溢出。
- 真实样例：《世界通史》15 页既有检查点离线合并，0 次远程 OCR 请求，新增 14 个页边界空行，原 PDF、原 TXT 和检查点未修改；原有 TXT 整合 CLI 也运行成功。以该书的两份副本验证批量目录保留及缺页清单，不声称重新 OCR 两本书。证据与输出：`tmp_pdf_ocr_changes/`。
- codestable：已把 `features/2026-07-30-ocr-epub-export/ocr-epub-export-design.md` 标记为历史撤回设计；无当前 project/epic spec 需要毕业回写。
- 已知边界：旧整本 TXT 不自动覆盖，可从页检查点离线整合；页边界可能切开同一自然段，不猜测语义分段。ZIP 仍在内存中构造，大批量/多用户时可另行评估临时 ZIP + FileResponse。
- 后续变化：`2029-o-ff-ocr-page-break-markers.md` 将空行升级为显式物理换页标记，并补齐阶段 6 提示词及规则消费边界。上述通用 TXT 整合 CLI 的运行证据仅证明片段整合可用，不代表能重建带 PDF 换页标记的 TXT；带标记重建应复用公共 PDF OCR 合并流程。
