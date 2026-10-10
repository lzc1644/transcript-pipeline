---
kind: issue
title: 人工保真校对记忆独立实验
type: feature
status: open
---

# 人工保真校对记忆独立实验

## 已批准方案

独立 `src/proofreading_memory.py` 深模块、薄 CLI `scripts/12_memory_experiment.py` 与 SQLite。显式三稿 → 有界修改片段 → 离线请求/响应或显式 Responses 分析 → 待审核候选 → 人工审核 → 精确范围检索 → 冻结 task-context / 提示词数据块。详见 [方案与操作说明](../../docs/PROOFREADING_MEMORY_EXPERIMENT.md)。

- 不接入生产阶段6，不回写上游、词表或历史任务，不改默认模型和 cleanup 提示词。首轮为CLI实验；用户已追加授权独立Web/API操作板块，边界见下文。
- 完整 book/speaker/content_type scope 与 synthetic/real_human 隔离；OCR 只作只读出处，候选必须审核，不能解锁 locked_quote。
- 来源逐字区间校验、有限预算、事务写入、去重、版本审核审计、冻结快照不漂移；不训练参数、不引入 Hermes/外部服务/embedding/全局复杂对齐。
- 复用2038/2039：Chat 稿不是人工稿，离线回放只能证明工程链路；编辑政策仍由生产模板拥有。
- 无现行 project spec。跨轮常规 feature，保持 open，不自动 commit/push、不部署。CLI基础已按用户明确授权保存为 `b63836d`。

## 验收状态

实验切片实现完成，事项保持 open，真实人工保真稿与语义质量评估仍待后续。

- 改动：独立深模块/薄CLI、两份实验模板、设计/操作文档、README入口、99个聚焦用例（初版90）；生产流水线/配置/词表/cleanup和历史任务未改。
- 实现：严格来源与选中窗口校验、显式范围与synthetic隔离、单事务pending导入、去重别名/不可变状态版本/人工审核审计、有限词形检索、完整块预算与冻结指纹；可显式启用已有标准Responses客户端，但不自动批准或接阶段6。
- 初版测试：聚焦90 passed；全量740 passed（32.73s）。P1修复后重跑：`.venv/bin/python -m pytest tests/test_proofreading_memory*.py -q`：99 passed；`.venv/bin/python -m pytest`：749 passed（33.10s）。实际客户端本机固定SSE测试通过，不是远端模型调用。
- 真实源材料离线验证：`tmp/proofreading-memory-experiment/`，复制fcb任务ASR/system/OCR，构造明确synthetic稿及replay响应；29次CLI运行（23成功、6预期拒绝），4候选未审核context为0，模拟approve后默认4000字预算选2条/3156字符，20000预算选4条。其他书/讲者、缺scope、real_human任务全为空；重复导入/审批不增加计数。
- disable后新指纹变化、旧文件与render不变；revise生成pending；最终approved=2、disabled=1、pending=1，audit=10。5种伪造/坏JSON整批拒绝不污染库，缺联网开关拒绝；两原任务六份材料hash前后相同，0次新ASR/OCR或远端模型调用。
- 自评：只做批准实验范围，上游/JSON兼容不变，使用.venv、真实CLI及输出已检查。字面引用与synthetic/replay仅证明工程链路，不证明术语/阅读感/语义质量。v1完整scope保守、不自动泛化；行级hunk长改动拒绝，OCR页定位依赖可靠标记；指纹不是签名。下一步用用户真实修改稿逐条审核与受控评估。
- P1修复：经验去重不再丢弃后续提案出处，新增append-only memory_occurrences关联完整validated proposal/request/source/mode；request_id+canonical proposal（排除transport mode）为身份，重导入幂等、首次mode保留。新增佐证未独立审核，不改approved版本/status/review/置信度/审计，不当投票或复活停用/拒绝条目；不同请求不代表独立来源确认。
- 消费隔离：list可检查全部occurrences，但export_context只读取私有immutable当前版本，不透传未审佐证；测试和真实CLI确认第二occurrence加入后approved context JSON及render逐字不变。
- 数据库schema v2；旧v1必须显式`migrate-v1`，事务验证并回填既有版本提案，保留原五张表内容/审计/快照；非法旧提案迁移回滚。不猜测恢复旧去重时已丢失的提案，可重导入原响应补全。
- 修复验证：`tmp/proofreading-memory-experiment/fixes/` 原29步离线CLI重跑，另12条出处/迁移CLI，共41条（34成功、7预期拒绝）；1个approved v2经验、2个occurrences、第二human hash/request/理由可追溯；context字节不变，首次replay模式不被offline重导入覆盖。复制旧v1库回填5个occurrences，表内容和旧context字节不变。六份原材料hash不变，全部synthetic/replay/offline，0远端模型/新ASR/OCR调用；日志与摘要见occurrence-validation.json/occurrence-run.log/pytest-full.log。git diff --check及新文件whitespace检查通过，无staged文件。
- 首轮没有关闭issue、commit/push或部署，当时未接Web/API/外部记忆服务/训练/embedding或自动学习发布；随后用户授权CLI提交及以下独立Web板块。

