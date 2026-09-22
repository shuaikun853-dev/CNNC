# TESTING.md - 验证指南

> 增量 tick、SQLite checkpoint、finalize/materializer 与历史 identity 回归命令见 [第一步增量调度手册](docs/runbooks/STEP_1_INCREMENTAL_SCHEDULER.md)。

> 自动化测试与生产验收分开。测试不能调用真实 `/add`、审批、推送、SQL 或本地爬虫；只有用户明确要求执行某一步时，才按对应 runbook 做生产验收。

## 1. 环境

- Python 3.11+；当前机器使用 Python 3.14。
- 安装：`python -m pip install -r requirements.txt`
- 项目内主要依赖：FastAPI、Pydantic、PyYAML、pytest、httpx、openpyxl。
- 敏感输入位于被忽略的 `data/*.curl`、`data/*.env`、`data/*.json` 或当前进程环境变量；不得打印其值。
- 运行前设置：`$env:PYTHONIOENCODING='utf-8'`。

## 2. 自动化测试

### 收集和全量

```powershell
cd E:\.A中核运维\cnnc-ops-agent
$env:PYTHONIOENCODING='utf-8'
python -m pytest --collect-only -q
python -m pytest tests -q
```

不要在文档中长期写死测试总数；以收集命令实时输出为准。`StarletteDeprecationWarning` 和本机 requests 依赖兼容警告目前不等于测试失败，但应在升级依赖时单独处理。

### 按改动范围

