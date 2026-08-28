# STEP_1 Dashboard 受控人工操作台设计（2026-08-28）

基线:adba734(prod/closeout-20260818)。分支:feat/dashboard-manual-ops-20260828。

## 0. 数据流(实现前确认的事实)

```
SaaS 快照行(audit_items/snapshot_members)
  → stage_work(DETAIL/MODEL)
  → crawl_work(remote_job_id 单向绑定,UNIQUE(run_id,mall,source,sku,url,backend))
  → remote_jobs(task_name=batchNumber,UNIQUE(run_id,logical_key)/(run_id,task_name)/(run_id,remote_identity))
  → 招商局远端任务(task_id|task_number,mall)
  → queryTaskProgress?batchNumber=<task_name>   ← 6 字段 rspDesc 解析(parse_task_progress_desc)
  → export/wait(harvest) → crawl SUCCESS/NO_RESULT
  → freeze(18:00) → finalize readiness(blocker) → materialize final.xlsx
```

关键既有事实:
- 建任务规则:`task_name = cnnc_current_opt_{mall_name}_{14位数字}`,batchNumber=task_name
  (auto_crawl_task.py create_single_mall_task:401 + runtime finalize:1252-1257)。
- `build_browser_task_identifiers` 会把任意名规范化回 `cnnc_current_opt_{mall_name}_{尾部14+位数字}`
  ⇒ 替换任务名必须严格满足该形态(尾部纯数字≥14位),否则 durable 名≠远端名。
- 进度解析:`parse_task_progress_desc(rspDesc)` → 6 字段;分类器
  `classify_remote_execution_state`(remote_running_recovery.py)是唯一 ACTIVE/TERMINAL/UNKNOWN 权威。
- 当前 Dashboard 子任务数来自 recovery_evidence observations(recovery probe 有
  attempts/wall 门槛)⇒ 普通任务无 observation ⇒ 显示 "—"。这是本轮修复的 bug。
- closeout:`run_runtime_command("closeout", invocation="manual")`,ProcessLock→ALREADY_RUNNING,
  FINALIZED→ALREADY_FINALIZED,默认预算 720s ⇒ HTTP 不可同步等待 ⇒ 子进程 + 轮询。
- 本机防护先例:main.py `check_local_access`(loopback + X-Local-Workflow-Token)。

## 1. 模块与职责

| 模块 | 职责 | 写远端? | 写 SQLite? |
|---|---|---|---|
| app/services/zhaoshang_progress_probe.py | 只读 queryTaskProgress + 缓存/single-flight/限流 | 只读 GET | 否 |
| app/services/dashboard_actions.py | operator action 运行时:action 记录(JSONL durable)、子进程启动、状态查询 | 由子动作决定 | action 日志文件 |
| app/services/dashboard_manual_ops.py | re-export / rebuild 编排入口(进程内函数,CLI 亦调用) | 是(经现有 adapter seam) | 是(经现有 store 方法) |
| app/services/remote_job_supersede.py | rebuild 协议:身份/停止确认/原子迁移/replacement intent | 是(disable 经 adapter) | 是(新表+新方法) |
| recovery/harvest.py 重构 | 抽取 `_exact_existing_harvest_core`(参数化 scope+state 门),原 wrapper 语义不变 | — | — |

## 2. Progress probe(功能 A)

- adapter 新增 `query_task_progress_for_job(job, task_id, task_number, timeout)`:
  exact lookup(check_existing_task)→ identity 校验(live==durable,冲突抛 RemoteTaskIdentityConflictError)
  → query_task_progress(batchNumber=task.get("batchNumber") or resolved_task_name)
  → `remote_business_response_ok` 门 + `parse_task_progress_desc` + 返回 progress dict + task_status_rsp_desc 不需要(进度层不用 status 接口,只做 progress;status 仍属 recovery probe)。
  失败一律异常,由缓存层降级。不查 export、不写任何状态。
- `ZhaoshangProgressService`(进程内单例,注入 dashboard_projection):
  - cache key=(business_date, batch_number);entry={progress, state(ACTIVE/TERMINAL/UNKNOWN), observed_at, error}
  - TTL:ACTIVE/UNKNOWN=15s;TERMINAL=300s;force refresh 独立 10s 限流/job
  - single-flight per key;并发上限 2;单查 timeout 10s;auth 刷新计入同一 deadline
  - 失败:返回 last-good(带 observed_at 年龄);无 last-good → unavailable
  - 每任务失败绝不抛出 summary;summary 对每 job 独立 try/except
