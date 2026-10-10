# 保真校对记忆独立实验（v1）

这是用户批准的独立实验，不改变生产主链。常规跨轮 feature 记录：[2040](../codestable/issues/2040-o-proofreading-memory-experiment.md)，保持 open。无现行 project spec。

## 方案、目标与范围

用户人工修改稿保留讲话原意及有效对话、改善阅读感。实验以人工改动分析候选，积累术语、专名、误识案例及编辑偏好；不训练模型参数。

批准链路：**显式三稿 → 可解释有界修改片段/请求 → 离线响应导入或显式 Responses 分析 → pending 候选 → 人工审核 → 精确任务范围检索 → 冻结 task-context / 有限模型数据块**。

借鉴 Hermes 的本地持久记忆、候选审核、按需检索和冻结快照，不接入 Hermes 或外部记忆服务。独立深模块 `src/proofreading_memory.py`，薄 CLI `scripts/12_memory_experiment.py`，stdlib SQLite，无新依赖。CLI 与测试使用同一道接口；模型边界可替换。

已有 `glossary_utils.py` 是字符串词表、顺序截取400字，不承载来源或审核，本实验不复用其存储。`refine_utils.py`、`job_runner.write_job_settings`、生产 cleanup 提示词、通用词表、默认配置全部不改。

复用2038/2039的经验：离线回放不等于真实质量验收，Chat稿不等于人工稿；编辑政策仍由默认模板拥有。记忆不能解锁单 ASR `locked_quote` 的实词与顺序。

### 实现顺序（本轮已执行的实验切片）

1. 核对编号与基线，先写新 open issue / 方案。
2. 实现离线输入、引用校验、SQLite 审核与冻结接口、聚焦测试。
3. 薄 CLI、独立分析/上下文模板、显式已有 Responses 客户端适配。
4. 隔离真实源材料的 synthetic/replay 离线 CLI 验证；全量测试及自评。

## 数据契约

### 显式输入与范围

- ASR：UTF-8 TXT/MD；system：TXT/MD 或阶段6 JSON **顶层非空字符串 `final_markdown`**；human：TXT/MD。不猜字段，不自动找人工稿。
- 三稿必须不同路径/inode，参考若提供也须不同；缺文件、目录、空白、非UTF-8、错扩展/JSON/字段明确拒绝。无音频读取/精确对齐。
- `metadata.json` 必需 `dataset_kind: real_human|synthetic` 与 `scope` 对象。scope 只支持 `book_id`、`speaker_id`、`content_type: reading|conversation`；可选 title/chapter/notes。synthetic 可记 synthetic_edits/source_hashes。
- v1 **完整精确 scope**，无通配符或自动跨书/讲者泛化。缺 scope 可准备和保存 pending，但不能 approve；任务缺 scope 返回空并解释原因。不要从书名/LLM猜讲者。未知讲者可用 `unknown-speaker:任务ID` 隔离，明确不是识别人名。
- synthetic 条目仅供显式 synthetic 任务，绝不进入 real_human 上下文。材料和模型返回值是不可信数据；不执行其中指令、命令、文件路径或审核动作。

请求包含 schema_version=1、metadata、sources、fragments、coverage、limits、冻结 analysis_prompt 与其 hash、canonical request_id。source 记录路径（只保留在本地包）、role、原文件字节 SHA256、解码正文 SHA256、selector、正文。system JSON 的原始文件 hash 与 final_markdown 正文 hash 分开；**原JSON完整审计内容不嵌入请求**，离线重验只能验证正文和记录的raw hash格式，不能从正文重构JSON字节。请求自包含，导入重验正文hash、指纹、片段与coverage。

system↔human 使用有字符offset的**行级 difflib.SequenceMatcher(autojunk=False)**，不是语义/音频对齐，不做复杂全局DP。变化行不可隐式裁剪，窗口最多600字、每侧最多80字上下文；长变化行拒绝并要求调用者提供较小、明确标注的片段输入。保留样本只取第一个相同区间的前600字，coverage明确这个采样策略；相同文字不是听音确认。ASR只在 human窗口与ASR逐字唯一匹配时记录unique_literal span，否则not-established。

模型数据使用选中 system/human **窗口及全局offset**，而不是重复发送其全文；ASR与可选参考的有界全文发送。不会将本地source path或完整settings/key发送。coverage含全量变化数、selected/omitted IDs、partial；显式子集不声称全面分析。保留样本的采样和上下文窗口不会冒充全稿分析。

### 明确预算（默认）

