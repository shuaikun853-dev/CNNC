# STEP_1 Remote RUNNING Stall Recovery — Design (Root Cause + Solution)

**Date:** 2026-08-25
**BASE_SHA:** 6efdc79c5bb10605f05f2931066a833f986952e5 (origin/prod/closeout-20260818)
**Scope:** CNNC STEP_1 增量调度对招商局 remote RUNNING stalled job 的自动恢复状态机。仅此一个 Bug。

## 1. Root Cause Statement

普通 `RUNNING` remote job 只有两条退出路径：

1. harvest 探测到远端完成并导出（`EXPORTED/COMPLETED`）；
2. `harvest_attempts >= 120` 或累计 callback 活跃时间 `harvest_elapsed_seconds >= 21600` 时由
   `claim_harvest`/`record_harvest` 转 `MANUAL_REQUIRED`。

白天 sweep 每 15 分钟一次、单次 callback 活跃约 30 秒：120 次 attempts 当天不可达（需 ~30 小时），
elapsed 只累计 callback 活跃时间更不可达。因此 Job 53 型真 stall（progress 完全静止 + export 持续
count=0）在当天表现为**无限 harvest**：linked crawl 永远 pending，ordinary final 的
`remote_pending` 永不清零。系统同时缺少三样东西：

- **progress 证据持久化** —— `remote_jobs` 无任何进度字段，`updated_at` 被 harvest 自身刷新，无法区分 slow 与 stalled；
- **自动恢复** —— 生产已证明「精确禁用→精确启用」可救活 Job 53（07:02 创建，09:45 人工重启，09:50 COMPLETED），
  但 CNNC 侧没有任何 disable/enable 调用链；
- **恢复失败后的本地终态** —— 没有「已放弃等待、不再爬」的合法 durable state，final 无法收敛。

## 2. 生产证据画像（2026-08-25，只读 SQLite）

| 样本 | wall-clock | attempts | 结局 | 结论 |
|---|---|---|---|---|
| 91/93 个 COMPLETED | ≤30 min | ≤3 | COMPLETED | 正常短 RUNNING（Job 88 同类：25 min 时仍 RUNNING 属正常） |
| Job 20 | 130 min | 10 | COMPLETED | 慢但 progress 推进 → **不能按时间判 stalled** |
| Job 53 (lxwl) | 168 min | 16 | 人工禁用→启用后 5 min COMPLETED | progress 静止 + export `count=0`/「未检索到商品价格信息」→ 真 stall，重启有效 |
| Job 88 (colipu) | — | 13 | MANUAL_REQUIRED | 科力普爬虫问题，本轮禁改 |

外部工具 `E:\.A2026\招商局自动建任务\external_tools\crawl_task_tool\auto_crawl_task.py` 已有且生产在用：

- `check_existing_task(session, task_name, mall_code)` — 精确唯一 lookup（多条/商城不符即 raise）；
- `get_task_identity(task, expected_mall_code)` — taskId/taskNumber/mallLogo 严格校验；
- `disable_task(session, task_id)` / `enable_task(session, task_id)` — `crawlTaskManage/updateState` state=0/1；
- `query_task_status` / `query_task_progress` / `parse_task_progress_desc` — progress 字段
  `{total, pending, running, failed, waiting_db, finished}`（progress 按 batchNumber=display task name 查询）。

**结论：CNNC 只需在 Adapter 层调用外部工具既有能力，不修改外部工具。**

## 3. 判定设计（fail-closed）

`progress_signature` = 对 `parse_task_progress_desc` 六个业务字段按固定键序 canonicalize 的 tuple。
**不用** `remote_jobs.updated_at` 判进度。

**Probe 候选**（才开始花只读远程调用）：`state==RUNNING` + remote_identity 合法 + `harvest_attempts>=2` + 距 `created_at>=30min`。

**STALL = 全部满足**：

1. `state == RUNNING`
2. exact remote_identity `mall|task_id|task_number` 合法
3. `harvest_attempts >= 8`
4. 距 `created_at >= 90 min`（wall-clock，来自 durable 时间戳）
5. 尾部连续 ≥4 条 progress observation 的 signature 完全相同，且首尾跨度 ≥60 min
6. 这些 observation 的 export 证据全部为 `count==0` 且 rspDesc==「未检索到商品价格信息」
7. 无 SUBMIT_UNKNOWN / MANUAL_REQUIRED / COMPLETED（state==RUNNING 已保证）
8. `recovery_attempts == 0`