```powershell
# 第一步审计、模型、Excel、cURL、爬价回填及 multi-site 并发 claim/锁定 final/SUCCESS 价格/全表终态/双 Git 门禁（全 fake worker）
python -m pytest tests\test_work_01_saas_approval_json_audit.py -q

# SaaS / 招商局 Token 初始化、Rotation、原子保存、并发锁和只读 401 单次重试
python -m pytest tests\test_auth_manager.py -q

# 第一步审计后受控驳回/通过（仅测试替身，不调真实写接口）
python -m pytest tests\test_reject_saas_audit_excel.py tests\test_approve_saas_audit_excel.py -q

# SaaS 文档 ID 受控价格补推（仅测试替身，不调真实写接口）
python -m pytest tests\test_push_saas_commodity_price.py -q --basetemp=.pytest_tmp_push_controlled

# 第二步状态判定和 Excel
python -m pytest tests\test_export_current_monitor_status.py -q

# 第三步授权开关、分阶段并行、恢复模式、回填和任务结果映射
python -m pytest tests\test_monitor_stb_fallback.py tests\test_process_monitor_zs_result_mapping.py tests\test_step3_prewarm_cache.py tests\test_zhaoshang_task_phases.py tests\test_zhaoshang_resume_modes.py -q

# 招商局当前任务优先级保护（含 commit 期间 task_status 漂移仅观测、task-id/is_valid/weight 回读继续 fail-closed）
python -m pytest tests\test_zhaoshang_priority_boost.py tests\test_zhaoshang_resume_modes.py -q

# 招商局 batch fast path（exact executeNumber/PK、单 schema、02/03 终态竞态、priority/harvest 独立预算、旧 SQLite migration、事务回滚、SQL 价格/title 语义、nbdeli-only 数字 `PCS` 等价、qstbg/lxwlmall 严格 URL 变体、mymro/ofs submitted URL code 身份与不可解析链接逐条 legacy fallback、jslink→nbdeli 新建规范化、逐商城 batch/legacy 混合路由、source_id scoped executeNumber→唯一 crawl_work、progress/probe/SQL 共用绝对 deadline、durable mixed harvest 和跨 tick/崩溃恢复；全 fake DB/HTTP/本地 SQLite）：`python -m pytest tests\test_batch_zhaoshang_priority.py tests\test_batch_zhaoshang_adapter.py tests\test_batch_sql_result_adapter.py tests\test_daily_audit_store.py tests\test_incremental_price_audit.py tests\test_incremental_audit_runtime.py tests\test_legacy_zhaoshang_adapter.py tests\test_customer_prewarm_discovery.py -k "batch or priority or submit_unknown or sql_result or jslink or hybrid" -q`
# 旧诊断和工作流安全边界
python -m pytest tests\test_safety_guard.py tests\test_scope_guard.py tests\test_workflow_scope_guard.py -q
python -m pytest tests\test_cnnc_auth_browser_harvest.py tests\test_cnnc_auth_browser_login_autofill.py tests\test_cnnc_auth_browser_tmp_root.py -q  # CNNC 认证浏览器一次性临时 Profile 落仓库 runtime/browser_tmp（生产 checkout 在 E 盘；runtime 根不可用时回退系统 TEMP 保认证）+ 启动清扫陈旧残留（2026-09-15 C 盘满事故回归）

# 上下文文档体积、链接和必备规则
python -m pytest tests\test_context_documents.py -q
```
### 增量价格审计
```powershell
# Scheduler 路由（白天 15 分钟、17:00-17:57 每 3 分钟完整 tick、18:00-20:00 每 5 分钟 closeout、20:35 一次性 safety closeout）、snapshot 180 秒有限重试、公平/post harvest、freeze gate、writer lock、只读 status；simulation 文件另证四 trigger 时间线 + IgnoreNew 重叠丢弃/不堆积/单 writer。runtime 测试还覆盖显式 runtime root 的 Windows Junction 别名不被解析到其他 worktree、`daily_status.json` 原子替换遇到短暂 `PermissionError` 时有界重试且重试耗尽不反转已完成业务命令，以及 BATCH_STATE 固定兼容文件被 Windows reader 占用时不反转已 checkpoint 的命令、不可变 `batch_state_snapshots/BATCH_STATE_*.md` 仍持续生成且 newest 可解析；固定 BATCH_STATE 未锁时继续原样刷新并保留外部招商局事件。相关套件同时覆盖固定 8 站 remote 终态转 local、按站预检及 partial settle、exit 2/3、启动前 live tick 预算与未启动 claim 退还尝试次数，以及最终物化 intent、FINALIZED/manifest/最终 metrics 同事务回滚和幂等、晚到采集重试、prewarm 不漂移；文件与 DB 不具备跨系统 ACID。
python -m pytest tests\test_incremental_audit_operations.py -k "scheduler or scheduled" -q; python -m pytest tests\test_scheduler_frequency_simulation.py -q
python -m pytest tests\test_harvest_only.py -q  # 白天 harvest-only：无 discovery/Detail/Model/submit/finalize、本地 NOOP、全量 sweep、幂等
python -m pytest tests\test_incremental_price_audit.py tests\test_incremental_audit_runtime.py -k "existing_remote_drain or drain_only" -q; python -m pytest tests\test_remote_running_recovery.py -q  # RUNNING 停滞判定/一次性精确重启/RECOVERY_EXHAUSTED 终态（全 fake adapter）
python -m pytest tests\test_daily_audit_store.py tests\test_incremental_audit_runtime.py tests\test_incremental_audit_operations.py -k "process_lock or writer_lock or lock_conflict or lock_status or active_writer or materialization_remains" -q
python -m pytest tests\test_daily_audit_store.py tests\test_cnnc_saas_snapshot_adapter.py tests\test_saas_audit_core.py tests\test_title_match_model_retry.py tests\test_incremental_price_audit.py tests\test_incremental_audit_runtime.py tests\test_harvest_only.py `
  tests\test_incremental_audit_operations.py tests\test_incremental_audit_resilience.py tests\test_incremental_audit_recovery.py tests\test_scheduler_frequency_simulation.py tests\test_partial_exact_harvest.py tests\test_incremental_price_audit_finalize.py tests\test_finalize_failed_link_rows.py tests\test_incremental_production_readiness_audit.py tests\test_incremental_audit_materializer.py tests\test_legacy_zhaoshang_adapter.py tests\test_incremental_price_audit_history.py tests\test_incremental_history_benchmark.py tests\test_incremental_local_fallback.py tests\test_customer_prewarm_lifecycle.py tests\test_customer_prewarm_dispatch.py -q
