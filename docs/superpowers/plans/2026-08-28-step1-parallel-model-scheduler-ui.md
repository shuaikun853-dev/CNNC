# Plan: STEP_1 并行化 / 模型吞吐 / 调度隔离 / 假人工告警修复 (2026-08-28)

BASE_BRANCH=origin/prod/closeout-20260818
BASE_SHA=adba73435504746a146354c450ee04e3fecbcb72
DEV_BRANCH=fix/step1-parallel-model-scheduler-ui-20260828

## 调查结论(三路并行代码走查)

1. crawl 链(propose → ensure_crawl_work → finalize_crawl_plan → prepare_remote_intent → dispatch)
   不读取 DETAIL 结果、不读取 MODEL 结果;输入只有快照行的 extLink/vendorName/skuId 与
   冻结的 `__crawl_*` policy。唯一耦合是 `run_incremental_tick` 的编排顺序
   (detail → model → submit)以及 submit 前 `MODEL state == SUCCESS` 的 eligibility 过滤
   (`app/workflows/incremental_price_audit.py:1316-1323`)。这是流程串行,不是数据依赖。
2. MODEL 批处理 `_stage_model_batches` 逐批串行;`_model_batch_worker` 内
   `_temporary_model_environment` 会改写并恢复 `os.environ`,并发调用非线程安全。
3. `crawl.unplanned` 在 status(`incremental_audit_status.py:283-312`)对"必爬且无 crawl_work"
   一票计数,并被投影层(`dashboard_projection.py:41-44,157`)直接归入"需人工"红色告警;
   由于 crawl planning 在 MODEL 之后,MODEL 进行中的正常商品必然暂时无 crawl_work → 假告警。
4. 调度任务 `CNNC-PriceAudit-Incremental` 的 Action 参数只有 `-NoProfile -ExecutionPolicy
   Bypass -File ...`,无 `-WindowStyle Hidden` / `-NonInteractive`;Principal 为
   Interactive/Limited。顶层可见 PowerShell 窗口被用户关闭时,Job Object
   (KILL_ON_JOB_CLOSE)会连带杀掉 Python 子进程 —— 与 2026-08-28 15:00 tick 被
   0xC000013A 强杀现象一致。

## 改动设计

### Bug 1: crawl 与 MODEL 解耦

`run_incremental_tick` 新阶段顺序:`recover → harvest → discover → submit → detail → model
→ (post_harvest) → running_recovery → checkpoint`。

- submit 移到 discover 之后、detail 之前("不依赖 DETAIL → discovery 后尽快 plan+submit")。
- eligibility 改为"全部当前快照 items";planner 自身已按 `__crawl_required` 冻结 policy
  过滤(propose 对非必爬行返回 None 跳过)。不再引用 MODEL state。
- `_submit` 增加返回值:submit 能力已装配且本轮 planning 自然完成(未因 budget 中断、
  未被 stall gate 暂停)时,tick 把 `submit_pass_snapshot_id=<snapshot_id>` 写入 counters,
  随 `update_daily_metrics` 合并进 durable metrics(供 Bug 5 判定"planner 已获得执行机会")。
- 幂等/不可变 intent/SUBMIT_UNKNOWN/恢复语义全部不动(dispatch/claim/link 逻辑原样移动)。

### Bug 5: 假人工告警(planning_pending vs unplanned)

status 契约(`incremental_audit_status.py`):

- 保留原始统计"必爬且无 crawl_work"(`raw_unplanned`)。
- `planner_had_chance = run.frozen_snapshot_id is not None
  or int(metrics.get("submit_pass_snapshot_id") or 0) == target_snapshot_id`
  (target = frozen 或 latest)。
- `crawl["unplanned"] = raw if planner_had_chance else 0`(真 blocker);
  `crawl["planning_pending"] = 0 if planner_had_chance else raw`(正常暂态)。
- `computed_final_pending` 同时计入两者;`blocker_summary` 增加 `crawl_planning_pending`;
  `_next_actions`:`planning_pending → "run the next tick so crawl planning can advance"`。

投影(`dashboard_projection.py`):

