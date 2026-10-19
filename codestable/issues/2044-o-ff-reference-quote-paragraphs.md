---
kind: issue
title: 保留引用原文自然段，避免逐句碎分段
type: ff
status: open
created: 2026-10-19
---

# 保留引用原文自然段，避免逐句碎分段

任务 `7de4af69a719` 的预替换输入把句子用空行拼接，模型沿用碎分段。现按参考文本行/段落归属组装锁定引用，并传递内部段落定位提示；同段连续朗读应合并，真实插话不跨越，不在导出阶段无差别合并引用。

- 改动：`src/refine_utils.py`、`config/prompts/final_cleanup.md`、`docs/PIPELINE_PLAN.md`；`tests/test_refine.py` 仅更新既有用例对段落格式和内部段号的预期。句子与源段编号由同一内部切分函数一次生成，公开切句函数仅投影文本；参考逐句匹配、锁定阈值和下游 JSON 主结构不变。参考非空行编号只是定位提示，硬换行及真实跨页段界仍由模型结合上下文判断。
- 验证：清理前 `.venv/bin/python -m pytest`：696 passed，含 12 项临时回归，覆盖同段并句、不同段保留、混用换行、重复句定位、真实插话、锁定切换、段内单换行语义、既有 OCR 换页与锁定实词校验。按用户要求，新增的 `tests/test_refine_paragraphs.py` 及临时验证目录已删除，不纳入提交；保留既有测试的必要预期更新。清理后再次运行 `.venv/bin/python -m pytest`：684 passed；`git diff --check` 通过。
- 真实材料离线运行：`.venv/bin/python tmp/reference-paragraph-validation-7de4af69a719/validate.py`；隔离运行真实 `scripts/06_refine.py` 和 `07_export_markdown.py`，使用历史响应回放。380 个片段和忽略空白/标点后的输入文字不变，66 个锁定片段；预替换输入的空行分隔段数从 858 降为 762，同段锁定句已并回，段号及新政策进入捕获请求。原报告位于 `tmp/reference-paragraph-validation-7de4af69a719/report.json`，现已随临时目录清理；当时验证原任务文本/JSON/配置/日志哈希未变，0 次远端调用。
- 验收边界：当前环境缺少模型 API 凭据，尚未验证新模型 Markdown 排版质量；回放导出仍是历史稿，不宣称旧任务稿件已修正。下一步在实际运行环境仅重跑该任务阶段 6，再导出阶段 7，检查无插话引用同段合并及真实插话保留。
- codestable：无稳定 spec 需要修改；阶段 6 行为边界同步到 `docs/PIPELINE_PLAN.md`，仅新增本条快改记录，不关闭既有事项。不重跑 ASR/OCR，不覆盖上游和原任务产物，不部署、不重启；本次按用户明确要求提交 Git commit，不推送。