# Issue #9 客户参考链接预爬旁路:提取等价/发现状态/缓存复用/每日同步幂等/STEP1 隔离/customer-only 恢复/scanner CLI 与 installer 文本门禁
python -m pytest tests\test_customer_reference_links.py tests\test_customer_prewarm_store.py tests\test_customer_prewarm_discovery.py tests\test_customer_prewarm_daily_sync.py tests\test_zhaoshang_history_backfill.py tests\test_customer_prewarm_step1_isolation.py tests\test_customer_prewarm_customer_recovery.py tests\test_customer_prewarm_scan_cli.py -q
# Issue #11 史泰博本地预爬:URL 识别/失败分类/状态机与 claim 租约/暂缓退避/缓存同步幂等/plan-only 零写与 live 单次有界执行(覆盖 app/services/staples_prewarm.py 与 scripts/staples_prewarm_run.py)
python -m pytest tests\test_staples_prewarm.py -q
# Issue #10×#11 集成契约:候选归属与 dispatcher 隔离/CACHE_HIT 终态/旧 schema additive 迁移保行/plan-only 双零写/同 DB 共存不互污染/STEP1 隔离
python -m pytest tests\test_customer_prewarm_integration.py -q
# Refresh v2 合并树交叉契约(Case H-K):客户缺席×prewarm intake 共存/intake 失败不破坏缺席收口/快照技术失败 fail-closed 不变缺席跳过/prewarm 在场时 finalize 投影 1:1 对齐
python -m pytest tests\test_refresh_v2_cross_contract.py -q
```
历史 fixture 缺失时，最后一项会 skip；不得用生产接口补齐它。
### Dashboard 测试（只读看板）
`tests\test_dashboard_projection.py`（投影/分级/日历/scheduler 降级/fingerprint）+ `tests\test_dashboard_api.py`（GET-only、400/503、ETag/304、自启脚本与 runbook 的 -Apply 门控）：
`E:\Python314\python.exe -m pytest tests\test_dashboard_projection.py tests\test_remote_diagnostics.py tests\test_dashboard_api.py tests\test_dashboard_action_runner.py tests\test_dashboard_actions_api.py tests\test_manual_reexport.py tests\test_manual_rebuild.py tests\test_live_proof_fallback.py tests\test_incremental_price_audit_force_finalize.py tests\test_incremental_audit_status.py -q --basetemp=runtime\pytest-tmp\dash; node tests\js\run_dashboard_js_tests.js`（受控操作台均用 fake runner/tmp SQLite,不触网、不碰生产 runtime；投影/JS 同时覆盖 MANUAL priority 具体原因、SUBMIT_UNKNOWN 原始诊断、最近 reconcile 与 `1/4`、历史诊断不可恢复诚实文案，以及 Token/Cookie/Authorization/camel-snake-hyphen credential/JWT/Windows 与 Unix 私有路径脱敏；其中包括任意 token/secret/API-key/Authorization 后缀键、X-API-Key、Proxy-Authorization、多值 Cookie/Set-Cookie、Basic Authorization、escaped quoted secret、无扩展名 Windows 路径，并验证 SUBMIT_UNKNOWN durable 写边界不落原始值。`test_live_proof_fallback.py` 额外覆盖历史 collapsed `MANUAL_REQUIRED` 只有在 exact durable `PRIORITY_UNCONFIRMED(reason=PriorityScopeSafetyError,retryable=false)`（`retryable` 必须是显式 JSON boolean false）+ live `REMOTE_TERMINAL` 六计数严格满足 `total=finished=immutable request count` 且 `pending=running=failed=waiting_db=0` 时才允许 re-export existing task；计数缺失/规模不一致/任一失败、ACTIVE/UNKNOWN/身份冲突/可重试或其它非重试型 priority reason 均保持 fail-closed；同文件还覆盖 collapsed MANUAL 的 exact customer-prewarm producer group 可切 `prewarm_payload`，以及缺 link/错 producer/额外 candidate 继续 frozen-snapshot fail-closed，普通 prewarm production-shape 仍由 lifecycle 独占；re-export 绝不 create/prioritize/stop）。
`tests\test_saas_flow_diagnostic.py`（SaaS 链路诊断：SQL registry 只读、降级/缓存/限流、分支分类、18:00 前增长分叉、summary 隔离；全部 fake connection/tmp SQLite，绝不连真实数据库）一并执行，命令追加该文件名即可。
`tests\test_production_identity.py`（生产部署身份只读投影：fake runner + tmp git 迷你仓库，不碰生产 git/调度器/网络）：
`E:\Python314\python.exe -m pytest tests\test_production_identity.py -q --basetemp=runtime\pytest-tmp\prodidentity`
# Issue #13 客户链接预爬看板:projection/手动重爬/精确派发/API+页面 smoke；Batch 专项同时覆盖 purpose 隔离、placeholder→真实 SKU、returned URL、唯一 executeNumber、多结果 fail-closed、mall+backend 分组、redispatch/manual/cross-day mixed lifecycle、legacy 历史保持和 plan-only 零构造
# 跨日生命周期:tests\test_customer_prewarm_cross_day_reconcile.py 覆盖 identity recovery、跨轮 priority、pre-remote linkage guard、Batch 已 claim 但无 identity 的手工/调度恢复、MANUAL 阻断 A3、真实 recovered job harvest→SUCCESS→shared cache→CACHE_HIT、(owning_day,job_id) 去重、两个 durable fairness cursor、dispatch 超时后 lifecycle 独立保底窗口、当天日锁 LOCK_BUSY 时仍运行历史 reconcile/harvest（但 SKIPPED_ALREADY_RUNNING 不双跑）、同号/未来日错绑防护、manual/同步错误报告、lock busy/missing DB/预算及四库 plan-only 零写；tests\test_customer_prewarm_lifecycle.py 覆盖 A3 扫描互斥和候选重检、多候选原子重绑与 prior crawl 恢复、未发送大组缩小后原子退役并只重排剩余候选、旧 marker 精确判定、退出组员不残留 PENDING、退役后中断恢复、POST 前 marker 和跨日不确定提交人工阻断/报表、预算/NOT_ATTEMPTED 跨日重试、max-jobs 硬限额、无关联当天 SUBMIT_UNKNOWN 不触发远程读写，以及当天 MANUAL/不确定 NOT_FOUND fail-closed；tests\test_customer_prewarm_dispatch.py 覆盖跨日 PENDING 孤儿防重复和无精确 link 禁裸 ID 提优；tests\test_prewarm_manual_recrawl.py 覆盖 Dashboard 新请求 Batch 优先和 exact claimed Batch 无重复 POST 的跨日 continuation；tests\test_customer_prewarm_store.py 覆盖旧 schema 只读预览；tests\test_customer_prewarm_daily_sync.py 覆盖全入口共享同步锁；tests\test_finalize_failed_link_rows.py 覆盖 manual blocker 报表可见且错绑不冒认失败；tests\test_prewarm_priority_recovery_contract.py 是两轮 priority liveness 门禁
# production-shape 跨日收割:tests\test_prewarm_cross_day_production_shape.py 覆盖真实 dispatch 创建的 D1 任务(无 snapshot / 无关冻结 snapshot)经 prewarm-payload 溯源收割成 SUCCESS；并覆盖 exact nonretryable PriorityScopeSafetyError MANUAL 在 producer/link/payload identity + terminal 14/14 全成功 live proof 后同日或 D2 跨日仅 harvest 成 COMPLETED，13/14、failed>0、total 不符、可重试、错 reason/identity/producer、URL group/payload mismatch、缺 durable event 和 UNKNOWN live proof 均保留 MANUAL，且全过程以 tripwire 证明 recovery 不 create/prioritize/restart/disable/enable。`tests\test_customer_prewarm_lifecycle.py` 另证正常 harvest 不被旧 MANUAL 占满 cap、正常队列自身用 durable cursor 轮转、deadline 为 MANUAL probe 保留 bounded 时间片，MANUAL 独立 bounded cursor 多轮可达。普通 STEP1 run_manual_reexport 对同一任务继续 fail-closed
python -m pytest tests\test_prewarm_dashboard_projection.py tests\test_prewarm_manual_recrawl.py tests\test_customer_prewarm_dispatch_exact.py tests\test_customer_prewarm_cross_day_reconcile.py tests\test_customer_prewarm_lifecycle.py tests\test_prewarm_priority_recovery_contract.py tests\test_prewarm_cross_day_lifecycle.py tests\test_prewarm_cross_day_production_shape.py tests\test_prewarm_dashboard_api.py tests\test_prewarm_page_smoke.py tests\test_prewarm_round2_infra.py -q
fixture 原则：只用 `tmp_path` 下的临时 SQLite/`daily_status.json` 或 monkeypatch 替身，不碰生产 `runtime\incremental_price_audit`；不注册真实计划任务，自启用例只读文本、从不执行 `-Apply`。
### 编译检查

```powershell
python -m py_compile `
  scripts\audit_saas_excel_file.py `
  scripts\export_current_monitor_status.py `
  scripts\process_monitor_no_push_zhaoshang.py `
  scripts\reject_saas_audit_excel.py `
  scripts\approve_saas_audit_excel.py `
  scripts\push_saas_commodity_price.py `
  scripts\auth_manager.py scripts\curl_utils.py
```
只有修改外部招商局项目时才编译其脚本；不要为了查看帮助而创建真实任务。
## 3. 文档与交接验证
只改文档时，至少运行：

