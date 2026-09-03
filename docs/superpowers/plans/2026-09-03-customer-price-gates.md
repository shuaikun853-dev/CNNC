# Plan: 客户价数据正确性修复 (customer-price gates) — 2026-09-03

BASE_PROD_SHA: e4bbd3f89bd64e633bc8fe9c32c45a0c9cf85462 (PRODUCTION_MOVED: NO)
DEV BRANCH: fix/customer-price-gates-20260903
WORKTREE: E:\.A中核运维\cnnc-ops-agent_customerprice_fix

## 1. 已确认根因（Phase 1 实证）

RC-1 Detail 业务失败被记成 stage SUCCESS（永不重试）
- `app/services/incremental_audit_runtime.py::_detail_worker` HTTP 成功后直接
  `return extract_customer_price_from_detail(payload)`；`NOT_FOUND /
  DETAIL_REQUEST_FAILED / DETAIL_PARSE_FAILED`（customerPrice=None）正常返回。
- `app/workflows/incremental_price_audit.py::_stage` 无差别
  `complete_stage_work` → SUCCESS。
- 生产实锤（只读探针 runtime/incremental_price_audit）：2026-09-01 已 FINALIZED
  的最终表里 304 行中 100 行 SUCCESS+DETAIL_REQUEST_FAILED+noprice、5 行
  LOOKUP_FAILED；随表出货且可被自动 APPROVE。

RC-2 自动 APPROVE 不要求客户价有效
- `app/workflows/work_01_saas_approval_json_audit.py::_decide_action`：
  `price_vs_customer_check` 只用于 REJECT 分支；customerPrice=None →
  CUSTOMER_PRICE_MISSING，SAME+SKU MATCH+price MATCH 仍 APPROVE。

RC-3 finalize readiness 是技术口径不是业务口径
- `FinalizeReadiness.ready` 不含 `detail_terminal`；
  `build_frozen_audit_artifact` 对 SUCCESS 直接解码 result_json，
  customerPrice=None 的业务失败完全穿透到 `build_records`/audit。

RC-4 Dashboard 只有 Detail 技术计数
- `dashboard_projection._stages` 只有 `("Detail", f"{d.completed}/{uniq}")`；
  `_attention_items` 的 "Detail 有终态失败/不阻塞出表/客户价留空继续收口"
  文案与新门禁矛盾；无客户价任何可观测性。

## 2. 生产证据驱动的关键设计决策

- LOOKUP_FAILED / DETAIL_REQUEST_SKIPPED 是"当日权威客户导出中无此单据"的
  确定性缺席（重试无意义；9-01、9-02 每天 5 行）→ 归类
  MISSING_PROVEN：允许 finalize 收口（行级 MANUAL，证据随表），绝不自动 APPROVE。
- NOT_FOUND / DETAIL_REQUEST_FAILED / DETAIL_PARSE_FAILED 是可重试业务失败
  → 进入 durable retry（detail_max_attempts=3），预算耗尽后：
  NOT_FOUND → MISSING（阻塞）；REQUEST/PARSE → FAILED（阻塞）。
  两者都阻塞 ordinary finalize 且进 force-finalize NON_TOLERABLE。
  依据：任务书验收 D-2/D-3/D-6 与第 17 节红色 blocker 文案；生产 6 天样本中
  非故障日 NOT_FOUND 为 0，阻塞风险可控，重试大概率恢复（9-01 是认证故障窗口）。
- customer_price_required(row) = True（集中谓词，单一来源）。本管线每行都要
  以客户价为价格审核基准；"合法无价"仅指 MISSING_PROVEN（证据齐全的确定性缺席）。

## 3. 统一语义模块（新增） app/services/customer_price_semantics.py

纯模块（无 HTTP/DB/IO），全仓唯一权威，workflow/finalize/status/dashboard 复用：

- `CUSTOMER_PRICE_FOUND_STATUSES = {"FOUND","FOUND_SINGLE_REFERENCE","EXISTS"}`
- `CUSTOMER_PRICE_RETRYABLE_DETAIL_STATUSES = {"NOT_FOUND","DETAIL_REQUEST_FAILED","DETAIL_PARSE_FAILED"}`
- `CUSTOMER_PRICE_PROVEN_ABSENT_STATUSES = {"LOOKUP_FAILED","DETAIL_REQUEST_SKIPPED","NOT_PROVIDED"}`
- `DetailBusinessRetryRequired(RuntimeError)`: `result` 属性（完整五字段 dict）；
  `str()` = `customerPriceStatus=<STATUS>；<warning>`（结构化前缀，可解码、已脱敏）。