| 对象 | 限额 |
|---|---|
| 每文件 | 1MiB，解码正文100000字，4000行 |
| 请求工件 / 响应 JSON | 各1MiB；拒绝重复key、NaN/坏JSON |
| 片段 | 最多40；每个system/human窗口600字 |
| 完整模型 instructions+input | 40000字符（实际计数） |
| 候选 | 最多50；text800字，reason300字；检索key最多12个/80字 |
| 引用 | 每候选1..12个，每个excerpt最多800字 |
| task-context 模型数据块 | 默认8条、完整块4000字符（含说明/ID/版本/来源/审核等） |

默认超限明确报错，过多片段给出可选ID。CLI `--fragment-id` 可重复选择子集。若全文件/单hunk/payload超限，必须明确提供较小输入，不能偷偷截断。Python接口 AnalysisLimits 可显式调整；工件记录实际预算，CLI使用默认。

### 可选已有参考/OCR

只读显式单文件，不新增OCR或批量爬词、不并入glossary。reference_metadata 必需 `source_kind: ocr_text|reference_text`、`version`（未知填unknown）、`book_id`；可选 pdf_sha256/notes。book_id与任务一致。

只识别 `ocr_page_markers.py` 的完整相邻物理换页标记；连续标记序列支持物理PDF页范围 `[起页,止页]`，非书籍印刷页。无法定位填null及page_note；不猜页码/定义。引用不得包含工程标记，应分成页内引用；不连续/无标记返回unknown。定位依赖输入标记真实可靠，模块不验证原PDF。OCR可能有误，不是术语权威。

### 模型候选（严格JSON）

```json
{"schema_version":1,"request_id":"请求ID","candidates":[{
  "kind":"correction_case",
  "scope":{"book_id":"book-1","speaker_id":"speaker-1","content_type":"reading"},
  "text":"在证据支持时纠正特定误识，保留原意。","reason":"比较三稿的短理由。",
  "retrieval_keys":["实际词形"],"fragment_ids":["f0001"],
  "evidence":[{"source_id":"system","start":0,"end":2,"excerpt":"逐字","pdf_page":null},
              {"source_id":"human","start":0,"end":2,"excerpt":"逐字","pdf_page":null}]
}]}
```

上述offset/excerpt仅示意，实际必须等于输入切片；start/end是**解码原文0起始半开字符区间**。kind仅term/correction_case/reading_preference/preservation_case；可选observed_form/preferred_form为观察词形，不是同义概念许可。scope须完全等于请求，fragment_ids只能已选ID；system/human引用须位于对应选中窗口；不得伪造source、excerpt、page或request_id。纠错/阅读感需system+human证据；保留案例需相同证据及unchanged_sample。OCR-only术语允许提议但附警告、仍pending。

未知字段（如status/path/DB ID/审核动作）拒绝，不自动修坏JSON。**整批验证后单事务入库**，任一候选非法则零写入。结构/逐字引用合法不代表语义正确，人工approve仍须核验。模型提出，不批准、不写正文、不删问答、不改观点/否定/玩笑、不重排朗读讲解、不解锁locked_quote。

## 持久化、审核与冻结

实验接口仍为v1，**SQLite数据库schema_version=2**，foreign_keys开启。analysis_requests自包含请求；memories当前指针；memory_versions不可变完整payload/status/review；memory_keys保留历次去重指纹别名；audit_events记录动作、身份、理由、from/to版本和时间；新增append-only **memory_occurrences** 保存memory↔已校验提案↔request出处关系。

经验去重是kind+精确scope+dataset_kind+规范化文本/观察词形的canonical SHA256，不合并相关概念。新memory及去重命中都事务保存完整validated proposal、request_id、response_mode与occurrence_id；命中同经验返回duplicates，但**不丢弃后续请求的理由/引用/hash和可追溯关系**。occurrence身份是request_id+canonical validated proposal（排除response_mode）的SHA256；同occurrence重导入幂等，首次模式声明保留，后续换offline/replay/remote不创建新occurrence、不覆盖首次声明。不同请求不等于独立来源确认，同请求不同理由/证据也可为不同提案。

`list`/list_entries只读展示occurrences，其review_status=unreviewed表示未作为独立佐证审核；初始候选/修订也保留提案关系，但approve只审核对应immutable version，不自动审核或合并后来佐证。**新增occurrence不改current version/status/review/置信度/审计、不当投票、不复活rejected/disabled**。修订后的原候选再导入仍命中原ID/原occurrence；修订提案mode=manual-revision。模式标签是操作者声明，不是网络证明。export_context使用独立私有当前版本读取，只取ID/version/status/candidate/review，不通过扩展list传播occurrences；模型仍只使用已审核版本证据，新增佐证不会改变旧/新approved context的字节、预算或指纹。

### 显式v1数据库迁移

未知数据库版本/既有无版本库拒绝；打开v1库明确要求先运行 `migrate-v1`，**不隐式迁移**。先备份实验库，再执行：

```sh
.venv/bin/python scripts/12_memory_experiment.py migrate-v1 \
  --db tmp/proofreading-memory-experiment/memory.sqlite3
```

