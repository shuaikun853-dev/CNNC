# CNNC 架构加固重构计划(2026-08-23)

> THIS IS A PRODUCTION RELIABILITY REFACTOR.
> 不重写应用;不改变可观察行为(除非后续任务明确授权);不改变远程副作用语义、重试语义、
> SQLite 事务边界、已持久化状态字符串、调度语义、freeze/finalize 业务语义;重构期间不调用生产 API。
> 本计划只做拆分与加固,不做重新设计。执行任何 Task 前先读本文件对应 Phase 与「每 Task 工作协议」。

## Baseline(基线记录)

| 项 | 值 |
|---|---|
| 生产工作区 | `E:\.A中核运维\cnnc-ops-agent`,分支 `prod/closeout-20260818`,HEAD `06e2c6a`,有 3 个未提交文件(`scripts/monitor_stb_fallback.py`、`scripts/process_monitor_no_push_zhaoshang.py`、`tests/test_monitor_stb_fallback.py`,均与本重构目标无关)——**生产工作区不被直接重构** |
| 重构 worktree | `E:\.A中核运维\cnnc-ops-agent-worktrees\refactor-cnnc-architecture-hardening`,新分支 `refactor/cnnc-architecture-hardening`,自 `06e2c6a` 创建 |
| `python -m compileall app scripts` | PASS |
| `python -m pytest --collect-only -q` | 1378 tests collected,0 errors |
| 增量价格审计完整矩阵(TESTING.md §2,17 个测试文件) | **464 passed, 9 skipped, 0 failed**(250s) |
| 9 个 skip | 历史基准/fixture 缺失类既有跳过,与生产工作区一致,非本次引入 |
| 环境说明 | worktree 初次跑矩阵 2 个失败(`test_project_resources_and_default_runtime_root_do_not_depend_on_cwd`、`test_finalize_dependency_construction_from_non_project_cwd_is_offline`),根因是新 worktree 缺 gitignore 的 `data/` fixture(`latest_saas_sameAudit_queryAll.curl`、`model.env`);同一对测试在生产工作区验证 **2 passed**。按既有 worktree 惯例(stability-fix-0817 等三个 worktree 均带 data/ 副本)复制 `data/` 后重跑全绿。**后续所有基线/回归都在 worktree 内执行,该 2 个测试不得再失败** |
| fixture 基线指纹(worktree 本地副本,与生产源逐字节一致,**内容不入库**) | 来源 `E:\.A中核运维\cnnc-ops-agent\data\` → `…-worktrees\refactor-cnnc-architecture-hardening\data\`:`latest_saas_sameAudit_queryAll.curl` 1560 bytes,SHA256 `8510BAAF1C407DFA9F10F0E26766E9176537C6D5C96D03EF5DFB7E28AE1FB732`;`model.env` 679 bytes,SHA256 `C686BDF1749F93DB88318090FEF48BB9AB58D31CDFE99771FFE3AC8603D7FC61`。Release Gate 又发现第三个未跟踪 fixture:根目录 `商城sourceId映射表\商城sourceId映射表.xlsx`(15615 bytes,SHA256 `57957A2623BE9FA0611F09DCB6F10B7EC854A45BFB827F7F7C315C223C20D790`,`scripts/zhaoshang_mall_mapping.py:11` 的 DEFAULT_MAPPING_PATH,缺失时 excluded-template 类测试失败)。基线复跑前先核对这三个指纹,保证 baseline 可重复 |

基线时点发现并已核实的**既有潜在缺陷**(非本次重构造成,处理见各 Phase 标注):

1. `app/services/incremental_audit_runtime.py:456,500` 捕获 `RecoveryRejected`,但 import 块(44–56)从未导入该名字——这两个 except 路径一旦触发会 `NameError`。Phase 2 含一个独立的微型修复 Task(需单独授权,属行为修复)。
2. `app/services/legacy_zhaoshang_adapter.py:1720` 注解使用 `Sequence[CrawlRequest]` 但未导入 `Sequence`,仅靠第 3 行 `from __future__ import annotations` 生效。Phase 3 拆分时任何新文件都必须保留 future import 或补导入。

## Current Architecture(当前架构)

管线(括号为实现锚点):

```
Windows Task Scheduler「CNNC-PriceAudit-Incremental」
  每 15 分钟、IgnoreNew、32 分钟硬上限
  └─ scripts/run_incremental_price_audit_scheduled.ps1(纯墙钟路由)
       09:00–17:00 整点 + 17:15/17:30/17:45 → tick
       白天非整点 15 分槽 → harvest-only
       18:00–19:45 每 15 分钟 + 20:00 → closeout;20:15 后快速空退
       └─ scripts/incremental_price_audit.py(CLI)
            └─ app/services/incremental_audit_runtime.py::run_runtime_command
                 ① 校验命令/参数 → runtime_paths(每日目录:<root>/<date>/state.sqlite3、
                    incremental_price_audit.lock、BATCH_STATE.md、saas_audit_final_*.xlsx;
                    detail.lock 为跨日全局)
                 ② 每业务日 ProcessLock(msvcrt 非阻塞字节锁 + owner.json 展示元数据)
                    冲突 → ALREADY_RUNNING / SKIPPED_ALREADY_RUNNING
                 ③ _run_runtime_command_locked:
                    drain/harvest-only 无 state 文件 → 本地 NOOP;
                    FINALIZED → ALREADY_FINALIZED NOOP;
                    依赖装配 build_default_dependencies / build_drain_dependencies
                    (planner/submitter 仅 --enable-remote-writes 接通;harvester 恒为只读 legacy)
                    submit_gate = remote_submit_pause_reason(store, day)(durable remote-stall 熔断)
                 ④ 分派:
                    closeout → app/services/incremental_audit_closeout.py(单进程循环 tick(finalize=True))
                    drain-only / harvest-only → app/workflows/incremental_price_audit.py 对应入口
                    tick / finalize → run_incremental_tick(finalize=…)
                 ⑤ 出版候选:仅 finalize 或 DRAINED;余量 < 60s 降级 BUDGET_EXHAUSTED+publication_deferred;
                    FinalizeAlreadyRunning → 延迟;FinalizeBlocked 可恢复等待 → 原样返回
                 ⑥ _record_batch_result(BATCH_STATE.md,旁路文件锁;SQLite 才是权威)