## 用户追加：独立 Web 处理板块

入口为工具侧栏「校对记忆实验」 `/proofreading-memory`，覆盖三稿上传 → 请求/修改窗口检查 → 显式联网分析或离线JSON导入 → 候选/证据检查 → 人工审核 → 冻结上下文下载。空间、响应式和关键状态契约见 [Web工作台](../../docs/PROOFREADING_MEMORY_EXPERIMENT.md#web-工作台)。本轮无需新框架或仅为行数拆页面，不接生产stage6。

- 归属：独立 typed API 文件与专用页面；独立 `APIRouter` 拥有上传token、实验文件和后台任务；合法性、去重、版本审核/审计和冻结选择仍由 `src/proofreading_memory.py` 拥有。领域模块未修改。
- 数据：专用 `data/proofreading-memory/`，不自动合并CLI库、不允许HTTP指定服务器输入/DB/输出路径；请求/响应/冻结文件独立保留，源稿不覆盖。按当前保存请求的scope/dataset筛选，左侧未保存范围不影响审核/导出。
- 交互能力：沿用工作台主题、导航和窄屏抽屉，空/加载/失败/后台运行有反馈，防重复提交，审核具备理由/身份/版本及二次确认。证据：真实Chrome操作、深浅主题与1440/390布局、展开态DOM边界。
- 信息安全性/可靠性：1MiB上传限制、UTF-8、token/路径/软链接校验、整批候选验证；联网必须显式同意，不自动入库/审核或重试。复用Web运行设置，以客户端实例参数传入地址/密钥/代理，避免后台任务改动全局环境；不持久化密钥到实验包。证据：API回归、本机固定SSE实际传输与环境不变检查。
- 保持批准payload与occurrences所有权分离；页面明确occurrences未独立审核，不是多佐证自动审核、投票或置信度学习。修订暂用CLI，不增加网页修订编辑器。
- **运行边界：一个API进程独占实验目录。** router私有active集合/锁不是跨worker租约或互斥；多worker/多实例共享目录会误判另一进程任务中断，也不能跨进程防重。默认单worker可用，未来必须先实现持久owner/lease才可支持多进程。本轮只记录限制，不引入分布式锁。
- 自评与最终验证：`.venv/bin/python -m pytest` **775 passed（36.37s）**；记忆核心/校验/Web聚焦 **123 passed**（其中24个Web用例，另2个SDK代理回归）；前端 `vue-tsc + Vite` 构建通过，既有大chunk告警保留；`git diff --check`及新文件空白检查通过。
- Chrome真实操作6组通过：新页1440/390px×深浅主题、窄屏键盘抽屉/Escape/焦点恢复及七路由可达、隔离API完整处理流程。展开证据/出处/模型块、手机下拉浮层无裁切/横向溢出；输入可访问名称与非BMP字符窗口已修正验证。新增轮询/任务结束同锁保护及并发回归，避免陈旧running值覆盖已完成success。
- 真实源材料样例：复制现有fcb ASR/system/OCR与明确synthetic人工稿，**15次实际HTTP操作成功**；未审核选0、模拟批准选1（2003字符）、停用新快照选0，旧下载字节不变，两个任务六份原材料hash不变；实际CLI读取同一v2实验库成功。人工稿/响应为synthetic/replay，不是质量确认。
- 本机固定SSE用真实Responses客户端验证Web地址/密钥/model/reasoning/代理读取及环境不变；curl参数单测不发请求，浏览器fixture拒绝联网分析。0次远端模型/新ASR/OCR调用。
- 证据：`tmp/proofreading-memory-web-check/pytest-final.log`、`pytest-focused.log`；`run-4TkfVs/browser-final/results.json`、展开态截图和`memory-flow.json`；`run-4TkfVs/real-source-validation.json`、`real-source-context.json`、`real-source-block.txt`、`cli-memory-inspection.json`。已按REVIEW_CHECKLIST自查：范围、输入/数据保护、兼容、测试、真实脚本/输出与下一步边界均核对。事项仍open，真实人工稿/真实模型语义收益待用户受控实验；本轮不部署、不自动提交。
