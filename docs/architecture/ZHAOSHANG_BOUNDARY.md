# ZHAOSHANG BOUNDARY — 招商局能力边界架构契约

> Agent 修招商局 bug 前**必读**。本文件是招商局边界的权威架构说明:
> 能力地图、状态权威、任务身份生命周期、各能力语义、优先级写安全、并发模型、失败语义与禁止模式。
>
> - 建立日期:2026-09-05(闲时任务 3:招商局架构治理)
> - 基线:`prod/closeout-20260818 @ 2bc7cc6`;分支 `refactor/zhaoshang-boundary-20260904`(c96fcb5 特征化测试 → 5b5f2f0 Progress 能力抽取)
> - 前置审计:`docs/CNNC_LEGACY_ZHAOSHANG_ADAPTER_AUDIT_20260823.md`(2147 行时代,历史 Bug 台账 Z-1..Z-4 / 潜伏 L-1..L-7 / 测试缺口 G1-G7 仍有效)
> - 治理方法:MATT POCOCK 深模块原则 + Expand→Migrate→Contract;每能力一 commit,MOVE-only,facade 不变

---

## 0. 一页总览

```
Dashboard (app/static/dashboard/app.js)
   │  POST /dashboard/api/jobs/{id}/re-export | /rebuild
   ▼
dashboard_api.py → DashboardActionRunner(app/services/dashboard_actions.py)
   │  JSONL 意图先行 → 子进程 CLI: scripts/incremental_price_audit.py
   ▼
CLI 子命令 → dashboard_manual_ops.py / incremental_audit_runtime.py(recovery/ 子包)
   │  全部经 LegacyZhaoshangAdapter facade
   ▼
CnncLegacyZhaoshangAdapter(app/services/legacy_zhaoshang_adapter.py:478)
   ├── zhaoshang_task_progress.query_task_progress   [已抽出,只读进度]
   │        └── zhaoshang_remote_task(共享远端语义)
   ├── zhaoshang_remote_task                          [已抽出,共享远端语义层:异常族+enabled 判读+业务响应门]
   │        ↑ remote_running_recovery(recovery)同样从这里 import 业务响应门
   ├── zhaoshang_progress_probe(观测缓存层,独立模块)
   ├── 外部工具子进程/模块加载(E:\.A2026\...\auto_crawl_task.py)
   │      └── zhaoshang_priority_boost.py(唯一 DB UPDATE 所在地,仓库外)
   └── (待迁移:Submission/Identity证据/Export验收/Probe/Restart-Stop/Reconcile)
```

**改动铁律**:修改招商局任何能力前,先找到它在本文档能力地图中的位置与对应测试文件;跨能力的行为改动必须拆分提交;修复行为 Bug 单独 commit,禁止藏进 refactor。

---

## 1. Capability Map(当前代码位置)

`CnncLegacyZhaoshangAdapter`(`legacy_zhaoshang_adapter.py:478`,3211 行)= 招商局 Facade,11 个注入 seam(runner/reconciler/priority_runner/export_probe/running_probe/restart_runner/progress_probe/stop_runner/event_recorder/auth_manager/monotonic)。