同一道深模块接口为 `MemoryStore.migrate_v1(db_path)`。事务重验现存memory_versions的请求/提案，回填各唯一occurrence后设user_version=2；保留memories、versions、keys、analysis_requests、audit原行及旧context，失败回滚表创建/回填与版本号，重复迁移明确拒绝。迁移只能回填v1已经保存的版本提案，**此前去重时丢弃的提案无法仅凭request全文恢复，不猜测或伪造关联**；可重导入原响应JSON补充出处。

- pending → approved/rejected，approved → disabled；revise → 新版pending。
- **每次真实状态改变或修订都产生新版本**，保留旧版完整状态，故approve v1后current是v2、disable需expected-version=2。
- approve/reject/disable/revise都须reviewer/reason/expected-version；旧版本、非法迁移、错引用、范围扩张、修订去重冲突明确失败。
- 对当前已达目标状态的重复审核幂等、changed=false，不增加版本/审计；重复批准是expected当前version，而不是绕过乐观锁接受旧version。
- revise读单个原候选格式JSON，必须仍引用原请求，kind/scope不能换，不是改DB字段；新版pending立即移除旧approved在新context中的可用性。审核动作先取得SQLite写锁再读版本。

检索先过滤current approved +完整精确scope+dataset_kind。中文轻量策略：NFKC/空白/大小写仅用于比较，精确retrieval_keys/observed_form/preferred_form命中，输出method/matched_keys/score；不提供拼音、embedding、二字相似回退或同义词扩展。reading_preference可按完整task scope使用，明确method=task_scope_preference；其余无词形命中不塞全库。

稳定排序score降序/kind/ID/version。超预算**整条省略**，不裁证据；零预算合法空块，负数拒绝。冻结JSON含task_scope/dataset_kind/query及hash、预算/policy、完整selected条目（ID/版本/文本/证据/hash/审核来源/解释）、omitted及原因、fingerprint。pending同scope明确not-approved，其他书/讲者不泄露到省略清单。canonical无运行时间/输出路径，重复导出字节一致。

模型块不含query全文或本地source path，只含有限已批准经验和使用边界。render只验证/消费冻结内容，**不查DB或当前模板替换旧值**。停用/修订改变新context，但旧文件及render不漂移。指纹是可复验完整性摘要，**不是签名/真实性认证**；有权限恶意改工件并重算hash的人仍需由本地文件访问控制约束。不要把来历不明的context当已审核可信工件。

输出默认拒绝已有文件/软链接/输入路径，以同目录临时文件+fsync+原子无覆盖link发布；context和可选block各自原子，不是跨文件事务（第二输出失败时已生成的JSON仍有效）。原稿和上游产物绝不覆盖。数据库和请求含原稿，须自行管理敏感资料/备份与文件权限。

## 使用

全部Python使用项目 `.venv/bin/python`。输出及库应放隔离目录，例如 `tmp/proofreading-memory-experiment/`，不能当生产通用记忆。

```sh
# metadata 必须明确真实人工或 synthetic；用户真实人工稿尚未提供时只能synthetic
.venv/bin/python scripts/12_memory_experiment.py prepare \
  --asr tmp/proofreading-memory-experiment/inputs/asr.txt \
  --system tmp/proofreading-memory-experiment/inputs/system.md \
  --human tmp/proofreading-memory-experiment/inputs/human.synthetic.md \
  --metadata tmp/proofreading-memory-experiment/inputs/metadata.json \
  --reference tmp/proofreading-memory-experiment/inputs/reference.txt \
  --reference-metadata tmp/proofreading-memory-experiment/inputs/reference-metadata.json \
  --output tmp/proofreading-memory-experiment/request.json

.venv/bin/python scripts/12_memory_experiment.py import-response \
  --request tmp/proofreading-memory-experiment/request.json \
  --response tmp/proofreading-memory-experiment/response.replay.json \
  --response-mode replay --db tmp/proofreading-memory-experiment/memory.sqlite3

.venv/bin/python scripts/12_memory_experiment.py list \
  --db tmp/proofreading-memory-experiment/memory.sqlite3 --status pending
.venv/bin/python scripts/12_memory_experiment.py approve MEMORY_ID \
  --db tmp/proofreading-memory-experiment/memory.sqlite3 \
  --reviewer synthetic-test-reviewer --reason 'synthetic链路验收，非质量验收' --expected-version 1

.venv/bin/python scripts/12_memory_experiment.py context \
  --db tmp/proofreading-memory-experiment/memory.sqlite3 \
  --task-metadata tmp/proofreading-memory-experiment/inputs/metadata.json \
  --query-file tmp/proofreading-memory-experiment/inputs/asr.txt \
  --max-items 8 --max-chars 4000 \
  --output tmp/proofreading-memory-experiment/context.approved.json \
  --block-output tmp/proofreading-memory-experiment/context.approved.txt
```