- `encode_business_error(status, warning) -> str` / `decode_business_error(text) -> (status, warning)`
- `sanitize_customer_price_text(text) -> str`: 剥离 cookie/token/authorization 等敏感模式
- `customer_price_business_state(detail_state, result, error_text) -> CustomerPriceBusinessState`
  - dataclass: `key, status, detail_state, blocking, wait_class`
  - key ∈ READY / PENDING / RETRYING / MISSING / MISSING_PROVEN / FAILED / INVALID
  - 规则表（权威）：
    | detail state | result/error | key | blocking |
    |---|---|---|---|
    | PENDING/CLAIMED | - | PENDING | True |
    | RETRYABLE_FAILED | - | RETRYING | True |
    | TERMINAL_FAILED | error 前缀 NOT_FOUND | MISSING | True |
    | TERMINAL_FAILED | error 前缀 REQUEST/PARSE 失败或无前缀 | FAILED | True |
    | SUCCESS | price>0（任意 status）| READY | False |
    | SUCCESS | status∈PROVEN_ABSENT | MISSING_PROVEN | False |
    | SUCCESS | status∈RETRYABLE（旧兼容 invalid-success）| FAILED | True |
    | SUCCESS | price<=0 / 不可解析 | INVALID | True |
    | （无 stage 行） | - | PENDING | True |
- `customer_price_required(row) -> True`（集中谓词 + docstring 说明）
- `DETAIL_MAX_ATTEMPTS_DEFAULT = 3`（与 TickConfig.detail_max_attempts 一致，仅展示用）

## 4. Detail 重试（改 runtime + workflow + store）

- `_detail_worker`: `extract_customer_price_from_detail` 返回后，status∈
  RETRYABLE 集合 → `raise DetailBusinessRetryRequired(result)`；
  EXISTS/LOOKUP_FAILED/DETAIL_REQUEST_SKIPPED 保持正常返回（确定性，不重试）。
- `incremental_price_audit.py`：detail `_stage` 调用新增
  `retry_classifier=_is_retryable_detail_failure`：
  DetailBusinessRetryRequired/瞬时传输/认证拒绝 → retryable；
  RuntimeConfigurationError → 不 retry（快速终态）。不加 backoff（tick 间距 15min，
  attempts 上限 3，无 busy-loop 风险）。
- 旧 durable state 兼容（不篡改历史、幂等、有界）：
  tick 在 detail 循环前（仅当 deps.detail 不为 None）调用新 store 方法
  `requeue_invalid_business_success_details(token)`：扫描 stage=DETAIL 且
  state=SUCCESS 且 result_json.status∈RETRYABLE 集合的行 →
  state=PENDING, attempts=0, elapsed_seconds=0, retryable=1,
  error_text='requeued invalid business success: customerPriceStatus=X',
  result_json=NULL。每行至多一次（新代码下这些 status 不可能再产出 SUCCESS）；
  证据保留在 error_text。counter: detail_business_requeued。
- `_backlog` 增 additive 计数 customer_price_missing / customer_price_failed；
  tick outcome：`detail_terminal` 并入 MANUAL_REVIEW_REQUIRED 条件。

## 5. APPROVE P0 门禁（work_01）

- `_decide_action` APPROVE 分支新增必要条件
  `price_vs_customer_check == "WITHIN_THRESHOLD"`（蕴含客户价有效>0）。
- `_reason` 增分支：CUSTOMER_PRICE_MISSING/INVALID →
  "缺少有效客户价，价格审核基准不完整，转人工，不予自动通过"。
- 同款判断/客户价判断边界不变：不动 title/model 提示词与证据链。

## 6. Finalize 门禁（incremental_price_audit_finalize.py）

- `FinalizeReadiness` 新增 additive 字段：customer_price_pending /
  customer_price_missing / customer_price_failed / customer_price_invalid /
  customer_price_absent_proven（仅信息展示，不阻塞）。
- `ready` 增加：customer_price_missing/failed/invalid（pending 已由 detail_pending
  覆盖，同时用独立计数器显式化）。
- `NON_TOLERABLE_BLOCKERS` 增加：customer_price_missing / customer_price_failed /
  customer_price_invalid（force-finalize 不可绕过）；customer_price_pending 与
  detail_pending 同源不重复添加。
- 分类调用 semantics 模块（对每行 detail stage + result_json + error_text）；
  `_detail_failure` 升级：用 decode_business_error 解析 error_text 前缀，
  终态行保留真实 customerPriceStatus（NOT_FOUND 不再被降级为 REQUEST_FAILED）。
