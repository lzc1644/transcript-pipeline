---
kind: issue
title: 独立 PDF OCR 中外文混排提示词优化
type: ff
status: open
created: 2026-10-18
---

# 独立 PDF OCR 中外文混排提示词优化

将独立 PDF OCR 提示词从书籍描述及重复完整性要求，改为只转写所提供图像；重点保留语言切换顺序、外文原文/大小写/重音/标点、页底外文注释和编号，不翻译、不按语言重排、不凭记忆补全。不承诺消除上游输出过滤；后续同页重复请求未观察到完整成功，不能宣称已解决混排识别或过滤问题。

- 改动：`src/reference_utils.py` 的 `build_pdf_book_ocr_image_prompt`；保留函数签名，不再向模型附加文件名和页数。不修改通用参考提取 OCR 提示词、模型、检查点身份、重试或错误处理。
- 用户进一步要求停止重复请求、在提示词中明确 OCR 意图：仅将首句改为“这是一个 OCR（图像文字识别）任务。请识别我提供的这张图片中实际可见的文字，并输出纯文本。”，保留其余混排/注释要求，同步已有首句断言；未跑测试、未新增模型请求。下述 6 轮实验使用的是修改前的首句，不能当作本次明确 OCR 版本的效果证据。
- 验证：用户要求停止测试前已执行 `.venv/bin/python -m pytest`，661 passed，并在 `tests/test_pdf_book_ocr.py` 补充提示词、混排候选/补页与过滤失败回归。此后纯提示词微调按用户要求不再跑测试。
- 真实材料：`tmp/ocr-mixed-language-prompt-validation/` 保存原单页 PDF 的离线 CLI 请求捕获、相同 PNG、前后提示词及诊断。本终端缺少配置的 API Key，CLI 未完成 OCR，0 次远程调用；未伪造成功产物，原 PDF 和两份任务状态哈希不变。此结果不代表用户外站配置，也不能证明模型识别质量提高。
- 用户后续明确授权远程重复实验，并要求继续：通过与 5173 后端相同的 `load_frontend_settings` / `codex_lb_environment`，在独立目录串行执行 6 次真实单页请求；同一 PNG、291 字混排提示词、`gpt-6-luna / xhigh`、快速模式关闭，请求体 SHA-256 六次完全一致。4 次 HTTP 200 SSE 以 `response.incomplete / content_filter` 终止，均未返回用户可见正文；另外 2 次 curl 35 TLS unexpected EOF，未获得 HTTP 响应。0 次 completed；不将 HTTP 成功当 OCR 成功，不将 TLS 失败计为模型过滤。
- 远程证据：`tmp/ocr-repeat-e2739961/summary.json`、每轮 `attempt-N/trace/` 原始 SSE 与传输信息、`request.json` 及响应 ID。原任务 `pdf-ocr-e2739961d27b4927b8828cf06d96f179` 状态和输入 PDF 未修改，未写入原成功检查点。只新增诊断产物，无业务代码调整，未重跑 pytest。
- 用户随后授权对明确 OCR 首句的版本重试一次：相同单页、`gpt-6-luna / xhigh`，核对实际请求首句含“OCR（图像文字识别）”；29.061 秒后 HTTP 200 SSE 再次以 `response.incomplete / content_filter` 终止，未返回可见正文。响应 ID：`resp_080f3ea2d80deafc016ad599dced3c87d0a62fff32eb01c581`。证据：`tmp/ocr-explicit-ocr-e2739961/attempt-1/` 及 `prompt.txt`。仅执行一次，未追加请求、未跑 pytest，原状态与 PDF 哈希不变；仅增加 OCR 字样未解决本次失败。
- 用户要求仅输出正文、不返回注释作对照。本次准备独立实验目录时发现 `tmp/ocr-body-only-e2739961/` 已有一份完成记录，因此未覆盖目录、未再发起模型请求，而是核对其实际请求、原始 SSE 和文本：与此前明确 OCR 但保留注释的请求相比，除提示词外 payload 完全一致，图片也完全相同；`gpt-6-luna / xhigh`，31.676 秒，HTTP 200，最终 `response.completed`，返回正文 478 字符。响应 ID：`resp_0dc5fbefce1396e4016ad59bc0392487d0a7f1d6bc4fff2297`。正文从页面开头到“然而，使”，不含页底两条注释及正文引用编号；原页本就在这里断句。该观察支持输出范围/注释关联，但单次成功不能证明英文注释的具体触发机制或版权问题。
- 仅正文结果不等于整页完整 OCR：正式提示词仍保留全部注释要求，未修改业务代码、未跑 pytest、未把诊断结果写入原任务检查点；核对时原任务仍为 `partial`、0/1 页。目录中的 `pipeline_success: true` 只对应本次限定正文范围，不代表完整页验收。
- 用户明确授权单独验证注释：从同一 PDF 第 1 页（原书物理第 259 页）按与项目相同的默认 150 DPI 渲染，裁剪 x=300、y=2580、W=2100、H=900，人工查看确认完整包含中文注释（29a）和英文注释（30），不含正文；使用明确 OCR、保留注释编号与中英文原文的临时提示词，`gpt-6-luna / xhigh`，仅一次真实请求。19.414 秒，HTTP 200，但最终 `response.incomplete / content_filter`，无可见正文。响应 ID：`resp_0a101ce7453ca0a1016ad59d656c2887d0833ebb01f870f024`。证据：`tmp/ocr-notes-only-e2739961/notes.png`、`crop.json`、`prompt.txt`、`attempt-1/trace/` 及 `summary.json`。原 PDF/任务状态不变，无成功页写入；未修改业务代码、未跑 pytest、未追加重试。此次同时变化输入裁剪与输出范围，不足以精确归因于某一句或某种语言。
- 用户授权分别验证两条注释，各一次：人工确认两张裁图完整、互不混入另一条；中文（29a）裁剪 x=300/y=2580/W=2100/H=230，英文（30）裁剪 x=300/y=2800/W=2100/H=680，均为同 PDF 默认 150 DPI。两个实际请求除 input_image 外完全相同，使用相同明确 OCR 提示词和 `gpt-6-luna / xhigh`。中文（29a）17.489 秒 `response.completed`，返回 57 字符，响应 ID `resp_0e0ebdbf44381ecc016ad59e46bf3c87d0950e685ea27e4e4f`；英文（30）5.806 秒 `response.incomplete / content_filter`，无正文，响应 ID `resp_02b5bc822f5a224e016ad59e58984c87d0a8f771bdcd52a619`。没有重试、没有跑 pytest、没有修改业务代码或原任务产物。汇总：`tmp/ocr-note-split-e2739961/summary.json`；原始证据分别在 `tmp/ocr-note-29a-e2739961/`、`tmp/ocr-note-30-e2739961/`。
- 用户进一步提供完整英文注释、后半段、前半段三张 PNG，按同一提示词及 `gpt-6-luna / xhigh` 各请求一次，核对三个请求除图像外完全相同：第 1 张完整注释 8.345 秒 `content_filter`，响应 ID `resp_04140a3ee2aa3aa6016ad5a2888aa087d0993cfb37c649ff10`；第 2 张从“The wheat”开始的后半段 6.708 秒 `content_filter`，响应 ID `resp_07dd8f4f6bd88248016ad5a290d5d087d082dd4f047506e808`；第 3 张到“in very unequal times.”为止的前半段 16.219 秒 `response.completed`，返回 132 字符，响应 ID `resp_049b8b64679fb699016ad5a2979e4c87d0b35626974a59d63c`。前两张无可见文字，第三张保留 circu-/late 跨行，左引号被模型规范化为弯引号，未返回顶边截断的中文碎片。证据：`tmp/ocr-three-images-1792385528539/summary.json` 及各 `attempt-N/`；没有重试、没有跑 pytest、没有修改业务代码、原任务/原 PDF 未变。
- 当前结论：前半段英文可识别，而后半段单独输入仍被拦；现有观察将范围进一步缩到后半段相关输出，而非所有英文或所有注释均失败。不同片段的内容与长度同时变化，仍未证明具体文本匹配规则、精确触发句或版权违规。默认完整页目标仍未完成。
- 下一步建议：停止同参数盲目重试；可用英文注释（30）的裁图、响应 ID 和原始 SSE 向上游反馈疑似误拦。如目标是完整整理稿，可另行授权专用 OCR 或人工补录该注释并保留来源，不能静默丢弃，也不能将“正文成功”当“整页成功”。不得直接切换整本任务推理强度后假定仍只补缺页，避免身份变化导致检查点不复用。
- codestable：无现有 Project Spec 需要同步；仅更新本条快改记录，实际问题仍未解决。
- 顺手发现（未修改）：`strip_leading_ascii_noise` 会删除部分合法行首外文，例如 `Adam Smith认为资本存在周转。` 被清洗为 `认为资本存在周转。`；这不是上游过滤的解释，也不能仅靠提示词保证修复。离线复现保存在 `cleanup-probes.json`，应另行明确修复范围。
