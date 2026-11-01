---
kind: epic
title: 内网共享 PDF 书库
status: open
created: 2026-11-01
---

# 内网共享 PDF 书库

本轮已批准完整实现五批能力。设计、接口和集成由主代理负责；worker 仅执行下列固定职责。现有服务不在开发中重启，旧上传、CLI、批量、对谈和整本 OCR 契约保留。

## 要改变的用户路径

成员在浏览器上传并搜索书籍，按 PDF 物理页预览、识别和修订，跨书选择有序页段保存材料，直接用于网页单次校对；已准备页不再次 OCR。书库是长期共享资料，不再依附可删除的临时 OCR 任务。

包含多 PDF 上传、书名/作者/版本/标签搜索、按需预览、手动整本或缺页 OCR、逐页原文和人工修订历史、选页顺序与重复提醒、固定材料复用/PDF和TXT下载、历史任务选择导入、单次校对直连、归档/引用保护、停服一致性备份恢复说明。
不包含账户权限、公网分享/搜书/下载、正文全文搜索或向量检索、印刷页码/章节识别、批量/CLI专用书库入口、新 OCR/ASR/校对算法或额外服务。

目标布局（沿用 Vue/Naive UI 与现有主题）：

```text
PDF书库 [书籍 | 材料]                [上传] [历史导入]
  搜索书名/作者/版本/标签 → 分页列表 → 书籍详情
  详情：页导航 / 页段选取
        [原PDF页预览 | 本页文字、保存、历史]  桌面左右
        [原页 / 文字]                    手机切换
  本次材料：有序页段 [拖动 / 上移 / 下移] → 草稿 → 固定版本
  新建单任务：[原参考方式 / 书库固定版本] → 查看材料 → 开始校对
```

加载、预览失败、OCR失败各自反馈；缺页可保存草稿但不能提交校对。保存冲突保留输入。手机页码/选页动作可见，动作和现有听写浮层不遮正文。

## 质量与有界取舍

- 正确性：同一展开清单驱动PDF/TXT/来源；空页占位；原页身份及修订可追溯。以两书真实选页和逐页对照验证。
- 兼容性：旧整本完成语义、上传、CLI/批量/对谈、JSON结构和模型条件不变；新字段可选。以原测试及入口回归验证。
- 可靠性：人工文本不被OCR覆盖；CAS冲突；任务独立快照；文件发布和数据库状态一致；重启不自动收费。以中断窗口、并发和恢复测试验证。
- 交互能力/性能：分页元数据，不加载全书文本；手机原页/文字切换；预览按需且容量受限。以构建和桌面/手机浏览器验证。
- 范围上限：沿用单Uvicorn进程、SQLite、单个书籍OCR作业和页内并发，原PDF总量预期不超过10GB。只有实际多进程部署/吞吐需求改变才重评调度；首版不引入分布式基础设施。

已核对现状：`src/pdf/ocr.py` 只有整本公开执行入口；检查点先落盘再上报；`/api/jobs` 原来先返回后复制；TXT参考直接读取；自定义pipeline可达align；历史状态含容器绝对路径及缺少页来源的旧格式。对应修正体现在下列契约中。

## 所有权

- PDF worker：`src/pdf/`、`src/ocr_scheduler.py` 及其新测试。
- 书库 worker：`src/library/` 及 `tests/test_library*.py`（不含 Web 测试）。统一拥有 SQLite、队列、修订、导入、材料状态。
- Web worker：`src/web/library.py`、前端及 `tests/test_library_web.py`。
- 主代理：`api_server.py` 注册/生命周期/旧任务保护，`src/web/models.py`、`src/web/tasks.py`、job/参考/对齐/校对接入、文档、真实验收。

## PDF 能力

新增 `ocr_pdf_pages(source_pdf, page_numbers, options, *, gateway, checkpoint_dir=None,
progress_callback=None, page_callback=None, prompt_builder=..., retry_plan=None,
stop_event=None, _runtime=None)`。

- 页号为原 PDF 1 起始物理页，拒绝越界，去重后按页执行。
- 返回对象提供 `page_count`、`texts_by_page`、`errors_by_page`、`fallback_page_models`、`interrupted`。
- `page_callback(page, text, source_dict)` 在成功检查点写入后调用；复用成功检查点也必须回调，空串合法。
- 只读取/请求指定页；停止信号阻止新投递，已在途请求完成后落盘。默认不传信号时旧行为不变。
- `ocr_pdf()` 复用该能力，保留公开签名、进度、异常和全书完整且 TXT 发布才成功的语义。

