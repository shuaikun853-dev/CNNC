# AGENTS.md — 中核 SaaS 运维 Agent 接手指南

> 本文件面向后续接手的 AI agent 或开发者。请**先读完本文件**再动代码。

---

## 一、项目定位

中核 SaaS（tenant_id=188605、commodity_pool_id=222）的运维自动化工具集，覆盖：

1. **价格审计全流程**：SaaS 待审批数据 + 中核客户数据 → 调中核 detail 接口取客户最高价 → 调大模型校验标题同款 → 多商城爬取页面价格 → 计算"超客户价 30% / 价格变动 / 最终决策" → 输出中文表头 Excel
2. **监控状态查询**：中核待处理数据 → SaaS 监控接口查采集/同款/价格计算状态 → 输出可推送/需处理分类表
3. **离线诊断 API（旧）**：输入运维问题 → 生成只读 SQL 模板 + 知识库片段（不连库、不执行 SQL）

**核心业务目标**：帮助中核运维团队日清"同款审批 + 价格校验"，让商品从"校验中"变成"校验成功"，并拦截超价/价格异常数据。

---

## 二、主要目录说明

```
cnnc-ops-agent/                       本项目根
├── app/                              FastAPI 服务 + 业务逻辑
│   ├── main.py                       FastAPI 入口（/health /diagnose /workflows/*）
│   ├── config.py                     配置（路径、版本、只读模式）
│   ├── agent/                        旧诊断：scope_guard / planner / sql_selector / safety_guard
│   ├── workflows/                    工作流：base / router / scope_guard / local_access_guard / human_approval_guard
│   │   ├── work_01_saas_approval_json_audit.py   ★ SaaS 同款审批 JSON 审查规则（核心）
│   │   ├── work_01_same_product_approval.py      work_01 规则版
│   │   └── work_02_price_index_feedback.py       work_02 价格指数反馈
│   ├── services/
│   │   ├── title_match_model_adapter.py   ★ 大模型标题同款校验（单条+批量）
│   │   ├── knowledge_loader.py            知识库加载/搜索/写库SQL脱敏
│   │   ├── link_audit_worker.py           链接审计
│   │   └── workflow_knowledge_adapter.py
│   ├── schemas/workflow.py            Pydantic 模型（WorkflowAction 枚举等）
│   └── templates/                    5 个 SQL 模板 YAML（旧诊断用）
├── scripts/
│   ├── audit_saas_excel_file.py      ★★ 价格审计主脚本（全流程入口）
│   ├── audit_saas_json_file.py       JSON 审计（无 Excel）
│   ├── crawl_ofs_fallback.py         OFS 爬虫兜底
│   └── export_excluded_malls.py      京东/震坤行模板导出
├── data/                             输入数据（gitignore，本地才有）
│   ├── saas_json.json                SaaS 审批导出
│   ├── customer_json.json            中核客户导出
│   ├── detail_request.curl           中核 detail 接口 cURL（含登录态）
│   ├── model.env                     大模型 API 配置
│   └── docs/cnnc_ops_knowledge_pure.md  知识库
├── runtime/                          运行输出（gitignore）
│   └── saas状态查询/                 ★ 监控状态导出归档目录
├── tests/                            pytest 测试（577 个用例）
├── tools/                            一次性工具脚本（_ 开头，非核心）
├── docs/                             设计/流程文档
├── zhaoshang_priority_boost_integration/   招商局优先级加速集成
├── 爬取模板/                         京东/震坤行爬取模板（按日期）
├── 商城sourceId映射表/               商城映射
└── requirements.txt
```

**外部依赖项目（不在本仓库）：**

| 路径 | 作用 |
|------|------|
| `E:\.A2026\多商城爬取\multi_mall_crawler` | 多商城页面价格爬取（28 个商城） |
| `E:\.A2026\招商局自动建任务` | 招商局自动建任务爬京东/震坤行等 |
| `E:\.A中核运维\自动拉取` | 自动拉取 SaaS + 中核接口数据（含 .env 认证） |
| `E:\.A中核运维\monitor_status_export.py` | 监控状态导出工具（独立脚本） |
| `E:\.A中核运维\cnnc_to_saas_monitor.py` | 中核→SaaS 监控状态汇总（独立脚本） |

---

## 三、项目入口文件

| 入口 | 作用 | 启动命令 |
|------|------|---------|
| `app/main.py` | FastAPI 服务（旧诊断 + workflow API） | `uvicorn app.main:app --reload --port 8000` |
| `scripts/audit_saas_excel_file.py` | ★ 价格审计全流程（最常用） | 见 TESTING.md |
| `scripts/export_current_monitor_status.py` | ★ 第二步监控状态查询固定入口 | `python scripts\export_current_monitor_status.py` |
| `scripts/process_monitor_no_push_zhaoshang.py` | 监控表 `是否可推送=否` 三文件处理 + 招商局优先爬价 | 见 TESTING.md |
| `cnnc_to_saas_monitor.py`（项目外上级） | 中核→SaaS 监控状态汇总 | `python cnnc_to_saas_monitor.py` |
| `monitor_status_export.py`（项目外上级） | 本地JSON/直接查SaaS监控 | `python monitor_status_export.py --input x.json` |

---

## 四、关键业务流程

### 四-A、第一步：价格审计全流程（第一步对应会话的固定操作范围）

> **第一步标记：开始。** 后续新建会话接手“第一步”时，必须先阅读本节，再阅读本文件第六、七、九节和 `TESTING.md` 的价格审计验证命令。本节是第一步的执行清单；下面原“流程 A”只作为摘要。进入第一步的会话除非用户明确切换步骤，否则只处理第一步，不执行第二步、第三步。

#### 1. 第一步的业务目标

第一步固定指“价格审计全流程”：SaaS 同款审批数据 + 中核客户数据 + 中核 detail 客户最高价 + 标题同款模型校验 + 招商局多商城爬价 + Excel 决策输出。

当用户说“跑截止到现在的数据”“跑全流程”“处理当前数据”时，在本会话中都按第一步理解，不得只拉 100 条、只跑模型、只建爬虫任务或只生成半成品表。

#### 2. 第一步运行前必须具备的本地输入

| 输入 | 固定位置或来源 | 用途 |
|------|----------------|------|
| SaaS 同款审批 cURL | `data/latest_saas_sameAudit_queryAll.curl` | 分页拉取 tenantId=188605、commodityPoolId=222 的当前待处理数据 |
| 中核列表 query cURL | `data/latest_cnnc_query.curl` | 分页拉取当前中核客户数据 |
| 中核 detail cURL | `data/detail_request.curl` | 按 `documentId + fourCategoryId` 补客户最高价 |
| 招商局 add cURL | `data/latest_zhaoshang_add.curl` | 只提取当前建任务认证和浏览器 payload 基准 |
| 招商局 exportTask cURL | `data/latest_zhaoshang_exportTask.curl` | 只提取当前导出认证和导出请求基准 |
| 模型配置 | `data/model.env` | 标题同款模型调用 |
| 招商局优先级数据库配置 | 当前进程的 `ZHAOSHANG_DB_*` 环境变量 | 仅用于当前招商局任务 `weight_type=-1` 调整 |
| 商城 sourceId 映射 | `商城sourceId映射表/` | 确定哪些商城必须走招商局及对应 sourceId |

缺少或无法解析任一必需输入时必须暂停并说明缺失项。不得把真实 Cookie、Token、API Key、数据库密码写入 Markdown、业务代码或日志。

#### 3. 第一步固定执行顺序

