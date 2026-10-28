---
kind: issue
title: 正文与听写共用编辑光标
type: ff
status: closed
created: 2026-10-28
---

# 正文与听写共用编辑光标

移除独立虚线听写标记；点击、方向键和本地正文编辑都由原 controller 自动建立 pause／flush／resume 样本边界，连续移动／键入合并等待300ms，组词期间不恢复。当前轮次落字推进真实编辑光标，旧尾句归旧位置；选区末尾插入保留原文。主动暂停／结束优先取消自动恢复，错误保持麦克风关闭。

- 改动：`frontend/src/components/{DictationEditor,DictationDock}.vue`、`frontend/src/composables/{dictationDocument,useDictation}.ts`、`frontend/src/views/DictationView.vue`；更新 `frontend/tests/{dictation-notebook.mjs,README.md}`、`docs/DICTATION.md`。
- 验证：项目 `.venv` pytest **864 passed / 59.32s**；前端类型检查／build、state 通过。当前单光标真实浏览器定向 **14项通过、0页面异常**，包含160ms逐字键入／Enter、保存阻塞与迟到旧句／恢复后新句、IME事件、权限／worklet／暂停／结束竞争和录音所有权；截图已查看。证据 `tmp/unified-cursor-final/`，日志 `tmp/unified-cursor-{pytest,final-build,final-state,final-browser}.log`。此前完整32项中的HTTP／历史等未受影响证据沿用 `tmp/unified-cursor-verified-2/`；语音和整理均为fixture，无实际ASR／付费调用。
- codestable：在 `2046-o-two-pass-dictation.md` 标明旧双光标合同已替代，保留历史和open状态；原流水线／中间JSON／模型不变。真实短录音与物理中文输入法仍由用户验收；未提交、推送或部署。