```

`run_incremental_tick`(`app/workflows/incremental_price_audit.py:1084`)内部阶段序:
`recover → harvest(开扫) → discover(ingest_snapshot) → detail → model → submit → post_harvest → checkpoint(freeze 决策)`,`finally` 释放 lease+lock。

各阶段落点:

| 阶段 | 实现 | 关键写库 | 远程副作用 |
|---|---|---|---|
| Discovery | runtime `snapshot_discoverer` → store `ingest_snapshot` | snapshots/audit_items/snapshot_members(单事务,NEW/CHANGED/UNCHANGED) | SaaS queryAll 快照(180s 预算) |
| Detail | runtime `_detail_worker`(全局 detail.lock) | stage_work(DETAIL) | 中核/SaaS detail API |
| Model | runtime `_model_batch_worker` + workflow `_stage_model_batches` | stage_work(MODEL) | 标题匹配模型批调用 |
| Crawl Planning | workflow `_submit` → bridge `propose`/`finalize`(模板落盘) | crawl_work、`finalize_crawl_plan` | 无(本地模板) |
| Remote Intent | store `prepare_remote_intent` + `link_crawl_work_batch_to_remote_job` | remote_jobs(SUBMIT_INTENT,immutable payload) | 无(先落库) |
| Submit | `claim_remote_submission`(先烧 post_attempted)→ bridge `submit` | mark_submit_unknown / reconcile_remote_job | **招商局 /add(create-only,一次)** |
| Reconcile | bridge `reconcile_unknown`(精确 task_name+mall 查找,只读) | durable CREATED/ENABLED/EXPORTED 事件 | 只读查询 |
| Harvest | workflow `_harvest`/`_harvest_batch` → adapter `harvest`/`prioritize` | reconcile_remote_job、complete/fail_crawl_work、record_harvest | wait-export-only 子进程;priority UPDATE |
| Recovery | `app/services/incremental_audit_recovery.py` 10 个 run 函数 + CLI 11 个子命令 | 见下文 Phase 1 | 仅 prioritize/probe/harvest,绝不 /add |
| Freeze | workflow 1240–1277 冻结门 → store `freeze_snapshot` | daily_runs.frozen_snapshot_id、FINALIZING | 无 |
| Finalize | `app/workflows/incremental_price_audit_finalize.py` readiness 门 → `prepare_finalization` | daily_runs FINALIZED + manifest hash | 无 |
| Materializer | `app/services/incremental_audit_materializer.py`(原子 xlsx + sidecar manifest) | 无(文件系统) | 无 |

### Data Flow(数据流)

```
SaaS queryAll 快照(row_count/count 全量对账)
  → DiscoveredState(frozen crawl eligibility 标记、双指纹)
  → DailyAuditStore.ingest_snapshot(ordered_hash = 有序 (source_fingerprint, customer_fingerprint) 指纹)
  → ordered_snapshot_items → stage_work(DETAIL: detail 结果)→ stage_work(MODEL: 匹配结果)
  → CrawlPlan/CrawlMember → crawl_work(身份 = run+mall+source_id+sku+url+backend_version)
  → remote_jobs(logical_key/task_name/payload_hash/remote_identity)
  → 导出 result.xlsx → zhaoshang_result_adapter 读取
  → complete_crawl_work(result_json: page_price_*)/NO_RESULT 证据
  → build_frozen_audit_artifact(冻结范围读模型)→ LegacyWorkbookMaterializer → 正式 xlsx + manifest