```powershell
python -m pytest tests\test_context_documents.py -q
git diff --check
git diff -- AGENTS.md CURRENT_STATE.md TESTING.md README.md docs\runbooks tests\test_context_documents.py docs\superpowers

$secretPatterns = @(
  ('x-access-token=' + 'eyJ'),
  ('auth-token=' + 'eyJ'),
  ('Bearer ' + 'eyJ'),
  ('sk-' + '[A-Za-z0-9]{20,}'),
  ('password' + '\s*=\s*[^<]')
)
Select-String `
  -Path AGENTS.md,CURRENT_STATE.md,TESTING.md,README.md,docs\runbooks\*.md `
  -Pattern $secretPatterns

Get-ChildItem runtime -Recurse -Filter BATCH_STATE.md |
  Select-String -Pattern $secretPatterns
```

验收：

1. `AGENTS.md` 仍是短总规则，没有重新复制三步全文或运行历史。
2. `CURRENT_STATE.md` 只描述并行发现和交接协议，不保存唯一当前批次。
3. 每个活跃批次只更新自身运行目录的 `BATCH_STATE.md`；多个步骤及同一步多个批次可以同时为 `ACTIVE`。
4. 其他批次状态不构成当前批次的启动门；约 20/30 轮只提醒和准备交接，不拒绝继续执行。
5. 三个 runbook 的输入、授权门、暂停条件和输出与当前代码一致。
6. `README.md` 只承担项目概览和快速入口。
7. 没有真实密钥、Cookie、Token、账号、密码或连接串。