**阈值依据**：Job 88（25min/6attempts）被 3+4 拦下；Job 20（130min/10attempts）被 5 拦下（progress 推进）；
Job 53（168min/16attempts/静止/count=0）全中。任何字段缺失/未知 → 不判（缺证据≠stalled）。

**向后兼容**：旧 durable job 无新 evidence 字段 → 「尚未证明 stalled」→ 正常 harvest。

## 4. Restart 协议（对精确原任务，最多一次）

phase 状态机持久化在 `remote_jobs.recovery_evidence_json`（写前持久化，进程重启可续）：

`NOT_STARTED → DISABLE_SENT → DISABLE_CONFIRMED → ENABLE_SENT → ENABLE_CONFIRMED`

- 任何写前重新精确 reconcile（STEP1 精确查询 + STEP2 确认仍 eligible）；
- disable/enable 响应 unknown → **不盲重发**，先重新查询真实状态再决定；phase 已 SENT 的方向绝不重发；
- DISABLE_SENT 后重查仍 enabled 且超 15min 确认窗 → fail-closed MANUAL_REQUIRED（诊断入 evidence）；
- ENABLE_CONFIRMED → `recovery_attempts=1`，job 回 RUNNING，交回正常 harvest；
- 同一 job 第二次 restart 永久禁止（recovery_attempts==1 或 phase 已 SENT）。

## 5. RECOVERY_EXHAUSTED（新本地终态）

进入条件（全部）：`recovery_attempts==1` + phase==ENABLE_CONFIRMED + 重启后 ≥2 条新 observation
（observed_at > recovery_at）signature 相同 + export 仍 count=0/no-price + 距 recovery_at ≥30min。

动作：`remote_jobs.state='RECOVERY_EXHAUSTED'`（本地放弃语义，非远端 COMPLETED，不伪造）；
linked crawl → `NO_RESULT`，evidence_type=`REMOTE_STALL_RECOVERY_EXHAUSTED`，保留
remote_identity / recovery 时间 / disable+enable 确认 / 末次 progress signature / export count=0 /
rspDesc 完整审计链。**不调用也不削弱** strict NO_RESULT resolver（`EXPORT_NO_PRICE_TERMINAL` /
`EXPORTED_NO_MATCHING_ROW` 语义原样保留）。

Finalize：RECOVERY_EXHAUSTED + 完整 evidence（post_attempted + remote_identity + recovery_attempts==1
+ phase==ENABLE_CONFIRMED）→ 可接受 terminal no-result（ordinary final 可收敛）；evidence 不完整 →
`remote_invalid` blocker（fail-closed）。`_HARVESTABLE_REMOTE_STATES` 不含它 → 不再 harvest。
`SUBMIT_UNKNOWN`/`MANUAL_REQUIRED`/普通 `RUNNING` 的 blocker 语义不变；force-finalize ≤10 规则不动。
RECOVERY_EXHAUSTED 不做第二次远端 disable（无生产安全证据，Remaining Risk 记录）。

## 6. Durable schema（最小增量，沿用既有 ALTER TABLE 惯例）

```
remote_jobs.recovery_attempts      INTEGER NOT NULL DEFAULT 0
remote_jobs.recovery_evidence_json TEXT      -- 结构化 JSON：observations/restart/phases
```

不污染 `result_json`/`error_text` 原语义；旧库 ALTER TABLE 兼容，旧行 NULL/0 = 未证明 stalled。

## 7. 挂点与门控

- recovery pass 挂在 `run_incremental_tick` harvest 之后 → tick/finalize/closeout 三路径覆盖；
- harvest-only / drain-only 不跑 recovery pass（不引入远程写开关，runbook 语义不变）；
- probe（只读）在 legacy backend 常规可用；restart 仅当 `--enable-remote-writes`（deps.submit_crawl 存在）时组装；
- 科力普/multi_mall_crawler/Scheduler/STEP_2/STEP_3/force-finalize ≤10 全部不碰。