1. **读取交接与安全规则**：先读 `AGENTS.md`、`CURRENT_STATE.md`、`TESTING.md`、`README.md`、`.env.example`，确认仍处于中核租户和第一步范围。
2. **解析 cURL，不手工拆 Token**：所有浏览器 cURL 必须由 `scripts/curl_utils.py` 解析，并使用无代理请求。SaaS query、中核 query/detail 可以执行只读连通测试；`crawlTaskManage/add` 是真实写接口，禁止用 POST `/add` 做 Token 或连通性健康检查。add cURL 只能本地解析，第一次真实 `/add` 调用必须是本轮需要创建的正式任务。
3. **拉取完整当前数据**：SaaS 和中核 query 都按接口 `count` 分页拉完，不能因 cURL 示例中的 `limit=10/100` 只取首页。拉取后核对 `实际行数=count`；不相等必须停止。
4. **保存本轮快照**：只在 `runtime/price_audit_<时间戳>/` 保存本轮 `saas_json.json`、`customer_json.json`、`fetch_summary.json` 和日志。不得覆盖 `data/` 下原始文件。
5. **关联 SaaS 与中核数据**：保留全部原字段，按现有主脚本规则匹配客户记录，不能为追求命中率擅自放宽租户、类目或商品边界。
6. **补客户最高价**：调用中核 detail，使用每条记录自己的 `documentId`、`fourCategoryId`；同一请求键只调用一次并复用缓存。最高价取 detail 返回的有效参考价最大值。接口失败、仅一条参考价、无参考价必须分别保留状态，禁止把列表价或 0 静默当成客户最高价。
7. **调用标题同款模型**：调用 `app/services/title_match_model_adapter.py`。先去重、再分批；生产稳定参数默认 `TITLE_MATCH_MODEL_BATCH_SIZE=10`、`TITLE_MATCH_MODEL_BATCH_CONCURRENCY=1`。模型 HTTP、超时、解析失败或缺行必须重试，不能自动降级为“人工复核”；“人工复核”只能是模型成功返回的业务结论。
8. **生成招商局模板**：按商城和 sourceId 拆分，创建任务前必须按商城内商品编码/URL 去重，避免大量重复编码导致商城任务无价格。模板写到 `爬取模板/<日期>/`，不覆盖原始输入。
9. **招商局建任务并立即提优**：每个有 sourceId 的商城必须按 `add -> queryListByPage -> updateState 启用 -> 优先级 SELECT 轮询 -> weight_type=-1 UPDATE -> affected_rows 校验 -> 等待完成 -> exportTask` 执行。任务名必须是 `cnnc_current_opt_<商城名>_<纯数字唯一时间标识>`，每次完全不重复。并发默认 6，单任务等待导出上限 30 分钟。
10. **优先级 SQL 只允许当前任务**：严格遵守本文件第六节。只允许在当前任务三片段范围内，把 `task_status IN ('00','01')` 的 `central_control_sub_zsz.s_task_info.weight_type` 参数化更新为 `-1`。不得执行其他 SQL，不得修改任何其他字段。只有 `affected_rows>0` 才能报告提优成功；若任务已先变成 `02`，记录“未确认提优，任务已完成”，直接进入导出，不得放宽 SQL。
11. **导出招商局结果**：本地结果文件必须使用 ASCII 安全文件名；子进程使用 UTF-8。导出返回“未检索到商品价格信息”时按既有次数重试，仍失败则保留商城、taskId、taskNumber、链接数和原因，不得虚报已取得价格。
12. **回填并重新计算全部价格规则**：首次爬价、补跑、第二次导出或后补价格都必须走同一回填函数，重新计算 `page_vs_customer_check`、`price_changed`、`over_30pct`、`final_decision`。页面价为数值 0 时必须标记“价格为0”并整行高亮；JSON 价格与本次爬取价格不同必须标记价格变动并参与最终决策。
13. **生成最终 Excel**：输出到 `runtime/price_audit_<时间戳>/saas_audit_final_<时间戳>.xlsx`。保留中文表头、全部原始字段、模型结果、客户价、商城价格、状态、决策和高亮，不覆盖历史结果。
14. **最终验收与汇报**：至少汇报 SaaS 总数、中核总数、客户价状态分布、模型结论分布、招商局成功/失败商城、回填数量、价格为 0 数量、价格变动数量、最终决策分布、输出文件和日志路径。父日志没有 `affected_rows` 证据时，不得笼统宣称所有任务都已提优。

#### 4. 第一步必须暂停的情况

- SaaS 或中核分页行数与接口 `count` 不一致；
- 中核 query/detail 认证失败、代理错误或请求头大小写被改变；
- 模型配置存在但模型持续失败；此时保持重试或暂停，不得伪造结果；
- 招商局 `/add` 认证失败、payload 校验失败、商城模板字段错误；
- 招商局当前任务优先级 SQL 超出允许范围，或错误地需要执行其他 SQL；
- 招商局未覆盖、任务失败、导出无价格或仍有未回填链接。

发生以上情况时，先输出失败清单。招商局未爬到价格后，只有用户明确确认，才允许运行本地多商城脚本兜底；不能自动切换。

#### 5. 第一步完成标准

- 当前 SaaS 与中核数据均完整分页拉取并留有本轮快照；
- detail、模型、招商局三个阶段都有可核对状态，不用空值冒充成功；
- 所有可导出的招商局结果均已回填，后补价格也完成价格变动、0 价格和最终决策复算；
- 最终 Excel 可打开，记录数与 SaaS 当前总数一致；
- 只执行了当前招商局任务允许的优先级 SQL，没有审批写接口、生产业务写库或本地脚本自动兜底；
- 最终回复使用本文件“每次任务完成后的输出格式”。

> **第一步标记：结束。** 第二步和第三步不得从本节自行推断，必须分别读取下面的第二步、第三步标记范围。

### 四-B、第二步：截至当前的中核 SaaS 监控状态查询（本会话固定操作范围）

> **第二步标记：开始。** 新建会话接手“第二步”时，必须先阅读本节，再阅读本文件第六、七、九、十一节，以及 `TESTING.md` 中的监控状态查询说明。除非用户明确要求切换步骤，本会话只执行第二步；不得执行价格审计、招商局建任务、审批推送、数据库 SQL 或本地爬虫。

#### 1. 第二步的业务目标

第二步固定指“截至当前的中核 SaaS 监控状态查询”：拉取中核 `sassCheckStatuss=["0"]` 的全部待 SaaS 处理记录，再查询每条记录在 SaaS 监控中的采集、同款匹配、价格计算状态，生成可推送/需处理/SaaS 监控缺失/统计 Sheet 的 Excel。

当用户说“跑截止到现在的数据状态”“查当前的数据状态”“生成状态表”时，按第二步执行。不能只处理 cURL 示例中的 10 条、100 条或某个手工粘贴的商品 ID；必须以中核接口返回的 `count` 为准完整分页。

#### 2. 第二步运行前的必需输入

| 输入 | 固定位置 | 用途 |
|------|----------|------|
| 中核待处理列表 cURL | `data/latest_cnnc_query.curl` | 查询 `sassCheckStatuss=["0"]` 的全部中核待处理数据。 |
| SaaS 监控状态 cURL | `data/saas_zhuangtai.curl` | 查询 tenant_id=`188605`、commodity_pool_id=`222` 的采集/同款/价格状态。 |
| 统一 cURL 解析工具 | `scripts/curl_utils.py` | 解析 URL、请求头、Cookie 和请求体，并设置内网无代理环境。 |
| 汇总工具 | 项目上级 `E:\.A中核运维\cnnc_to_saas_monitor.py` 与 `E:\.A中核运维\monitor_status_export.py` | 复用中核分页、状态解析、Excel 导出和样式逻辑。 |

缺少任一 cURL、`y1AmuISe`、Cookie 或 SaaS `x-access-token` 时必须暂停并说明缺失项。不得手工拆分 Token，不得把认证信息复制到代码、日志、文档、临时 `.py`、`.env` 或输出 Excel。

#### 3. 第二步固定执行顺序

