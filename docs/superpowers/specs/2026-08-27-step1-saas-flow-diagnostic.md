# STEP_1 Dashboard SaaS Flow Diagnostic — Design Contract

- Date: 2026-08-27
- Base branch: `origin/prod/closeout-20260818`
- Base SHA: `13b425eac95b290f0c554a763e9c49502c710c46`
- Development branch: `feat/step1-saas-flow-diagnostic-20260827`
- Scope: CNNC only, `tenant_id=188605`, `commodity_pool_id=222`
- Status: implemented on `feat/step1-saas-flow-diagnostic-20260827` (2026-08-27; investigation confirmed authoritative counts persist in `state.sqlite3` `snapshots.evidence_json` with `observed_at`, so growth detection reads existing durable evidence and the STEP_1 state machine is untouched)

## 1. Goal

Add a read-only SaaS first-crawl diagnostic module to the existing STEP_1 Dashboard so that, before 18:00, when customer-side data continues to increase but the SaaS page count does not increase correspondingly, the page can identify the exact pipeline stage where data is currently waiting or stuck.

This feature is diagnostic only. It must never perform approval, reject, retry, task creation, priority adjustment, `UPDATE`, `INSERT`, `DELETE`, or any other business write.

## 2. Very narrow SQL exception

This feature introduces one explicit exception to the existing “no production SQL” rule:

> `STEP_1 Dashboard SaaS Flow Diagnostic` may connect with a designated read-only database account and execute only predefined, parameterized read queries required by this diagnostic module. The scope is permanently fixed to `tenant_id=188605` and `commodity_pool_id=222`. Arbitrary SQL, write SQL, browser-supplied SQL, and user-editable table/database names are forbidden.

Hard requirements:

1. Only backend-owned predefined `SELECT`/read-only CTE queries are allowed.
2. The browser/API must not accept SQL text.
3. The browser/API must not accept arbitrary tenant, pool, schema, or table names.
4. Tenant and pool are fixed in backend constants: `188605` and `222`.
5. No DML/DDL: `UPDATE`, `DELETE`, `INSERT`, `REPLACE`, `MERGE`, `TRUNCATE`, `DROP`, `ALTER`, `CREATE` are forbidden.
6. No database credentials may be committed to Git. Real host/user/password values live only in local ignored configuration. `.env.example` may contain variable names and placeholders only.
7. Database failure must degrade only this diagnostic panel; it must not break the existing runtime dashboard.
8. The existing STEP_1 business workflow/state machine must not be modified by this feature.

## 3. Confirmed diagnostic chain

This chain is developer-confirmed and has been cross-checked against live production SQL results.

### 3.1 Top-level status

`saas_onecode_price.commodity_pool_188605.IS_SYNC_STANDARD = 0`

Meaning: the commodity has not reached final closure yet. It does **not** by itself mean “not shown on SaaS page”, “crawler still running”, or “system error”.

### 3.2 First branch: `commodity_list_table_188605.crawl_status`

For commodities still in process, first inspect:

`saas_compare_price.commodity_list_table_188605.crawl_status`

- `crawl_status = 1`: crawler callback has not completed yet.
- `crawl_status = 2`: crawler callback has completed; continue diagnosis in the downstream search/model/approval flow.

Observed live cross-check on 2026-08-27:

- `crawl_status=1`: 7 commodities, all 7 still had running task rows, 0 had search results.
- `crawl_status=2`: the large majority had already entered search/model processing.

### 3.3 Branch A: `crawl_status = 1`

Only for this branch, continue downward through crawler infrastructure:

1. `task_table_188605`
2. extract exact batch from `task_id`
3. `central_control_sub_demo.s_task_info`
4. crawler / callback evidence as needed

Required classifications:

- **TASK_NOT_CREATED**: no matching task row exists.
- **TASK_CREATED_NO_CENTRAL_RECORD**: SaaS task exists, but exact batch has no `s_task_info` record.
- **CENTRAL_NOT_ENQUEUED**: exact child tasks exist with `task_status='00'`, `queue_status='00'`, `enqueue_time IS NULL`.
- **CENTRAL_ENQUEUED_WAITING**: queue lifecycle shows enqueued but not completed, e.g. `00/01`.
- **CRAWLER_EXECUTING**: execution state such as `01/02`.
- **CRAWLER_FAILED**: central/crawler failure state, e.g. `03/*`, with evidence shown.
- **CALLBACK_LAG**: crawl execution has completed but commodity-level `crawl_status` is still 1. This must be evidence-based; do not infer solely from one status column.

