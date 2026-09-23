# AGENTS.md - 中核 SaaS 运维 Agent 总规则

> 本文件只放长期稳定的总规则。三步业务细节在 `docs/runbooks/`，并行状态协议在 `CURRENT_STATE.md`，每个批次的实时进度在自身运行目录的 `BATCH_STATE.md`。不要把运行历史追加到本文件。

## 1. 项目边界

- 唯一业务范围：中核 `tenant_id=188605`、`commodity_pool_id=222`。
- 日常工作固定为三步；受控补推是用户明确发起的专项维护动作，不属于每日三步，也不得自动衔接到任一步骤。除非用户明确切换，否则一个会话只处理一个步骤或一个专项维护批次。
- 旧 `app/main.py` 诊断 API 只生成只读 SQL 模板，不连接数据库、不执行 SQL。
- 工作流、审计结果和 Excel 结论默认只是建议。仅允许三个受控写例外：第一步最终审计表 F 列红/黄色互联网数据驳回、F 列非红非黄色且当前仍待审数据通过，以及用户提供准确 SaaS 文档 ID 清单后按 `docs/runbooks/SAAS_CONTROLLED_PUSH.md` 执行的受控价格补推；其他审批、驳回、通过、保存、提交或推送仍禁止。

## 2. 新会话最小读取集

不要再默认通读四份大文档和全部历史设计。按以下顺序读取：

1. `AGENTS.md`。
2. `CURRENT_STATE.md`；它只描述并行批次的发现和交接方式，可以读完。
3. 接管已有批次时，读取用户指定批次目录中的 `BATCH_STATE.md`；新建批次时不通读其他批次状态。
4. 根据用户请求只读一个步骤手册：
   - 第一步：`docs/runbooks/STEP_1_PRICE_AUDIT.md`
   - 第二步：`docs/runbooks/STEP_2_MONITOR_STATUS.md`
   - 第三步：`docs/runbooks/STEP_3_NO_PUSH_PROCESSING.md`
   - 用户明确要求按文档 ID 补推时：`docs/runbooks/SAAS_CONTROLLED_PUSH.md`
5. 需要运行或改代码时，只读 `TESTING.md` 对应章节和将要执行的入口代码。
6. `README.md` 仅用于项目概览，不是每次接管必读文件。

`docs/handoff/`、`docs/SAAS_AUDIT_FLOW.md`、`docs/cnnc_price_data_fetch_guide.md` 等是历史或扩展资料。它们与当前 runbook 冲突时，以 `AGENTS.md`、`CURRENT_STATE.md`、对应 runbook 和当前代码为准。

## 3. 三步路由

| 用户表达 | 固定步骤 | 入口 | 不能自动做的事 |
|---|---|---|---|
| “第一步”“跑全流程”“跑截止现在的价格审计” | 第一步：价格审计 | `scripts/audit_saas_excel_file.py`，但运行前必须先取得本轮完整快照 | 第二步、第三步、未经确认的本地爬虫兜底、未二次授权的驳回 |
| “第二步”“查当前状态”“生成状态表” | 第二步：监控状态查询 | `scripts/export_current_monitor_status.py` | 审批、推送、SQL、招商局、爬虫、第三步 |
| “第三步”“处理不能推送的数据”“处理这个监控表” | 第三步：不能推送数据处理 | `scripts/process_monitor_no_push_zhaoshang.py` | 重新跑第一/二步、未经确认的脚本或 detail 参考补全 |
| “推送这些文档 ID”“补推这些数据” | 专项维护：SaaS 价格补推 | `scripts/push_saas_commodity_price.py` | 每日三步、未锁定清单的写入、对发送结果不确定的记录重试 |

用户说“处理当前数据”但没有给出足够上下文时，先结合已给路径和批次状态判断；仍可能对应多个步骤或多个批次时再问一个简短问题。步骤完成后不得自动进入下一步。

## 4. 全局生产红线