- 身份检查:同一 run 内 task_name 出现 >1 个非 SUPERSEDED job ⇒ TASK_BATCH_CONFLICT(该 job 进度不显示,页面明示)。
- 展示规则:probe 可用→真实计数+classify;probe 失败但有 last-good→"12/18 · 最后远端进度 Xs 前";
  从未成功且 crawl_total>0→"任务包含 N 条采集数据 · 远端进度暂不可用";绝不再显示裸 "—"。
  finished+failed==total 且 failed>0 ⇒ "处理完成 N/N · 成功 x · 失败 y",不算全成功。
- 本地 crawl_total 只是 expected binding count,绝不冒充 remote finished。

## 3. Re-export(功能 B)

- 从 `run_existing_remote_harvest` 抽共享 core:
  参数 `admissible_states`(recovery wrapper 维持 {SUBMITTED,RUNNING})、
  `source_scope`(recovery=frozen sources;dashboard=冻结则 frozen,未冻结则 latest snapshot sources,无 snapshot 拒绝)。
  core 保留:exact identity 校验(prepare_job 一致性 + exact_priority_remote_identity)、
  payload 重建校验、validate_linked_crawls_against_remote_payload、terminal crawl 拒绝、
  ProcessLock + lease、claim_harvest、no create。
- dashboard wrapper 门:state ∈ {SUBMITTED, RUNNING} ∪ {MANUAL_REQUIRED 且 error_text ∈
  wait-export/no-price harvest 诊断集}——后者先 `requeue_manual_harvest_job`(同 wait_export recovery)。
  EXPORTED 亦准入(导出后仍有未决行)。
- 执行前强制 progress 预检(exact batchNumber):TERMINAL→导出;ACTIVE→允许有界执行,结果必须
  标注"任务仍在执行";UNKNOWN→REMOTE_PROGRESS_UNKNOWN fail-closed,不导出。
- 可重复:不因 harvest_attempts/export_count=0 禁用;每次 max_attempts=headroom(+2)。
- 结果四态:REEXPORT_COMPLETED(validated>0)/REEXPORT_COMPLETED_NO_RESULT(18/18 导出 0 条,可再点)/
  REEXPORT_STILL_RUNNING(ACTIVE)/错误类别(REMOTE_PROGRESS_UNKNOWN/STALE_ACTION/LOCK_CONFLICT/INVALID_REMOTE_STATE…)。
  create 调用数恒为 0(remote_calls 仅 harvest 1)。

## 4. Rebuild(功能 C)

协议(burn-before-send,全部 durable 先行):
1. 前置:job 有 remote_identity+post_attempted;state ∈ {SUBMITTED,RUNNING,EXPORTED,RECOVERY_EXHAUSTED,MANUAL_REQUIRED};
   SUBMIT_INTENT/SUBMIT_UNKNOWN/PRIORITY_PENDING ⇒ REBUILD_NOT_ALLOWED(先走既有对账)。
2. exact identity:prepare_job 一致性 + exact_priority_remote_identity == durable。
3. progress 探查(exact batchNumber):UNKNOWN ⇒ REMOTE_PROGRESS_UNKNOWN;
   ACTIVE ⇒ 停止协议(下步);TERMINAL ⇒ 记 evidence 直接迁移。
4. ACTIVE 停止:adapter `stop_running_task`(新,镜像 _restart_protocol 的 disable 半程):
   exact lookup→identity→disable→re-lookup 确认 enabled=False。未确认 ⇒ OLD_TASK_STOP_UNCONFIRMED。
   任何失败 ⇒ 不创建 replacement(fail closed)。
5. 原子迁移(store 新方法 `supersede_remote_job_to_replacement`,单事务):
   - 校验:replacement task_name 当日唯一且 != old;old 尚无 replacement;eligible crawls ==
     old 名下非 SUCCESS/NO_RESULT/TERMINAL_FAILED 的行且当前绑定==old;SUCCESS/终态行不动。
   - 写 remote_job_replacements(old_id,new_id,action_id,reason,status,old/new batch,stop evidence);
     INSERT replacement remote_jobs(state=SUBMIT_INTENT,logical_key=f"{old_logical_key}#rebuild#{action_uuid8}",
     task_name=new,payload 同 old(immutable payload 复制,input/result 路径指向新 job_dir));
     old.state=SUPERSEDED(错误文本保留,证据保留);eligible crawls → remote_job_id=new, state=PENDING, error 清空。
6. 新 task_name 生成:`cnnc_current_opt_{mall_name}_{14位}` = sha256(old_task_name|action_id)[:14位映射],
   同 action 幂等重算一致;≠old;当日唯一(UNIQUE 约束兜底)。新名==新批次号(创建时 batchNumber=task_name)。