Important live-data corrections that must be preserved:

- `start_time` alone is not proof that a child task actually started.
- `enqueue_time + task_status + queue_status` is better evidence for queue/execution lifecycle.
- `task_status='02'` with `end_time IS NOT NULL` can represent completed execution even when `queue_status` is still `01`; therefore `02/02` must not be the only success condition.
- `task_status=1` in `task_table` must not override commodity-level `crawl_status=2`; commodity-level callback diagnosis starts with `crawl_status`.

### 3.4 Branch B: `crawl_status = 2`

Do not continue debugging central control by default. Diagnose downstream:

1. `search_table_188605`
2. `audit_result`
3. `model_audit_result`
4. `sync_state`
5. manual approval / final closure

Required classifications:

- **CALLBACK_DONE_NO_SEARCH**: callback complete but no search row yet. Treat as transient until it exceeds a configured grace window.
- **MODEL_PENDING**: search exists but model processing is still pending/processing according to verified status values.
- **INTERNET_SAME_PENDING_APPROVAL**: internet same-product result exists and is currently waiting for manual approval.
- **REFERENCE_SAME_ONLY**: reference-link same-product exists but no internet same-product exists.
- **INTERNET_CANDIDATES_ALL_NON_SAME**: internet candidates exist but all were judged non-same by the model.
- **NO_INTERNET_CANDIDATE**: no internet candidate exists for the commodity.
- **MODEL_DONE_NO_SAME**: model completed and no same-product result exists.
- **APPROVAL_OR_FINAL_CLOSURE_PENDING**: manual approval/result exists but `IS_SYNC_STANDARD` has not reached final closure.

Do not describe `sync_state=1` as a background handler/stream backlog. In the current SaaS page semantics it represents the manual approval state for the corresponding result set.

## 4. Before-18:00 growth-stall validation

### 4.1 Authoritative count sources

Reuse the existing `CnncSaasSnapshotAdapter` authoritative discovery instead of inventing new count semantics. It already returns both:

- `customer_count`
- `saas_count`

for the fixed CNNC/SaaS scope and validates the expected endpoints and scope.

The Dashboard diagnostic module should consume persisted/current authoritative snapshot evidence where possible rather than issuing duplicate high-frequency business API requests.

### 4.2 Detection rule

Only evaluate the growth-stall rule before 18:00 Asia/Shanghai.

For each pair of consecutive successful authoritative observations within the same business date:

- `customer_delta = current.customer_count - previous.customer_count`
- `saas_delta = current.saas_count - previous.saas_count`

States:

- **NORMAL**: no evidence of customer growth without SaaS growth.
- **LAG_WATCH**: `customer_delta > 0` and `saas_delta <= 0` for one successful interval.
- **LAG_PERSISTING**: the above condition is observed in at least two consecutive successful intervals.

When `LAG_WATCH` or `LAG_PERSISTING` is reached, the Dashboard must run/show the stage diagnosis from §3 so the user sees where the missing growth currently resides.

This check must not claim that SaaS is broken merely because counts differ. One customer commodity can yield zero, one, or multiple SaaS same-product rows. The purpose of the count comparison is only to detect a **growth divergence signal**, then explain the pipeline distribution with SQL.

### 4.3 Required UI wording

Example:

> 客户侧仍在新增，但 SaaS 数量本轮未增长。当前新增链路正在分布于以下阶段：爬虫链路未完成 X、已回调待模型 X、互联网候选全部判非同款 X、待人工审批 X……

Do not use wording such as “SaaS 丢数据” unless the evidence proves an actual missing handoff/record.

## 5. Dashboard architecture

Do not add production DB queries to the existing 5-second `/dashboard/api/summary` polling path.

Add an independent read-only endpoint, for example:

`GET /dashboard/api/saas-flow?date=YYYY-MM-DD`

Requirements:

- independent failure boundary from `/dashboard/api/summary`
- backend cache, recommended 60 seconds minimum
- manual refresh may request a fresh diagnostic, but rate-limit repeated refreshes
- no browser-provided SQL
- no write endpoints
- no direct coupling to finalize/closeout

Suggested backend module:

`app/services/saas_flow_diagnostic.py`

Responsibilities:

- read-only DB connection
- fixed-scope query functions
- query aggregation and classification
- no UI formatting side effects
- no business writes

## 6. Query-performance contract