1. **确认范围与只读边界**：只处理 tenant_id=`188605`、commodity_pool_id=`222`；只调用中核列表查询和 SaaS 监控查询接口。禁止审批、推送、保存、提交、建招商局任务、写库和执行任何 SQL。
2. **解析本地 cURL**：必须通过 `scripts/curl_utils.py` 的 `load_curl_command()` 解析 `data/latest_cnnc_query.curl` 与 `data/saas_zhuangtai.curl`。认证信息只能注入当前进程的环境变量；调用 `apply_internal_no_proxy_env()` 并清除 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`，避免内网请求错误走本机代理。
3. **完整拉取中核待处理数据**：按中核接口的 `count` 分页拉取，筛选条件保持 `sassCheckStatuss=["0"]`。拉取结束后校验实际行数等于 `count`；不相等时停止，不生成“全量”状态表。
4. **提取 documentId 并分批查 SaaS**：以中核记录的 `documentId` 查询 SaaS 监控。SaaS 接口单次按商品编码搜索最多支持 **200 条**，超过时必须按 `200` 条分批；请求 `commodityId` 时不附加 `文档ID` 等额外前缀，避免被接口计入第 201 个编码。每批均须检查 `rspCode=0000` 和返回 `count`。
5. **合并并分类**：以 `documentId == commodityId` 合并。中核存在、SaaS 未返回的记录必须保留，标记为 `SaaS监控缺失` 和 `是否可推送=否`，不得丢弃或假设成功。可推送条件固定为 `isSync=1 AND isSyncStandard=1 AND syncPriceStatus!=5 AND newPrice 为可解析、有限且大于 0 的数值`。判定时先检查采集和同款状态，再拦截 `syncPriceStatus=5` 为“价格检测校验失败”，最后校验 `newPrice`；`null`、空字符串、非数字、非有限数值、`0` 和负数均归为“推送价格为空或无效”。除状态 `5` 外，其他 `syncPriceStatus` 均继续执行 `newPrice` 校验。
6. **生成新的状态表归档**：每次运行使用新的时间戳目录，不覆盖历史结果：`runtime/saas状态查询/<时间戳>/中核待处理_SaaS监控状态_<时间戳>.xlsx`。Excel 至少包含 `全量监控结果`、`可推送数据`、`需处理数据`、`SaaS监控缺失`、`状态统计` 五个 Sheet，并显式保留原始 `newPrice` 列供核验。
7. **核验并汇报**：核对中核总数、SaaS 匹配数、缺失数、可推送数、需处理数及各状态分类；最终回复使用本文件第十一节的七段格式，并给出可点击的输出文件路径。

#### 4. 第二步必须暂停的情况

- 本地 cURL 缺失、无法解析，或中核/SaaS 认证返回业务登录错误；
- 中核分页实际行数与接口 `count` 不一致；
- SaaS 任一分批请求返回非 `0000`、返回数量异常，或未按最多 200 条拆分；
- 发现租户不是 `188605`、商品池不是 `222`；
- 需要调用审批、推送、写库、SQL、招商局或爬虫才能继续。

发生上述情况时，只报告已确认的读取结果和阻塞原因；不得用旧状态表、空数据或部分数据冒充“截至当前的全量状态”。

#### 5. 第二步完成标准

- 中核待处理数据已按接口 `count` 完整分页；
- 全部 `documentId` 已按最多 200 条/批查询 SaaS，且每批查询成功；
- 合并行数等于中核总数，SaaS 缺失行被显式保留和分类；
- 新状态表可打开，含五个约定 Sheet 和统计；
- 未改动业务代码、认证文件、`data/` 原始输入，也未执行任何写接口或 SQL。

> **第二步标记：结束。** 第二步完成后，只有用户明确要求处理“需处理数据”时，才能切换到第三步；不得自动进入三文件处理、招商局或爬虫流程。

### 四-C、第三步：处理监控表不能直接推送的数据（第三步对应会话的固定操作范围）

> **第三步标记：开始。** 新建会话接手“第三步”时，必须先阅读本节，再阅读本文件第六、七、九、十一、十三节，以及 `TESTING.md` 的“验证 7”“验证 8”和招商局优先级验收说明。用户说“第三步”“处理这个表不能推送的数据”“处理 `runtime/saas状态查询/<时间戳>`”时，均按第三步执行。进入第三步的会话除非用户明确切换步骤，否则只执行第三步；不得自动执行第一步的全量价格审计、第二步的全量状态查询、模型同款审批或任何审批/推送接口。

#### 1. 第三步的业务目标

第三步固定指：读取第二步生成的中核 SaaS 监控状态 Excel，只处理 `是否可推送=否` 的记录；按每条记录自己的 `documentId + fourCategoryId` 查询中核 detail 并提取参考商城链接；按链接数量分流；对可处理链接先走招商局建任务、最高优先级调整、等待完成和导出；在用户确认后才对招商局仍未取得价格的指定商城运行本地专用脚本；最后生成可导入的 zhimport 模板、处理日志和直接推送失败数据。

第三步只处理用户指定的监控表批次，不重新拉取“截至当前”的全部监控数据。若用户只给出若干商品编码并要求取 detail 链接，必须生成只含这些编码的独立结果或模板，不得把结果追加到历史全量模板而产生额外行。

#### 2. 第三步运行前的必需输入

| 输入 | 固定位置或来源 | 用途 |
|------|----------------|------|
| 监控状态 Excel | `runtime/saas状态查询/<时间戳>/中核待处理_SaaS监控状态_<时间戳>.xlsx` | 筛选本批次 `是否可推送=否` 的记录。 |
| 中核列表 query cURL | `data/latest_cnnc_query.curl` | 必要时按 `documentId` 补充中核记录和四级类目。 |
| 中核 detail cURL | `data/detail_request.curl` | 使用每条记录的 `documentId + fourCategoryId` 查询参考链接。 |
| 招商局 add cURL | `data/latest_zhaoshang_add.curl` | 读取当前建任务认证和浏览器 payload 基准；不得用于健康检查。 |
| 招商局 exportTask cURL | `data/latest_zhaoshang_exportTask.curl` | 读取招商局认证和导出请求基准。 |
| 招商局优先级数据库配置 | 当前进程的 `ZHAOSHANG_DB_*` 环境变量 | 仅用于当前新建或启用任务允许的 `weight_type=-1` 调整。 |
| 招商局优先级本地配置 | `data/ZHAOSHANG_DB.env` | 通过固定入口的 `--zhaoshang-db-env` 仅加载到本次进程；不得打印、提交或写入实际值。 |
| 商城 sourceId 映射 | `商城sourceId映射表/` | 判断商城是否由招商局处理及其 sourceId。 |
| 固定处理入口 | `scripts/process_monitor_no_push_zhaoshang.py` | 完成筛选、detail、分流、招商局、回填和三文件生成。 |
| 史泰博专用脚本 | `E:\.A2026\多商城爬取\multi_mall_crawler\scripts\stbchina_fast_crawler.py` | 仅在已获得用户确认时处理 `stbchina.cn` 缺价链接。 |

所有 cURL 必须通过 `scripts/curl_utils.py` 解析并使用内网无代理环境，不得手工拆 Token。缺少监控表、中核 query/detail cURL、招商局 add/exportTask cURL或需要建任务却缺少 `ZHAOSHANG_DB_*` 时，必须暂停并说明缺失项。不得把真实 Cookie、Token、API Key、数据库账号或密码写入文档、日志、Excel、临时脚本或业务代码。

#### 3. 第三步固定输出

本批次文件统一输出到 `runtime/模板以及推送失败数据/<时间戳>/`，不得覆盖历史批次或输入监控表。最终固定产出：

1. `导入zhimport模版_<时间戳>.xlsx`
2. `处理日志_<时间戳>.xlsx`
3. `直接推送失败的数据_<时间戳>.xlsx`

招商局结束后如仍有缺价链接，还必须生成 `待确认脚本兜底清单_<时间戳>.xlsx` 或等价的明确清单。经用户确认运行史泰博脚本后，补价结果另存为 `导入zhimport模版_<时间戳>_招商局_STB补价后.xlsx`，仍无价格的链接另存为 `STB补价后仍无价格清单_<时间戳>.xlsx`；不得覆盖招商局阶段的模板。

用户进一步明确要求用中核 detail 参考数据补全时，才可显式传入 `--detail-reference-fallback`。该选项只补“价格和互联网标题同时为空”的行，必须按同一 `documentId + 完整 URL` 精确匹配 `referenceInfos[]`，价格取该链接自己的 `referencePrice`，标题取本次 detail 的 `commodityName`，不得覆盖招商局或已授权脚本的结果。结果另存为 `导入zhimport模版_<时间戳>_招商局_STB_DETAIL参考补全后.xlsx`，并生成同名阶段处理日志和 `DETAIL参考补全后仍无价格标题清单_<时间戳>.xlsx`；回填行在处理日志中标记 `中核detail参考回填`，不得把该价格报告为商城实采价。

#### 4. 第三步固定执行顺序

1. **读取交接规则并确认批次**：阅读本节和关联章节，定位用户指定监控表。若目录中有多个 Excel，优先使用 `中核待处理_SaaS监控状态_<时间戳>.xlsx`，不得误用旧模板或其他批次结果。
2. **只筛选不能直接推送的数据**：只处理 `是否可推送=否`。保留原始 `documentId`、四级类目、客户商品描述、状态字段和失败原因；不得把 `是` 的记录混入模板。
3. **查询中核 detail 并提取链接**：优先使用监控表已有的 `documentId` 和四级类目；字段缺失时才使用中核 query 补充。detail 必须逐条使用正确的 `documentId + fourCategoryId`，同一请求键只调用一次并复用缓存。参考链接同时检查 detail 的参考信息数组和顶层参考链接字段，保留原始查询参数并去重。
4. **按有效参考链接数分流**：有效链接 `>=2` 的记录进入 zhimport 处理范围；有效链接为 `0` 或 `1` 的记录进入 `直接推送失败的数据_<时间戳>.xlsx`。任何接口失败、字段缺失或链接解析异常都必须写入处理日志，不得用空链接冒充成功。
5. **补全 URL 和 SKUID/商品编码**：优先使用现有 `SKUID`、商品编码、uid；缺失或明显异常时，再从 URL 的 `itemId`、`skuId`、`id` 或详情路径提取。创建招商局任务前按“商城 + 商品编码/URL”去重。仍无法提取的行写入待确认清单，不得静默创建错误任务。
6. **先走招商局，且一次创建全部可建商城任务**：按商城/sourceId 拆分所有支持招商局的缺价链接；不要只建一个商城后停下，也不要重复创建已有结果的任务。固定顺序为 `add -> queryListByPage -> updateState 启用 -> 优先级 SELECT 轮询 -> weight_type=-1 UPDATE -> affected_rows 校验 -> 等待完成 -> exportTask`。可以按现有并发配置同时处理多个商城，但每个商城均必须独立记录 taskId、taskNumber、链接数、提优证据和导出状态。
7. **招商局任务必须调到最高优先级**：任务名使用 `cnnc_current_opt_<商城名>_<纯数字唯一时间标识>`。严格执行本文件第六节唯一允许的参数化范围 SELECT 和 `weight_type=-1` UPDATE；只有 `affected_rows>0` 才能报告提优成功。任务在可更新前已变成 `task_status='02'` 时，只能记录“任务已完成、未确认提优”并导出，不能放宽 SQL；其他优先级失败必须按第六节暂停处理。
8. **等待完成并导出、回填**：导出结果必须按 URL 或 SKUID 精确匹配到模板行。只有同一次有效采集同时返回价格时，才允许回填商城商品标题、价格、SKUID/uid、评论数等采集字段。没有价格的行不得使用中核客户标题、历史标题或推测标题填入“商城商品标题”，该字段保持空白，只保留链接和可确认的基础字段。
9. **先给缺价清单，再决定脚本兜底**：招商局全部结束后，先统计各商城成功数、缺价数和失败原因，生成待确认脚本兜底清单并向用户确认。未获得明确确认时禁止运行任何本地商城脚本，也不得把尚未确认的三文件报告为最终完成。若用户当前任务中已明确说“史泰博直接跑脚本”或“跑史泰博脚本”，该句话即视为仅对本批次史泰博链接的确认，不需要重复询问；固定入口可在该授权下显式传入 `--auto-stbchina-fallback`，仅自动处理本批次仍缺价的 `stbchina.cn` 链接。
10. **按确认范围运行专用脚本**：只处理用户确认的商城和 URL。史泰博必须先读外部 `AGENT_STBCHINA_FAST.md`，只用 `stbchina_fast_crawler.py`，控制并发和请求间隔，禁止绕验证码、代理池、轮换 UA、自动登录或 stealth。遇到 403、405、验证码或登录限制时停止并记录。
11. **回填后生成最终三文件**：脚本结果仍按 URL 或 uid/SKUID 精确匹配；没有价格的行不得只回填标题。用户拒绝脚本兜底时，以招商局已取得的数据生成最终三文件，并在处理日志及缺价清单中保留未取得价格的链接和原因。只有用户明确要求使用 detail 价格和标题时，才可追加 `--detail-reference-fallback`，按本节固定输出规则另存参考补全模板，不改变招商局/脚本阶段模板。
12. **最终验收与汇报**：核对监控表“否”行数、detail 成功/失败数、参考链接数量分布、进入模板记录数、直接推送失败记录数、各商城招商局任务数、`affected_rows>0` 的提优任务数、招商局回填价格数、脚本回填价格数和最终仍缺价数，并给出全部输出文件和日志路径。

固定入口优先使用独立 cURL 文件参数：

```powershell
python scripts\process_monitor_no_push_zhaoshang.py `
  --monitor-xlsx "runtime\saas状态查询\<时间戳>\中核待处理_SaaS监控状态_<时间戳>.xlsx" `
  --cnnc-query-curl-file "data\latest_cnnc_query.curl" `
  --detail-curl-file "data\detail_request.curl" `
  --zhaoshang-export-curl-file "data\latest_zhaoshang_exportTask.curl" `
  --zhaoshang-db-env "data\ZHAOSHANG_DB.env" `
  --timestamp "<时间戳>" `
  --zs-wait-timeout 600 `
  --auto-stbchina-fallback `
  --detail-reference-fallback
```