| 能力 | 代码位置(legacy_zhaoshang_adapter.py) | 迁移状态 |
|---|---|---|
| **Task Identity / Batch 证据** | `CrawlRequest.identity` :253-265(五元组);`_read_batch_events` :711;`_submission_evidence` :765;`_submission_marker_time` :793;`_submission_intent_marker` :822;`_priority_evidence` :728;`exact_export_remote_identity` :1433;`exact_priority_remote_identity` :1456;mall 映射 `request_from_saas_row` :524 | NOT STARTED(部分种子已进 zhaoshang_remote_task) |
| **Submission(建任务)** | `_validate_submit_inputs` :865;`_create_args` :885;`_production_runner`(pre-send 边界):913;`submit` :1082 | NOT STARTED |
| **Reconcile(SUBMIT_UNKNOWN 对账)** | `_identity_from_remote` :1133;`_one_reconciled_remote` :1139;`_remote_takeover_conflict` :1157;`_remote_is_enabled` 别名 :1184;`_default_reconciler` :1195;`reconcile_unknown` :1227 | NOT STARTED |
| **Priority(提优)** | `_production_priority_runner` :983;`prioritize` :1314;DB UPDATE 实现在**仓库外** `E:\.A2026\...\zhaoshang_priority_boost.py` | EXISTING DEEP MODULE(外部)+ adapter 编排层 NOT STARTED |
| **Progress(只读进度)** | facade `query_task_progress_for_job` :1855 + 工具箱段 :1877-1920;领域逻辑**已抽出** → `zhaoshang_task_progress.query_task_progress` | **MIGRATED (2026-09-05)** |
| **Running Probe(只读活探测)** | `probe_running_task` :1617 + `_production_probe_running_task` :1634 | NOT STARTED(推荐下一个迁移) |
| **Restart / Stop(写控制)** | `restart_running_task` :1936;`_restart_protocol` :2029;`stop_running_task` :2278;`_stop_protocol` :2336 | NOT STARTED |
| **Export(收割/验收)** | `_matches_legacy_export_url` :1383;`probe_export_no_price` :1473;`_validate_result_workbook` :2512;`harvest_existing` :2709;`validate_existing_export_exact_requests` :2758;`classify_existing_export_partial` :2964;`harvest` :3160;死代码 `_classify_existing_export_partial_legacy` :2776(审计 L-5,零引用) | NOT STARTED |
| **共享基础设施** | 状态枚举 :211-250;outcome dataclass :282-366;`_BoundedReadOnlySession` :368;`_locked_session_construction` :455;模板发布 `_write_or_verify_template` :613;`prepare_job` :659;`_load_module` :970 | 留在 adapter(后续随各能力逐步分流) |
| **已抽出模块** | `zhaoshang_remote_task.py`(共享远端语义:异常族 + `remote_task_is_enabled` + 业务响应门 `remote_business_response_ok`/`PROVEN_BUSINESS_SUCCESS_RSP_CODES`,2026-09-05 r1 迁入);`zhaoshang_task_progress.py`(`query_task_progress`);`remote_running_recovery.py` 保留 re-export 兼容旧 import | 2026-09-05 |

外部观察层(不在 adapter 内):`zhaoshang_progress_probe.py`(Dashboard 只读进度缓存/TTL/single-flight,经延迟 import 构造 adapter)。

## 2. Source of Truth(状态权威)

1. **`state.sqlite3`(`<runtime_root>/<business_date>/state.sqlite3`)是唯一业务状态权威**(DailyAuditStore,`daily_audit_store.py`)。`remote_jobs`(UNIQUE(run_id,logical_key)、UNIQUE(run_id,task_name)、部分唯一索引 ux_remote_jobs_run_remote_identity)、`crawl_work`(mall/source/sku/exact_url/backend_version 五元组 + remote_job_id)、`remote_job_replacements`、`remote_job_rebuild_actions`。
2. **`BATCH_STATE.md` JSONL 事件是远端证据层**(工具与 adapter 追加):ADD_INTENT/CREATED/ENABLED/PRIORITY_*/EXPORTED。所有终判(submit/prioritize/export 身份)只认事件证据,不认子进程 stdout。
3. 远端平台是事实源但**不可信源**:一切远端响应必须过身份校验与业务门才可采纳。
4. **禁止**新建任何 JSON/第二 SQLite 作为业务 truth(见 §12)。

## 3. Task Identity(任务身份生命周期)

- **durable 身份五元组**:`(mall_key, mall_source_id, sku_id, exact_url, backend_version)`(CrawlRequest.identity)。URL 白名单重写(`_matches_legacy_export_url`:1383)仅容错导出重写,**永不参与身份构造**。
- **remote_identity 三段式**:`"mall_key|task_id|task_number"`,存 remote_jobs.remote_identity,无独立 task_number 列。写入点仅二:runtime submit 后经 publish 写回(remote_identity 构造 `incremental_audit_runtime.py:1487,1516`,落库走 DailyAuditStore.reconcile_remote_job)与 rebuild 派发(dashboard_manual_ops.py:1592-1601)。
- **task_name**:runtime 指纹命名(fingerprint 14 位后缀,日内稳定);rebuild 新名 = `_replacement_task_name`(dashboard_manual_ops.py:107-131,以 (旧名, action_id) 加盐,形如 `cnnc_current_opt_{mall}_{14位}`)。
- **生命周期**:创建(prepare_job :659 校验单 mall+模板 manifest)→ 提交(submit 证据终判)→ 身份回写(remote_identity)→ 查询(progress/probe 反复校验 live==durable,冲突抛 RemoteTaskIdentityConflictError)→ Dashboard 展示(remote_jobs 投影)→ 导出(EXPORTED 事件身份==提交身份 + output_path 精确匹配,`exact_export_remote_identity`:1433)→ 重建(supersede:旧 job 置 SUPERSEDED、新 job 新 task_name 新 logical_key(`#rebuild#` 后缀)、replacement 行、crawl 迁移)→ 再查询(一切以新 job 为准)。
- **重建后系统认定"新任务"的字段**:新 remote_job_id(旧 job SUPERSEDED 不可再导出/导出白名单 admissible_states 不含 SUPERSEDED)+ 新 task_name(UNIQUE 保护)+ 新 remote_identity。历史关系查 `get_replacement_for_job/get_replacement_of_job`。
- **batchNumber ≠ taskNumber**:进度端点键是任务记录的 `batchNumber`(三处默认==task_name);`/add`=name+batch;status=taskId;export=taskNumber+taskId+mallLogo;DB UPDATE=task_num+mall+时间窗。**新代码选错键 = Z-4 事故重演**,标识符选择只允许出现在已抽出/将抽出的能力模块内一处。