- review_mode 现有 gate（detail_pending/terminal 阻断 review 导出）保持。

## 7. Status 投影（incremental_audit_status.py，additive）

- `detail` dict 新增（旧键全部保留）：customer_price_required /
  customer_price_ready / customer_price_pending / customer_price_retrying /
  customer_price_missing / customer_price_failed / customer_price_invalid /
  customer_price_absent_proven / customer_price_ratio。
  需要 stage_work.result_json + error_text 参与分类（扩展 _stage_status 或新增
  只读函数，复用 semantics）。
- `computed_final_pending` 增加 detail.terminal；blocker_summary 新增
  detail_terminal 与 customer_price_* 键（additive）。

## 8. Dashboard（projection + API + UI）

- `_stages` prep sub 增加 ("客户价", f"{ready}/{required}")。
- attention 重写 detail 相关三条（重试中=黄"自动重试"；重试耗尽=红 blocker 文案
  按任务书第 17 节；MISSING_PROVEN=黄"客户价缺失(证据齐全,行级转人工,不阻塞)")。
- `_final_text` blocker mapping 增加 customer_price_* 条目。
- 新 `project_customer_price(state_path, business_date)` 只读行级投影：
  documentId/vCommodityId、标题(queryStr/skuName)、skuId、SaaS 价(price)、
  客户价、customerPriceStatus、customerPriceSource、detail state、attempts、
  max_attempts(=3)、retryable、error_text(经 sanitize)、updated_at。
  全部文本经 esc() 渲染；不输出任何 header/cookie/token。
- `build_dashboard_summary` 纳入 customer_price 汇总块（fingerprint 自动覆盖）。
- `dashboard_api.py` 新增 GET `/dashboard/api/customer-price?date=&filter=`
  （filter ∈ all/unacquired/retrying/terminal；白名单参数；只读；零写；零网络）。
- 前端：数据准备卡"客户价 n/N"行 + 点击打开客户价 Drawer（表格 + 过滤按钮
  只看未获取/待重试/终态失败）；app.js 沿用 esc()/Drawer/poll 既有模式；
  不新增独立轮询链（打开时拉取 + summary 渲染时若 Drawer 开着刷新）。

## 9. 测试矩阵（新增+翻转）

A 解析（已有 tests/test_saas_audit_core.py 保持，新增 alternate paths 若缺）
B Detail retry：timeout/500/429→RETRYABLE；401→refresh+立即重试（已有）；
  NOT_FOUND 第 1/2 次→RETRYABLE_FAILED、第 3 次→TERMINAL(MISSING)；
  REQUEST_FAILED 同理→TERMINAL(FAILED)；RuntimeConfigurationError→不重试；
  跨 tick 重试；进程重启后 durable 状态正确；兼容 requeue 幂等（SUCCESS+FOUND
  不被 requeue；SUCCESS+NOT_FOUND 被 requeue 一次且证据在 error_text）。
C APPROVE：SAME+MATCH+customerPrice=None → 绝不 APPROVE（MANUAL）；
  customerPrice 有效 + WITHIN_THRESHOLD → 其余条件满足仍 APPROVE；
  CUSTOMER_PRICE_MISSING/INVALID/0/-1 → 不 APPROVE。
D Finalize：required+retry pending→blocked；required+terminal missing(NOT_FOUND
  耗尽)→blocked；required+SUCCESS 但 price None(旧态)→blocked；required+有效价
  →allowed；MISSING_PROVEN(LOOKUP_FAILED/SKIPPED)不被误阻塞；force-finalize 对
  customer_price_missing/failed/invalid 拒绝（FORCE_FINALIZE_BLOCKED）、对
  MISSING_PROVEN 不拒绝。
E Dashboard：summary 数量、attempts 展示、filter、XSS 转义、GET 零写、
  fingerprint 随客户价状态变化、隐藏/恢复轮询机制不破坏（现有
  tests/js/run_dashboard_js_tests.js 需继续通过）。
翻转旧语义测试（有据更新，不是放松）：
- tests/test_incremental_price_audit.py:303 detail terminal outcome 断言
- tests/test_incremental_price_audit_finalize.py:391/:413 detail terminal 可出表断言
- 其它因 computed_pending/blocker_summary/summary 计数变化而红的投影断言

## 10. 约束

- 不改 DB schema；不做 migration；不动生产分支；不 merge/deploy。
- 并行设计不变（Detail 循环在 tick 内逐行、Model 并发、招商局链路不动）。
- 不把客户价塞进同款判断提示词。
- 最终 Excel 列契约不变（客户价五列已存在）。