## 4. 第一步验收
权威流程：`docs/runbooks/STEP_1_PRICE_AUDIT.md`。

### 运行前

- 本轮批次目录为新目录，目录内 `BATCH_STATE.md` 为 `ACTIVE / STEP_1`；其他批次状态不影响本轮。
- 五份 cURL 均由 `scripts/curl_utils.py` 本地解析；没有通过 `/add` 探测认证。
- `data/auth_state.json` 不存在时由 `scripts/auth_manager.py` 一次性从 cURL 初始化 SaaS/招商局两组 Token；存在后不再从 cURL 读取运行时 Token。
- refresh 同时返回并原子保存新的 accessToken、refreshToken；日志不含完整 Token。招商局 `/add` 只在调用前刷新，任何异常都不自动重试创建。
- SaaS 与中核快照都满足 `实际行数 == count`，并已保存 `fetch_summary.json`。
- 模型参数使用稳定值：批大小 10、并发 1。
- 已通过既有受控加载器从 `data/ZHAOSHANG_DB.env` 向当前进程加载完整 `ZHAOSHANG_DB_*`，但日志不显示值。
- 初始命令没有 `--enable-crawler` 或 `--auto-stbchina-fallback`，除非用户已明确授权。

### 结果

- 最终 Excel 有 `summary`、`record_results`，记录数等于 SaaS 快照总数。
- detail 状态区分成功、单参考价、无参考价、请求失败。
- 模型没有把 HTTP/超时/解析错误伪装成“人工复核”。
- 每个招商局任务都有 taskId/taskNumber、链接数、导出状态和提优结论。
- 招商局单商城模板必须有商品编码；URL 可空，仅 URL 无商品编码必须拒绝；有 URL 时商城分组以域名为准。
- “已提优”都有 `affected_rows>0`；已按精确主键范围确认全为 `-1` 的幂等状态单独统计且不冒充本次提优。`task_status=00/01/02` 都不影响 priority 更新资格，完成任务也必须回读确认全部精确有效行均为 `-1`。
- 同一 taskNumber 可对应多个商城；测试必须证明以配置 mall code、taskNumber、marker create_time 窗口定位子任务，并且 UPDATE 只含目标 `task_id` 主键。
- 页面价 0 使用 `PRICE_IS_ZERO`/“价格为0”并整行高亮。
- 任意后补价格都重新计算 `page_vs_customer_check`、`price_changed`、`over_30pct`、`final_decision`。
- 缺价时先有清单和授权门，未授权不启动本地脚本。
- multi-site 首轮只生成 review/manifest 并锁定候选数、清单及文件 SHA-256、已审查 Git 提交 `28870666aeb99a10494ebb91419aadf3bff4c0a8`，设 `WAITING_USER`且零爬取；计划失败只用 review 恢复入口，不重跑第一步；二次授权后的独立续跑不重跑招商局，不继承认证/模型/数据库密钥，并严格对账每站输入输出；初始化/建任务/启用/提优证据异常、exit 2/3、部分/外来/重复输出都不生成 formal final，也不自动重跑。
- 驳回和通过都不是第一步默认动作；驳回仅限指定最终 Excel，受控通过可用指定最终 Excel，或用户在同一授权消息中明确指定为本轮结果表的 `saas_audit_review_*.xlsx`。两者都必须有候选数、清单 SHA-256、写前待审核对、逐批 `rspCode=0000` 证据和写后目标无遗留；驳回固定使用 `data/sass-bohui.curl`，`--fill-colors yellow-only --require-customer-price` 必须只选 F 列黄色且表头“客户价”非空的行；通过固定使用 `data/sass-tongguo.curl` 并排除 F 列红/黄色行，`--require-customer-price` 必须再排除表头“客户价”的空值和空白字符串，数值 0 视为非空。