```

### Control Flow(控制流)

调度器(15min,IgnoreNew)→ runner ps1(墙钟路由)→ CLI → runtime(每日 ProcessLock)→
workflow 入口(tick/drain/harvest/closeout;lock 可重入,内层再取)→ store lease(acquire→renew(重调用前)→release)→
阶段序执行(TickBudget:can_start_heavy 预留、callback_timeout=min(60s, stage 剩余, tick 剩余))→
closeout 单进程循环(observation 300s / sweep 600s 节奏,blocker 清零即物化)→
FINALIZED 后一切入口 NOOP。

### State Mutation Flow(状态变迁流)

- `daily_runs.state`: OPEN(schema 默认)→ FINALIZING(freeze_snapshot / prepare_finalization)→ FINALIZED(mark_finalized)
- `stage_work.state`: PENDING → CLAIMED → SUCCESS | RETRYABLE_FAILED →(下一 tick 再领)→ … | TERMINAL_FAILED;MODEL 特有: CLAIMED → RECOVERED_PENDING_REVIEW(recover-model)→ SUCCESS(release-model-recovery)
- `remote_jobs.state`: SUBMIT_INTENT →(claim 烧 post_attempted + POST)→ SUBMITTED → RUNNING → EXPORTED → COMPLETED;异常分支: SUBMIT_UNKNOWN(仅精确对账)、PRIORITY_PENDING(提优待证)、MANUAL_REQUIRED(人工);合法迁移矩阵在 store `reconcile_remote_job:1202-1210`
- `crawl_work.state`: PENDING → SUBMITTED → RUNNING → EXPORTED → SUCCESS | NO_RESULT | TERMINAL_FAILED;RETRYABLE_FAILED / SUBMIT_UNKNOWN / MANUAL_REQUIRED 分支
- `snapshot_members.change_kind`: NEW / CHANGED / UNCHANGED

### Remote Side Effect Flow(远程副作用流)

写副作用仅三处,全部有"先落库后发送"证据链:
1. `/add` create-only(bridge submit → 外部工具子进程):POST 前 `prepare_remote_intent`(immutable)+`link_crawl_work_batch_to_remote_job`+`claim_remote_submission`(post_attempted=1);超时/中断 → SUBMIT_UNKNOWN,后续仅 `reconcile_unknown` 精确查找,**绝不重发**。
2. priority UPDATE(weight_type=-1,`_production_priority_runner` 直连 ZHAOSHANG_DB):PriorityTarget(task_name, task_number, mall_code, marker−2min~marker+15min 窗口)+ expected_subtask_count;durable PRIORITY_UPDATED/PRIORITY_ALREADY_HIGHEST 证据短路;失败落 PRIORITY_UNCONFIRMED,不自动重试。
3. wait-export-only 子进程 + export 下载(harvest):有 attempts/elapsed 上限,超限 → MANUAL_REQUIRED。
只读副作用:SaaS 快照/分页、detail API、模型调用、`probe_export_no_price`(固定 4 次调用预算)、`check_existing_task`。

### Transaction Boundary(事务边界)

- store `_connect`:每次操作新连接;`isolation_level=None`、`busy_timeout=5000`、WAL、`foreign_keys=ON`(测试 `test_each_store_opens_its_own_connection` 钉死)。
- **每个 mutator 方法 = 恰好一个 `BEGIN IMMEDIATE` 事务**;多行原子方法:`ingest_snapshot`、`link_crawl_work_batch_to_remote_job`、`release_recovered_model_work_batch`、`requeue_terminal_model_work_batch_preserving_evidence`、`_initialize`(DDL+迁移)、freeze/prepare_finalization/mark_finalization 三连(各自独立幂等)。
- Submit 管线**故意多事务**(intent → 绑定 → claim → POST → reconcile),崩溃缝隙由 `recover_ambiguous_remote_submissions` 下一 tick 精确对账修复——这是 crash-safety 设计,不得"合并成一个事务"。
- junction 类方法(partial-exact 家族、`finish_partial_exact_harvest_job`、`complete_crawl_work_export_no_price` 等)在**同一事务内**校验 remote_jobs 行(`_job_for_lease`)再写 crawl_work——拆 Repository 时不得把该校验拆成两次调用(TOOTOU)。
- `ingest_snapshot` 事务内读 `daily_runs.frozen_snapshot_id` 做冻结守卫——跨 Run 关注点,必须留在同一事务。

### Recovery Flow(恢复流)

- 进程崩溃:CLAIMED stage → `recover_claimed_stage_work` → RETRYABLE_FAILED;SUBMIT_INTENT 且 post_attempted → `recover_ambiguous_remote_submissions` → SUBMIT_UNKNOWN(只读对账)。
- 终态修复(全部 CLI 子命令、全部先验证后单点写入、全部 fail-closed):
  - MODEL:`requeue-model-transient`(精确集合窄 requeue,保留 attempts/evidence)、`recover-model`(→RECOVERED_PENDING_REVIEW)、`release-model-recovery`(带 `--allow-future-remote-submit` 安全栓)
  - 远端:`recover-remote-harvest`(纯本地 harvest_existing)、`recover-partial-exact-harvest`(三方精确分流,逐行可续)、`resolve-no-result`(completed 导出可证缺行 → NO_RESULT)、`resolve-remote-export-no-result`(live probe 4 调用预算 → 整 job NO_RESULT,阈值 ≤10)、`recover-remote-priority` / `recover-remote-priority-zero-rows`(只对既有 job 提优/再探)、`recover-remote-wait-export`(原任务 wait-export 重取)、`harvest-existing-remote`(有界等待+回填)
- 所有 run 函数共用前置:状态文件存在、run 绑定、精确 `remote_identity` 三段等值、`post_attempted==1`、payload 可重构、hash/mall 一致、fan-out 门(`validate_linked_crawls_against_remote_payload`)通过、ProcessLock+lease(`_acquire_recovery_lease`,先锁后约、finally 双释放)。

## 热点分析(第四步四问,按代码证据)

热点:`incremental_audit_recovery.py`(2182 行)、`legacy_zhaoshang_adapter.py`(2147)、`daily_audit_store.py`(1903/~84 方法)、`incremental_audit_runtime.py`(1905)、`incremental_price_audit.py`(1312)、`incremental_price_audit_finalize.py`(727)。

**Q1 哪些职责因不同原因变化?**
- recovery:MODEL 终态策略、NO_RESULT 判定阈值、priority 远端探测、wait-export 恢复、harvest 证明规则——五类独立演进压力共存于一个文件,10 个 run 函数各自对应不同 runbook 段落。
- adapter:模板发布(Windows AV/扫描行为驱动)、重试错误码分类(DB 错误码集驱动)、商城别名映射(映射表驱动)、URL 重写白名单(商城页面结构驱动)、子进程装配(外部工具变更驱动)——变化原因互不相干。
- store:七张表的访问 + lease/lock 基础设施 + 迁移——表结构演进与并发策略演进耦合。
- runtime:路径布局、BATCH_STATE 人类交接、13 个 recovery 命令包装、模型配置、detail 节流、两座 bridge、依赖装配、生命周期——纯装配层,职责最多但每个都薄。
- workflow:tick 阶段编排、三种入口的重复样板、冻结门、结果分类阶梯。
- finalize:readiness 读模型、复核表书写、出版编排三层。

**Q2 哪些函数共享同一 invariant?**
- recovery 10 个 run 函数共享"先全量验证、后首次变更"序 + lease/lock 配对 + 计数器契约(remote_calls/add_calls)。
- store 全部 mutator 共享"_validate_lease 在每个事务内"。
- workflow 三入口共享 lock→lease→FINALIZED-NOOP→recover→finally-release 序(但 lease owner 前缀 drain-/harvest-/tick- 各异,属可观察差异)。
- adapter 的 submit/reconcile/prioritize/harvest 共享"refresh-before-send、send 后不重试"。

**Q3 哪些代码必须处于同一 transaction/failure boundary?**
- 见上文 Transaction Boundary;junction 方法与 ingest_snapshot 冻结读为强制同事务对。
- priority 恢复的三写尾(requeue_manual_priority_job → reconcile_remote_job → advance_crawl_work_for_job)目前**依赖 lease+幂等重入而非事务**——拆分时保持三次调用原样,不得引入跨调用新状态。

**Q4 哪些 side effect 绝对不能因拆文件改变调用次数?**
- `/add`(一次)、`prioritize`(证据短路)、`probe_export_no_price`(4 次)、`harvest` 子进程(一次/轮,existing workbook 短路为 0 次)、模型批(一批一次)、detail(逐行受全局锁)、SaaS 快照(每 tick ≤180s 预算内)。所有恢复路径 add_calls 恒 0。

## Observed Coupling(观察到的耦合)

1. **recovery 样板重复(最大重复源)**:10 个 run 函数共享约 60% 相同前置段(状态路径、`identity_parts` 三段解析于 596/997/1177/1332/1626/1937、frozen 门、`frozen_sources` 构建于 819/1017/1192/1342/1638/1951、payload 重构于 462/626/863/1066/1222/1369/1663/1972、prepare_job 一致性校验、fan-out 门、finally 双释放)。
2. **G↔H 收割对称**:`run_existing_remote_harvest`(1492–1586)与 `run_remote_wait_export_recovery`(1801–1895)后置写块逐字符相同,仅结局前缀不同;claim 预算不同(+1/+60 vs +2/+120)。
3. runtime 13 个 `run_*_command` 薄包装 + `_record_recovery_event`/`_opportunistic_recovery_closeout`。
4. workflow 三入口样板重复(drain 845–970 / harvest 989–1081 / tick 1102–1312)。
5. **状态词汇重复**:`_backlog`/`_HARVESTABLE_REMOTE_STATES`/`_DRAIN_REMOTE_STATES` 的字符串字面量在 `incremental_audit_status.py` 再枚举一遍(273–279、337–358、434–441)——漂移风险。
6. 哈希助手三份:workflow `_canonical_hash:199`、materializer `_canonical_hash:95`、store `_fingerprint:492`;`_file_sha256` 两份(finalize:513、materializer:100);表头同义词表两份(finalize `_conflicting_export_price_reason:213-222`、materializer `_header_index:284-286`)。
7. **跨模块冻结字面量**:`EXPORT_NO_PRICE_RSP_DESC`("未检索到商品价格信息")在 recovery:1904 与 finalize:170 各自定义——两处都是冻结契约,Phase 5 才可合并为常量引用(值不变)。
8. store junction 方法(partial-exact 家族 6 个)横跨 CrawlWork×RemoteJob。
9. finalize 文件三层合一(readiness 读模型 / 复核表书写 / 出版编排)。
10. adapter 门面超宽:5 个生产模块 + 13 个测试文件导入,含 2 个私有名(`_BoundedReadOnlySession`、静态 `_load_module`);`_load_module` 以固定名注册 `sys.modules`,并发适配器实例有碰撞约束。
11. `run_partial_exact_harvest_recovery` 内联重写 fan-out 门(660–704,语义**更严**:精确集合相等),与共享门(计数超集)不同——不可盲目统一。

## Protected Invariants(受保护不变量,任何 Phase 不得破坏)

任务给定的 12 条 + 代码证据:

1. **SQLite 是 durable checkpoint 权威**(七张表;BATCH_STATE.md/daily_status.json 只是便利快照;runbook 明文)。
2. **POST 成功但本地 commit 前 crash → 不得盲目再次 POST**:`claim_remote_submission` 先烧 post_attempted;崩溃缝隙进入 SUBMIT_UNKNOWN 由下一 tick 精确对账。
3. **SUBMIT_UNKNOWN 必须 reconcile,禁止 blind resend**:`reconcile_unknown` 只读;零匹配保持 unknown,多匹配/冲突/未启用 fail-closed;接管需补写 CREATED/ENABLED 等价证据。
4. **同一 remote intent 不得产生额外真实 remote task**:`prepare_remote_intent` immutable + UNIQUE(run_id,logical_key) + UNIQUE(run_id,task_name) + task_name 确定性(`cnnc_current_opt_<mall>_<14位哈希>`,同日稳定跨日不同)+ remote_identity 部分唯一索引。
5. **same-day writer fencing 不得弱化**:每业务日 ProcessLock(msvcrt/flock 字节锁,非阻塞,owner.json 仅展示);拿不到 → 立即 SKIPPED_ALREADY_RUNNING 正常退出。
6. **lease fencing 不得弱化**:acquire_lease(generation+expiry)于每个事务内 `_validate_lease`;unexpired takeover 必须同时持有 ProcessLock。
7. **repeated tick 幂等**:stage/crawl SUCCESS 复用、快照 (hash,observed_at) 复用、SUBMIT_INTENT 恢复优先于重新规划、FINALIZED 前置 NOOP(任何远程请求之前)、冻结后 membership 不可变。
8. **freeze 语义**:冻结门 = 本轮携带新鲜权威观测 + `row_count == saas_count`(严格相等)+ `source_stability_seconds ≥ 600s`(同 (row_count,ordered_hash) 连续);customer_count 只是观测指标不入门;冻结后 discovery 只认冻结快照。
9. **finalize 不得提前**:readiness 十类计数全零(仅 detail_terminal/crawl_no_result 允许)先于 `prepare_finalization`;review 路径不得写 final/占用正式路径/置 finalized;输出路径 intent 先唯一登记;FINALIZED 后拒绝不同输出。
10. **NO_RESULT 与 remote failure / parse failure 严格区分**:NO_RESULT 只能经带证据戳的专用方法写入(`EXPORTED_NO_MATCHING_ROW` 或 `EXPORT_NO_PRICE_TERMINAL` + rspDesc="未检索到商品价格信息" + export_count=0 + reason 非空);价格留空绝不为 0;阈值 `EXPORT_NO_PRICE_NO_RESULT_LIMIT=10`。
11. **harvest 不得借用 sibling SKU/URL 价格**:绑定序 = 先 (sku_id, exact_url);URL-only 回退仅当该 URL 在 payload 中恰好映射一个 SKU(516–532、1496–1522、1805–1831;测试 `test_no_result_recovery_uses_exact_completed_export_without_borrowing_sibling_price` 钉死)。
12. **既有导出结果按当前 identity 规则恢复**:batch-state EXPORTED 事件 output_path 正规化等值、批次号==task_name、执行编号以 task_number 为前缀且批内唯一、商城标识==mall_key、URL 重写白名单(qstbg detail.do↔initParameter.do、jd i-item↔item.jd.com、/detail/inner 剥离)。

补充不变量(代码证据强制):
13. **send 前刷新认证、send 后不自动重试**(/add、审批类、补推同规)。
14. **priority SQL 作用域**:精确 task_name/taskNumber/mall code/marker 窗口/主键回读全 -1;不得放宽。
15. **错误码→重试分类冻结**:retryable {1040,1053,1203,1205,1206,1213,2002,2003,2004,2006,2013,2055} / deterministic {1044,1045,1049,1142,1143,1227,1251}。
16. **store 每方法一事务 + 每操作新连接 + WAL**;`_initialize`(DDL+迁移+碰撞 fail-closed)为一个单元。
17. **锁定次序**:ProcessLock 外层 → lease 内层;释放逆序;recovery 先锁后约、异常路径锁释放语义。
18. **模型批次基线 10/并发 1**;callback 60s 上限;adapter 3 attempts/300s durable ceiling。
19. **物化幂等**:input-manifest sha256 嵌入工作簿 + sidecar;crash-after-replace 可恢复;冲突不覆盖。
20. **导入无副作用**:materializer/finalize 对 `scripts.audit_saas_excel_file` 的导入保持懒加载(测试 228 钉死)。
21. **结果计数器契约**:outcome dict 的 `remote_calls`/`add_calls`/`model_calls`/`priority_calls` 键与值语义(恢复路径 add_calls 恒 0)。

## 重构策略总纲(Compatibility Facade / Strangler)

- **禁止同名 module/package 并存**。Python import resolution 下,同目录同名 package 会遮蔽 `.py` module,使"compatibility facade"在包创建时就改变解析目标。因此统一采用:**原 `.py` 模块永久保留为 compatibility facade,实现移入不同名的新包**,原模块只逐步变薄、最终从新包 re-export/delegate。绝不创建与现有模块同名的目录(如 `app/services/incremental_audit_recovery/`、`app/services/incremental_audit_runtime/`、`app/services/daily_audit_store/`)。
- **旧 import 路径(如 `from app.services.incremental_audit_recovery import X`)必须永久继续工作**;现有 callers 与 tests 不做一次性迁移。
- 门面必须 re-export 被外部导入的**全部**名字,包括测试导入的私有名(见各 Phase 冻结清单)。
- 结构拆分(Phase 1A 类)只允许 MOVE / IMPORT / RE-EXPORT / DELEGATE:不改逻辑、不改签名、不改字符串、不改调用次数、不改调用图。去重与共享助手提取是独立的后续轮次(Phase 1B 类),每提取一个 helper 一个独立 Task/commit/验证。
- characterization contract test 用于锁住现有 public interface:**必须在重构前 PASS**(`write characterization test → verify PASS on baseline → refactor → verify still PASS`);只有新增行为/bugfix 才要求 RED→GREEN。
- 每个 Task 一个 commit;commit 粒度见「禁止事项」。

---

## Phase 1 — incremental_audit_recovery 拆分(1A 结构 + 1B 去重)

**布局(修正 1:禁止同名 module/package 并存)**:

```
app/services/incremental_audit_recovery.py   ← 永久保留为 compatibility facade,只逐步变薄
app/services/recovery/                       ← 新实现包(不同名)
    __init__.py      (最小化;不放实现)
    common.py        RecoveryRejected、BackendIdentity、_acquire_recovery_lease
                     (第一轮只放必须共同依赖、且不提取就无法安全拆分的最小内容)
    validation.py    validate_linked_crawls_against_remote_payload
    model.py         requeue_model_transient_failures、run_model_recovery、
                     release_model_recovery、_TIMEOUT_EXHAUSTION、_safe_error
    harvest.py       run_local_remote_harvest_recovery、run_partial_exact_harvest_recovery、
                     run_existing_remote_harvest
    priority.py      run_existing_remote_priority_recovery、
                     run_existing_remote_priority_zero_rows_recovery、
                     ZERO_ROWS_PRIORITY_RECOVERY_DIAGNOSTICS
    wait_export.py   run_remote_wait_export_recovery、WAIT_EXPORT_MANUAL_DIAGNOSTIC、
                     WAIT_EXPORT_STILL_RUNNING_DIAGNOSTIC、WAIT_EXPORT_RECOVERY_ACCEPTED_ERRORS
    no_result.py     resolve_exported_no_result、run_remote_export_no_price_resolution、
                     EXPORT_NO_PRICE_*(含 WAIT_EXPORT_NO_EXPORT_DIAGNOSTIC 与
                     EXPORT_NO_PRICE_ACCEPTED_ERRORS,后者 import wait_export 常量)
