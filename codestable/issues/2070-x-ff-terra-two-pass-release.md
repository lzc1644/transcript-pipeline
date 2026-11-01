---
kind: issue
title: 正式采用双 Terra Fast 校对并精简实验代码与测试
type: ff
status: closed
created: 2026-11-01
---

- 做了什么：阶段6正式默认Terra high→Terra xhigh→人工听音校对；固定模型、强度、Fast仍只由`refinement_two_pass.py`局部replace设置。产品schema/settings默认terra_terra，低层RefinementOptions/refine_document保持单步默认、自带提示词及CLI/多后端契约。保留单步显式选择及CLI/Web透传，不在提交/重跑/持久化时复制固定参数。完整两稿、疑点、删除记录和失败证据独立保存，失败不重试/切模/发布回退稿，未把交付格式通过当作语义验真。
- 精简了什么：删除未采用的Flash新调用分支、专用提示词及UI/API选项；实验runner改为单一正式runner，移除只为旧实验新建的包装函数。三个新测试文件合并为`tests/test_refinement_two_pass.py`，去掉Flash执行矩阵和重复字段/模式检查，净减少32项用例；保留参数所有权、实际priority请求、各阶段失败、完整第二步输入、历史预览/ZIP和库兼容校验。六份未提交的过程issue收敛为本记录，原始快照及本轮证据留在`tmp/terra-release-cleanup-20261101/`，不纳入commit。
- 历史兼容：旧Flash三项artifact ID仍读取已有draft.md/polish.md/experiment.json，已保留一个兼容测试；停止新执行不影响历史两稿链接。旧配置及全局保存值terra_flash在读取时升级为正式terra_terra，不回写真实任务；新请求不再接受Flash模式。保留既有目录、manifest文件名、主JSON和旧式显式单步辅助入口。
- 改了哪些：两步runner与独立rewrite提示词；schema、配置、覆盖与Web/CLI模式接线；设置/阶段页、两稿预览和ZIP；Docker提示词白名单；合并后的测试与默认提示词打包回归；README、BACKEND_MODULES、PIPELINE_PLAN及能力边界note。用户已接受158867d67c1c最新10:18运行供人工校对；保留其high→xhigh配置，单份两稿对比不能证明xhigh必需，不新增付费比较。
- 怎么验证：`.venv/bin/python -m pytest -q` **974 passed**，1条已有Starlette弃用警告；vue-tsc/Vite构建通过，仅大bundle提示；`git diff --check`通过。真实18002字符ASR、10183字符参考及已保存两稿作为响应fixture，经实际06_refine CLI/正式导出验证两次模拟调用high/true→xhigh/true、16项复核、分稿与最终导出一致，0远端请求。隔离API/Playwright检查默认模式、Fast显示不改存储、单步切换、阶段继承及桌面/手机无溢出/页面错误；实际读取158867d67c1c正式两稿及3ac4d6c569b1历史Flash三个产物ID均成功。归档旧测试最初仍以test_*.py命名被pytest收集，已改为文本快照，不修改测试发现配置或屏蔽有效测试。
- 对codestable/的影响：已同步`codestable/notes/001-backend-capability-boundaries.md`的产品默认/库入口分层与停止Flash新执行边界；按REVIEW_CHECKLIST自评范围、兼容、环境和真实输出。本轮按用户要求提交本地commit，不推送或部署；无第三轮、新模型请求或ASR/OCR重跑。下一步由用户更新镜像使用正式默认流程。