7. dispatch:复用 tick 引擎语义——replacement 落在 SUBMIT_INTENT 后,由下一次 tick 的
   pending_intent 循环自动 claim+POST;dashboard 动作立即尝试 dispatch(同一 adapter.submit seam),
   失败→SUBMIT_UNKNOWN 交由既有 reconcile。POST 结果未知 ⇒ 状态页显示"重建正在确认远端结果",
   按钮不可重复创建(同 action_id 幂等;已有 replacement 的 old 不可再 rebuild)。
8. finalize/status:SUPERSEDED job 不计入 remote_pending/manual/invalid(有 replacement 行证据),
   缺证据 ⇒ remote_invalid(维持 fail-closed);replacement 按其真实 state 正常阻塞。
   crawl 已绑新 job ⇒ 正常走 replacement 的 blocker 计数。

## 5. 人工 closeout(功能 D)

- POST /dashboard/api/actions/closeout → 立即 spawn 子进程
  `python scripts/incremental_price_audit.py closeout --invocation manual`(budget 默认),
  action 记录 durable;HTTP 立即返回 ACTION_RUNNING。
- 结果轮询:GET /dashboard/api/actions/{action_id}(读 action 记录 + ProcessLock.inspect +
  daily_status);子进程退出码/最后输出行映射 FINALIZED/BLOCKED/ALREADY_RUNNING/ALREADY_FINALIZED。
- 不绕过 blocker:纯复用 run_runtime_command 语义。绝不 force-finalize。

## 6. API 面(功能 E + 安全)

```
GET  /dashboard/api/operator-context        → {action_token}(每进程启动随机,内存态)
POST /dashboard/api/actions/closeout
POST /dashboard/api/jobs/{id}/refresh-progress
POST /dashboard/api/jobs/{id}/re-export
POST /dashboard/api/jobs/{id}/rebuild       body: {action_id?, confirm: true}
GET  /dashboard/api/actions/{action_id}     → 状态轮询
GET  /dashboard/api/actions                 → 最近动作列表(操作记录)
```
防护:Host ∈ {127.0.0.1:8765, localhost:8765};Origin 若存在必须同源;Content-Type 必须
application/json;X-Dashboard-Action-Token 必须匹配(operator-context 颁发,CSRF 无法跨站读取);
body 仅 {remote_job_id(路径), action_id, expected_updated_at?};不接受任何 taskName/batchNumber/
路径/identity 作为权威——服务器一律从 durable state 取。GET 全部保持只读零写。

## 7. 前端(功能 F + 布局)

- 任务表"子任务"列:真实进度/降级文案;状态列含 ACTIVE 感知。
- Drawer:当前实时进度块(total/pending/running/waiting_db/failed/finished + 状态徽标)+ 按钮
  【刷新任务进度】【重新导出】【重建任务(二次确认,后端生成新名)】+ 操作记录(最近动作)+ 既有恢复时间线分离。
- closeout:最终收口卡片【立即收口】按钮,pending 状态禁用,结果四态映射文案。
- 阶段卡"最终出表"只显示文件名;完整路径放独立"输出位置"行(overflow-wrap:anywhere+可选中+复制按钮
  仅 navigator.clipboard)。CSS 防御:.stage/.sub/div、td.mono、.drawer、table 对超长字符串
  word-break;长任务名/batchNumber 同样处理。XSS:全部 esc()。

## 8. 测试清单落地位置

- tests/test_zhaoshang_progress_probe.py(§54 全部 22 项)
- tests/test_dashboard_manual_ops.py(re-export §55 14 项)
- tests/test_remote_job_supersede.py(rebuild §56 20 项)
- tests/test_dashboard_actions_api.py(closeout §57 + API 安全)
- tests/js/run_dashboard_js_tests.js(§58 15 项)
- 既有套件回归:finalize/status/store/recovery/harvest/closeout。

## 9. 不变量(评审 checklist 对应)

1. queryTaskProgress 永远用 batchNumber==task_name,绝不用 RW task_number。
2. rebuild:new taskName == new batchNumber ≠ old;当日唯一;同 action 只建一个 replacement。
3. 无用户点击 ⇒ 零业务写(GET/轮询/304 全只读)。
4. progress probe 不写 SQLite;业务状态迁移仅 workflow/recovery/dashboard_manual_ops(带 lease)。
5. 旧任务未确认停止 ⇒ 不创建 replacement(OLD_TASK_STILL_ACTIVE/OLD_TASK_STOP_UNCONFIRMED)。
6. superseded 旧 job 不重复阻塞 finalize,但证据可查;缺证据 ⇒ 阻塞(fail-closed)。
7. re-export 永不 create;不因历史 export_count=0 禁用。
8. closeout 不绕 blocker;ProcessLock/ALREADY_* 语义原样。
9. 浏览器看不到任何凭据;API 不带 cookie/token/secret。
10. summary 永不被单个 progress 查询拖死(独立降级)。