```

禁止创建 `app/services/incremental_audit_recovery/` 目录。旧 import `from app.services.incremental_audit_recovery import X` 永久有效(门面 re-export)。

**冻结 public interface**(门面必须永久提供,17 名):
`RecoveryRejected`、`validate_linked_crawls_against_remote_payload`、`requeue_model_transient_failures`、`run_model_recovery`、`release_model_recovery`、`run_local_remote_harvest_recovery`、`run_partial_exact_harvest_recovery`、`resolve_exported_no_result`、`run_existing_remote_priority_recovery`、`run_existing_remote_priority_zero_rows_recovery`、`run_existing_remote_harvest`、`run_remote_wait_export_recovery`、`run_remote_export_no_price_resolution`、`WAIT_EXPORT_MANUAL_DIAGNOSTIC`、`WAIT_EXPORT_STILL_RUNNING_DIAGNOSTIC`、`EXPORT_NO_PRICE_CRAWL_REASON`、`WAIT_EXPORT_NO_EXPORT_DIAGNOSTIC`(导入方:runtime:44、scripts/incremental_price_audit.py:32、7 个测试文件)。

### Phase 1A — Pure Structural Decomposition

**只允许:MOVE / IMPORT / RE-EXPORT / DELEGATE。禁止**:deduplicate、extract shared helper、rewrite logic、change call graph unnecessarily、change state、change error text、change counters、change timing、change DB interaction、change side-effect count。**即使存在重复(如 G↔H 对称块、10 个 run 函数的重复前置段),1A 中原样保留**。`common.py` 第一轮只放 `RecoveryRejected`、`BackendIdentity`、`_acquire_recovery_lease`——不提取 `_load_frozen_run`、`_frozen_sources`、`_parse_remote_identity`、`_reconstruct_payload`、`_prepare_and_verify_job`、`_persist_export_outcome`;如确需提取,必须先证明不提取就无法完成安全拆分,并作为独立 Task。

移动规则:函数体逐字符移动(按行区间切割原文件,不手抄);每个子模块头部 = `from __future__ import annotations` + 原模块完整 import 块(未用 import 允许暂时保留,修剪属于后续清理 Task)+ 跨子模块 import;门面同步加回 re-export。

**Task 序列**(每 Task 走「每 Task 工作协议」11 步):

- **1.0 Characterization Contract Gate**:新增 `tests/test_recovery_facade_contract.py`,锁住 17 个 public 名(12 函数 callable、`RecoveryRejected` 为 RuntimeError 子类、4 个诊断常量逐字符等于现值)。**必须在未改动基线上 PASS**(修正 2),此后所有 Task 保持 PASS。
- **1.1** 建 `app/services/recovery/` 包(`__init__.py` 最小化)+ `common.py`:移动 `RecoveryRejected`(26–27)、`BackendIdentity`(30)、`_acquire_recovery_lease`(145–163);门面 re-export 回。
- **1.2** `validation.py`:移动 `validate_linked_crawls_against_remote_payload`(33–119);从 common import `RecoveryRejected`、`BackendIdentity`。
- **1.3** `model.py`:移动 `_TIMEOUT_EXHAUSTION`(131–133)、`_safe_error`(136–142,唯一消费者在 model 簇)、`requeue_model_transient_failures`(166–242)、`run_model_recovery`(245–359)、`release_model_recovery`(362–417)。
- **1.4** `harvest.py`:移动 `run_local_remote_harvest_recovery`(420–578)、`run_partial_exact_harvest_recovery`(581–794,保留其更严的内联门)、`run_existing_remote_harvest`(1317–1589,保留其字面量 `"legacy wait-export-only requires manual review"` 不改为常量引用)。
- **1.5** `priority.py`:移动 `ZERO_ROWS_PRIORITY_RECOVERY_DIAGNOSTICS`(122–128 含注释)、`run_existing_remote_priority_recovery`(979–1151)、`run_existing_remote_priority_zero_rows_recovery`(1154–1314)。
- **1.6** `wait_export.py`:移动 WAIT_EXPORT 常量块(1592–1600 含注释)、`run_remote_wait_export_recovery`(1603–1898)。
- **1.7** `no_result.py`:移动 `resolve_exported_no_result`(797–976)、EXPORT_NO_PRICE 常量块(1901–1911,含 `WAIT_EXPORT_NO_EXPORT_DIAGNOSTIC`;`EXPORT_NO_PRICE_ACCEPTED_ERRORS` 从 wait_export import `WAIT_EXPORT_MANUAL_DIAGNOSTIC`)、`run_remote_export_no_price_resolution`(1914–2182)。
- **1.8** 门面定型:facade 只剩 re-export(17 名 + docstring);清理中间态空行;全量验证。

**1A 每 Task 验证**:focused(见下)→ recovery 全套(`test_incremental_audit_recovery.py`、`test_partial_exact_harvest.py`、`test_recovery_fanout_gate.py`、`test_remote_priority_zero_rows_recovery.py`、`test_remote_wait_export_recovery.py`、`test_remote_export_no_price_resolution.py`、`test_recovery_facade_contract.py`)→ TESTING.md 17 文件完整矩阵 → `python -m compileall app scripts` → `git diff --check` → inspect diff → commit。

**focused 映射**:1.1/1.2 = 契约测试 + fanout gate;1.3 = recovery 测试 `-k "model or transient"`;1.4 = recovery + partial_exact_harvest;1.5 = recovery `-k priority` + zero_rows + fanout gate;1.6 = remote_wait_export_recovery;1.7 = remote_export_no_price_resolution + finalize 测试;1.8 = recovery 全套。

**1A 准出门槛**:全部 Task 绿后 **STOP**(输出 PHASE 1A COMPLETE/BLOCKED 报告),不得自动进入 1B。

### Phase 1B — Deduplication / Helper Extraction(1A 完成并全绿后才允许)

前置门槛:1A 的 compileall PASS、focused recovery tests PASS、完整 17 文件矩阵 PASS、`git diff --check` PASS。每提取一个 helper 一个独立 Task/commit/验证;候选(以等价性测试为先):`_persist_export_outcome`(G/H 后置写块,保留各自 claim 预算 +1/+60 vs +2/+120 与结局前缀)、`_load_frozen_run`/`_frozen_sources`/`_parse_remote_identity`/`_reconstruct_payload`/`_prepare_and_verify_job`(10 个 run 函数共享前置段)、子模块未用 import 修剪。**统一 `run_partial_exact_harvest_recovery` 内联门与共享门的语义差异(精确集合相等 vs 计数超集)禁止在 1B 消除**——那是行为变化。

**Phase 1 元信息**:
- **Exact tests**:各 Task focused 清单 + recovery 全套 7 文件 + 完整矩阵(17 文件,命令见 TESTING.md §2)。
- **Expected diff scope**:原单文件(2182 行)保留为门面并逐步变薄,最终仅含 re-export;新增 `app/services/recovery/` 包 8 文件;caller 零改动;1A 无任何逻辑变更。
- **Risk**:中。风险点:(a) 移动后 `__module__` 变化(函数体与异常文本不变,契约测试锁字符串);(b) 诊断常量既是入口过滤又是再持久化错误串——随生产者迁移,不得改值;(c) 门面中间态必须始终可导入(每 Task 后 compileall + 契约测试)。
- **Rollback boundary**:每 Task 一 commit,`git revert <commit>` 即回;Phase 完成前不合并生产分支。

## Phase 2 — incremental_audit_runtime.py 拆分【NEEDS_REPLAN — Phase 1 完成前不得执行】

> 原方案的"同名 package(`app/services/incremental_audit_runtime/`)"违反「禁止同名 module/package 并存」规则,**整体标记 NEEDS_REPLAN**。重规划方向:原 `.py` 永久保留为门面,实现移入不同名包(如 `app/services/runtime/`),与 Phase 1 相同的 facade-file 模式;契约测试同样采用 baseline-PASS characterization gate。在 Phase 1(含 1B 决策)完成并合入前,不启动本 Phase 的任何 Task。以下内容为重规划时的素材基线,布局需按上述方向改写:

**拆分**(→ `app/services/incremental_audit_runtime/` 包):

| 新文件 | 内容(原行号) |
|---|---|
| `__init__.py` | 门面 re-export 全部冻结名(含测试导入的 4 个私有名) |
| `paths.py` | `SHANGHAI`、`PROJECT_ROOT`、`PROJECT_DATA_ROOT`、`DEFAULT_RUNTIME_ROOT`、`RuntimePaths`、`RuntimeResult`、`business_date_in_shanghai`(147)、`cutoff_for_business_date`(154)、`runtime_paths`(159)、`read_runtime_status`(1882)、`RuntimeConfigurationError` |
| `batch_state.py` | `_batch_state_block`…`_record_batch_result`(612–825),含旁路锁与 TimeoutError 吞并语义 |
| `commands.py` | 13 个 `run_*_command`(297–609)+ `_record_recovery_event`、`_opportunistic_recovery_closeout`、`_attach_recovery_closeout` |
| `model_worker.py` | `_read_model_configuration`…`_runtime_model_batch_size`(957–1061) |
| `detail_worker.py` | `_detail_worker`(883–954) |
| `discovery.py` | `_customer_index`、`snapshot_discoverer`(828–880) |
| `bridges/legacy.py` | `legacy_zhaoshang_bridge`(1064–1350) |
| `bridges/batch.py` | `batch_zhaoshang_bridge`(1352–1464) |
| `dependencies.py` | `build_default_dependencies`(1467)、`build_drain_dependencies`(1524) |
| `lifecycle.py` | `run_runtime_command`(1546)、`_run_runtime_command_locked`(1632) |

**冻结 public interface**:CLI 导入 15 名 + `runtime_paths`、`RuntimeResult`、`RuntimeConfigurationError`、`read_runtime_status`、`snapshot_discoverer`、`legacy_zhaoshang_bridge`、`DEFAULT_RUNTIME_ROOT`(status 模块:14)+ 私有名 `_detail_worker`、`_ensure_batch_state`、`_load_model_configuration`、`_model_batch_worker`(测试 16–19 导入)。**注意**:测试 monkeypatch `app.services.incremental_audit_runtime.call_detail_api` 与 `._load_model_configuration`、`.call_title_match_model_batch`(resilience 测试 502/1580/1591)——门面 `__init__` 必须把这些名字再导出到包命名空间,且 lifecycle/detail 调用点必须经由包命名空间属性访问(维持 monkeypatch 语义,与现状等价:现状它们就是模块级名字)。
**Tasks**:2.0 契约测试(导入面 + monkeypatch 面)→ 2.1 骨架 → 2.2 paths → 2.3 batch_state → 2.4 model_worker+detail_worker → 2.5 discovery → 2.6 bridges/legacy → 2.7 bridges/batch+dependencies → 2.8 commands → 2.9 lifecycle+门面定型+全矩阵。
**2.10(独立微型修复,需明确授权)**:补 `from app.services.incremental_audit_recovery import RecoveryRejected` 修复 456/500 NameError 缺陷;先写回归测试(模拟 `run_existing_remote_harvest` 抛 `RecoveryRejected`,断言被捕获分类而非 NameError),再修复;单独 commit,标注 behavior-fix。
**Exact tests**:focused = `test_incremental_audit_runtime.py`、`test_incremental_audit_operations.py`、`test_harvest_only.py`、`test_closeout_before_1830.py`、`test_incremental_audit_resilience.py`、`test_remote_stall_breaker.py`;终局 = 完整矩阵。
**Expected diff scope**:runtime 单文件(1905 行)→ 包内 11 文件;caller 零改动。
**Risk**:中高。风险点:monkeypatch 命名空间语义(2.0 钉死)、BATCH_STATE 锁语义逐字符保持、publication 候选逻辑不动。
**Rollback boundary**:同 Phase 1。

## Phase 3 — legacy_zhaoshang_adapter.py 只拆职责(不重设计协议)

**策略**:原文件**保留为门面文件**(不做包转换,规避 `_load_module` 的 `sys.modules` 注册语义变化);新建 `app/services/legacy_zhaoshang/` 包放实现,`CnncLegacyZhaoshangAdapter` 以 mixin 组合:

| 新文件 | 内容 |
|---|---|
| `template.py` | 模板发布族(`_expected_template_rows`…`_write_or_verify_template`、`TemplateConflictError`;保留 uuid 临时→xb→fsync→os.replace(5 次 PermissionError 重试)→验证后替换的原子序) |
| `evidence.py` | batch-state 事件族(`_read_batch_events`、`_priority_evidence`、`_submission_evidence`、`_submission_marker_time`、`_submission_intent_marker`、`_record_batch_event`、两个 EvidenceConflict 异常) |
| `runner.py` | 子进程/装配族(`EXTERNAL_TOOL_PATH`、`DEFAULT_DB_ENV_PATH`、`_temporary_environment`、`_BoundedReadOnlySession`、`_production_runner`、`_load_module`、`_default_reconciler`、`_run`、args 构造) |
| `priority.py` | `prioritize`、`_production_priority_runner`、分类器三函数 + 常量(`RETRYABLE_PRIORITY_ERROR_DIAGNOSTICS`、`REMOTE_NOT_MATERIALIZED_*`) |
| `reconcile.py` | `reconcile_unknown` + `_remote_*` 谓词 |
| `results.py` | 结果解析/映射族(`RESULT_*` 常量、`_result_column_*`、`_matches_legacy_export_url` **冻结白名单**、`_validate_result_workbook`、`validate_existing_export_exact_requests`、`classify_existing_export_partial`) |
| `probe.py` | `probe_export_no_price`、`_production_export_no_price_probe`、`ExportNoPriceProbe`(固定 4 调用预算) |
| 门面 `legacy_zhaoshang_adapter.py` | 组合类 + re-export 全部冻结名;补 `Sequence` 导入或保留 future import(缺陷 2 一并消除,属无行为影响的注解修复,随 3.0 commit) |

**冻结 public interface**(5 生产模块 + 13 测试文件):`CnncLegacyZhaoshangAdapter`、`CrawlRequest`、`PreparedLegacyJob`、`LegacySubmitOutcome/HarvestOutcome/PriorityOutcome/ReconcileOutcome`、`PartialExactHarvestOutcome`、`ExportNoPriceProbe`、`SubmitStatus/HarvestStatus/ReconcileStatus/PriorityStatus`、`LegacyZhaoshangAdapterError`、`TemplateConflictError`、`PriorityEvidenceConflictError`、`SubmissionEvidenceConflictError`、`EXTERNAL_TOOL_PATH`、`DEFAULT_DB_ENV_PATH`、`TEMPLATE_HEADERS`、`RESULT_*` 四组、`RETRYABLE_PRIORITY_ERROR_DIAGNOSTICS`、`REMOTE_NOT_MATERIALIZED_DIAGNOSTICS`、`REMOTE_NOT_MATERIALIZED_LEGACY_DIAGNOSTIC`、`retryable_priority_error_diagnostic`、`is_retryable_priority_error_diagnostic`、`remote_not_materialized_diagnostic`、私有 `_BoundedReadOnlySession`(测试 2126/2140)与静态 `_load_module`(test_zhaoshang_priority_core:13,依赖 `EXTERNAL_TOOL_PATH.with_name("zhaoshang_priority_boost.py")` 路径关系)。
**Tasks**:3.0 契约测试 → 3.1 `template.py` → 3.2 `evidence.py` → 3.3 `runner.py` → 3.4 `priority.py` → 3.5 `reconcile.py` → 3.6 `results.py` → 3.7 `probe.py` → 3.8 门面定型 + 全矩阵。
**Exact tests**:focused = `test_legacy_zhaoshang_adapter.py`、`test_zhaoshang_priority_core.py`、`test_zhaoshang_priority_boost.py`、`test_remote_stall_classification.py`、`test_remote_stall_breaker.py`;终局 = 完整矩阵。
**Expected diff scope**:adapter 单文件(2147 行)→ 门面文件 + 包内 7 文件;caller 零改动。
**Risk**:高(协议字符串密布)。风险点:`PreparedLegacyJob` 必须保持 `dataclasses.replace` 可用;`_load_module` 的 sys.modules 固定名注册保持;task_name/批次号/执行编号过滤等值链不得动;`from __future__ import annotations` 每个新文件必带。
**Rollback boundary**:同 Phase 1。

## Phase 4 — DailyAuditStore 拆 Repository(最高风险,最后做)【布局需按修正 1 重排】

> 注意:原方案写的是"→ `app/services/daily_audit_store/` 包"——同样违反「禁止同名 module/package 并存」规则。重排方向:`daily_audit_store.py` 永久保留为门面,实现移入不同名包(如 `app/services/daily_store/`),`DailyAuditStore` 由 mixin 组合;Repository 划分本身不变。

**前提**:Phase 1–3 已合入并全矩阵绿色;store 的全部调用方已稳定。

**拆分**(→ `app/services/daily_audit_store/` 包,`DailyAuditStore` 由 mixin 组合,方法集与调用点不变):

| 新文件 | 内容 |
|---|---|
| `__init__.py` | re-export `DailyAuditStore`、`ProcessLock`、`LeaseUnavailable`、`StaleLease`、`FrozenSnapshotConflict`、`RemoteIntentConflict`、`LeaseToken`、`DEFAULT_TENANT_ID`、`DEFAULT_POOL_ID` |
| `_base.py` | `_connect`、`_transaction`、`_reader`、`_initialize`、`_migrate_snapshot_observation_constraint`、值助手(`_utcnow`…`_source_fields`)——**迁移与 DDL 不可再拆** |
| `locks.py` | `ProcessLock` 类**原样移动**(msvcrt 语义与 inspect 契约零改动) |
| `run_repo.py` | `ensure_run`、`get_run`、`update_daily_metrics`、`table_names` |
| `lease_repo.py` | `acquire_lease`、`renew_lease`、`release_lease`、`_validate_lease`、`force_lease_expiry_for_test`(takeover 规则单一来源,供其他 mixin 复用) |
| `snapshot_repo.py` | `ingest_snapshot`(冻结读留在事务内)、`snapshot_members`、`get_snapshot`、`list_snapshots`、`latest_snapshot`、`first_snapshot_at_or_after`、`ordered_snapshot_items`、`get_audit_item` |
| `stage_repo.py` | 13 个 stage 方法(MODEL 恢复三方法可再分文件,但 `release_…batch`/`requeue_…batch` 各自单事务保持) |
| `remote_repo.py` | 16+2 个 remote 方法(含 `reconcile_remote_job` 合法迁移矩阵、`claim_harvest`/`record_harvest` "terminal evidence wins over cap") |
| `crawl_repo.py` | 15 个 crawl 方法 |
| `junction_partial.py` | partial-exact 家族 6 方法(跨 CrawlWork×RemoteJob,同事务 job 校验保持) |
| `finalization_repo.py` | `freeze_snapshot`、`prepare_finalization`、`mark_finalized`(frozen_snapshot_id FK 检查留事务内) |

**约束**:每个 mixin 方法保持"恰好一个 BEGIN IMMEDIATE + 新连接 + WAL";不改变任何 SQL 语义;`_initialize` 每次 `DailyAuditStore(...)` 构造仍全量执行(迁移测试 201/638 端到端钉死)。
**Tasks**:4.0 契约测试(全部 ~84 方法存在性 + 异常类型)→ 4.1 骨架 → 4.2 `_base`+`locks` → 4.3 `run_repo`+`lease_repo` → 4.4 `snapshot_repo` → 4.5 `stage_repo` → 4.6 `remote_repo` → 4.7 `crawl_repo` → 4.8 `junction_partial` → 4.9 `finalization_repo` → 4.10 门面定型 + **全量 `python -m pytest tests -q`**(不只矩阵;18 个导入 store 的测试文件全过)。
**Risk**:最高。任何事务边界弱化都会破坏不变量 2/3/5/6/7。junction 与 ingest_snapshot 为最险两点。
**Rollback boundary**:同 Phase 1;此外 Phase 4 每个 Task 后在 worktree 手工跑 `doctor` 冒烟(只读,不发网络请求)。

## Phase 5 — State typing / invariant hardening(与结构重构分开提交)

仅在前四个 Phase 全部绿色后进行,每项独立 commit:
- **5.1** 新建 `app/services/incremental_states.py`(或 workflow 侧 `states.py`):全部状态字符串单一来源(daily_runs/stage/crawl/remote/change_kind/outcome/backlog 集合),store/workflow/status 改为引用——**字符串值与集合成员逐字符不变**;消除 status.py 的字面量再枚举(耦合 5)。
- **5.2** `EXPORT_NO_PRICE_RSP_DESC` 等跨模块冻结字面量改为共享常量引用(值不变)。
- **5.3** typed dataclasses / TypedDict 加固 outcome dict 与 evidence json 形状(仅类型层,不改运行时构造)。
- **5.4** 可选:StrEnum 替换纯字符串注解(**不得**改 DB 持久化值——序列化处 `.value` 显式转换并测试钉死);中央迁移校验(reconcile 矩阵提为显式表)。
- 禁止与任何 move/rename 混在同一 commit。

---

## 每 Task 工作协议(第七步,严格执行)

1. 一个清晰责任;2. 小范围 diff;3. 现有行为不变;4. **先**确定保护该行为的测试(各 Phase focused 清单;无既有覆盖时先补契约测试);5. 修改后运行 focused tests;6. 再运行增量审计完整矩阵;7. `git diff --check`;8. `git status`;9. review diff;10. 才允许进入下一 Task。

**禁止一个 commit 同时**存在以下任意两项:move files + rename public API + change state representation + change DB schema + change behavior。

**通用红线**(全 Phase):不调用生产 API;不跑 `/add`;不改 `data/` 与 `runtime/` 生产内容(worktree 内测试写 tmp/basetemp);测试均用测试替身;`python -m compileall app scripts` 于每个 Task 后执行。

## Rollback boundary(总体)

- 所有工作在 `refactor/cnnc-architecture-hardening` 分支(worktree);生产分支 `prod/closeout-20260818` 不动。
- Task 级:`git revert <task-commit>`(每 Task 恰一 commit,前置依赖按序 revert)。
- Phase 级:每 Phase 完成时打 tag(`refactor/phase-1-done` …),Phase 回退 = reset 到前一 tag(仅在未合并生产前允许,且需用户确认)。
- 整体:分支不合并即零生产影响;合并须用户在完整矩阵 + `python -m pytest tests -q` 全绿后明确授权。
- worktree 的 `data/` fixture 副本为本地 gitignored 内容,不进入任何 commit。

## 风险排序与建议首阶段

最高风险边界(按破坏后果排序):① store 事务边界与 junction 校验(Phase 4);② adapter 协议字符串与 no-retry 语义(Phase 3);③ submit 管线多事务缝隙与 SUBMIT_UNKNOWN 对账(横切 Phase 2/3,本次不动逻辑);④ runtime monkeypatch 命名空间(Phase 2);⑤ recovery 样板提取的等价性(Phase 1)。

**建议首阶段:Phase 1A(recovery 纯结构拆分)**——外部调用面最窄(2 个生产导入点 + 7 个测试文件)、行为测试最密(6 个专项测试文件 + 1A 新增契约测试)、且 MOVE-only 约束使风险最低。Phase 4 必须最后做。
