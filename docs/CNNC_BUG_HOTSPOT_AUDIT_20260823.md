# CNNC Bug Hotspot Audit

- 日期：2026-08-23
- 仓库：`E:\.A中核运维\cnnc-ops-agent`
- 生产分支：`prod/closeout-20260818`，HEAD `44d5500`（docs(recovery): record mall-mapping fixture fingerprint found during release gate）
- 审计性质：只读调查。未修改任何生产代码（app/ scripts/ 均未触碰，仅新增本文档）。
- 工作区说明：审计开始时工作区已有本地改动（`scripts/monitor_stb_fallback.py`、`scripts/process_monitor_no_push_zhaoshang.py`、`tests/test_monitor_stb_fallback.py`），本次审计未触碰。全部 Git 证据取自提交历史（HEAD 44d5500），不受本地改动影响。

---

## 1. Executive Conclusion

**历史 Bug 最多的模块（按 Bug 起源地 ORIGIN 计）**：

1. `scripts/audit_saas_excel_file.py` + `scripts/export_current_monitor_status.py`（audit-excel 时代脚本群）——7 个已确认 Bug（2026-06~07），但该时代已被增量管道架构取代，当前风险低。
2. `app/services/legacy_zhaoshang_adapter.py`（招商 legacy 适配层）——当前时代（2026-08 增量管道）最多的 Bug 起源地：4 个本地起源事故 + 08-19 风暴三事故的共同契约边界。**这是现在仍然活跃的头号热点。**
3. `app/services/recovery/`（recovery 包，Phase 1A 拆分前后视为同一逻辑模块）——自身起源 2 个 Bug（均出生于 08-19 风暴当天、36 小时内被修），同时是 ≥8 起事故的"收拾残局"地点。

**未来风险最大的模块**：

1. `legacy_zhaoshang_adapter` ——全部远端副作用的唯一出入口，外部契约假设最密集，15 个测试文件围护仍持续出新故障形态。
2. `incremental_audit_runtime` ——编排中枢 + submit gate + 恢复入口接线；**HEAD 上存在已验证的潜伏 NameError**（`runtime.py:456,500` 捕获未导入的 `RecoveryRejected`）。
3. `daily_audit_store` ——历史起源 Bug ≈ 0（纪律好），但它是全部 durable 状态的载体，任何事务/租约回归的爆炸半径是全系统。

**最值得下一步优化（TOP 3 详见第 9 节）**：

1. freeze/finalize 跨源缺口守卫（P0：2026-08-20 曾以 5 行快照误 finalize 出正式表，修复未落库）+ runtime 两个潜伏 import Bug。
2. `legacy_zhaoshang_adapter` 契约硬化（类型化诊断 + 四阶段协议 golden fixture）。
3. recovery 包共享前置段收敛（Phase 1B，消灭 fanout 类 per-function 漂移）。

**关于 Phase 1A recovery 的直接回答（第十二节问题）**：
FACT：recovery 不是历史 Bug 最多的 ORIGIN。它自身起源的确认 Bug 只有 2 个（b3f2bd0 修的 fanout 门、06e2c6a 修的 still-running 诊断入口），且都是 08-19 风暴当天在高压下新增代码的次生 Bug。它同时是 ≥8 起事故的 RECOVERY/FIX location——那 8 起的 ORIGIN 在 adapter 验收门（2 起）、招商远端行为（3 起）、模型终态分类（1 起）、wait-export 语义（1 起）、freeze 门（1 起）。Phase 1A 拆分针对的"五类演进压力 + 60% 样板重复"与证据吻合：recovery 的真实弱点不是逻辑错误率高，而是 10 个 run 函数各自复制 15+ 个 fail-closed 前置检查导致的漂移风险——fanout Bug 正是这种漂移的实例。

---

## 2. Methodology

证据来源（四路交叉）：