## 书库服务公共接口

`from src.library import LibraryService, LibraryError`。`LibraryError` 提供 `status_code` 和 `detail`。
构造 `LibraryService(project_root)`；`recover()` 初始化并把未结束作业标为 interrupted，不投递；`close()` 停止新投递并收束在途工作。

所有 ID 都由服务生成并解析；不让客户端提供服务端路径。列表统一返回 `{items, total}`。业务对象为普通 dict，时间为 ISO 字符串。

### 书籍和页

- `add_book(pdf_path, *, filename=None, title=None, author='', edition='', tags=None)`：复制原件、校验页数、内容去重。返回书籍 dict，重复返回同书并带 `duplicate: true`。
- `list_books(q='', page=1, page_size=20, archived=False)`、`get_book(book_id)`。
- `update_book(book_id, *, base_version, title, author, edition, tags)`。
- `archive_book(book_id, *, base_version, archived=True)`、`purge_book(book_id)`。
- `get_page(book_id, page)`、`page_history(book_id, page)`。
- `save_page(book_id, page, *, text, base_revision_id, note='')`。
- `preview_page(book_id, page) -> Path`：按需 Poppler，容量受限缓存。

书籍字段至少含 `id,title,author,edition,tags,page_count,ready_pages,edited_pages,failed_pages,version,archived,updated_at`。
页字段含 `page_number,text,current_revision_id,source_kind,ocr_status,error`；没有修订时 ID 和 text 为 null，空串不是缺页。
书籍与材料另含 `state,cleanup_error`。永久清理失败保留可见deleting记录并允许按原ID重试；重启继续已批准的清理，仍检查引用。书籍归档只影响默认发现，不限制已有材料草稿的修改和生成。

### 材料

- `create_material(name, ranges=None)`、`list_materials(archived=False)`、`get_material(material_id)`。
- `update_material(material_id, *, base_version, name, ranges)`；ranges 为有序 `{book_id,start_page,end_page}`，两端包含，保留重复。
- `publish_material(material_id, *, base_version)`：同步生成（Web 在线程池调用），返回固定版本 dict。
- `list_material_revisions(material_id)`、`get_material_revision(revision_id)`。
- `revision_file(revision_id, kind) -> Path`，kind 只允许 pdf/txt/sources。
- `archive_material(material_id, *, base_version, archived=True)`、`purge_material(material_id)`。
- `snapshot_for_job(revision_id, job_root) -> dict`：保护引用、复制三件套并原子发布到 `job_root/attachments/library/`；返回包含 `revision_id,name,revision,pdf_path,txt_path,sources_path` 的快照元数据。成功后 job 不再依赖书库。

材料字段含 `id,name,version,ranges,page_count,ready_pages,missing_pages,duplicate_pages,archived`。
固定版本含 `id,material_id,revision,name,page_count,status,created_at,has_updates`。
来源清单 schema_version=1，`pages` 各项含 `output_page,book_id,title,edition,pdf_page,text_revision_id,fragment_id`。
`fragments` 各项另记录 TXT 正文的 Python Unicode 字符区间 `text_start,text_end`，用于辅助对齐直接按结构分片，避免把来源标题作为正文猜测解析。输出统一 LF。
连续原页使用原页码 OCR_PAGE_BREAK；换书/跳页/重复/倒退开启新来源片段，用固定 `LIBRARY_FRAGMENT` 标记及来源行。空白页保留清单与边界。

### OCR 和历史导入

- `enqueue_ocr(book_id, pages, *, options, gateway=None)`：pages=None 为所有缺页；options 为非秘密快照（model,reasoning_effort,fast_mode,max_concurrency,submit_interval_seconds,timeout_seconds 及非秘密连接配置）。gateway 是执行期注入，不序列化。
- `list_jobs()`、`get_job(job_id)`、`resume_job(job_id, *, gateway=None)`；设置 `service.gateway_factory(options)` 供显式恢复重建连接。
- `history_candidates() -> {items,total}`：条目含 `id,task_id,source_file,title,page_count,reusable_pages,warnings`。
- `import_history(candidate_ids) -> {items,total}`：逐书复制、逐书报告 `status,book_id,imported_pages,warnings,error`。候选 ID 标识具体来源；同页冲突由用户选择候选，不按时间覆盖。`active_import_task_ids()` 暴露需禁止旧任务删除/重试的 task IDs。