只允许修改命令行中的批次路径、时间戳和既有配置参数；禁止为每个批次生成临时执行脚本。`--auto-stbchina-fallback` 和 `--detail-reference-fallback` 都必须分别取得用户对本批次的明确授权。用户明确要求“先不跑招商局，只产出可处理数据”时才可使用 `--skip-zhaoshang`，并且必须在结果中标明未执行招商局、未调整优先级、未运行脚本兜底。

#### 5. 第三步的接口、SQL 和文件边界

- 允许调用中核 query/detail 只读接口、招商局建任务/查询/启用/进度/导出接口；禁止调用中核或 SaaS 的审批、通过、驳回、保存、提交、推送接口。
- 数据库唯一允许的操作是本文件第六节限定的当前招商局任务优先级范围 SELECT 和 `weight_type=-1` UPDATE；禁止执行任何其他 SQL，禁止修改其他字段。
- `中核 detail` 默认只用于取得参考商城链接，不把 `referencePrice` 直接当作商城采集价。仅当用户对本批次明确要求且入口显式使用 `--detail-reference-fallback` 时，才可按同一 `documentId + 完整 URL` 将该链接的 `referencePrice` 和 detail `commodityName` 写入独立的“DETAIL参考补全后”模板；必须标记来源，且不得称为商城实采价。
- 只在 `runtime/模板以及推送失败数据/<时间戳>/` 和已授权外部爬虫项目的 input/output 目录写本轮文件；不覆盖监控表、`data/` 认证文件或历史模板。
- 不持久化、不打印、不回显真实认证信息；所有异常输出必须脱敏。

#### 6. 第三步必须暂停的情况

- 监控表不存在、无法识别 `是否可推送` 列，或筛选行数无法核对；
- 中核 query/detail cURL 缺失、解析失败、认证失败、四级类目无法确定或 detail 返回异常；
- 招商局 `/add` 认证失败、payload/模板字段错误、任务创建或启用失败；
- 当前任务优先级 SELECT/UPDATE 越界、`affected_rows=0` 且任务未完成，或需要执行其他 SQL 才能继续；
- 招商局导出失败、未返回价格或仍有未回填链接；此时必须先给待确认清单，不能自动跑脚本；
- 用户未明确确认脚本兜底，或者脚本遇到验证码、403、405、登录限制、封禁风险；
- 输出文件被 Excel/WPS 占用，或模板行数、链接数、分流数量无法对账。

#### 7. 第三步完成标准

- 只处理了用户指定批次中 `是否可推送=否` 的记录，处理数量与监控表筛选数量一致；
- 每条处理记录都有可核对的 detail 状态和参考链接数量，`>=2` 与 `0/1` 分流数量能够对账；
- 全部招商局支持商城均已建任务或有明确失败记录，且每个任务的提优结论都有 `affected_rows` 或任务已先完成的状态证据；
- 招商局未取得价格的数据已经过用户确认，未确认时没有运行本地脚本；
- 最终三文件可打开，直接推送失败数据行数正确；无价格行没有伪造商城标题或价格；
- 全程没有审批/推送写接口、越界 SQL、临时执行脚本、文件覆盖或敏感信息落盘；
- 最终回复使用本文件“每次任务完成后的输出格式”。

> **第三步标记：结束。** 新建第三步专用会话时，可直接要求：“先阅读 `AGENTS.md` 的第三步标记范围，只执行第三步，处理 `<监控表路径>`。”该会话不得自行扩展到第一步或第二步。

### 流程 A：价格审计全流程（`audit_saas_excel_file.py`）

```
读 SaaS JSON + 客户 JSON
 → 生成京东/震坤行等商城爬取模板
 → 调中核 detail 接口拿客户最高价（缓存，同 documentId/vCommodityId 只调一次；支持旧 `detail` 的 `documentId+fourCategoryId` 请求体，也支持 `standardCommodityModel/queryStandardDetail` 的 `vCommodityId+fourCategoryId` 请求体）
 → 调大模型批量校验标题同款（去重→分批30条→并发12批）
 → 招商局建任务优先爬有 sourceId 映射的商城（并发6，轮询到完成）
 → 招商局未覆盖或未爬到的链接先暂停汇报；只有用户明确确认后，才允许 multi_mall_crawler 脚本兜底
 → 回填所有价格
 → 计算：saas_price_vs_customer / price_changed / over_30pct / final_decision
 → 输出 runtime/saas_audit_final.xlsx（中文表头 + 高亮 + 冻结首行）
```