reject/disable同approve选项；revise另加 `--replacement candidate.json`。CLI打印coverage/候选/重复/状态版本/选中省略与路径；失败exit=1，不自动回退。

### 显式可选网络分析

```sh
.venv/bin/python scripts/12_memory_experiment.py analyze \
  --request tmp/proofreading-memory-experiment/request.json \
  --allow-network --config config/settings.yaml --model YOUR_EXPERIMENT_MODEL \
  --output tmp/proofreading-memory-experiment/response.remote.json
```

缺 `--allow-network` 拒绝；离线操作不加载配置/密钥。真正调用已有CodexLBClient.responses_stream_text，标准Responses `stream=True/store=False`，复用config_loader的model/reasoning/timeout/output-token参数；模型覆盖只作用本次。不隐式换后端或回退，不改默认settings。**选中system/human窗口、ASR及可选参考全文会发到现有配置后端**，不是只发统计；发送前自行确认资料隐私与后端。仅导出合法响应，不自动入库/批准。传输/JSON/出处错误无候选写入，不记录密钥/完整含秘配置。

## 验证与下一步

聚焦测试覆盖输入拒绝、JSON/hash/预算、四类候选/伪造来源、事务零污染、显式联网开关、实际标准Responses客户端本机固定SSE、去重/审核/修订/旧版本、synthetic/scope隔离、轻量检索/预算、冻结/无覆盖和CLI同接口。命令：

```sh
.venv/bin/python -m pytest tests/test_proofreading_memory.py
.venv/bin/python -m pytest
 git diff --check
```

真实源材料验证复用 fcb933b77a18 的ASR/系统稿/已有OCR并复制到tmp，人工稿**人为构造，明确synthetic**；模型响应是本地逐字切片构造的**replay/offline**。检查未审核不可用、模拟审核后精确scope命中、停用后新context变化/旧快照不变、重复导入不增长、非法响应零污染。两个原任务六份材料前后hash一致。没有新ASR/OCR或远端模型调用；本机固定SSE客户端测试也是replay，不能称真实LLM语义分析。

初次实现验证：聚焦90 passed，全量740 passed（`.venv/bin/python -m pytest`，32.73s）。隔离样例 `tmp/proofreading-memory-experiment/validation.json` 记录29条CLI命令（23成功、6预期拒绝）及六份源hash；`run.log`、`pytest-full.log` 保留真实输出。4候选pending时选中0；模拟approve后默认预算选2/3156字符，20000预算选4；其他书/讲者/缺scope/real_human任务均0。停用与修订后最终approved=2、disabled=1、pending=1、audit=10；9份冻结JSON/块复验通过，旧快照未变。原任务六份材料hash一致，0次新ASR/OCR/远端模型调用。

P1出处积累修复后的实际验证：聚焦99 passed，全量749 passed（33.10s），其中9个新增用例覆盖不同请求同经验1 memory/2 occurrences、首次模式保留、同occurrence幂等、拒绝/停用不复活、修订后原候选重导入、整批非法/写失败零污染、显式v1迁移及失败回滚、CLI检查与context逐字不漂移。全量日志：`tmp/proofreading-memory-experiment/fixes/pytest-full.log`。

离线CLI重新执行原29步至 `tmp/proofreading-memory-experiment/fixes/offline/`，另12条CLI验证同经验两个不同human hash/request的提案出处，以及复制旧v1库的显式迁移，共41条（34成功、7预期拒绝）。1个approved v2 memory关联2个occurrences，第二理由/来源hash可从list读取且仍未审核；新增佐证后context JSON和render **byte-for-byte一致**，重复导入换模式保留首次replay，不增加occurrence/版本/审计。旧v1实验库复制后迁移回填5个occurrences，原五张表内容与旧context字节一致；原库/旧冻结工件不回改。产物：`fixes/occurrence-validation.json`、`occurrence-run.log`、`list.occurrences.json`、`context.before-occurrence.*`、`context.after-occurrence.*`、`context.migrated.*`。六份原材料hash仍相同，全部synthetic/replay/offline，0次远端模型/新ASR/OCR调用。

此版只输出可人工用于实验的冻结context/提示词数据块，不调用阶段6，不消费其模型输出自动学习，不接API/Web、不自动发布、不新增OCR/ASR、不做全局复杂对齐/embedding/知识图谱/微调。不自动提升OCR或synthetic到通用记忆。缺真实人工稿，不能声称术语准确性、阅读感或保真质量改善。下一步应由用户提供真实人工修改稿，逐条核验候选，并在冻结上下文的受控对照中评估语义保真；跨书/讲者共享推广另需明确审核设计。