一次一个书籍 OCR 作业，页内沿用现有并发。执行/继续先摄入可信成功检查点，再检查当前缺页。任何已有可用当前文本不被后台 OCR 覆盖。模型选项快照持久化，凭据不进入数据库/日志。
导入与材料生成另以轻量operations表持久保存阶段进度、报告和错误，并合并到jobs查询；仅OCR提供resume，其他操作失败后从原入口重新执行。新书库网关使用现有总超时能力，持续滴流也受启动时期限约束。
历史 `/app/data/` 仅按明确部署根映射到本项目 data；仍限制在 uploads/pdf-ocr 下。父身份可靠的无 sidecar 页可复用并标注来源不完整；已有 sidecar 须检查页码和原完整性字段。不能确认时仅导入 PDF。

## Web 路由（`install_library(app, root)` 设置 app.state.library 并注册）

- `POST /api/library/books?filename=` 原始文件流；GET 列表；GET/PATCH/DELETE `/books/{id}`（DELETE 默认归档，purge=true 永久清理）；恢复通过 PATCH archived=false。
- GET `/books/{id}/pages/{page}`、GET `.../preview`、GET `.../history`、PUT `.../text`。
- POST `/books/{id}/ocr-jobs`，GET `/jobs`、GET `/jobs/{id}`、POST `/jobs/{id}/resume`。
- GET/POST `/materials`、GET/PATCH/DELETE `/materials/{id}`。
- GET/POST `/materials/{id}/revisions`；GET `/revisions/{revision_id}/{kind}`。
- GET `/imports` 候选；POST `/imports` 提交 `{candidate_ids:[...]}`。
- DELETE `/cache/previews` 只清预览缓存，不删正式资料。

更新携带 base_version 或 base_revision_id；冲突 409，缺失 404，输入错误 400。API 只暴露业务原因，不暴露密钥。

## 单次校对接入（主代理）

`SingleJobRequest` 增加 `library_material_revision_id`；读书会二选一，对谈规则不变。
返回 202 前固定 job 自有三件套；仅 TXT 进入扫描目录。新增 `reference.library_sources_file` 可选字段指向 job 自有 sources.json，旧配置默认 null。
书库 job 明确启用 reference.enabled/allow_txt。实际使用的原提示词 + 书库规则一起冻结，不替换自定义指令。辅助 align 根据 fragments 的正文区间逐片使用旧分块函数；只对书库结果阻止精确工程标记残留发布，不改模型算法。
附件使用固定白名单接口，不送入原 UTF-8 文本产物读取器。重跑不查询书库。

## 验收进度

- [x] [1 指定页 OCR、真实历史页读取、两书组合、TXT 零 OCR 通路](issues/001-o-critical-path.md)
- [x] [2 书库、预览、历史导入及独立保存](issues/002-o-books-import.md)
- [x] [3 持久队列、修订/冲突、中断恢复](issues/003-o-ocr-revisions.md)
- [x] [4 固定材料、下载、网页单任务接入](issues/004-o-materials-jobs.md)
- [x] [5 完整 pytest、前端构建/浏览器、真实材料、备份恢复、文档](issues/005-o-acceptance-recovery.md)

每批整合后使用 `.venv/bin/python -m pytest`。外部模型只做少量缺页 OCR 和复用已有 ASR 的校对衔接，离线证据与真实模型证据分开记录。不自动 commit、部署或重启。

## 当前交付与关闭条件

实现及工程验收完成，Epic和所属Issue保持open，未执行关闭毕业、提交或部署。使用入口见 [PDF书库说明](../../../docs/PDF_LIBRARY.md)。最终完整测试1027 passed，前端构建、桌面/手机操作、真实单页OCR、真实双Terra校对衔接、隔离重启和备份恢复证据见批次005。
下一步由Astra检查本次diff，再由用户决定上线/关闭。关闭时把书库正式用户路径、资料生命周期、按页PDF契约与恢复要求合并到项目规格；不把开发夹具或单次模型结果当作普遍内容质量结论。