- 红色 attention「存在未规划采集的必爬行」仅由真 `unplanned` 触发;
  `planning_pending` 不产生"需人工",计入 `zs_uncleared`(未完成)但不计入 `zs_bad`。
- `_final_text` 增加"条必爬行待规划"条目。

finalize 侧 `crawl_unplanned`(NON_TOLERABLE)不动 —— freeze/finalize 时的真实 unresolved
required crawl 仍阻塞出表。前端 app.js 无需改动(渲染后端列表)。

### Bug 2: MODEL 有界并发

- `TickConfig.model_max_concurrency:int = 1`(引擎默认保持串行,现有离线契约不变);
  runtime 经 `TITLE_MATCH_MODEL_MAX_CONCURRENCY` 注入(正整数,默认 6,实测后定稿)。
- `_stage_model_batches` 重构为"durable claim 主线程串行 → ThreadPoolExecutor 有界并发
  HTTP → 完成回主线程串行写 SQLite"。as_completed 收集;budget 不足即停止派发新批次,
  已派发批次自然收敛;with 块保证线程回收。
- `_model_batch_worker` 的 env 变更改为 apply-once(进程内幂等、不再恢复),消除并发竞态;
  单条 `_model_worker` 保持原 temporary-context 语义。
- adapter 重试 sleep 增加随机抖动(×U[0.75,1.25])避免 429 并发重试对齐;重试上限/退避
  预算不变。
- 观测:counters 增加 `model_batches`、`model_max_inflight`、`model_batch_elapsed_seconds`。

### Bug 3: SAME 严格模型 + 确定性硬规则 veto

新模块 `app/services/title_match_hard_rules.py`,仅在模型输出 SAME 时运行:

- R1 型号单边/冲突:标准侧强型号码(归一化:全半角/大小写/去分隔符;先剔除
  数量+单位表达式与年份)在候选标题(去非字母数字后)无子串等价表达 → NOT_SAME。
  子串包含天然覆盖 SATA-91403≡91403、G50-2TB≡G50-2T、AR-EP65 前缀等价。
- R2 多件包装单边:N+单件单位+/+包装层级(盒/箱/包/套/袋…)或 N+单位+装 或
  套装…N+单位;两侧数量集合无交集(或单边缺失)→ NOT_SAME。单件销售单位
  (个/件/把/台/张/片无包装分母)不触发;包装层级词(套装/盒/箱/包/套)不豁免。
- veto 结果:`match_result=NOT_SAME`,reason 附 `hard-rule veto(R1/R2)`,risk_points 追加。
- 在 `call_title_match_model` / `call_title_match_model_batch` 解析后统一应用。
- prompt 收紧:关键字段(型号/容量/尺寸/功率/包装数量)单边明确存在而另一边缺失时
  不得 SAME;`TITLE_MATCH_PROMPT_VERSION` 为内容寻址,自动失效缓存。

### Bug 4: 调度隔离

- register ps1 的 Action 参数在 `-NoProfile` 后追加 `-NonInteractive -WindowStyle Hidden`
  (与 dashboard 自启任务一致的形态);Principal 维持 Interactive/Limited —— S4U 未验证
  网络/cookie/profile,不切换。
- runner/日志/超时/Job Object 语义不动;新增测试断言 preview JSON 的 Arguments 含
  Hidden/NonInteractive、Execute 为 powershell.exe、脚本源仍为 Interactive principal。

## 测试计划(新增/更新)

- A 解耦:MODEL callback 阻塞时远端 submit 已发生;MODEL FAILED/PENDING 不阻止建任务。
- B 并发:fake provider 计 inflight(max>1)、429/500/timeout/部分批失败、300/500 行基准。
- C 调度:preview Action 参数、principal 源断言、现有 timeout/job-object 测试保持。
- D 假告警:MODEL 进行中不产生 manual;planner 完成后仍无法规划 → 真 blocker。
- E 全量:全新解释器进程跑完整 pytest suite。
- 现有受影响契约同步更新(phase_trace 顺序、unplanned 语义、prompt 版本)。

## 禁区

只 push 开发分支;不 merge、不部署、不改生产分支、不写生产数据库。