final_decision 取值：PASS / REJECT_SAAS_HIGH / REJECT_PRICE_CHANGED / REJECT_BOTH

### 流程 B：监控状态查询（`scripts/export_current_monitor_status.py`）

```
调中核接口拉取"待SaaS处理"数据（sassCheckStatuss=["0"]）
 → 拿 documentId 列表
 → 调 SaaS 监控接口查采集/同款/价格计算状态
 → 合并输出 Excel（可推送 / 需处理 / SaaS监控缺失 / 统计）
 → 归档到 runtime/saas状态查询/<时间戳>/
```

可推送条件：isSync=1 AND isSyncStandard=1 AND syncPriceStatus!=5 AND newPrice 为可解析、有限且大于 0 的数值。`syncPriceStatus=5` 优先归为价格检测校验失败；其他状态继续校验 `newPrice`，空值、null、非数字、非有限数值、0 和负数均不可推送。

### 流程 C：离线诊断 API（旧，`app/main.py`）

```
POST /diagnose {question}
 → scope_guard 检查是否中核范围
 → planner 识别场景（weekly/single_item/crawler/relation/task/knowledge_qa）
 → sql_selector 加载 YAML 模板 + 参数校验 + 渲染
 → safety_guard SQL 安全检查（只允许 SELECT，白名单表）
 → knowledge_loader 检索知识库（写库SQL脱敏）
 → 返回诊断步骤 + SQL 模板 + 知识片段
```

---

## 五、模型调用方式

**配置文件**：`data/model.env`

```env
TITLE_MATCH_MODEL_API_KEY=<key>
TITLE_MATCH_MODEL_BASE_URL=https://ark.cn-beijing.volces.com/api/v3/chat/completions
TITLE_MATCH_MODEL_NAME=doubao-seed-2-0-mini-260215
```

**调用入口**：`app/services/title_match_model_adapter.py`

- `call_title_match_model(req)` — 单条调用
- `call_title_match_model_batch(reqs)` — 批量调用（去重 + 分批 + 并发）

**返回**：`TitleMatchModelResult`
- `match_result`: SAME / NOT_SAME / MANUAL_REVIEW / MODEL_UNAVAILABLE
- `confidence`: 0.0-1.0
- `brand_match` / `model_match` / `spec_match` / `risk_points` / `reason`

**环境变量调参**（审计脚本用）：
- `TITLE_MATCH_MODEL_BATCH_SIZE`（默认 30）
- `TITLE_MATCH_MODEL_BATCH_CONCURRENCY`（默认 1，避免批量并发过高导致模型失败）
- `TITLE_MATCH_MODEL_TIMEOUT`（默认 180 秒）
- `TITLE_MATCH_MODEL_RETRY_SLEEP`（默认 5 秒）
- `TITLE_MATCH_MODEL_MAX_RETRIES`（默认不限制，持续重试直到模型调用成功；本地调试可设正整数）

**失败处理**：只要 `TITLE_MATCH_MODEL_API_KEY` / `TITLE_MATCH_MODEL_BASE_URL` 已配置，模型 HTTP、超时、解析失败或批量结果缺行都必须重试，不允许静默降级为 `MANUAL_REVIEW`。未配置模型环境变量时才返回 `MODEL_UNAVAILABLE`。
**安全约束**：不打印 API KEY（只显示 SET / NOT_SET）；不把 model.env 提交 git。

---

## 六、数据库或文件操作边界