## 4. Submission(提交)

- 唯一 `/add` 出口 = `submit`(:1082)→ create-only 子进程(`--create-only`)。三层防双发:工具内 ADD_INTENT 先落+已有 ADD_ 事件拒建;runtime `claim_remote_submission` 先烧 post_attempted;adapter 证据终判。
- **pre-send/post-send 边界**(`_production_runner`:913):Popen 前一切失败=LegacyPreSendFailure→NOT_ATTEMPTED(可安全重试);Popen 后超时/未知→SUBMIT_UNKNOWN,**永不自动重发**,只能走 reconcile_unknown 精确对账(唯一匹配+enabled+身份一致才接管,冲突→MANUAL)。
- SUBMIT_UNKNOWN ≠ FAILED ≠ RETRY CREATE(§11)。

## 5. Progress(进度)【已迁移能力,迁移模板】

- 实现:`zhaoshang_task_progress.query_task_progress`(领域)+ adapter `query_task_progress_for_job`(:1855,facade:工具箱 ①timeout守卫 ②工具文件检查 ③deadline ④模块加载 ⑤auth 刷新有界 ⑥BLADE_AUTH+COOKIE 双变量 ⑦有锁 session 构建)。
- 语义:exact lookup→身份冲突分类(RuntimeError 查找失败/商城标识不一致/live≠durable → RemoteTaskIdentityConflictError;畸形响应保持无类型)→batchNumber 键控(缺失回退 resolved_task_name)→业务门(`remote_business_response_ok`,实现在 `zhaoshang_remote_task` 共享层,Recovery 同源;success/rspCode 不过→progress=None,REMOTE_UNKNOWN)。
- 返回形状(钉死,特征化测试保护):`{"task_id","task_number","batch_number","progress","rsp_desc","enabled","remote_calls":2}`。
- **Dashboard 子任务数显示不均的根因**(记录,非 bug):投影 `subtasks` 字段只来自 `recovery_evidence_json.observations.signature`(6 元计数签名,dashboard_projection.py:409-413);未经恢复观测、或 rspDesc 缺计数标签/业务失败的任务天然无计数。live 进度是独立的 `live_progress` overlay(:822)。
- 保护测试:`tests/test_legacy_adapter_progress_stop.py`(22 个,含 10 个特征化)、`tests/test_zhaoshang_progress_probe.py`。

## 6. Export(导出/收割)