- **A. Git 历史**：分支全部 70 个 commit（2026-06-15..2026-08-23），23 个 `fix:` commit 的完整 diff 逐个通读（排除 docs/formatting/rename/refactor 后确认全部为真实 Bug 修复或防护补强）；`safety/prod-*` 快照分支序列还原 08-18→08-20 事故时间线；全部 `codex/*` 分支确认已并入 prod。
- **B. 回归测试**：56 个测试文件（29,067 行）全量清单；事故型命名测试函数逐条摘录；测试文件→生产模块映射（import/monkeypatch 目标）。
- **C. 历史文档 + runtime 记录**：AGENTS.md / CURRENT_STATE.md / TESTING.md / docs/runbooks/* / docs/superpowers/{plans,specs}（2026-07-17..2026-08-23）/ docs/architecture/* / `runtime/incremental_price_audit/<date>/BATCH_STATE.md`（2026-08-11..2026-08-21，含 `2026-08-20_superseded_final5_1907`）。
- **D. 当前源码**：store 事务/租约机制（`BEGIN IMMEDIATE`+`busy_timeout`+fenced lease）、recovery facade 纯 re-export 形态、SUBMIT_UNKNOWN 处理路径、scheduler 注册脚本（IgnoreNew）、`scheduled_process_host.cs`（Job Object kill-on-close）、潜伏 NameError 现场验证。

方法论说明：任务要求的 `obra/superpowers` 三个 skill（systematic-debugging / writing-plans / verification-before-completion）未安装于本机，无法加载；本审计按其纪律执行——每条结论标注 FACT/INFERENCE/HYPOTHESIS/UNKNOWN，完成前对关键 FACT（如 NameError、freeze 门语义）在 HEAD 源码上复核。Windows 命令遵循 `windows-shell-verified`（显式 git.exe/python.exe，UTF-8 由 Python subprocess 处理规避 cmd 代码页问题）。

ORIGIN / DETECTION / RECOVERY / FIX 四分法应用于每条 ledger 记录。LOC 仅作次要证据（见第 4 节表内说明）。

评分模型（0–100 Bug Hotspot Score，权重）：

| # | 维度 | 权重 |
|---|------|------|
| 1 | Confirmed historical bug count（ORIGIN 计） | 30% |
| 2 | Bug-fix commit frequency | 15% |
| 3 | Regression-test density | 10% |
| 4 | State-machine complexity | 10% |
| 5 | Remote side-effect exposure | 15% |
| 6 | Crash/retry exposure | 5% |
| 7 | Production blast radius | 10% |
| 8 | Current coupling/churn | 5% |

---

## 3. Evidence Ledger

图例：类型 = 第七节的 A–N 分类；强度 = HIGH（diff+test+文档三者吻合）/ MEDIUM（两类）/ LOW（代码推断）。

| Bug ID | Date | Symptom | Origin module | Detection | Recovery module | Fix module | Commit | Regression test | Impact | Root-cause category | Strength |
|---|---|---|---|---|---|---|---|---|---|---|---|
| B-01 | 06-16~17 | agent 工作流安全缺陷：scope 未强制、人工审批标志未强制、风险词放行、无效客户价参与 30% 规则 | app/workflows + app/main（agent 时代） | 评审 R2/R3.5/R4 | — | 同 origin | a9babae, c14cdf6, da5aa82, f90bc90, 5ab314a, db28818 | test_work_01_* / test_human_approval_guard 等 | 不安全自动审批风险（产品期，未上调度线） | A/N | MEDIUM（部分为评审驱动而非生产事故） |
| B-02 | 06-19 | 标题匹配模型冷启动超时 | title_match_model_adapter（默认 8s） | 生产 | 人工重试 | 同 origin | 42b5d24 | test_title_match_model_retry | 模型阶段批量失败 | E | HIGH |
| B-03 | 06-19 | 爬虫子进程收相对路径（cwd=爬虫目录）找不到输入 | scripts/audit_saas_excel_file.py 子进程调用 | 生产 | — | 同 origin | 3588fbc | （时代脚本，无专门回归） | 爬取任务失败 | F/N | HIGH |
| B-04 | 06-22 | 导出工具多段 JSON 缺右括号/尾部垃圾字符 → 解析失败 | 外部导出工具数据；解析在 scripts | 生产 | — | scripts（JSON healing） | 514ea4e (+c5f1769 扩展) | （内嵌） | 数据行丢失 | L | HIGH |
| B-05 | 06-23 | price=0 被当"缺失"处理：合法零价被排除/误分类；任务名 mall_key→mall_cn 错位 | scripts 数据语义 | 生产 | — | 同 origin | c5f1769 | （时代脚本） | 业务数据正确性 | L | HIGH |
| B-06 | 06-23 | 零价行未高亮 | scripts Excel 呈现 | 人工 | — | 同 origin | 5a8bf05 | — | 报表可读性 | L | HIGH（次要影响） |
| B-07 | 06-26 | 任务名秒级时间戳碰撞 → `skip_if_exists` 静默跳过建任务 | scripts 任务命名 | 生产 | 人工单任务补建 | 同 origin | 186614a | test_zhaoshang_resume_modes 等 | 任务未创建/重复风险 | B/I | HIGH（handoff 文档佐证） |
| B-08 | 06-23 发现 | 同名 `run_zhaoshang_tasks` 双定义遮蔽——代码阅读与真实行为背离 | scripts 巨石脚本 | 维护审计 | — | 服务抽取时删除遮蔽定义（2026-08-11 plan Task 1） | （df96f56 时代） | AST 唯一性测试 | 维护事故温床 | N | MEDIUM |
| B-09 | 07-29 | 空/0/负 newPrice 与 syncPriceStatus=5 被判"可推送" | scripts/export_current_monitor_status.py | 生产推送前核查 | — | 同 origin | d85369d (+2963c75, c92cc6) | test_export_current_monitor_status | 错误推送风险 | L | HIGH（plan 文档 Step 6 红灯记录） |
| B-10 | 08-12 | 离线/测试环境缺 data/model.env 时 RuntimeConfigurationError | incremental_audit_runtime 配置耦合 | 测试 | — | 同 origin（fallback 名） | bef9d15 | test_incremental_* | 离线工具不可用 | M | MEDIUM |
| B-11 | 08-13 | 远端已完成但本地 harvest 整表验收过严 → `REMOTE_EXISTING_HARVEST_MANUAL` 大面积卡死（jd 240 行等）、FinalizeBlocked、36 manual、10 MODEL 终态 | legacy_zhaoshang_adapter 整表验收门 | runtime BATCH_STATE | recover-remote-harvest（溯源：批次号==task_name） | recovery + adapter | （08-13 工作树时代，矩阵测试在库） | test_incremental_audit_recovery | 全天 finalize 阻塞 | A/I/L | HIGH（BATCH_STATE.md:151,238,297） |
| B-12 | 08-14 | 54 个 MODEL 项 TimeoutError 耗尽重试被判 TERMINAL_FAILED（传输故障当终态） | title_match_model_adapter 重试耗尽分类 + 阶段标记 | runtime BATCH_STATE | requeue-model-transient（窄条件） | recovery | （同上时代） | recovery model 测试 | 54 项误终态 | E/K | HIGH（BATCH_STATE.md:84） |
| B-13 | 08-14 | 部分导出（缺行/SKU 列不一致/AF0620700 fanout 歧义）→ all-or-nothing 验收卡 MANUAL | legacy_zhaoshang_adapter 验收门 | runtime BATCH_STATE | recover-partial-exact-harvest | recovery + adapter + store | （同上时代） | test_partial_exact_harvest, test_recovery_fanout_gate | 任务卡死/manual 残留 | A/I | HIGH（spec + BATCH_STATE:207-211） |
| B-14 | 08-18 | closeout 路由/回滚不安全：20:00 自动 finalize、closeout 预算 720s 不足、ExecutionTimeLimit 14min 过紧 | scheduler PowerShell + closeout 编排 | 生产晚间窗口 | — | run/register ps1 + runbook + tests | ffb7be7 (47856aa) | test_closeout_before_1830, test_incremental_audit_operations | 晚间出表受阻/过早 finalize 风险 | F/J | HIGH |
| B-15 | 08-19 | 招商远端 subtask rows 未物化 → `PriorityUpdateZeroRowsError`，任务卡 PENDING/MANUAL，且系统继续向瘫痪远端新建任务 | 外部招商后端（ORIGIN）+ 本地缺 stall breaker（共同起源） | runtime remote_jobs 证据 | recover-remote-priority-zero-rows（活探测再分类） | **stall_breaker（新）** + recovery + runtime submit gate + adapter 诊断分类 + store 证据查询 | 7da7c04 | test_remote_priority_zero_rows_recovery / test_remote_stall_breaker / test_remote_stall_classification | 风暴核心：新建任务全部注定失败 | C/E/A | HIGH |
| B-16 | 08-19 | 新加的恢复身份门要求 payload↔linked 计数器完全相等，拒绝合法 source fanout（crawl 423/job71、427/job72）→ 恢复卡死 | **recovery 包自身**（18:28 引入，20:00 即修） | 生产恢复操作 | — | recovery（validate_linked_crawls_against_remote_payload 单点共享门） | b3f2bd0 | test_recovery_fanout_gate（含 case10_job71 用例） | 恢复通道不可用 | A/I | HIGH（commit message 直引生产 ID） |
| B-17 | 08-19 | job54 wait-export MANUAL 无出口，永久卡死 | 状态机缺口：legacy wait-export-only "requires manual review" 语义 + 无恢复路径（起源分布在 adapter 语义/状态机设计） | runtime BATCH_STATE | run-remote-wait-export-recovery | recovery（新增） | e6fb943 | test_remote_wait_export_recovery（17 tests） | 任务永久 manual | A/J | HIGH（分支名 fix/job54-*） |
| B-18 | 08-19 | 远端任务完成但导出"未检索到商品价格信息"count=0 → 无终态路径（wait-export 卡/NO_RESULT 误判风险） | 远端导出语义 + 状态机缺口 | runtime BATCH_STATE | resolve-remote-export-no-price | recovery + adapter(probe) + store(终态证据) + finalize(接受 NO_RESULT) | 85d9e63 | test_remote_export_no_price_resolution | 无价格商品悬空 | A/I | HIGH（BATCH_STATE:668） |
| B-19 | 08-19 | 新 probe 用 RW-taskNumber 查进度（应为 batchNumber）；无价格应答可缺 count 字段 → 解析崩 | **legacy_zhaoshang_adapter**（当天 22:46 新增代码，22:52 即修） | 生产 | — | adapter | 5cf8ccf | test_remote_export_no_price_resolution（新增 2 用例） | 探测路径不可用 | L/K | HIGH |
| B-20 | 08-20 | wait-export 恢复只接受 MANUAL 诊断，而它自身有界等待会持久化 "still running" 诊断 → 真正还在跑的远端无法再次进入恢复 | **recovery 包自身**（前一天引入） | 生产 | — | recovery（接受集合扩为 2） | 06e2c6a | test_remote_wait_export_recovery | 长任务恢复通道断 | A/E | HIGH |
| B-21 | 08-20 | **19:07 以 5 行快照 finalize 正式表**（saas_count=5、customer_count=193、source_count_gap=188），后以 629 行重跑取代 | freeze/finalize 门信任 SaaS API 自身 count，无跨源缺口守卫 | 人工发现（正式表过小） | 人工重跑 + 目录改名 superseded | **无代码修复落库**（runbook 明确 customer_count 不参与 freeze gate） | 无 | 无 | **已发生一次错误正式表** | J/D | HIGH（runtime 双目录铁证） |
| B-22 | 08-20 | priority UPDATE 建任务后即遇 DB InterfaceError → unconfirmed | 外部 priority runner DB 路径 | batch state 事件 | PRIORITY_UNCONFIRMED(可重试) 事件 + 后续恢复 | adapter/外部工具 | （外部，无库内 commit） | test_zhaoshang_priority_boost | 提优证据链断裂（可恢复） | C/H | MEDIUM |
| B-23 | 08-23 发现 | **潜伏**：`runtime.py:456,500` 捕获未导入的 `RecoveryRejected` → 触发即 NameError；adapter:1720 缺 `Sequence` 导入 | runtime / adapter import 块 | 重构基线审计 | — | **未修**（计划 Phase 2.10，需授权） | 无 | 计划先写回归测试 | targeted-harvest 异常路径被 NameError 掩盖 | K/N | HIGH（本次审计在 HEAD 复核确认 import 块 44-56 行无该名字） |
| B-00 | 史前 | 招商任务重复创建"16 变 32"（POST 超时后盲目重发） | legacy 提交流程 | 生产 | — | batch 契约设计（独立编号分片证明已存在） | （仓库外/早期） | batch 契约测试 | 远端重复任务 | B/C | MEDIUM（仅文档记载，日期 UNKNOWN） |

模块×Bug 起源汇总（ORIGIN 计，含共同起源按主归属折算）：

| Origin 模块 | 确认 Bug 数 | Bug IDs |
|---|---|---|
| scripts audit-excel 时代脚本 | 7 | B-03..B-09 |
| legacy_zhaoshang_adapter | 4（+3 起共同起源） | B-11, B-13, B-19,（B-07 co）+ B-15/17/18 契约边界共同起源 |
| recovery 包（自身） | 2 | B-16, B-20 |
| app/workflows（agent 时代） | 1 簇（≈4 项） | B-01 |
| title_match_model_adapter | 2 | B-02, B-12 |
| incremental_audit_runtime | 2（1 潜伏） | B-10, B-23 |
| scheduler PowerShell | 1 | B-14 |
| freeze/finalize 门 | 1（未修复） | B-21 |
| daily_audit_store | 0 | — |

---

## 4. Historical Bug Frequency Ranking（Ranking A）

按 ORIGIN 计的确认 Bug 数；Bug-fix commit 数为触达该模块的 fix commit 数（含作为 FIX location 的情形，故大于 origin 数）。

| Rank | Module | Confirmed bugs (origin) | Bug-fix commits | Regression evidence | Main bug types | Confidence |
|---|---|---|---|---|---|---|
| 1 | scripts/（audit-excel 时代：audit_saas_excel_file.py, export_current_monitor_status.py） | 7 | 10（scripts_py 合计） | 少量专门测试（时代后期补） | L 数据语义/解析 ×4、B/I 任务身份 ×1、F/N 子进程/环境 ×2 | HIGH（git+docs+plan 三源吻合；**时代已终结**） |
| 2 | app/services/legacy_zhaoshang_adapter.py | 4 + 3 共同起源 | 3 | 15 个测试文件（全库最高） | L 契约/解析、A 验收门、I 身份映射、C 远端副作用 | HIGH |
| 3 | app/services/recovery/（Phase 1A 前后同一模块） | 2 | 5（app/* 内最高密度） | 7 个测试文件 + facade 契约测试 | A 状态门漂移、E 诊断入口集合 | HIGH |
| 4 | app/workflows/（agent 时代 work01 + guards） | 1 簇 ≈4 项 | 8（workflows 合计） | test_work_01_* / guard 测试群 | A 业务规则缺口、N 审批安全 | MEDIUM（评审驱动为主） |
| 5 | app/services/title_match_model_adapter.py | 2 | 2 | 4 个测试文件 | E 超时/重试、K 错误伪装 | HIGH |
| 6 | app/services/incremental_audit_runtime.py | 2（1 潜伏未修） | 4 | 6 个测试文件 | M 配置耦合、K 潜伏 NameError | HIGH |
| 7 | scheduler PowerShell + scheduled_process_host.cs | 1 | 1（scheduler_ps） | 2 个测试文件（进程树/路由） | F 进程生命周期、J finalize 时机 | HIGH |
| 8 | freeze/finalize 门（workflows/finalize 协作） | 1（B-21，**未修复**） | （无 fix commit） | test_closeout_before_1830 部分覆盖 | J 过早 finalize | HIGH（runtime 铁证）/修复状态 UNKNOWN |

LOC 参考（次要证据）：adapter 2147 / runtime 1905 / store 1903 / recovery 包拆分后合计 2403（拆分前 2183）/ tick workflow 1312 / finalize 727。测试合计 29067 行。

---

## 5. Future Reliability Risk Ranking（Ranking B）

| Rank | Module | Risk score | Blast radius | Reason |
|---|---|---|---|---|
| 1 | legacy_zhaoshang_adapter | 79/100 | 全管道（唯一远端副作用出口；错 submit→重复远端任务，错 harvest→错价格入正式表） | 契约怪癖密集（batchNumber vs taskNumber、count 可缺、零行物化延迟、wait-export 人工语义、16→32 史前事故）；诊断靠异常类型名+消息前缀分类；15 个测试文件围护仍持续出新形态；B-23 潜伏 import 缺陷在册 |
| 2 | incremental_audit_runtime | 55/100（历史起源少但结构风险高） | 全管道（编排中枢：tick 预算、submit gate、全部恢复命令接线） | 已验证潜伏 NameError（B-23）；submit_gate 是单点 choke；状态机复杂度最高之一；08-19 一天内 4 个 fix 触达 |
| 3 | daily_audit_store | 41/100（历史起源 0 是优点） | 灾难级（全部 durable 真相；事务/租约/fencing 回归即全系统状态损坏） | 1903 行 93 个方法全走 BEGIN IMMEDIATE+busy_timeout+fenced lease，纪律成熟；17 个测试文件；历史无起源 Bug——风险纯来自未来改动 |
| 4 | recovery 包 | 63/100 | 中高（恢复通道本身失效则事故变人工马拉松） | 风暴新代码；10 个 run 函数 60% 样板重复 = 漂移温床（B-16 即实例）；Phase 1A 刚拆分，1B 未做；诊断入口集合枚举易漏（B-20 即实例） |
| 5 | scheduler PS/.cs + closeout/finalize 门 | 40/100 | 高（进程树、晚间出表窗口、正式表正确性） | B-21 过早 finalize 无守卫（未修）；IgnoreNew 饥饿仅注册层断言、无端到端复现测试；ExecutionTimeLimit 刚调 32min 待观察 |
| 6 | workflows/incremental_price_audit_finalize + materializer | ~35/100 | 高（正式表内容正确性；publish 崩溃窗口） | crash-after-publish 已有幂等回归；NO_RESULT 证据链已钉；风险中等 |

（adapter 79、recovery 63、audit-excel 时代脚本 62——后者带"时代已终结"标记、当前风险实际低于分值——、tick workflow 60、runtime 55、finalize ~49、store 41、scheduler 40、model 39。）

---

## 6. Module-by-Module Findings

### 6.1 app/services/legacy_zhaoshang_adapter.py（2147 行，63 函数）
- FACT：当前时代 Bug 起源 No.1：B-11（整表验收过严）、B-13（all-or-nothing 验收）、B-19（probe 用错标识符 + count 缺失崩）；B-15/17/18 的契约边界共同起源。
- FACT：15 个测试文件直接围护（全库最高），含 `test_submit_timeout_is_unknown_and_never_retried`、`test_harvest_preserves_two_skus_sharing_one_url_as_distinct_results`（sibling SKU 不借价）等钉死性用例。
- FACT：诊断分类机制 = 异常类型名 frozenset + 消息前缀（`remote_not_materialized_diagnostic`），07da7c04 引入；史前 bare `PriorityUpdateZeroRowsError` 无法区分零行与校验失败，只能靠活探测再分类。
- INFERENCE：四阶段协议（create→priority→progress→export）各自独立实现标识符选择与响应解析，是 B-19 类"新代码复用错标识符"的结构性原因。
- 潜伏：B-23 之 `Sequence` import 缺失（annotation-only，风险低）。

### 6.2 app/services/incremental_audit_runtime.py（1905 行）
- FACT：编排中枢；`_run_runtime_command_locked` 内接 submit_gate（remote_stall_breaker durable 门，"gate 必须先于 claim，否则烧掉 post_attempted 证据"——workflows/incremental_price_audit.py:648-656）。
- FACT：B-23 潜伏 NameError 已在 HEAD 验证（456/500 行 except 表达式引用未导入名；import 块 44-56 行无 `RecoveryRejected`）。触发条件：targeted-harvest 回退路径收到 RecoveryRejected——NameError 将掩盖原异常并使该命令崩溃。
- INFERENCE：tick 预算/租约/门控/恢复接线集中一处，是"改一处、牵全身"的高耦合点；08-19 风暴中 4 个 fix 触达本文件。

### 6.3 app/services/daily_audit_store.py（1903 行，93 方法）
- FACT：历史 Bug 起源 0；全部写事务 `BEGIN IMMEDIATE` + `busy_timeout=5000`；lease 有 fencing 验证（`_validate_lease` 在事务内）+ 续租/接管/过期语义，配套 test_daily_audit_store 的 ProcessLock/lease 崩溃接管回归。
- FACT：08-19 风暴中被 3 个 fix 作为 FIX location 扩展（stall 证据只读查询、export no-price 终态证据、requeue 变体）——扩展方式均为加方法而非改既有事务结构。
- INFERENCE：这是全库纪律最好的核心模块；未来风险来自体量（93 方法）与持续加法式演进。

### 6.4 app/services/recovery/（Phase 1A 拆分后 2403 行；拆分前单文件 2183 行）
- FACT：2 个自身起源 Bug（B-16、B-20）均诞生于 08-19 风暴 36 小时内的高压新增代码，且都是"per-function 前置检查漂移/入口集合漏枚举"形态。
- FACT：≥8 起事故的 RECOVERY/FIX location：B-11（remote-harvest 溯源）、B-12（model 窄 requeue）、B-13（partial-exact）、B-15（zero-rows）、B-17（wait-export）、B-18（export no-price），另加 SUBMIT_UNKNOWN 精确对账、no-result 解析等常备通道。
- FACT：Phase 1A 为 MOVE-only 结构拆分（1c2733c 先钉 facade 契约，43f863f..f975b6f 移动，`recovery/__init__.py` 仅 docstring、facade 46 行纯 re-export）；重构动机文档明确为"五类演进压力共存 + 10 个 run 函数 60% 样板重复"，非特定历史 Bug。
- INFERENCE：样板重复正是 B-16 的根因形态（身份门在多个函数各写一份、语义不一致）；收敛共享前置段是消灭该 Bug 类的直接手段。

### 6.5 app/workflows/incremental_price_audit.py（tick 引擎，1312 行）
- FACT：submit_gate 防线（"gate 必须在 claim 之前"注释直接源自事故教训）；状态机枚举 PENDING→PRIORITY_PENDING→SUBMITTED→RUNNING→EXPORTED→COMPLETED/SUBMIT_UNKNOWN/MANUAL_REQUIRED。
- FACT：agent 时代 B-01 簇发生于此层（2026-06）；freeze 门（row_count==saas_count + 600s 稳定窗）在此实现——B-21 表明该门信任外部 count 无跨源校验。
- INFERENCE：MANUAL_REQUIRED 的出口集合靠恢复命令逐个补齐（B-17/18/20 模式），状态机缺少"每个 MANUAL 必须有文档化出口"的完整性约束。

### 6.6 app/workflows/incremental_price_audit_finalize.py + closeout/materializer（727+250+437 行）
- FACT：finalize 十类 readiness 计数守卫 + "blocked readiness 绝不写正式表"回归；crash-after-publish 幂等回归在库。
- FACT：B-21 中 freeze→finalize 链通过（5 行即出正式表），证明 readiness 计数不覆盖"源本身不完整"场景；`customer_count` 明确不参与 freeze gate（runbook + ffb7be7 diff 双证）。
- INFERENCE：NO_RESULT 上限（10）与 export 证据类型枚举是好的防线，但跨源缺口是当前最大单点。

### 6.7 scheduler：run/register ps1 + scheduled_process_host.cs
- FACT：B-14 后定型——18:00-20:00 closeout（20:00 最后一次自动）、无 20:00 后自动 finalize、closeout 预算 1680s、ExecutionTimeLimit 32min；IgnoreNew + 非阻塞 OS file lock + fenced DB lease 三层防重叠；Job Object kill-on-close 杀整进程树（.cs:31-101, 292-335）。
- FACT：IgnoreNew 仅注册层断言（`MultipleInstances == "IgnoreNew"`），无"错过 tick"端到端复现；无 orphan 实际事故文档记录（只有 32min 兜底设计）。
- UNKNOWN：IgnoreNew 饥饿是否会在长 closeout + 15min 唤醒组合下真实发生。

### 6.8 model adapter（title_match_model_adapter.py）+ model 恢复
- FACT：B-02（8s→30s）、B-12（TimeoutError 耗尽被标 TERMINAL_FAILED，54 项误终态，靠窄 requeue 救回）；现有 3 attempts/300s 单调预算 + "错误不得伪装成人工复核"规则（TESTING.md:151）。
- 现状良好；test_title_match_model_retry 354 行钉死预算钳制。

### 6.9 scripts/（audit-excel 时代 + incremental CLI）
- FACT：历史起源 No.1（7 起）但时代已终结：核心逻辑已于 2026-08-11 抽取为 app/services（saas_audit_core 等），巨石脚本进入维护态；shadowed 双定义（B-08）已清除并有 AST 守卫。
- `scripts/incremental_price_audit.py`（CLI）本身无确认 Bug 起源，仅随恢复命令扩展。

---

## 7. Bug Origin vs Recovery Location（recovery 专析）

| 维度 | 结论 | 依据 |
|---|---|---|
| recovery 是否历史 Bug 最多的 ORIGIN？ | **否**。自身起源确认 Bug 仅 2 个（B-16、B-20），且都是 08-19 风暴当日新增代码的次生缺陷 | git：b3f2bd0 修 7da7c04（同日 18:28→20:00）引入的门；06e2c6a 修 e6fb943（次日）引入的入口集合 |
| recovery 是否最大 RECOVERY location？ | **是**。≥8 起事故的恢复/修复落点 | B-11/12/13/15/17/18 + SUBMIT_UNKNOWN 对账 + no-result 解析 |
| 那些事故的 ORIGIN 在哪？ | adapter 验收门（B-11、B-13）、招商远端行为（B-15、B-17 语义、B-18 语义）、模型终态分类（B-12）、freeze 门（B-21）、外部 priority DB（B-22） | 各 ledger 行 |
| 典型"接锅不背锅"证据 | B-15：远端未物化是外部起源；recovery/runtime 只是检测与恢复方；修复主体是新增 stall_breaker（阻止问题扩大）而非 recovery 逻辑错误 | 7da7c04 diff 结构 |
| 典型"recovery 自己造 Bug"证据 | B-16：fanout 身份门语义错（payload 计数器 ≠ linked 计数器，source_id 维度被忽略）；B-20：自身持久化的诊断不在自己的入口集合里 | commit message 直引 job71/72、crawl423/427 |
| Phase 1A 是否被证据支持？ | 支持，但理由准确表述为：**降低漂移风险与演进压力**（60% 样板重复、五类压力共存），而非"recovery 历史 Bug 最多所以拆它" | docs/superpowers/plans/2026-08-23-cnnc-architecture-refactor.md 动机段 |

结论（FACT）：08-19 风暴的叙事不是"recovery 模块质量差"，而是"外部远端行为 + 状态机出口缺失把 recovery 推成风暴中心，高压当天新增的恢复代码自身又产生 2 个快速修复的次生 Bug"。

---

## 8. Root Cause Clusters

**不是很多独立小 Bug——是少数系统性问题反复表现成很多 Bug。** 五个簇：

| 簇 | 覆盖 Bug | 占比（23 起确认中） | 性质 |
|---|---|---|---|
| C1 招商远端契约边界的未声明假设（标识符选择、响应形状、物化时机、错误语义） | B-04, B-07(co), B-11, B-13, B-15, B-17, B-18, B-19, B-22, B-00 | ≈10/23 | 系统性：单一外部依赖的行为假设分散硬编码在各阶段代码里 |
| C2 异常但合法的远端结局缺少终态路径（MANUAL_REQUIRED 死端） | B-11, B-13, B-15, B-17, B-18（+B-20 的入口漏枚举同族） | ≈5/23 | 系统性：状态机出口靠事故逐个补齐 |
| C3 零价/空值/无效价 vs 缺失的语义混淆 | B-05, B-06, B-09, B-01(db28818) | ≈4/23 | 系统性：数据语义约定未前置统一定义 |
| C4 风暴日高压热修的次生 Bug（新增代码 <36h 内再修） | B-16, B-19, B-20 | 3/23 | 过程性：同日三连修均指向"新代码缺少共享前置段/入口矩阵" |
| C5 环境/进程/配置 | B-03, B-10, B-14, B-21 | 4/23 | 混合：B-21 单列（freeze 门信任外部 count，最重） |

INFERENCE：C1+C2 合计 ≈15/23——如果只做一件事，应该是"把招商契约显式化 + 给每个 MANUAL 状态定义出口"。

---

## 9. TOP 3 NEXT OPTIMIZATION TARGETS

> 本轮仅建议，不开始修改。均需人工授权后按既有 TDD/计划纪律执行。

### Target 1：freeze/finalize 跨源缺口守卫 + runtime 潜伏 NameError 修复
- **优先级：P0**（B-21 已在 2026-08-20 产出过一次错误正式表；B-23 是已验证的潜伏崩溃路径）
- 模块：`app/workflows/incremental_price_audit.py`（freeze 门）+ `incremental_audit_runtime.py`（import）
- 为什么：freeze gate = `row_count == saas_count` + 600s 稳定窗，saas_count 来自 SaaS API 自身；`customer_count`/`source_count_gap` 仅观测（runbook 明文）。当远端 count 本身错误（5 vs 实际 629），无任何防线。
- 历史 Bug：B-21（未修复，靠人工 supersede）；B-23（Phase 2.10 已规划未做）。
- 主要根因：信任单一外部计数源；import 完整性无静态检查。
- 现在最大的设计弱点：freeze 决策没有第二独立信号；"customer_count 不参与 gate"是为防误阻塞做的正确取舍，但缺一个"跨源差距异常大→挡下并要求人工确认"的中间档。
- 建议优化方向：(a) `source_count_gap` 超阈值（如 >N 或 >50%）时 freeze 前必须人工确认或延迟窗口；(b) 修复 `RecoveryRejected`/`Sequence` 导入（先写回归：模拟 run_existing_remote_harvest 抛 RecoveryRejected 断言被分类而非 NameError）；(c) 可选：CI 加 pyflakes/ruff F821 类未定义名检查防再犯。
- 是否需要改变行为：**是**（freeze 更严格 + 人工确认路径）——需明确授权与运营配合；import 修复本身不改正常路径行为。
- 风险：过严挡住合法小数据日 → 需要带 override 的确认流程而非硬阻塞。

### Target 2：legacy_zhaoshang_adapter 契约硬化
- **优先级：P1**
- 模块：`app/services/legacy_zhaoshang_adapter.py`
- 为什么：当前时代 Bug 起源 No.1（4 起 + 3 起共同起源）；全部远端副作用唯一出口；测试密度最高仍持续出新形态（B-19 证明新代码仍会复用错标识符）。
- 历史 Bug：B-11, B-13, B-19（+B-15/17/18 契约边界、B-07 共同、B-00 史前）。
- 主要根因：四阶段协议的标识符/响应形状假设散落；诊断分类靠类型名+消息前缀。
- 现在最大的设计弱点：无 golden 契约 fixture——batchNumber/taskNumber 选择、count 可缺、rspDesc 枚举、"未检索到商品价格信息"等知识只活在代码与个别测试里。
- 建议优化方向：(a) 类型化 RemoteContract 层集中定义每阶段的标识符选择与响应 schema 校验；(b) 每阶段 golden-file 回放 fixture（真实响应样本脱敏入库）；(c) 诊断分类表驱动化（延续 7da7c04 已开的 frozenset 方向）；(d) 保持 stall_breaker 证据契约不动。
- 是否需要改变行为：否（内部硬化 + fail-closed 分类，不改流程语义）。
- 风险：触碰 submit 路径——以现有 15 个测试文件 + 新增 golden 回放为安全网；分阶段小步。

### Target 3：recovery 共享前置段收敛（Phase 1B）
- **优先级：P1**（依赖 Phase 1A 已建立的 facade 契约门）
- 模块：`app/services/recovery/`
- 为什么：FIX location 承载 ≥8 起事故且自身 2 起 Bug 均为样板漂移形态；重构计划已诊断"10 个 run 函数 60% 重复前置段 = 最大重复源"，fanout Bug（B-16）是该诊断的实证。
- 历史 Bug：B-16, B-20。
- 主要根因：身份/冻结范围/证据不可变等 15+ 个 fail-closed 前置检查逐函数复制，语义各自漂移；持久化诊断字符串与恢复入口集合无单一对照表。
- 现在最大的设计弱点：新增一个恢复通道 = 复制整段前置 + 手工枚举可接受错误集合（B-20 的失败模式）。
- 建议优化方向：(a) 表驱动共享前置门（把 validate_linked_crawls_against_remote_payload 的单点化模式推广到其余前置）；(b) "持久化诊断 → 可用恢复命令"矩阵测试（入口完整性由测试枚举保证）；(c) 按 2026-08-23 计划推进 1B（去重不重设计）。
- 是否需要改变行为：否（consolidation only，由 test_recovery_facade_contract 钉住）。
- 风险：低（外部调用面 2 个生产导入点；1A 刚验证过拆分安全性）。

---

## 10. Things NOT To Change Yet

1. **daily_audit_store 的事务/租约纪律**——历史起源 0；BEGIN IMMEDIATE+fencing 模式成熟，勿"优化"重写。
2. **SUBMIT_UNKNOWN 精确对账协议**（immutable intent→POST→对账，禁盲重发）——设计正确且回归最密（never posts twice 系列）。
3. **stall_breaker**——08-19 新生防线，durable 证据派生方向正确，先观察再动。
4. **scheduler 的 IgnoreNew + 32min ExecutionTimeLimit + Job Object 组合**——08-18 刚定型，调参前需积累运行数据。
5. **Phase 1A facade 契约与目录形态**——当天刚钉住，让 1B 按计划走。
6. **audit-excel 时代巨石脚本**——已被服务抽取取代，历史热点但 ROI 低，维持现状。
7. **closeout 时间窗（18:00-20:00、20:00 末班）**——刚经事故调优（B-14），勿再动路由。

---

## 11. Unknowns / Missing Evidence

- UNKNOWN：B-00"16 变 32"重复创建事故的确切日期与修复 commit（仅 ZHAOSHANG_BATCH_BACKEND_CONTRACT.md 记载为历史动机）。
- UNKNOWN：B-21 之后是否有任何针对 freeze 门的代码改动（截至 HEAD 未发现；runbook 语义未变）——本报告按"未修复"处理。
- UNKNOWN：IgnoreNew 饥饿（长 closeout 吞掉后续唤醒）是否实际发生过——仅有注册层断言，无端到端复现，也无事故记录。
- UNKNOWN：2026-08-11 之前的 runtime 事故（runtime 目录自 08-11 始），6-7 月增量管道早期事故可能低记载。
- UNKNOWN：zero-rows 远端停滞在 stall_breaker 上线后是否复发（runtime 记录仅至 08-21）。
- LOW→UNKNOWN：4800660（prompt 模板花括号）发现渠道——diff 明确但无事故文档，按测试期发现处理。
- 方法学注记：obra/superpowers 三 skill 未安装，其纪律以人工方式内嵌执行；pyc 缓存文件被 git 追踪（.gitignore 缺口）属卫生问题，未计入 Bug。

---

## 附：审计产物

- 分析脚本与中间产物（只读 git 的 Python 脚本、23 个 fix diff 落盘）：`E:\.A中核运维\_run\bugspot_audit\`
- 本文档：`docs/CNNC_BUG_HOTSPOT_AUDIT_20260823.md`（唯一写入的文件；未 commit，待人工审核）
