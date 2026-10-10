---
kind: issue
title: 单任务输入与配置分栏及进度区域下移
type: ff
status: open
created: 2026-10-10
---

# 单任务输入与配置分栏及进度区域下移

按用户两张标注图调整单任务页；实现和页面检查通过，等待用户确认视觉效果。

```text
左：输入文件         │ 右：常用配置（模型 → 方案 → 双开关 → 高级参数）
                     │     开始整理
下：当前任务（提交后显示；未提交时无占位）
```

- 做了什么：两栏等宽且顶部对齐，配置移到右上并改为单列纵向排列，消除配置内部左右高低不齐；开始整理跟随配置；提交后的当前任务置于两栏下方，空占位移除；窄屏输入、配置、进度依次堆叠。控件绑定、请求和后端行为不变。
- 改动：仅 `frontend/src/views/SingleJobView.vue` 的模板、说明与局部布局样式，未调整批量页或共享样式。
- 验证：`.venv/bin/python -m pytest` 657 passed；vue-tsc/Vite 构建与 `git diff --check` 通过（既有包体积提示）；用户 localhost:5173 实际浏览器检查 1920px/390px 布局与高级参数展开，无横向溢出或运行错误，0 次 POST/模型调用。证据 `tmp/single-layout-validation/summary.json`、`desktop.png`、`mobile.png`。未新增测试用例，未做流水线/模型样例。
- codestable：无现有 project spec 需要同步；本记录保存用户确认的最新布局方向，2040 的开关行为保持不变。按 `docs/REVIEW_CHECKLIST.md` 自查范围、验证与兼容边界；未重启、部署、commit/push。