- `harvest`(:3160)= wait-export-only 子进程(**无 create 路径**);已存在文件→`harvest_existing`(:2709,零远端调用)。
- 验收门 `_validate_result_workbook`(:2512):provenance 必需(执行编号/批次号==task_name/商城标识==mall_key)+ 外批行过滤 + 全量 Counter 严格相等。部分导出走 `classify_existing_export_partial`(:2964,执行编号前缀 `{task_number}-` + ambiguous/exact_absent)。**此处是 Z-1/Z-2 历史 Bug 区,测试最密(30+),改动必须先读测试**。
- Re-export(Dashboard"重新导出")与首次导出共用 `_existing_remote_harvest_core`(recovery/harvest.py:569):create_calls 恒 0;admissible_states={SUBMITTED,RUNNING,EXPORTED};attempt_headroom=2;2026-09-04 hotfix 后 live_identity_proof(progress probe 答案)可替代 priority 证据过 exact-identity 门——修掉了"远端任务完好但 priority 从未确认→re-export 被 INVALID_REMOTE_STATE 拒"的根因。第三入口 `harvest_existing_customer_prewarm_job`(Issue #13 跨日收割,recovery/harvest.py:1001)复用同一 core:source_scope="prewarm_payload"——溯源取自 durable prewarm payload 的 mapping source(mall_source_id),无 STEP1 快照要求;re-export/首次导出的 frozen/latest 快照 scope 语义不变。
- 导出失败重试:子进程内 exportTask 重试≤5(读性质);启动失败分 transient(→RUNNING 同身份重排)与 deterministic(→MANUAL fail-closed)。

## 7. Rebuild(重建)——与 Re-export 严格互斥的语义

- **Re-export = 同一任务重新取导出;Rebuild = 弃旧建新(新 task_name/新 task_id/新 task_number/新 remote_identity)**。两协议不共用写路径,仅共用 preflight probe 与锁。
- 写前协议(dashboard_manual_ops.py `_run_manual_rebuild_once`:842):create_rebuild_action(INTENT)→只读 precheck(query_task_progress_for_job)→stop_running_task(幂等 disable,resume_phase 对账)→`supersede_remote_job_to_replacement`(单事务:新 SUBMIT_INTENT job+replacement 行+旧 job SUPERSEDED+crawl 迁移回 PENDING)→claim_remote_submission→adapter.submit→reconcile_remote_job 写新 remote_identity。
- 崩溃恢复:STOP_SENT 后重启只读对账不二次 stop;submit 中断绝不重复 create(test_rebuild_crash_recovery.py 钉死);`_resume_replaced_rebuild`(:670)恢复未烧毁 INTENT。
- 幂等/防双击:DashboardActionRunner `(action_type,target)` in-flight 占位 + rebuild action 唯一约束 + 同日锁。

## 8. Recovery(恢复)

已模块化于 `app/services/recovery/`(harvest/priority/wait_export/no_result/model/common/validation)。原则:
- 一切恢复**先 reconcile 后建**:SUBMIT_UNKNOWN→reconcile_unknown 精确查询;卡 RUNNING→probe_running_task 只读活探测→restart(_restart_protocol,写前 ACTIVE 再验证、外部禁用拒重启、单次 restart);export 无价→probe_export_no_price 四连查。
- 恢复通道入口集合用 frozenset 常量钉住(06e2c6a 教训);诊断字符串双重职责(人类日志+控制流)是已知 DESIGN RISK(审计 §11),新增恢复通道必须复用常量。

## 9. Priority Safety(优先级写安全——最高隔离要求)

- 唯一 DB UPDATE 实现在**仓库外** `zhaoshang_priority_boost.py`(E:\A2026);adapter `_production_priority_runner`(:983)只做:精确身份(target=task_name+task_number+mall_code)、marker 时间窗(window_start=marker−2min;window_end=max(marker+15min, now+30s))、外部 boost 调用(内部:FOR UPDATE 锁→仅按主键 UPDATE→rowcount 严格相等→事务内验证→commit→readback)、PRIORITY_* 事件落账。
- `prioritize`(:1314)幂等:已有 accepted 证据(UPDATED/ALREADY_HIGHEST,身份必须==CREATED 身份)直接重放;异常分类 retryable(MySQL 瞬态码白名单)/REMOTE_NOT_MATERIALIZED/不可重试三态。
- **禁止**:任何"通用 execute_sql 修生产数据"、第二套 priority 更新器、放宽身份/窗口/rowcount 校验。测试:test_zhaoshang_priority_core.py + test_zhaoshang_priority_boost.py(双副本防漂移)。

## 10. Concurrency(并发模型)

- 单写者:per-day `ProcessLock`(`<run>/incremental_price_audit.lock`,msvcrt)+ SQLite durable lease(acquire_lease,ttl+generation)。Scheduler tick、Dashboard 手动 re-export/rebuild、恢复通道全部走同一把锁(re-export 在 `_existing_remote_harvest_core` 内、rebuild 在 `_acquire_recovery_lease`)。
- 调度层:Task Scheduler `IgnoreNew` 丢弃重叠 tick。
- 进程内:DashboardActionRunner in-flight map(HTTP 409 ACTION_IN_FLIGHT)。
- 进程级 env 窗口(`_locked_temporary_environment`/`_locked_session_construction`)已加锁——但 adapter 各生产方法仍要求**串行调用**(tick 内各阶段天然串行);禁止在多线程中共享一个 adapter 实例并发调远端方法。

## 11. Failure Semantics(失败语义)

- 三态铁律:**UNKNOWN ≠ FAILED ≠ RETRY CREATE**。SUBMIT_UNKNOWN 只能 reconcile;EXPORT/waite-export 未知→RUNNING 同身份重排;只有 deterministic 证据才 MANUAL。
- 异常分类表(已抽出至 zhaoshang_remote_task):RemoteTaskIdentityConflictError=**被证明的**身份冲突(live≠durable/查找歧义/商城标识不一致);畸形/超时/传输故障**不是**身份冲突,保持无类型计 transient。LegacyPreSendFailure=发送从未发生。LegacyProbeEvidenceError=探针无可用证据。
- 业务门:success=false 或 rspCode∉{None,"0000"} 的响应不得捐献证据(计数/终态/重启授权)。唯一实现 `zhaoshang_remote_task.remote_business_response_ok`(2026-09-05 r1 起;`remote_running_recovery` 保留 re-export 兼容旧 import,`legacy_zhaoshang_adapter` probe/restart/stop 区段仍走旧路径,待相应能力迁移时换源)。

## 12. Forbidden Patterns(禁止模式)

1. 按 SKU 单字段直接复用结果(必须五元组身份;URL 有歧义时同理禁 URL 单字段)。
2. SUBMIT_UNKNOWN 后直接新建任务(必须 reconcile_unknown 精确对账)。
3. 重建后继续使用旧 task_number/task_name 导出(SUPERSEDED 不可导出;一切以 replacement 新 job 为准)。
4. Dashboard 直接操作招商局 DB/API(必须 Action Runner→CLI→能力模块链)。
5. 通用 execute_sql 修改生产数据;priority UPDATE 只许经外部 boost 模块既有协议。
6. 多个 Auth 实现(唯一 AuthManager;"capabilities→Auth Provider"单向依赖;各能力模块不自建登录)。
7. 新建 JSON/第二 SQLite 作为业务状态源(唯一 truth=state.sqlite3)。
8. re-export 与 rebuild 混为一个操作/一条代码路径。
9. `except Exception` 后当成功(fail-closed;吞错仅允许事件记录且原异常照抛)。
10. 未经特征化测试保护就移动/改写本文件能力地图中的任何区段(MOVE-only 先行)。
11. 在多个位置重复实现"端点→标识符"选择(batchNumber/taskId/taskNumber 键控只许一处)。

## 13. 迁移台账与下一步

**已完成(2026-09-05)**:Progress 能力 MOVE-only 抽出(zhaoshang_task_progress + zhaoshang_remote_task 种子;adapter re-export 全兼容;22 特征化测试 + 243 集群回归绿)。**Review Fix R1(2026-09-05)**:业务响应门 `remote_business_response_ok`/`PROVEN_BUSINESS_SUCCESS_RSP_CODES` 自 remote_running_recovery 逐字迁入 zhaoshang_remote_task(共享远端语义层),Task Progress 与 Recovery 平级依赖共享层,re-export 保旧路径,单一实现守护测试在位。

**推荐下一步(仅一个)**:迁移 **Running Probe**(`probe_running_task` :1617-1853,只读):与 Progress 同族(只读、共享 zhaoshang_remote_task 身份语义、复用会话工具箱),顺手把 adapter→remote_running_recovery 的函数级延迟 import 债务理顺;先补特征化测试再 MOVE。

**后续顺序建议**(每步一 commit,先测试后移动):Export 验收门(`_validate_result_workbook`+partial 分类,Z-1/Z-2 高危区,测试最密)→ Restart/Stop 写控制协议 → Submission+Reconcile → Batch 证据/Identity 静态函数族 → Priority 编排层(DB UPDATE 留在外部模块,永远最后或不动)。死代码 `_classify_existing_export_partial_legacy`(:2776,~188 行)先加"零引用"守护再删。

**每次迁移的验收标准**:特征化测试不改一行仍全绿;re-export/rebuild/恢复通道回归绿;re-export 保持 create_calls=0;无新 import 环(依赖方向只许 adapter→能力模块→zhaoshang_remote_task)。
