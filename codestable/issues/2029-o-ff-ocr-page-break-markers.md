---
kind: issue
title: PDF OCR 物理换页标记及阶段六校对规则
type: ff
status: open
created: 2026-10-04
---

# PDF OCR 换页标记

已实现并完成本地验证，等待用户校对效果验收。

- 做了什么：公共 Codex API PDF OCR 在原始物理页边界加入 `----- OCR_PAGE_BREAK: 1 -> 2 -----`；页内换行不加标记，空白页保留边界。标记只写入整本 TXT，原页检查点不变。阶段 6 默认参考文本提示词说明物理换页不代表新段、按语义衔接硬换行、保留脚注/尾注及最终文稿不保留工程标记。
- 改动：新增 `src/ocr_page_markers.py` 集中格式与精确识别；修改 `src/reference_utils.py` 合并输出、`src/align_utils.py` 的参考块切分、`src/refine_utils.py` 的参考句预替换及参考相似度评分。规则消费处过滤标记，源 TXT 和模型全文附件不改。更新 `config/prompts/final_cleanup.md`、`config/prompts/classify_and_correct.md`、前端说明、README 和相关测试。
- 验证：`.venv/bin/python -m pytest` 472 passed；前端类型检查/构建通过（仅 bundle 大小警告）；`git diff --check` 通过。新增 25 项回归覆盖完整标记语法、普通横线/数字保留、两种参考分段设置、参考块编号/评分、预替换、后端选择、三个精修后端的提示词装配、空白页和重复检查点复用。
- 真实样例：《世界通史》15 页既有检查点通过公共 PDF OCR 流程生成 14 条按原页序排列的标记；0 次 OCR/校对模型请求，原 PDF、旧 TXT、页检查点未修改。阶段四构建 343 个正文参考块且无标记污染；阶段六提示词装配保留真实全文标记和新规则。输出及证据：`tmp_pdf_ocr_page_markers/`。未宣称真实模型已完成硬换行校对。
- codestable：已更新 `2028-o-ff-pdf-ocr-project-actions.md` 关联后续变化；修正通用 TXT 整合 CLI 的适用边界，无当前 project/epic spec 需要毕业回写。
- 边界：`scripts/11_integrate_ocr_txt.py` 是任意 TXT 片段整合器，不知道物理页，不加标记。带标记离线重建必须复用原 PDF/模型/推理身份对应的全部页检查点及公共合并流程；缺页会请求 OCR，不能误称纯离线。未修改其他 OCR 后端、无参考的对谈提示词、上游 JSON 或最终输出结构，也未覆盖旧 TXT。