| 操作 | 允许 | 禁止 |
|------|------|------|
| 数据库 | 仅允许招商局当前爬虫任务优先级调整 SQL | 除招商局优先级调整之外的任何数据库连接、SQL 执行 |
| SQL 模板 | 只生成 SELECT 模板供人工执行 | 自动执行诊断 SQL、业务查询 SQL、UPDATE/DELETE/INSERT/TRUNCATE/DROP/ALTER |
| 文件读 | data/*.json, data/*.curl, data/*.env | — |
| 文件写 | runtime/*.xlsx, 爬取模板/*.xlsx | 不写 data/ 下任何文件 |
| 审批接口 | 不调用 | 禁止调用通过/驳回/保存/提交接口 |
| 爬虫 | 先走招商局爬取；脚本爬取只能在招商局未覆盖/失败后经用户确认执行 | 招商局失败后自动改走脚本爬取、不绕验证码、不自动登录、不代理池 |

**SQL 额外红线**：除“招商局当前任务优先级调整”这一类 SQL 外，任何 AI 或开发者都不能执行其他 SQL。不得为了排查、统计、验证、补数、诊断而执行额外 SELECT/UPDATE/DELETE/INSERT。招商局优先级 SQL 也必须只作用于当前创建的任务名、商城、批次标识对应范围。

**招商局最高优先级红线**：顺序必须为 `add` 建任务成功 → `queryListByPage` 取得 `taskId/taskNumber/batchNumber` → `enable_task` 启用成功 → 优先级查询与更新。当前浏览器请求的建任务 payload 使用 `taskWeight="0"`，`startEt` 使用建任务当前时间。任务名固定为 `cnnc_current_opt_<商城优先级名>_<纯数字时间标识>`；商城优先级名使用可稳定匹配 `batch_num` 的名称，例如 `得力`、`京东`、`欧菲斯办公用品`，不强制保留“商城”后缀。优先级 SQL 只允许参数化更新 `central_control_sub_zsz.s_task_info.weight_type=-1`，并且必须用当前任务名的三个唯一片段精确限定：`batch_num LIKE CONCAT('%', ?, '%')` 对应 `cnnc_current_opt`、商城优先级名、时间标识，同时限定 `task_status IN ('00','01')`。更新前必须先执行同条件的 `SELECT batch_num, task_status, weight_type, COUNT(1)` 范围确认；更新后只有 `affected_rows>0` 才能报告提优成功。页面 `taskId` 仅用于招商局 API 启用、查询和导出，不作为优先级 SQL 条件。`affected_rows=0` 时必须立即用同一三片段查询最终状态；若任务已全部完成，只记录“任务已完成、未确认提优”并进入导出，若仍有 `00/01` 或无法确认完成，必须暂停并汇报。不得修改 `task_status`、`queue_status`、`is_valid`、`is_main` 或扩大 SQL 匹配范围。

**招商局任务命名与认证红线**：招商局建任务脚本必须从当前进程环境变量读取 `BLADE_AUTH` 和 `COOKIE`，禁止回退到脚本内置真实 Token/Cookie。任务名必须能被优先级 SQL 精确匹配，当前统一格式为 `cnnc_current_opt_<中文商城名>_<纯数字时间标识>`；外部传入的 `CNNC_PRICE_*` 等旧格式任务名会在招商局脚本入口自动归一化。任务名允许保留中文用于招商局页面展示和 `batch_num` 三片段匹配，但本地导出结果 `output_path` 必须使用 ASCII 安全文件名，禁止把中文商城名或被编码成 `????` 的内容写入本地文件名。优先级调整必须在 `enable_task` 成功后执行；由于招商局子任务落入 `central_control_sub_zsz.s_task_info` 可能有延迟，当前优先级工具会按当前任务的三个 `batch_num LIKE` 条件轮询，直到命中 `task_status IN ('00','01')` 后再把 `weight_type` 更新为 `-1`。`affected_rows=0` 时必须用同一三片段复查最终状态；仅当任务已全部完成时可标记 `COMPLETED_UNCONFIRMED` 并导出，其他情况必须失败并中断。

**招商局复用任务红线**：无论本次任务是新建还是因防重复复用已有任务，只要本次流程的 `enable_task` 成功，就必须执行同一套“范围 SELECT → `weight_type=-1` UPDATE → `affected_rows` 校验”。禁止以 `created=True`、任务已存在或任务名已是 `cnnc_current_opt` 为由跳过优先级调整。历史已验证的标准任务名和批次号统一为 `cnnc_current_opt_<商城名>_<标识>`，三片段为 `cnnc_current_opt`、商城名、最后数字标识。优先级 SELECT/UPDATE 只作用于当前三片段范围内的 `task_status IN ('00','01')` 行，且唯一允许的写操作是 `weight_type=-1`。

**招商局落表验证规则**：`enable_task` 成功后不得先执行会延长竞争窗口的多余进度查询，必须立即开始 `task_status IN ('00','01')` 的三片段范围 SELECT 轮询，默认 60 次、每次 2 秒；命中后立即 UPDATE 当前范围的 `weight_type=-1`，再等待任务完成并导出。数据库连接使用 `autocommit=False` 时，每次轮询 SELECT 前必须结束上一轮只读事务并刷新事务快照，否则 MySQL 默认 `REPEATABLE READ` 会让后续新落表行在同一连接中持续不可见；最终状态分布查询前也必须刷新快照。窗口结束仍为空时，只允许在同一三片段 `batch_num` 范围内执行一次只读状态分布查询。若范围和状态分布都为空，必须再用当前精确 `batchNumber` 查询招商局进度 API；只有 API 确认全部完成才可标记 `COMPLETED_UNCONFIRMED` 并导出，否则必须同时检查当前 API 与 `ZHAOSHANG_DB_*` 是否属于同一招商局环境，不得继续通过修改任务名、放宽 SQL 或重复建任务试错。

**招商局优先级失败保护**：正式入口中，若优先级范围查询、状态分布诊断或 UPDATE 任一步失败，必须暂停后续等待和导出并抛错，但不得自动停用已经启用的任务。唯一例外是 UPDATE 与调度器并发竞争且同一三片段的数据库状态分布或招商局进度 API 已确认任务全部完成；此时可记录“任务已完成、未确认提优”并导出，不得报告提优成功。自动停用会阻断尚未完成的调度落表，掩盖数据库环境不一致或平台延迟。保留任务不代表已提速；只有 `affected_rows>0` 才能报告优先级成功。

**招商局状态字典**：`queue_status`：`00` 等待入队、`01` 已入队、`02` 已出队；`task_status`：`00` 待执行、`01` 执行中、`02` 执行成功、`03` 执行失败。优先级 UPDATE 仍只允许作用于 `task_status IN ('00','01')`；命中 `02` 说明任务已成功完成，不能再补调优先级，应直接按已完成任务的导出流程处理。

**招商局主任务 `TASK_WEIGHT` 测试限制**：`zhaosj_data_crawl.dc_main_task_info.TASK_WEIGHT=-1` 只能在用户明确授权时用于本次新建测试任务，并且必须使用接口返回的精确 `TASK_NUMBER` 参数化更新，禁止写死历史任务号。数据库 `affected_rows>0` 和回查值为 `-1` 只证明主任务字段写入成功，不等于调度提优成功；还必须观察 `queryTaskProgress` 从“待执行”推进到执行中或完成。2026-07-11 京东 1 条测试已证明该字段可写但 300 秒内仍未进入执行，因此当前不得把这条 UPDATE 自动接入正式价格审计流程，也不得用它替代已验证的子任务优先级机制。

**cURL 处理与内网代理红线**：浏览器复制出来的 SaaS / 中核 / 招商局 cURL 必须通过 `scripts/curl_utils.py` 统一解析，禁止在运行时把 `--data-raw` JSON 再拼成 PowerShell 命令执行，避免 JSON 引号被 PowerShell 吃掉导致 `JSON parse error`。项目内 Python 请求内网接口时必须绕过系统代理，优先使用 `internal_direct_opener()`；调用招商局外部脚本时必须使用 `internal_subprocess_env()` 清除 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` 并设置 `NO_PROXY`，覆盖 `saasportal.iio.com`、`zhaosj.iioprod.com`、`www.cnncmall.com`、`cnncmall.com` 等内网域名。标题同款模型调用也必须使用无代理 opener，避免本机 `127.0.0.1:7890` 代理连接挂起导致无限重试。排查 token 是否失效前，必须先确认不是代理或 shell 转义导致的失败。

**招商局建任务 cURL 认证规则**：价格审计主脚本优先从 `data/latest_zhaoshang_add.curl` 读取招商局建任务认证；文件不存在时才兼容读取 `data/latest_zhaoshang_exportTask.curl`。add/exportTask cURL 即使其 `Blade-Auth` 的 JWT 本地解析显示过期，也必须原样加载并让 `/add` 接口实际判定认证，因为 Cookie/刷新态仍可能有效；不得因本地 JWT 过期预检跳过任一 cURL。`exportTask`、`getAll`、`queryListByPage` 已验证可在无认证时返回数据，不能单独用于判断 `/crawlTaskManage/add` 的写接口登录态是否有效。运行 `scripts/audit_saas_excel_file.py --zhaoshang-task` 时必须通过 `scripts/curl_utils.py` 原样解析 cURL 的 `Blade-Auth` 和 `Cookie`，禁止人工拆 token。若 `/add` 对空 payload 仍返回 `rspCode=3333`，说明当前写接口认证无效，应更新 add cURL，不得继续批量建任务或改走本地脚本。

**招商局失败诊断规则**：正式入口捕获招商局外部工具异常时，必须在 stdout 输出完整的子进程 stdout/stderr（不截断），同时对 `Cookie`、`Blade-Auth`、`Authorization`、`access-token`、`refreshToken` 等认证值脱敏。必须先依据该完整输出区分认证失败、payload 校验失败或商城模板字段错误，才允许修改建任务逻辑；禁止仅凭截断的异常摘要判断 Token 失效或要求新的 cURL。

**招商局结果路径编码规则**：调用 `auto_crawl_task.py` 的外部子进程必须显式设置 `PYTHONIOENCODING=utf-8`。父进程依赖 stdout 中的“结果文件”行获取 ASCII 安全导出路径并回填价格；不得以系统默认代码页解码子进程输出，否则任务可成功导出但 `result_path` 解析为空、最终 Excel 不会回填。

**爬价顺序红线**：价格审计爬价必须优先使用招商局。招商局任务未走通、未返回价格或商城未覆盖时，必须先暂停并向用户说明失败商城、链接数量和原因；只有用户明确确认后，才允许运行本地多商城脚本或其他脚本兜底。

**本对话固定产出规则**：处理 `runtime/saas状态查询/<时间戳>/` 监控表时，固定产出三类文件到 `runtime/模板以及推送失败数据/<时间戳>/`：`导入zhimport模版_<时间戳>.xlsx`、`处理日志_<时间戳>.xlsx`、`直接推送失败的数据_<时间戳>.xlsx`。处理逻辑是先取监控表 `是否可推送=否` 的数据，去中核 detail 查询参考链接；参考链接数大于等于 2 的写入 zhimport 模板，0 或 1 条链接的写入直接推送失败数据。爬价仍必须先走招商局，招商局失败后暂停，脚本兜底需用户确认。

**固定脚本红线**：监控表三文件处理必须使用 `scripts/process_monitor_no_push_zhaoshang.py` 这个固定入口。不要每个批次临时生成执行脚本；后续只允许通过命令行参数修改 `--monitor-xlsx`、`--auth-curl-file`、`--timestamp`、`--output-base`、`--cnnc-query-limit`、`--zs-wait-timeout`。

**监控表快速产出规则**：当用户明确要求招商局暂不爬取、只先产出可处理数据时，固定入口可使用 `--skip-zhaoshang`。该模式仍调用中核 detail、生成三文件和待确认清单，但不会创建招商局任务、不会执行优先级 SQL、不会运行本地爬虫。独立 cURL 文件可通过 `--cnnc-query-curl-file`、`--detail-curl-file`、`--zhaoshang-export-curl-file` 传入，三项必须同时提供；旧 `--auth-curl-file` 仍兼容。

---

## 七、禁止事项（硬性红线）

1. **不绕过验证码 / 不自动登录 / 不注入 stealth / 不轮换 UA / 不用代理池**
2. **不调用审批/驳回/保存/提交类接口**（只允许查询接口）
3. **不写数据库 / 不执行 SQL**，唯一例外是招商局当前爬虫任务优先级调整 SQL
4. **不持久化 cookie/token/auth-token/access-token/y1AmuISe 到任何文件**
5. **不打印 cookie/token/auth-token/access-token/y1AmuISe 到日志**
6. **不把 detail_request.curl / model.env / .env 提交到 git**
7. **不处理非中核租户**（非 tenant_id=188605 一律拒绝）
8. **不删除已有文件**（除非用户明确要求）
9. **修改 .env / 配置 / 注册表 / 清缓存前必须用中文说明风险并等用户确认**
10. **招商局爬取失败后不得自动跑脚本兜底**，必须先暂停并取得用户确认

---

## 八、修改代码时的原则

1. **先读后改**：编辑前先 Read 目标文件和邻近文件，理解现有惯例
2. **不丢字段**：处理接口数据时保留所有原始字段，未映射的字段保持原英文名
3. **中文列名**：Excel 输出字段用中文（见 COLUMN_DISPLAY_NAMES 映射）
4. **异常处理**：JSON 解析失败提示具体文件；接口失败打印状态码和返回内容；Excel 导出失败提示路径
5. **不写死密钥**：token/cookie 只从命令行参数、环境变量或 .env 读取
6. **不加注释**：除非用户明确要求，不要添加代码注释
7. **遵循现有结构**：新函数放对位置，复用现有工具函数（如 `_norm`、`_extract_rows`）
8. **测试先行**：修改核心逻辑后必须跑 `pytest tests/ -v` 确认不破坏现有测试

---

## 九、高风险操作说明

| 操作 | 风险 | 要求 |
|------|------|------|
| 修改 `data/detail_request.curl` | 影响客户价获取 | 确认登录态有效 |
| 修改 `data/model.env` | 影响标题校验 | 确认 KEY 有效 |
| 修改 `.env`（自动拉取） | 影响 SaaS+中核接口认证 | 用中文说明风险后执行 |
| 修改 `audit_saas_excel_file.py` 核心逻辑 | 影响审计结果正确性 | 必须跑测试 |
| 修改 `safety_guard.py` | 可能放开写库 SQL | 禁止放宽白名单 |
| 修改 `scope_guard.py` | 可能放开非中核租户 | 禁止放宽范围 |
| 招商局建任务 | 消耗招商局配额 | 不要重复建同名任务；爬价必须优先走招商局；建任务后必须自动调最高优先级 |
| 招商局优先级 SQL | 生产库写操作 | 每次建招商局任务后必须执行；只允许调整当前招商局任务优先级，不允许执行其他 SQL |
| 脚本爬取兜底 | 可能绕开招商局任务体系 | 招商局未走通时先暂停汇报，用户确认后才可执行 |
| 全量查询 SaaS 监控（17万条） | 卡死 | 必须加 --max-rows |

---

## 十、后续 AI 接手时必须先看的文件

**必读顺序**（从上到下）：

1. `AGENTS.md`（本文件）
2. `CURRENT_STATE.md` — 当前项目进度和已知问题
3. `TESTING.md` — 如何验证项目
4. `docs/cnnc_price_data_fetch_guide.md` — 中核价格数据获取完整流程
5. `docs/SAAS_AUDIT_FLOW.md` — SaaS 审计 + 爬虫 + 招商局完整流程
6. `docs/cnnc_work_01_same_product_approval.md` — 同款审批业务规则
7. `app/services/title_match_model_adapter.py` — 模型调用（看 prompt 规则）
8. `scripts/audit_saas_excel_file.py` — 审计主脚本（前 200 行看结构）

---

## 十一、文档同步规则

### 1. 文档同步总原则

后续任何 AI 或开发者修改项目时，都必须判断本次改动是否会影响其他 AI 接手项目。

如果只是日常运行、单次查询、单次生成文件、单次执行测试、临时调试，且没有修改代码、流程、配置、模型调用、SQL、输入输出规则，则不需要写入交接文档。

如果本次改动影响项目逻辑、执行流程、模型调用、数据库处理、文件输入输出、配置、测试方式、安全边界、目录结构或后续理解成本，则必须同步更新对应 Markdown 文档。

信息无法确认时必须标注“待确认”，不要自行编造。交接文档只能记录变量名、文件位置、读取路径、用途和占位符；禁止写入真实 API Key、数据库密码、账号、Cookie、Token、生产连接串。

### 2. 需要同步更新文档的情况

只要发生以下任意一种情况，就必须同步更新对应文档：

1. 修改业务流程、Agent 执行步骤、异常处理流程、人工确认节点；
2. 新增、删除或调整处理逻辑、判断条件、过滤条件、字段映射、数据清洗、去重、合并、校验、排序、优先级、兜底、重试、失败处理逻辑；
3. 修改模型调用方式、模型名称、供应商、base_url、LLM Provider、fallback、模型优先级、prompt、结构化输出格式、JSON Schema、模型结果解析逻辑；
4. 修改 SQL、数据库表名、库名、字段名、JOIN、查询条件、筛选范围、写库保护、只读限制、租户或业务边界；
5. 修改输入文件、输出文件、Excel 模板、Sheet 名称、字段名、字段顺序、字段含义、文件命名规则、输出目录、是否覆盖源文件；
6. 新增或调整配置项、环境变量、启动参数、代理、超时、重试、并发数、日志级别、运行模式；
7. 新增、删除、移动重要目录或核心模块，修改入口文件、脚本位置、配置文件位置；
8. 修改启动命令、测试命令、依赖安装方式、人工验证步骤、常见报错排查方式；
9. 修改生产访问规则、数据库写入保护、文件覆盖保护、日志脱敏、Token/Cookie 使用方式、权限控制、高风险操作确认机制；
10. 新增或修改外部依赖、外部 API、爬虫、OCR、RPA、模型服务、浏览器自动化、网络代理；
11. 修复会影响业务结果、模型判断、SQL 生成、文件输出、异常处理、兼容性的 bug；
12. 任何会影响后续 AI 理解或接手项目的改动。

### 3. 不需要同步更新文档的情况

以下情况通常不需要写入交接文档：

1. 单次运行脚本，且没有修改代码；
2. 单次查询数据，且没有新增或修改查询逻辑；
3. 单次生成 Excel、Word、JSON、日志等文件，且没有改变生成逻辑；
4. 单次执行测试，且没有修改测试方式；
5. 临时调试，且没有保留代码改动；
6. 不改变项目逻辑的格式化；
7. 不影响业务含义的错别字修正；
8. 只阅读代码或分析问题，没有修改项目。

但如果上述操作暴露出流程、规则、风险、配置、测试方式存在缺失，且这些信息对后续接手有价值，则应该补充到对应文档。

### 4. 各文档职责

| 文档 | 职责 |
|------|------|
| `AGENTS.md` | 记录 AI 接手规则、修改原则、禁止事项、安全边界、数据库写库保护、文件处理保护、模型调用规范、文档同步规则、敏感信息处理规则。 |
| `CURRENT_STATE.md` | 记录当前项目阶段、已完成能力、当前核心流程、当前模型调用情况、数据库/文件处理范围、已知问题、未完成事项、最近重要修改、下一步建议。 |
| `TESTING.md` | 记录环境准备、依赖安装、启动命令、测试命令、人工验证步骤、关键功能验收清单、常见报错和排查方法。 |
| `README.md` | 记录项目用途、业务背景、主要功能、输入输出、快速开始、目录结构、基本使用方式。 |
| `.env.example` | 记录环境变量模板，只能写变量名、示例占位符和说明，禁止写真实密钥、真实账号、真实密码、真实 Cookie、真实 Token、真实生产连接串。 |

### 5. 文档同步判断规则

每次完成任务后，都必须判断：

1. 是否修改了业务流程？
2. 是否修改了处理逻辑？
3. 是否修改了模型调用或 prompt？
4. 是否修改了 SQL 或数据库范围？
5. 是否修改了文件输入输出？
6. 是否修改了配置或环境变量？
7. 是否修改了项目结构？
8. 是否修改了启动、测试或验证方式？
9. 是否修改了安全边界？
10. 是否新增外部依赖？
11. 是否修复了会影响业务行为的 bug？
12. 是否影响后续 AI 接手？

如果任意一项为“是”，必须更新对应 Markdown 文档。
如果全部为“否”，可以不更新文档，但最终回复中必须说明原因。

### 6. 每次任务完成后的输出格式

后续每次完成任务后，请按以下格式回复：

```markdown
### 1. 本次任务结果

说明本次完成了什么。

### 2. 修改的代码文件

列出修改过的代码文件，并说明每个文件改了什么。
如果没有修改代码，请写：本次未修改业务代码。

### 3. 修改的文档文件

列出同步更新过的 Markdown 文档，并说明每个文档更新了什么。
如果没有更新文档，请写：本次为日常操作或不影响项目逻辑的修改，因此未更新交接文档。

### 4. 文档同步判断

说明本次是否触发文档同步规则，触发原因是什么。

### 5. 测试和验证方式

说明如何验证本次修改。
如果没有执行测试，请说明原因和建议测试方式。

### 6. 风险点

说明是否存在数据库风险、文件覆盖风险、敏感信息风险、模型调用风险、兼容性风险、生产环境风险。

### 7. 后续接手建议

说明其他 AI 后续接手时应该优先阅读哪些文件，以及下一步建议处理什么。
```

### 7. 敏感信息写入规则

- 文档中只能记录敏感信息的变量名、存放位置、刷新方式和读取路径。
- 禁止在文档中写入真实 API Key、数据库密码、账号、Cookie、Token、生产连接串、登录态 cURL。
- 如果用户在对话中提供了真实 Token/Cookie，只能用于本次运行环境或用户明确允许的位置；默认不得写入仓库文件。
- 提交或交接前必须检查 `git status` / `git diff`，确认没有把 `data/model.env`、`data/detail_request.curl`、`.env`、真实认证脚本、运行产物提交出去。

---

## 十二、Git 提交规则

- **只在用户明确要求时才 commit/push**
- commit 前必须检查 `git status` / `git diff`，不要提交 secrets
- commit message 用中文，匹配仓库现有风格（`feat:` / `fix:` / `docs:` 前缀）
- 不要 force-push、不要 amend 已推送的 commit、不要 empty commit
- `.gitignore` 已排除：`.env`、`data/*.curl`、`data/*token*`、`data/*.env`、`data/*.json`、`runtime/`、`*.xlsx`

---

## 十三、监控表三文件处理规则补充：招商局后脚本兜底

### 1. 固定处理目标

处理 `runtime/saas状态查询/<时间戳>/` 监控表时，目标固定产出三类文件：

1. `导入zhimport模版_<时间戳>.xlsx`
2. `处理日志_<时间戳>.xlsx`
3. `直接推送失败的数据_<时间戳>.xlsx`

只处理监控表中 `是否可推送=否` 的数据。处理前必须先从中核侧查询对应商品数据和参考链接。

### 2. 基础处理顺序

必须按以下顺序执行：

1. 读取监控状态表，筛选 `是否可推送=否`。
2. 根据筛选结果去中核侧查询商品数据和参考链接。
3. 按参考链接数量分流：`>=2` 条进入 zhimport 模板，`0 或 1` 条进入直接推送失败数据。
4. 进入模板的数据先补全 URL、SKUID/商品编码、商城标识、商品名称等字段。
5. 对支持招商局的商城，必须先走招商局建任务和导出结果。
6. 招商局跑完后，先生成未爬到价格的数据清单，不要直接跑本地脚本。
7. 用户确认后，才允许对指定商城或指定 URL 跑脚本兜底。
8. 脚本兜底完成后，再回填价格并生成最终文件。
9. 没有价格的行不要自行填入商城侧商品标题；只保留链接、SKUID/商品编码等基础字段，待价格和标题由同一次有效采集结果一起回填。

固定入口：

```powershell
python scripts\process_monitor_no_push_zhaoshang.py `
  --monitor-xlsx "runtime\saas状态查询\<时间戳>\中核待处理_SaaS监控状态_<时间戳>.xlsx" `
  --auth-curl-file "<包含中核和招商局curl的文本文件>" `
  --timestamp "<时间戳>"
```

禁止为单个批次复制或生成新的临时执行脚本。

### 3. 招商局优先规则

- 支持招商局的商城，必须先建招商局任务。
- 招商局没有返回价格时，不允许立刻跑脚本兜底。
- 必须先输出未爬到价格的数据清单，让用户确认是否跑脚本。
- 用户确认前，禁止自动调用本地爬虫兜底。
- 招商局任务失败、token 失效、任务无商品信息时，应记录原因并暂停，不得静默跳过。

### 4. SKUID/商品编码补全规则

创建招商局任务前必须尽量补全 `SKUID` / 商品编码：

1. 优先使用已有字段中的 `SKUID`、`商品编码`、`uid`。
2. 如果字段为空或异常，应从 URL 中提取 `itemId`、`skuId`、`id` 或商品详情路径中的明显商品编码。
3. 对史泰博 `stbchina.cn` 链接，优先从 `itemId` 提取 uid。
4. 对补全后的模板应另存新文件，不覆盖原始模板。
5. 如果 SKUID 仍无法提取，应写入日志或待确认清单，避免招商局建任务失败时无法排查。

### 5. 招商局补跑规则

招商局第一次跑完后仍有价格缺失时：

1. 先检查缺价格行是否因为 SKUID 缺失或提错导致。
2. 补全 SKUID 后，只对“仍无价格且属于招商局支持商城”的行重新建招商局任务。
3. 不要重复跑已经有价格的行。
4. 招商局补跑完成后可输出补跑过程文件，例如 `导入zhimport模版_<时间戳>_skuid补全.xlsx`、`导入zhimport模版_<时间戳>_招商局补价后.xlsx`、`招商局补价后仍无价格清单_<时间戳>.xlsx`。
5. 如果补跑后剩余无价格数据全部为非招商局商城，则必须等待用户确认是否跑脚本兜底。

### 6. 史泰博脚本兜底规则

史泰博 `stbchina.cn` 链接只能使用专用脚本：

- 项目目录：`E:\.A2026\多商城爬取\multi_mall_crawler`
- 说明文档：`E:\.A2026\多商城爬取\multi_mall_crawler\AGENT_STBCHINA_FAST.md`
- 脚本：`scripts\stbchina_fast_crawler.py`

标准命令示例：

```powershell
cd "E:\.A2026\多商城爬取\multi_mall_crawler"

python scripts\stbchina_fast_crawler.py `
  --input "input\stbchina_<时间戳>_missing_urls.txt" `
  --output "output\stbchina_<时间戳>_missing_result.xlsx"
```

执行要求：

1. 只允许输入 `stbchina.cn` 商品链接。
2. 输入文件每行一个 URL。
3. 脚本依赖用户已授权 cookie，读取位置为 `cookies\stbchina.cn.tsv` 或 `cookies\stbchina.cn.json`。
4. 禁止绕验证码、破解登录、突破权限、代理池、轮换 UA、注入 stealth。
5. 如果出现 403、405、验证码、登录限制，应停止或记录失败原因，不得规避。
6. 回填模板时按 `商品链接/URL` 匹配，回填价格、商品标题、uid/SKUID、评论数。
7. 回填后另存为新文件，例如 `导入zhimport模版_<时间戳>_招商局_STB补价后.xlsx` 和 `STB补价后仍无价格清单_<时间戳>.xlsx`。
8. 如果某条 URL 没有成功采集到价格，不得只回填商品标题；无价格行的商城侧标题字段应保持为空。

### 7. detail 参考数据显式补全规则

只有用户对本批次明确要求使用 detail 价格和商品标题时，固定入口才可追加 `--detail-reference-fallback`：

1. 只处理招商局和已授权脚本结束后，价格、互联网标题仍同时为空的行。
2. 必须按同一 `documentId + 完整 URL` 精确匹配 `referenceInfos[]`；价格取该链接自己的 `referencePrice`，标题取本次 detail 的 `commodityName`。
3. 不覆盖已有任一价格或标题，不把 detail 参考价格称为商城实采价。
4. 另存“DETAIL参考补全后”模板、阶段处理日志和剩余清单，阶段日志将来源标为 `中核detail参考回填`。

### 8. 已验证案例

批次 `20260626194714` 已验证：

1. 招商局补跑后仍无价格 28 条。
2. 其中史泰博 `stbchina.cn` 链接 20 条。
3. 使用史泰博专用脚本采集成功 20 条、失败 0 条。
4. 回填后剩余无价格 8 条。
5. 输出文件包括 `E:\.A2026\多商城爬取\multi_mall_crawler\output\stbchina_20260626194714_missing_result.xlsx`、`runtime\模板以及推送失败数据\20260626194714\导入zhimport模版_20260626194714_retry2_招商局_STB补价后.xlsx`、`runtime\模板以及推送失败数据\20260626194714\STB补价后仍无价格清单_20260626194714_retry2.xlsx`。

批次 `20260721_233040` 已验证：405 条模板记录中，招商局有价 275 条、史泰博回填 52 条；剩余 78 条按同一单据和完整 URL 使用 detail 参考价及 `commodityName` 补全，已有 327 条未改，最终同时缺价格和标题 0 条。

### 9. 注意事项

- 不要把真实 cookie、token、access-token、auth-token 写入文档。
- 不要覆盖原始模板，补跑和回填都必须另存新文件。
- 本地 Python 在中文路径下可能出现路径编码问题，必要时可复制到 ASCII 临时目录处理，再复制回目标目录。
- 临时脚本和临时文件处理完应清理，正式输出文件保留。
