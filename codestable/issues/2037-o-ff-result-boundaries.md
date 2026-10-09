---
kind: issue
type: ff
title: "结果记录边界与 PDF 删除按钮位置"
status: open
---

- 做了什么：按用户截图快改，PDF OCR 历史记录的完整边框和选中底色移到外层行，删除按钮收进同一边框；选择与删除仍为同级按钮，保留删除确认。任务列表每条结果增加完整边框、10px 圆角、主题 raised 底色和内边距，手机端适当收紧。
- 改了哪些：`frontend/src/views/PDFBookOCRView.vue`、`frontend/src/views/JobListView.vue`。
- 怎么验证：前端 vue-tsc/Vite 构建通过（原有 >500 kB 提示）；`.venv/bin/python -m pytest` 604 passed；`git diff --check` 通过。按用户快速改动要求未跑整套浏览器检查，未执行模型/流水线样例；等待用户刷新确认视觉效果。
- codestable 影响：无现行 Project Spec；本次按用户最新反馈调整 2036 工作台记录中的任务列表呈现，不改其功能边界。按项目检查表自查：无后端/JSON/上游产物变更，无新增流水线阶段；未提交或部署。