1. 所有浏览器 cURL 只能通过 `scripts/curl_utils.py` 解析；不得手工拆 Token，不得把 JSON 重新拼成 PowerShell 命令。
2. SaaS、中核、招商局请求必须绕过系统代理，使用现有无代理工具；排查认证前先排查代理、JSON 转义和中核请求头大小写。
3. `crawlTaskManage/add` 是真实写接口。禁止用 `/add` 做 Token、`--help` 或连通性测试；第一次真实调用必须是用户要求的正式任务。
4. 禁止调用中核或 SaaS 的审批、驳回、通过、保存、提交、推送接口。仅有三个窄例外：用户对指定第一步最终 Excel 二次明确授权后，可使用 `scripts/reject_saas_audit_excel.py` 处理 F 列红/黄色行，或使用 `scripts/approve_saas_audit_excel.py` 处理 F 列非红非黄色且当前仍待审的行；用户提供准确 SaaS 文档 ID 清单并明确要求补推时，可按 `docs/runbooks/SAAS_CONTROLLED_PUSH.md` 使用 `scripts/push_saas_commodity_price.py`。三个入口都必须先只读预览并锁定候选数和清单 SHA-256，再由用户对准确文件、数量、SHA-256 和动作二次授权；写请求逐次验证 `rspCode=0000`，发送后不得自动重试。补推必须先通过监控 query 将每个文档 ID 唯一映射到关系 `id`，用户声明已经手工发送但结果不确定的关系 `id` 必须标记为跳过，不得重发。一次授权不得跨动作、文件或批次沿用。
5. 禁止执行任何 SQL,仅两个窄例外:既有招商局工具对本轮当前任务执行的优先级范围查询及 `weight_type=-1` 参数化更新;以及 `STEP_1 Dashboard SaaS Flow Diagnostic`(`app/services/saas_flow_diagnostic.py`)用指定只读数据库账号执行后端预定义、参数化 SELECT,scope 永久固定 `tenant_id=188605`、`commodity_pool_id=222`。浏览器与 API 不得提交或接受任何 SQL 文本、tenant、pool、schema、表名;只允许 SELECT/read-only CTE,禁止 UPDATE/DELETE/INSERT/REPLACE/MERGE/TRUNCATE/DROP/ALTER/CREATE 等一切 DML/DDL,也不提供执行 SQL、priority 调整、审批、驳回、重试、建任务等写入口。该例外不扩散到旧 `/diagnose` API、普通 workflow、STEP_2、STEP_3 或其他 Agent SQL;数据库故障只降级 SaaS Flow 面板,不影响主看板。
6. 招商局优先级只能通过既有受控工具执行。必须以精确 task_name 取得 taskNumber 和配置 mall code，并传入本次 payload 的有效子任务数；用 mall 索引、marker 时间窗轮询到数量一致的子任务主键，再按主键当前读锁定和更新。不得把 taskNumber 当唯一标识，不得手写临时 SQL、放宽主键范围、改其他字段或试探其他表。
7. 只有日志存在 `affected_rows>0` 才能报告“已提优”。若主键范围预查已全部为 `weight_type=-1`，只记录“已验证为最高优先级”的幂等状态，不称为本次提优。`task_status` 只表示采集进度，不得用于过滤 priority 查询或更新；无论任务正在执行还是已经完成，全部精确有效子任务都必须回读确认为 `weight_type=-1`。其他实际更新 0 行、回读不一致或异常必须暂停。
8. 完整 STEP_1 或人工 multi-site 流程遇到招商局失败、未覆盖、导出无价格或仍有缺价时，先生成清单并暂停；只有用户对本批次明确授权，才允许运行指定本地脚本。大江 `b.zdet.net` 不支持招商局，完整 STEP_1 必须绕过招商局；只有通过 STEP_1 人工 multi-site manifest 的候选数、SHA-256 和文件二次授权后，才可对 exact `b.zdet.net` URL 运行固定 revision 的 `zdet` 脚本，不得伪造 sourceId 或招商局映射。唯一无需逐批二次授权的例外仍是 `docs/runbooks/STEP_1_INCREMENTAL_SCHEDULER.md` 定义的固定 8 站增量自动兜底：最终远端查询成功后，仍无有效价格的 exact URL（含 remote `NO_RESULT` / `TERMINAL_FAILED`）可原子 claim；本地失败不得覆盖原 remote 终态证据。incremental `--shadow` 只关闭招商局建任务/重启写能力，不关闭既有任务的只读 harvest，也不关闭满足门禁后的该内建本地兜底；此例外不得扩散到完整 STEP_1、第三步或其他脚本。
9. 不绕验证码、不自动登录、不破解权限、不注入 stealth、不轮换 UA、不使用代理池。遇到 403、405、验证码或登录限制时停止并记录。
10. 不打印、不持久化真实 API Key、Cookie、Token、账号、密码、数据库连接串或登录态 cURL。文档只能写变量名、路径和占位符。
11. 不覆盖 `data/` 输入或历史 `runtime/` 批次；每次生产运行使用新时间戳目录。不得删除或清理用户文件，除非用户明确要求。
12. 当前工作区可能已有未提交改动和大量运行产物。先看 `git status`，只改任务需要的文件，不回滚、不清理、不顺手提交。
13. SaaS 和招商局 cURL 只作为接口模板及 `data/auth_state.json` 不存在时的一次性 Token 初始化来源。状态文件建立后必须以其中分系统保存的 Token 为唯一运行时来源；刷新失败不得回退 cURL。普通只读请求明确返回 401 时最多刷新并重试一次；`/add`、审批、驳回、提交、推送等写请求只允许调用前刷新，发出后不得自动重试。

## 5. 授权判定

