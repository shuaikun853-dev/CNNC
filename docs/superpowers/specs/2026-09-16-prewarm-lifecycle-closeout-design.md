# 客户链接预爬生命周期收口设计（治本三件套 + 失败进最终表）

日期：2026-09-16 · 状态：原始设计记录（用户拍板：失败数据按原行格式进表不建新 sheet；NO_RESULT 算失败；ofs/ycw 脚本兜底推迟到脚本定稿后接入）。下文 A2/A3 与“非目标”保留当时范围，现行跨日扩展及 MANUAL 安全规则见文末更新。

## 背景与问题

1. 当日 18:00 FINALIZED 冻结后创建的预爬 job 无人收割（所有 runtime 命令 ALREADY_FINALIZED 短路）——0915 实证：12 job 派发后 9 个靠人工原语补收，数据差一点全部滞留远端。
2. SUBMIT_UNKNOWN 无身份 job 没有任何恢复入口（reconcile_unknown 只活在当日活体 harvest 闭包）——0915/0916 持续产生僵尸（nbdeli 172、ycw、ofs），候选永远卡 REMOTE_DISPATCHED，主看板出现"需人工但按钮灰"死锁观感。
3. 爬取失败的链接在最终表中无声消失，审核侧不可见。

## 方案总览

派发 pass（scripts/customer_prewarm_dispatch.py → run_prewarm_dispatch_pass）在现有 Phase 1-4 后追加生命周期阶段（受同一 budget/deadline 约束），另加最终表失败行投影。三件套：

### A1 收割（harvest）

- 对象：当日 state DB 中 `sku_id LIKE 'zhpq-%'` 绑定、`remote_jobs.state ∈ {SUBMITTED, RUNNING, EXPORTED}` 且有 remote_identity 的 job。
- 动作：逐个调用既有原语 `app.services.recovery.harvest.harvest_existing_customer_prewarm_job`（ProcessLock+lease、无 create 通道、有界 wait/export、source_scope=prewarm_payload）。
- 语义：白天与 tick 冗余但幂等无害；冻结后/无人认领时由本阶段补位。REMOTE_EXISTING_HARVEST_RUNNING 视为正常在途，下轮再收。
- 结果落缓存仍走既有链（scan 每分钟镜像 SUCCESS 行 → 下轮派发 Phase 1 吸收）。

### A2 对账（reconcile SUBMIT_UNKNOWN）

- 对象：当日 state DB 中 `state='SUBMIT_UNKNOWN'` 且无 identity 的预爬 job。
- 动作：`_reconstruct_prepared_job`（payload 证据重建）→ `adapter.reconcile_unknown(prepared_job)`：
  - FOUND → `store.reconcile_remote_job(remote_identity, state='PRIORITY_PENDING')` + `advance_crawl_work_for_job` + 一次 prioritize 确认（复用 `_confirm_pending_priority_job`，PROVEN 才 SUBMITTED）。
  - NOT_FOUND/MANUAL → `remote_jobs.recovery_attempts` +1（该列已存在）；累计 ≥4 判死（不再对账，转 A3 重排）。tick 若已把 job 推 MANUAL_REQUIRED，同样按死处理。

### A3 重排（re-dispatch dead jobs）

- 对象：候选在当日 scope 内（含跨日候选，参照 manual-recrawl 跨日语义）、cache_status ∈ {REMOTE_DISPATCHED, REMOTE_REUSED}、其 remote_job_id 对应 job 已死（MANUAL_REQUIRED / SUBMIT_UNKNOWN 且 recovery_attempts≥4 / TERMINAL_FAILED）。
- 动作：按商城分组走 `_dispatch_mall_group` 同款创建链（ensure_crawl_work 幂等 + link_crawl_work_batch_to_remote_job 重绑爬取行到新 job + claim → submit → 提优确认）。新 job 记 linkage（producer 沿用 customer-prewarm-zhaoshang）。
- 熔断：每候选每日重排上限 2（`prewarm_remote_job_links` 按 candidate 当日计数）；超限保留死绑并在候选 last_error 记 "redispatch budget exhausted"。重排计数进 summary。

### A4 挂点（ofs/ycw 脚本兜底，本期不实现）

- A3 判死/失败的 ofs、ycw 候选集合即未来兜底通道的输入（设计已评审：临时 tsv → 子进程调 crawl_ofs/ycw_templates.py → 解析 8 列 xlsx → producer=fallback-script-{ofs,ycw} 写共享缓存 → 页面加"脚本兜底"标签与 KPI）。
- 等用户确认脚本定稿后另开任务实现；本设计只保证 A3 输出该集合的查询接口。

### B 失败数据进最终表（原生行格式）

- 时机：closeout 冻结/最终表生成（app/workflows/incremental_price_audit_finalize.py 的结果合并处）。
- 内容：对当日范围内爬取失败的客户链接（死 job 绑定 / NO_RESULT / TERMINAL_FAILED / 重排超限），按最终表既有行格式（与成功爬取结果同列结构）追加行，结果列写"爬取失败"，并带失败原因（提交超时无身份/远端无价/终端失败/重排超限）与链接、商城信息。
- 业务行口径不变（无价留空）；失败行是追加投影，不改动既有行的任何字段。
- 数据源只读：prewarm store + 当日 state DB。

## 测试策略

- A1-A3 单测：fake adapter/store（沿用现有 dispatch 测试的注入风格），覆盖 FOUND/NOT_FOUND/预算耗尽/重排上限/跨日候选/幂等重入。
- B 单测：冻结产物包含失败行、列结构与成功行一致、既有行零改动、空失败集零追加。
- 回归：prewarm 相关全量测试 + 既有 dispatch/canary 套件；生产 smoke 用 --plan-only 先行。

## 非目标

- 不改主看板按钮谓词（身份缺失的灰钮是诚实语义）。
- 不实现 ofs/ycw 脚本调用（脚本修改中，等定稿）。
- 不处理跨日 SUBMIT_UNKNOWN 的历史僵尸清理（A3 重排自然覆盖当日 scope；历史存量另行处理）。

## 2026-09-18 PR #26 扩展（覆盖原始 A2/A3 的跨日与 MANUAL 语义）

- 最新 durable candidate linkage 定位 `(owning_day, job_id)`：跨日无身份 `SUBMIT_UNKNOWN` 只做 exact reconcile；`PRIORITY_PENDING` 跨轮只做提优证明，`weight_type=-1` 才原子进入 `SUBMITTED`，不重发 submit/create。远程提优前检查 linkage，结果写回用短事务 fence；绝不在远程 I/O 期间持有 prewarm SQLite 写事务。
- 跨日 `SUBMITTED`/`RUNNING`/`EXPORTED` 由独立 historical harvest 阶段调用既有 prewarm recovery 原语，自己取得所属日 ProcessLock+lease；terminal SUCCESS 可重试 exact-job cache sync，避免 harvest 完成后进程中断造成缓存缺口。两个跨日阶段各有 durable fairness cursor、cap 和共享 deadline。
- adapter 的 `MANUAL` 或不完整 FOUND 是 provenance blocker：持久化 `MANUAL_REQUIRED` 与分类诊断，A3 排除；任何已 POST 且无 identity 的 MANUAL_REQUIRED 也不自动重建。只有无诊断的确切 NOT_FOUND 消耗有限 attempt；不确定 NOT_FOUND 保持待重试。