重点回归：

```powershell
python -m pytest tests\test_work_01_saas_approval_json_audit.py -q -k "Zhaoshang or zero_price or model or curl or fallback"
python -m pytest tests\test_zhaoshang_priority_boost.py -q
```

## 5. 第二步验收

权威流程：`docs/runbooks/STEP_2_MONITOR_STATUS.md`。

先验证帮助不会发网络请求：

```powershell
python scripts\export_current_monitor_status.py --help
python -m pytest tests\test_export_current_monitor_status.py -q
```

生产运行只有用户要求第二步时才执行：

```powershell
python scripts\export_current_monitor_status.py
```

验收：

1. 中核实际行数等于接口 `count`；SaaS 每批最多 200 个编码且每批成功。
2. `saas_matched + saas_missing = cnnc_total`。
3. `pushable + needs_processing + saas_missing = cnnc_total`。
4. 五个固定 Sheet 存在，全量表保留原始 `newPrice`。
5. 可推送条件严格为 `isSync=1`、`isSyncStandard=1`、`syncPriceStatus!=5`、`newPrice` 为有限正数。
6. `syncPriceStatus=5` 优先归为价格检测校验失败；其他状态继续校验 `newPrice`。
7. SaaS 只读分批明确返回 401 时只刷新并重试一次；第二次失败或任一分批异常时没有生成“全量”输出。

## 6. 第三步验收

权威流程：`docs/runbooks/STEP_3_NO_PUSH_PROCESSING.md`。

### 生产结果