- 用户明确说“跑第一步全流程”，即授权本批次必要的只读查询、模型调用、招商局正式建任务和受控优先级调整；不授权人工本地脚本兜底。增量 Scheduler 的固定 8 站自动兜底仅按上一节窄例外执行。
- 第一步不默认授权驳回或通过。只有用户在审计表生成后再次指定准确 Excel 路径、颜色范围和动作，才授权该文件当次的受控驳回或通过；授权不得沿用到另一动作、其他文件或批次，预览锁定后候选数或清单 SHA-256 改变必须暂停并重新授权。
- 受控补推不因用户给出 cURL 或文档 ID 清单而自动获得写授权。必须先生成新批次预览；用户随后明确指定该批次目录、`push_candidate_count`、`execution_plan_sha256` 并再次说“执行补推”，才授权当次写入。预览后必须说明执行会在写前原子刷新 `data/auth_state.json`；用户的二次执行授权同时视为同意本批次这一次认证刷新。任何已手工发送、超时或结果不确定的关系 `id` 都不得自动重试。
- 用户明确说“跑第二步”，只授权中核列表和 SaaS 监控只读查询。
- 用户明确说“跑第三步”，授权该批次 detail 查询和默认招商局流程；若用户说“先不跑招商局”，必须使用 `--skip-zhaoshang`。
- `--auto-stbchina-fallback`、`--detail-reference-fallback` 必须分别获得本批次明确授权。一次授权不得沿用到下一批次。
- 修改认证文件、`.env`、数据库配置、注册表、代理或清缓存前，必须用中文说明影响并等待用户确认。

## 6. 上下文生命周期

### 并行任务边界

- 一个任务只处理一个步骤或一个专项维护批次；新步骤或新批次使用新任务，第三步新批次必须带第二步输出的准确监控表路径。
- 三个步骤可以并行；同一步骤可以同时存在多个活跃批次，只要使用不同批次 ID 和运行目录。
- 其他批次不得成为当前批次的阻塞条件。Agent 只对自己负责的批次做防重复检查，不因其他批次为 `ACTIVE`、`WAITING_USER` 或 `BLOCKED` 而拒绝执行。
- 同一个批次同一时刻只能由一个任务执行生产动作。接管前必须确认原任务已停止操作，并按状态文件处理其后台进程，禁止两个任务并发续跑同一批次。
- `WAITING_USER` 或 `BLOCKED` 的批次在条件满足后可以在原任务继续，不强制换任务；批次 `COMPLETE` 或用户明确 `ABANDONED` 后，原任务才转为历史记录。

### 上下文提醒

- 达到约 20 轮用户/Agent 往返、经历 3 次以上失败重试，或已产生大量工具输出时，提醒用户可在下一个安全节点换任务，同时继续执行当前请求。
- 达到约 30 轮时，更新本批次 `BATCH_STATE.md` 并给出接管提示词，更明确地建议换任务，但仍继续执行，不能把当前工作推给尚未建立的新任务。
- 用户没有换任务时继续处理；上次提醒后每增加约 10 轮才再次提醒，避免反复打断。
- 上下文轮数本身不得作为暂停、拒绝或延后执行的理由。只有真实授权缺失、接口或数据失败、安全红线、目标批次不明确或无法排除重复写操作时才能暂停。

### 批次状态写法

- `CURRENT_STATE.md` 是稳定的并行状态协议，不写任何批次实时进度。每个批次在自己的运行目录维护唯一权威 `BATCH_STATE.md`。
- 新批次创建目录后立即写 `ACTIVE`；等待授权写 `WAITING_USER`；真实外部阻塞写 `BLOCKED`；完成写 `COMPLETE`；用户明确放弃写 `ABANDONED`。
- 至少记录：步骤、批次/输入、授权、已经完成且不得重复的动作、输出、阻塞、下一条准确动作、后台进程和最近上下文提醒。
- 不写 Token、Cookie、密钥、密码、完整请求头或数据库值，也不得修改其他批次的状态文件。
- 任何真实建任务、启用、提优、导出、驳回、通过、补推或脚本兜底完成后，都应立即更新本批次状态，避免接管 Agent 重复执行。

### 交接回复

需要换会话时，回复中必须包含：

1. 当前安全状态和未完成事项。
2. 已完成且不得重复的生产动作。
3. 相关输入、输出和日志路径。
4. 可直接复制的接管提示词；提示词必须写明步骤、`BATCH_STATE.md` 路径和已授权/未授权项。提供提示词后仍继续当前任务，除非用户选择换任务或存在真实暂停条件。

## 7. 修改代码和文档

- 先读目标文件和邻近测试，沿用现有结构；保留原始字段和中文 Excel 列名。
- 核心逻辑改动必须增加或更新回归测试。测试范围按 `TESTING.md` 选择。
- 业务流程、安全边界、输入输出、参数或验证方式发生变化时，同步更新对应 runbook 和 `TESTING.md`。
- `AGENTS.md` 只在跨步骤总规则变化时更新；`README.md` 只在项目用途、入口或快速开始变化时更新。
- `CURRENT_STATE.md` 只保留并行发现和交接协议；批次实时状态只写入各自 `BATCH_STATE.md`。历史经验若仍有价值，提炼进对应 runbook，不复制整段事故记录。
- 交接文档体积受 `tests/test_context_documents.py` 约束。新增内容前先删掉重复或过期内容。

## 8. 完成与 Git

- 最终回复简要给出：结果、生产动作、输出路径、验证、未决风险和下一步。没有内容的栏目不要硬凑七段。
- 只有用户明确要求时才 commit/push。提交前检查 `git status`、`git diff` 和敏感信息。
- 禁止 force-push、擅自 amend、硬重置或回滚用户改动。