The implementation must avoid the slow query patterns observed during manual incident diagnosis.

Required:

1. Scope target commodities first by tenant/pool/business date.
2. Prefer indexed `EXISTS` / `NOT EXISTS` and pre-aggregation.
3. Do not directly join large `task_table` and `search_table` one-to-many result sets and then rely on `COUNT(DISTINCT)` over the exploded cross product.
4. Only query `s_task_info` for exact affected batches after `crawl_status=1` identifies the target subset.
5. Use exact batch IDs / bounded `IN` sets where practical.
6. Apply client-side connection/read timeouts.
7. Cache diagnostic results; no 5-second production DB polling.
8. A slow/degraded subquery must return an explicit `diagnostic_unavailable`/partial-evidence state rather than blocking the whole Dashboard.

## 7. Security and credential handling

Configuration names only, no values in Git:

```dotenv
CNNC_DIAG_DB_HOST=<readonly_db_host>
CNNC_DIAG_DB_PORT=3306
CNNC_DIAG_DB_USER=<readonly_db_user>
CNNC_DIAG_DB_PASSWORD=<readonly_db_password>
```

The implementation must assume credentials are sensitive even if the username is read-only.

Unit tests must verify:

- every registered diagnostic statement is read-only
- no SQL can be supplied through request parameters
- tenant/pool cannot be changed through request parameters
- no DML/DDL keyword is present in executable diagnostic statements
- DB credentials are never included in API responses/logs
- DB failure does not break existing Dashboard summary

## 8. UI placement

Keep the existing first-screen information budget intact.

Add the SaaS diagnostic as a secondary/progressive-disclosure block, e.g. “SaaS 首次采集链路”. The collapsed summary should show only:

- customer count
- SaaS count
- divergence state (`正常` / `增长落后观察` / `持续增长落后`)
- the dominant waiting stage

Expanded view shows stage counts and evidence details.

Do not put raw credentials or arbitrary SQL on the page. Raw SQL text is not required for normal users; technical details may show query names and evidence counts only.

## 9. Acceptance criteria

Implementation is not mergeable until all are true:

1. Branch remains based on production SHA `13b425eac95b290f0c554a763e9c49502c710c46` unless production moves and the user explicitly authorizes rebasing/syncing.
2. Existing Dashboard behavior remains unchanged when DB diagnostic configuration is absent.
3. Feature operates with read-only DB credentials only.
4. Before 18:00, customer-growth/SaaS-flat divergence is detected from authoritative observations.
5. On divergence, stage distribution identifies at least:
   - crawl callback pending
   - task generation/handoff/central enqueue/execution/callback subclasses where applicable
   - callback done/no search
   - model pending
   - internet same pending approval
   - internet candidates all non-same
   - no internet candidate
   - model done no same
   - final closure pending
6. `crawl_status=1` and `crawl_status=2` route into different diagnostic branches as defined in §3.
7. No production DB query is executed every 5 seconds.
8. No write SQL or arbitrary SQL execution path exists.
9. Query timeout/failure produces a degraded diagnostic state, not a Dashboard outage.
10. Unit/integration tests cover security scope, state classification, growth-stall detection, caching, and degradation.
11. Fresh full test suite passes before review.
12. No merge, deployment, scheduled-task change, or production configuration change without explicit user approval.

## Round 2 Addendum — Central Bypass(2026-08-28)

crawl_status=1 主诊断链由 s_task_info central 查询替换为已生产实证的 callback 证据链:
task_table.task_id → _batch_from_task_id → shopping_spider.spider_task_record(batch_num 索引) → exe_number → spider_sync_log/_complated(execution_number 索引)。
central 状态类(TASK_CREATED_NO_CENTRAL_RECORD 等 6 项)退役;新状态:TASK_NOT_CREATED / PUSH_RECORD_NOT_FOUND / PUSH_CONFIRMED_NO_CALLBACK_RECORD / CALLBACK_IN_FLIGHT / CALLBACK_PARTIAL / CALLBACK_DONE_SAAS_PENDING / EVIDENCE_INCOMPLETE。
已实证语义:completed push_state=0000=回调成功终态、active 8888=在途、spider_task_record.is_call 0/1 对应未回调/已回调;其余 push_state 未验证 → 证据不足,不设 CALLBACK_FAILED;PENDING_URL_SYNC 无出处,禁用。
s_task_info 不再出现在 DIAG_SQL_REGISTRY;central_status_detail 保留 key 恒 None。