- 只处理指定监控表中 `是否可推送=否`；没有混入“是”。
- 每个否记录有 detail 状态，`>=2` 与 `0/1` 分流可以按记录对账。
- 基础三文件和待确认清单写入同一新批次目录，不覆盖输入或历史文件。
- 招商局先顺序完成全部 add、启用和提优，再以 `--zs-wait-concurrency` 有界并发等待/导出；并发 worker 没有写操作。
- 每商城都有精确 task_name、taskNumber、mall code 和提优证据；断点重跑复用同批次已有任务或有效导出，不重复 add、启用或提优。
- `BATCH_STATE.md` 在 `/add` 前已有 `ADD_INTENT`，并逐次记录 taskId、启用、提优和导出；已有 add 事件但查无任务时 fail-closed。
- 导出使用临时 XLSX 原子替换；复用前表头有效且输入/结果行数一致，重复商品编码不造成误判。
- 招商局结果解析必须保留页面真实 `商品最小品类单元id`、`商品标题/名称`、价格、商城标识和评论；页面 SKU 合法时写为 `CONFIRMED`，不得继续把 `zhpq-*` 占位码当真实 SKU。
- Shared Crawl Cache 命中必须发生在招商局建任务之前；只有价格、标题、真实 SKU 齐全才直接复用。京东/苏宁还必须有评论证据；整数走 `comment_count`，`1万+` 等原文可走 payload `comment_count_raw`。完整命中的 URL 不得再次进入招商局待爬集合。
- 没有价格的行不填商城标题；京东/苏宁有价格但缺评论数必须显式标记并从可导入 zhImport 的完整结果模板排除，同时进入待确认清单保留已有证据，不得静默当完整结果。
- `--skip-zhaoshang` 模式没有 add、启用、SQL 或脚本日志。
- `--auto-stbchina-fallback` 只处理仍缺价的 `stbchina.cn` URL，且必须有本批次授权。
- `--detail-reference-fallback` 只补同 `documentId + 完整 URL` 且价格、标题同时为空的行，来源标记 `中核detail参考回填`。

## 7. SaaS 受控价格补推验收
权威流程：`docs/runbooks/SAAS_CONTROLLED_PUSH.md`。
- ID 文件只接受 1 至 500 个不重复纯数字文档 ID；预览不得刷新认证或调用推送，并须将每个 ID 唯一映射到关系 `id`。
- 预览锁定 `push_candidate_count` 和包含状态、关系 `id`、人工跳过动作的 `execution_plan_sha256`；执行参数、保存的 manifest 和刷新认证后的复查必须完全一致。
- 已手工发送或结果不确定的关系 `id` 必须标记 `SKIP_MANUAL_ATTEMPT`；每项写请求无自动重试，任一失败停止后续项目。
- 成功项逐条 fsync；已有事件或汇总文件时拒绝整批重跑。只有汇总 `COMPLETED` 且确认数等于候选数才能报告成功。
## 8. 常见阻塞

| 症状 | 先检查 | 处理边界 |
|---|---|---|
| `rspCode=3333 / 请重新登录` | `data/auth_state.json` 是否完整、refresh 是否失败、代理是否已清除 | refreshToken 失效则暂停并要求人工登录；不得回退旧 cURL |
| detail 全失败 | cURL 请求体是 `documentId+fourCategoryId` 或受支持的 standard detail 形式 | 更新登录态后重跑；不用列表价冒充 |
| 模型持续失败 | 三个模型变量、无代理 opener、批大小 10/并发 1 | 保持重试或暂停；不降级成业务“人工复核” |
| `/add` 失败 | 调用前 refresh 证据、payload、模板字段 | 输出完整脱敏错误并暂停；请求发出后绝不自动重试 `/add` |
| 优先级 0 行 | 精确 task_name、taskNumber、mall code、marker 窗口、payload 有效子任务数、全量 `weight_type` 分布 | 轮询数量必须等于 payload 数；预查已全为 -1 则记录幂等；否则 0 行或回读不全为 -1 都暂停，不按 `task_status` 跳过且不放宽 SQL |
| 导出无价格 | 任务完成状态、export 返回、结果表头和 URL/SKUID 映射 | 生成缺价清单；未经授权不跑脚本 |
| Excel `PermissionError` | Excel/WPS 是否占用 | 关闭后重跑或使用新文件；不覆盖旧批次 |
| 403/405/验证码 | 商城登录/风控状态 | 立即停止，不规避 |

## 9. FastAPI 旧服务
仅修改旧诊断或 workflow API 时使用：

```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
python -m pytest tests\test_diagnose_integration.py tests\test_workflow_api.py -q
```

健康检查：`GET http://127.0.0.1:8000/health`。旧 `/diagnose` 只返回模板，不执行 SQL。

## 10. 生产就绪只读检查

一行命令：`python scripts\production_readiness_check.py --json`（退出码 0=READY、1=NOT READY、2=意外错误）；只读不注册任务/不连库/不刷新认证/不发网络请求，详见 [docs/runbooks/PRODUCTION_READINESS.md](docs/runbooks/PRODUCTION_READINESS.md)，测试 `tests\test_production_readiness.py`。
