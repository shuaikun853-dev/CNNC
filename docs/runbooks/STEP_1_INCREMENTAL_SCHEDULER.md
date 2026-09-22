# 第一步：增量调度

> 这是 legacy 全量价格审计之外的有限 tick / finalize 运行手册。仓库提供受控的 Windows
> Task Scheduler 脚本，但必须显式传 `-Apply` 才会改系统；legacy 对照和 rollback 仍按
> [STEP_1_PRICE_AUDIT.md](STEP_1_PRICE_AUDIT.md) 执行。

## 当前入口与状态

从仓库根目录运行。直接脚本与模块形式等价；Windows Task Scheduler 可采用前者：

```powershell
python scripts\incremental_price_audit.py tick --help
python scripts\incremental_price_audit.py finalize --help
python scripts\incremental_price_audit.py status --business-date 2026-08-11
python scripts\incremental_price_audit.py lock-status --business-date 2026-08-11
python scripts\incremental_price_audit.py doctor --business-date 2026-08-11
```

默认 runtime root 是 `runtime/incremental_price_audit`；每个业务日在各自目录保存
`state.sqlite3`、`incremental_price_audit.lock`、`BATCH_STATE.md`、`daily_status.json` 和正式 Excel。
SQLite 是唯一状态权威；`daily_status.json` 只是每次有限命令结束后的原子便利快照。显式传入的 `--runtime-root` 必须保留调用者路径（包括 Windows Junction 别名），不得通过 `resolve()` 静默改写到另一个 worktree。Windows reader 短暂占用 `daily_status.json` 时先有界重试原子替换；重试耗尽只记录 `DAILY_STATUS_PUBLISH_FAILED`，不得反向把已经完成的 PriceAudit 业务命令改成失败。`status` 和
`doctor` 都只读，缺失状态返回 `MISSING`，不会创建目录、数据库、发网络请求或修改 Scheduler。
`lock-status` 只探测该业务日的 OS writer lock；writer 活跃时报告
`production run currently active` 以及能够可靠读取的 PID、命令、启动时间和调用来源，不等待、不写 SQLite。

`status` 固定报告最新快照、发现/新增/变更/已处理数量、Detail/Model 的 pending/running/completed/
terminal、crawl 与远端任务的 pending/running/exported/unknown/manual、freeze/final pending/finalized，
并给出下一条受控动作。`doctor` 只核对本地文件、模型配置键名、SQLite `quick_check` / schema、
过期 lease 和 finalized artifact；不会显示密钥值。默认只把 shadow 所需依赖当硬要求；带 `--enable-remote-writes` 才把招商局依赖
计入健康判定，避免一个系统的认证问题无条件阻塞其他阶段。

SQLite 仅有十张 checkpoint 表：`daily_runs`、`snapshots`、`audit_items`、`snapshot_members`、
`stage_work`、`remote_jobs`、`crawl_work`、`local_fallback_work`、`remote_job_replacements` 和
`remote_job_rebuild_actions`。不要手改数据库、删除 lock，或把不同调度任务指向不同的 runtime root。

## 推荐的 Scheduler 配置

正式启用时只设置一个 Windows Task Scheduler 任务，避免 tick 与 finalize 两个任务在边界处竞争：

| 项目 | 配置 |
|---|---|
| Task name | `CNNC-PriceAudit-Incremental` |
| Program | Windows PowerShell |
| Arguments | `-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File <runner> ...`（隐藏窗口:自动触发不弹窗,用户关闭可见 PowerShell 窗口不再误杀 job object 内的 Python tick;2026-08-28 事故后加固） |
| Runner | `scripts\run_incremental_price_audit_scheduled.ps1` |
| Start in | 仓库根目录 |
| Principal | 注册时的交互用户,`LogonType Interactive` + `RunLevel Limited`(S4U 需先验证网络/cookie/profile,未验证不切换) |
| Trigger | 每周一至周五共四个 trigger：09:00 起每 15 分钟唤醒至 16:45；17:00 起每 3 分钟唤醒至 17:57；18:00 起每 5 分钟唤醒至 20:00；另加 20:35 一次性 Safety Closeout（最后一次自动启动 20:35） |
| Multiple instances | **IgnoreNew** |
| Application budget | tick/harvest-only **720 秒**；closeout **1680 秒** |
| ExecutionTimeLimit | **32 分钟（1920 秒）** |

runner 是一次性有限进程，不含常驻 daemon。09:00-16:00 整点运行 `tick`；09:15-16:45 的白天非整点
15 分钟唤醒运行 `harvest-only`（只回收已存在的招商局 remote jobs：不查 SaaS、不跑 Detail/Model、不
submit、不 finalize；本地无 eligible remote job 时纯本地 NOOP，不请求招商局）。17:00-17:57 每 3 分钟
运行一次完整 `tick`（harvest、reconcile、local fallback eligibility/execution 均在场；单轮超时不并发，靠
IgnoreNew 丢弃重叠 trigger）。不再保留 17:55 harvest-only 特例。18:00-20:00 每 5 分钟提供一次 `closeout` 启动机会，
**20:00 是 5 分钟窗口的最后一次**；20:35 是一次性的 Safety Closeout，也是当天最后一次自动启动：
19:55 的 closeout 运行超过 5 分钟时 20:00 trigger 会被 IgnoreNew 丢弃，若该轮随后以 WAITING_REMOTE
结束，当天仍由 20:35 补一次自动 closeout（20:00 起跑的一轮最晚 20:32 被 32 分钟硬上限终止，20:35
必可启动；当天已 FINALIZED 时按现有幂等语义直接 NOOP，不重写 final.xlsx）。20:36 后不再有任何
自动调度，即使被人工误调用也会快速空操作退出。

### 增量自动本地兜底

该策略只属于日常 incremental scheduler，与完整 STEP_1 的人工 manifest 授权入口相互独立：

- 09:00-16:59：远端任务开始满 60 分钟后的第一个完整 tick，先做最终招商局查询，仍无有效价格才进入本地兜底。
- 17:00-17:59：第一次远端检查只落账 `last_remote_check_at`；至少 3 分钟后的下一次完整 tick 再最终查询，无价格才以 `PEAK_3M` 兜底。
- 18:00 以后：closeout 先最终 harvest，再以 `AFTER_1800` 立即处理受支持的 unresolved URL，然后重新计算 blockers。

自动范围固定为 `nbdeli / guangbo / ofs / lxwlmall / qstbg / ycw / comix / tima`。每个 exact URL 原子 claim，partial job 只发送仍未取得可信价格的 URL；remote `NO_RESULT` / `TERMINAL_FAILED` 是无有效价格的证据，仍可进入本地兜底。最终远端查询若重新取得有效招商局价格则先用远端价格解决该 exact URL，不启动本地爬虫。本地 SUCCESS 可用 `本地兜底:<site>` 价格覆盖该 exact URL 的 remote 终态；本地失败或崩溃保留原 remote 终态及证据，本地 ledger 最多尝试 2 次且耗尽后不再 claim，同一轮不 tight-loop。验证码、登录墙、风控、外来/重复 URL、manifest 不一致和非法价格均 fail-closed，输入预检与输出解析均按站隔离，不把单站错误扩散到其他站。multi-site child 的 exit 2/3 不抹去其他站已严格校验的成功 XLSX；缺失或损坏的站点单独失败/风控停止。最终远端检查后、逐项 claim 时以及真正启动 child 前都重新检查 live tick 预算；扣除 heavy-work reserve 后不足 60 秒不启动 child，未启动的 claim 退还尝试次数、留待下轮，child timeout 不超过启动时剩余可用预算。招商局迟到结果不得覆盖 local SUCCESS；若远端状态仍为 `SUBMIT_UNKNOWN` 或 `MANUAL_REQUIRED`，但其关联 URL 已全部由可信结果解决，该陈旧任务状态不再阻塞 finalization。最终失败链接投影也按 exact `(mall, original_url)` 判定：`SUCCESS`、结果 JSON 可解析、价格语义有效且来源为 `本地兜底:*` 的 URL 不再追加“爬取失败”，同一 remote job 的其他 `NO_RESULT`、terminal 或仍未解决 URL 继续分别输出失败行。
### 客户参考链接预爬结果字段契约

招商局导出结果进入 `crawl_work.result_json` 和 Shared Crawl Cache 时，不得只保留价格。生产字段契约固定为：`page_price_value`、`page_sku_id`、`page_title`、`channel`、`comment_count`/`comment_count_raw`。其中 `商品最小品类单元id` 是页面真实 SKU 证据；合法值以 `sku_status=CONFIRMED` 写入 `real_sku_id`，只有页面 SKU 缺失时才允许回退到请求侧合法 SKU 并标记 `UPSTREAM_PROVIDED`，`zhpq-*`/`ROW<n>` 等临时码仍保持 `UNRESOLVED`。商品标题写入 cache `title`，整数评论写入 `comment_count`；`1万+` 等不能无损转整数的评论原文只保存在 payload `result.comment_count_raw`，不得伪造整数。

`sync_daily_internet_results` 和 customer-prewarm 招商局收割必须遵守同一字段契约。历史成功任务可使用 `backfill-zhaoshang-history` 从已经存在且通过 provenance 校验的 `result.xlsx` 追加式补全旧缓存的页面 SKU、标题和评论；补全不得重新 create/prioritize/reconcile/crawl，必须保留 append-only 历史并幂等。第三步消费规则见 `STEP_3_NO_PUSH_PROCESSING.md`：京东、苏宁只有评论证据也齐全时才算可直接复用的完整缓存命中。

closeout 在一个进程内持续推进，不为下一个 5 分钟唤醒等待：SaaS 稳定观察最快每 300 秒一次，
招商局 full sweep 最快每 600 秒一次（单批最多 6 个，一轮覆盖全部 eligible remote jobs）。18:00 后只要
最近 authoritative 观测的 `row_count` 与 `ordered_hash` 连续 600 秒一致且本次分页完整，即可 freeze
最新 snapshot（17:45 与 18:00 相同则 18:00 直接 freeze，无需等到 18:10；期间变化则窗口重新计时）。
`customer_count` 只作为独立观测指标，不参与 freeze equality gate。已 frozen 后不再 discover，
只继续 Detail/Model/Submit/Priority/Harvest/backfill/readiness/materialize；blocker 清零后同一进程立即
materialize final.xlsx。`state=FINALIZED` 后任何 Scheduler 或手工 tick/closeout/finalize 都在发出
SaaS/招商局/remote submit 请求前直接 `ALREADY_FINALIZED` NOOP。

高频窗口只提高空闲后的响应速度，不改变并发语义：任务级 `IgnoreNew` 保证任意时刻至多一个生产
writer。例如 17:30 的 tick 运行 9 分钟时，17:35 的 trigger 被自动丢弃，17:40 正常启动；18:00 的
closeout 运行 12 分钟时，18:05 与 18:10 被丢弃，18:15 重新进入；closeout 提前结束后最多约 5 分钟
即可获得下一次启动机会，而不再是 15 分钟；19:55 运行 28 分钟时 20:00 被丢弃、20:23 结束后由
20:35 Safety Closeout 补位。

Scheduler 默认仅周一至周五运行。周六、周日中核不产生业务推送，因此不执行自动增量处理。
主限制由 Weekly trigger 的 `Monday` 至 `Friday` 实现，不注册周末 trigger，也不启用
`StartWhenAvailable`；runner 的周末快速退出只是防止人工误调用的第二层保险。本配置不代表中国
法定工作日日历，不处理节假日或调休。

按 `runtime_root + business_date` 建立的非阻塞 OS file lock 是手工 CLI 与 Scheduler 共用的第一层
生产互斥；它在 SQLite、priority、export、download、harvest、backfill、readiness/freeze 和正式物化前取得，
并覆盖整个命令生命周期。`tick`、`closeout`、`finalize`、`drain-only`、`harvest-only` 竞争同一把锁；拿不到时立即输出
`SKIPPED_ALREADY_RUNNING` 并以 0 正常退出，不等待也不进入 lifecycle。Windows Task Scheduler 的
`IgnoreNew` 是额外的 no-overlap 防护，内部 fenced DB lease 继续保留。预算接近末尾
reserve 时不会领取新重工作，下一次调度唤醒继续 backlog（白天最多 15 分钟、17 点高峰与
closeout 窗口最多 5 分钟）。Python wall-clock budget 无法强行中断卡在
操作系统或第三方库里的单次调用，所以 Scheduler 在 32 分钟作硬兜底。若硬停止恰逢 POST，
已落库的 immutable intent 会在下一 tick 进入精确对账，而不是重发。调度日志按业务日写入
`runtime\incremental_price_audit\scheduler_logs\YYYY-MM-DD.log`。

注册脚本默认只打印计划，下面第一条命令不会修改系统。本阶段验证通过且获得启用确认后，才执行
第二条；默认预览仍保持 shadow，不创建远端任务：

```powershell
powershell -NoProfile -File scripts\register_incremental_price_audit_tasks.ps1
powershell -NoProfile -File scripts\status_incremental_price_audit_tasks.ps1
```

正式生产注册必须在核对预览后显式携带远端写授权：

```powershell
powershell -NoProfile -File scripts\register_incremental_price_audit_tasks.ps1 `
  -EnableRemoteWrites `
  -Apply
```

### 模型吞吐调参(可选,2026-08-28 Review Round 2 收口;Round 3 拆分两世界键名)

增量 STEP_1 有界并发模型批处理(tick 内 MODEL 阶段)支持两个可选调参键:

| 键 | 默认 | 生效位置与优先级 |
|---|---|---|
| `TITLE_MATCH_MODEL_MAX_CONCURRENCY` | 6 | 进程环境变量 > `data/model.env` > 默认;只作用于增量流水线 |
| `TITLE_MATCH_MODEL_INCREMENTAL_BATCH_SIZE` | 10 | 进程环境变量 > `data/model.env` > 默认;增量专属键,work_01 不识别 |

要点:

* 这两个键是 **optional runtime tuning**,不属于"API_KEY/BASE_URL/MODEL_NAME 三键必须齐"的 required 配置门;缺省完全合法。
* 无效值(非数字、0、负数)一律 **fail closed**:tick 构建 `TickConfig` 时直接抛 `RuntimeConfigurationError`,不会静默回退。
* **批大小键名两世界隔离(Round 3)**:work_01 旧路径的批大小键是 `TITLE_MATCH_MODEL_BATCH_SIZE`(仅进程环境变量,默认 30),增量流水线**不读该键**——历史 `data/model.env` 里为 work_01 写入的旧值不会在增量代码升级后被静默激活(增量批大小不会从 10 意外变成 30)。增量只认 `TITLE_MATCH_MODEL_INCREMENTAL_BATCH_SIZE`。
* work_01 旧批量路径不读 `data/model.env`,其并发变量为 `TITLE_MATCH_MODEL_CONCURRENCY` / `TITLE_MATCH_MODEL_BATCH_CONCURRENCY`(仅进程环境变量),与增量的 `MAX_CONCURRENCY` 是不同键,互不影响。
* scheduler 生产可操作路径:修改 `data/model.env`(或注册任务的 runner 环境)后,下一轮 tick 自动以新值构建 `TickConfig`;无需重新注册任务。默认 6 经离线饱和基准验证(observed max_inflight=6,500 行 12s/批延迟设计投影 ≤120s),调大前先确认模型侧限流能力。

### Deployment gate:注册脚本修改后必须重新 Apply（2026-08-28 Review Round 1）

**已经注册的 Windows Task 不会因为仓库里 `register_incremental_price_audit_tasks.ps1` 的代码更新而自动改变
Action/Principal/Settings。** 任何对注册脚本的修改（含本轮加入的
`-NoProfile -NonInteractive -WindowStyle Hidden` Action 参数）部署到生产目录后，必须重新执行上面的
`Register-ScheduledTask ... -Apply` 流程，否则线上任务仍以旧参数运行。重新 Apply 前先跑预览核对
Arguments,再 `-Apply`,然后用 `status_incremental_price_audit_tasks.ps1` 复核。

生产重新注册后的验收清单（全部通过才算部署完成）：

1. `status_incremental_price_audit_tasks.ps1` 输出的 `Arguments` 含
   `-NoProfile -NonInteractive -WindowStyle Hidden`（三者都在 `-File` 之前）。
2. 自动触发（或手动 Start-ScheduledTask）后,桌面/任务栏**无可见 scheduled PowerShell 窗口**。
3. Python child 正常运行：`scheduler_logs\YYYY-MM-DD.log` 出现该轮 START/END 结构化行与 python 输出。
4. 用户关闭任意无关 PowerShell/CMD 窗口,production task 不受影响（任务树仍在,日志持续追加）。
5. 用户在自己终端里 Ctrl+C 复制/中断操作,不影响 production task（无共享控制台）。
6. `scheduler_logs` 正常:按业务日追加、UTF-8 无 BOM、外部可并发读取。
7. Task Scheduler `LastTaskResult` 正常:成功轮次为 0;超时轮次为 124 且日志含 `TIMEOUT phase=...`;
   退出码 70/74 仅对应启动失败/日志写失败并在日志中有对应诊断行。

脚本固定使用 legacy 招商局 backend、`MultipleInstances=IgnoreNew`、三重复 trigger 加 20:35 一次性
Safety Closeout（白天 15 分钟、17:00-17:57 每 3 分钟、18:00-20:00 每 5 分钟、20:35 一次）和 32 分钟硬上限。
若发现旧的 `-Tick` / `-Finalize` 双任务仍存在，注册会拒绝继续，而不会暗中删除或制造竞争任务。
注册身份沿用当前项目的交互式低权限用户上下文；机器关机或用户未登录时不会强行改用另一套凭证。

自定义 root 或重新执行某日时，显式使用同一个绝对路径：

```powershell
python scripts\incremental_price_audit.py tick `
  --business-date 2026-08-11 `
  --runtime-root 'E:\.A中核运维\cnnc-ops-agent\runtime\incremental_price_audit' `
  --budget-seconds 720 `
  --shadow

python scripts\incremental_price_audit.py drain-only `
  --business-date 2026-08-11 `
  --runtime-root 'E:\.A中核运维\cnnc-ops-agent\runtime\incremental_price_audit' `
  --budget-seconds 3000 `
  --max-iterations 1000 `
  --zhaoshang-backend legacy

python scripts\incremental_price_audit.py lock-status `
  --business-date 2026-08-11 `
  --runtime-root 'E:\.A中核运维\cnnc-ops-agent\runtime\incremental_price_audit'
```

上面的 `drain-only` 是正式手工恢复入口，不是 dry-run，也不再由 Scheduler 自动调用；它与 tick/finalize
使用同一 writer lock。`harvest-only` 是 Scheduler 白天非整点槽位的自动命令，也可手工执行；它只调用与
tick/drain 同一个 `_harvest()` sweep（snapshot-at-start eligible 集合、按 `harvest_attempts,id` 排序、单批
最多 6 个、一轮覆盖全部 eligible、单 job 同轮只查一次），发现 `COMPLETED` 立即走现有 export/ingest 更新
crawl/remote durable state，但绝不 discover/Detail/Model/submit/freeze/materialize；CLI 不暴露任何远程写
开关。锁由文件句柄上的 Windows `msvcrt` 非阻塞字节锁持有，进程正常退出、异常、
Ctrl+C 或被操作系统终止后都会由 `finally` 或 OS 自动释放。旁路 owner JSON 仅用于展示，不能决定
锁是否占用；即使异常后留下 stale metadata，下一 owner 仍以 OS lock 为准并覆盖它。

`drain-only` 在 18:00 前返回未就绪；之后只处理已经由 finalize 通过来源数量 gate 冻结的 snapshot，
不会自行 discover 或冻结任意历史 snapshot。
内部 `harvest_batch_size` 仍为 6，但同一命令会重新读取 SQLite 并连续处理下一批，直到 existing remote
全部完成、整轮无有效状态推进、达到 wall-clock deadline 或 `max-iterations`。priority 从
`PRIORITY_PENDING` 推进到 `SUBMITTED` 后，下一内部 iteration 会在同一命令中继续 export/harvest，
不等待下一次调度。单批 harvest callback 上限仍为 60 秒；手工命令预算保留正式物化时间，外层 hard timeout
watchdog 和 `max-iterations` 是额外保险。已经 `COMPLETED` 的 job 不进入候选集。

命令可重复触发：未 drain 时保持 `FINALIZING`，只有 Detail/Model/crawl/remote pending 时按正常等待退出；terminal、未规划 crawl、
无效价格、remote `MANUAL_REQUIRED`、非法 remote state 或产物冲突才作为可操作阻塞返回非零。关联 frozen
mandatory crawl 的 remote job 通常必须全部为 `COMPLETED`；唯一例外是
`remote_job_is_closed_by_local_fallback(...)` 已证明该 job 的每条关联 crawl 均为有效 `SUCCESS`，且至少一条
来自 `本地兜底:*`。只有这个 exact-link 全量闭合证明成立时，陈旧 remote 状态才不阻塞 formal artifact；
任一关联 URL 仍是 `NO_RESULT`、terminal 或 unresolved 时都不能按 job 级别放行。命令总预算至少还剩 60 秒才开始正式物化，
否则 checkpoint 后交给下一次 `drain-only`。完成后 workbook/manifest 只会复用同一 frozen artifact，
输出路径 intent 在落盘前已唯一登记，冲突不会先写出第二份正式文件。
物化前先以私有 intent 固定本次输入 hash 和最终 metrics（不作为 finalized 指标发布）。workbook/manifest
物化后，正常与强制收口均在同一个 SQLite transaction 中提交 `FINALIZED`、`final_manifest_hash`
和 intent 中的最终投影/强制收口 metrics，并移除 intent；事务失败则保留 `FINALIZING`。
重试会校验并复用同一 workbook/manifest、提交首次物化时固定的指标，晚到采集不得改写原正式表或指标。
若输入变化造成 manifest 冲突，必须停下人工核对，不能静默改写已出版文件。
文件系统物化与 SQLite 提交不是跨系统 ACID transaction。

只有业务方对指定批次明确接受缺价时，已绑定 exact remote identity、任务已 `COMPLETED`、且本地
导出结果证据完整但找不到该 crawl identity 返回行的 `TERMINAL_FAILED`，才可受控转为
`NO_RESULT`。该状态保留原错误和导出证据，正式表显示“无采集结果”且价格留空，绝不能写成 0；
没有建立 crawl work 的 frozen 记录仍是 finalize blocker，不能借此批量放行。

## 20 点以后的人工 closeout

20:35 Safety Closeout 是当天最后一次自动 closeout。之后仍未 `FINALIZED` 时不再有任何自动 SaaS/招商局请求，
由运维按以下三步人工推进。

### 第一步：status

```powershell
E:\Python314\python.exe `
  "E:\.A中核运维\cnnc-ops-agent\scripts\incremental_price_audit.py" `
  status `
  --business-date 2026-08-18 `
  --runtime-root "E:\.A中核运维\cnnc-ops-agent\runtime\incremental_price_audit"
```

### 第二步：根据状态判断

- `state = FINALIZED`：停止，不做任何操作。之后即使误执行 closeout 也会 NOOP。
- `remote.running > 0` 或 `remote.pending > 0`：主要还在等招商局，可按需手动执行一次 closeout。
- `remote.manual > 0`、`remote.unknown > 0` 或 `crawl.manual > 0`：不要反复执行 closeout，
  先按下面"精确人工恢复"处理具体异常。

### 第三步：手动 closeout（一次性集中推进，不是提高调度频率）

```powershell
E:\Python314\python.exe `
  "E:\.A中核运维\cnnc-ops-agent\scripts\incremental_price_audit.py" `
  closeout `
  --business-date 2026-08-18 `
  --runtime-root "E:\.A中核运维\cnnc-ops-agent\runtime\incremental_price_audit" `
  --budget-seconds 1680 `
  --enable-remote-writes `
  --zhaoshang-backend legacy
```

说明：`--enable-remote-writes` 是生产写授权（不传则 shadow，只读对账不建招商局任务）；
`--budget-seconds 1680` 与 Scheduler closeout 预算一致（CLI 默认 720 秒，可按需调整）；
`--source-stability-seconds` / `--source-observation-interval-seconds` /
`--remote-sweep-interval-seconds` 已默认 600/300/600 秒，无需显式传；`--output` 可选，
默认输出 `runtime\incremental_price_audit\<业务日>\saas_audit_final_<YYYYMMDD>.xlsx`。

## 精确人工恢复

以下命令都会修改指定业务日的 durable SQLite，只能在确认没有运行中实例并获得当次明确授权后执行。
它们不注册、启动或停止 Scheduler，也不隐式执行 tick/finalize。

历史版本把已经耗尽 adapter 内部有限重试、但根因仍为 `TimeoutError` 的 MODEL 工作直接记为
`TERMINAL_FAILED` 时，可使用窄 requeue。命令要求当前业务日全部 terminal MODEL 的数量、durable
attempts 和安全错误摘要完全一致；任一行不匹配就整体拒绝。成功只把精确集合改为
`RETRYABLE_FAILED`，保留 attempts、elapsed 和 error evidence，不调用模型、不规划 crawl、不触发
remote lifecycle。下一次正常 tick 使用剩余的 durable attempt budget：

```powershell
python scripts\incremental_price_audit.py requeue-model-transient `
  --business-date 2026-08-14 `
  --expected-count 54 `
  --expected-attempts 1
```

`recover-model` 必须逐个提供 `TERMINAL_FAILED` 的 MODEL `stage_work` id。命令一次只调用模型批次，
没有 crawl planner、remote intent、submitter、harvester 或 finalizer，成功结果进入
`RECOVERED_PENDING_REVIEW`，不会成为后续 Scheduler 的 crawl 候选；命令 JSON 中的
`recovered_results` 用于人工检查，重复执行同一 id 会 fail closed：

```powershell
python scripts\incremental_price_audit.py recover-model `
  --business-date 2026-08-11 `
  --stage-work-id 42 `
  --stage-work-id 43
```

人工检查这些结果后，只有已明确授权它们在未来正常 tick 中进入 crawl/remote submit，才可使用
`release-model-recovery`。确认参数是必填安全栓；release 本身不调模型、不建任务、不 harvest，但已注册的
remote-write Scheduler 可在下一轮处理被释放为 `SUCCESS` 的结果：

```powershell
python scripts\incremental_price_audit.py release-model-recovery `
  --business-date 2026-08-11 `
  --stage-work-id 42 `
  --stage-work-id 43 `
  --allow-future-remote-submit
```

`recover-remote-harvest` 只接受一个 `MANUAL_REQUIRED` job 及其完整 durable remote identity。它要求
本地 input/result、immutable payload 和 CREATED/ENABLED/EXPORTED path evidence 完整，只调用
`harvest_existing` 做本地校验和回填；不会启动 legacy runner，不会 `/add`。

legacy 结果表必须同时提供执行编号、批次号和商城标识。恢复先按精确 remote task
建立 provenance：批次号必须等于 durable task name，商城标识必须等于 durable mall，
执行编号必须以该 task number 为前缀且在当前批次唯一。同一 execution 出现冲突价格、
execution 缺失或不属于该 task 都 fail closed；其他批次行先过滤。完成来源校验后，
导出的商品编码、SKU、URL 和价格都是 result payload，不再与 submitted 字段做 acceptance gate。
它们只用于在该 immutable remote payload 内定位 durable request group，不反查全局
`crawl_work`；无法唯一落到 durable group 才转 manual。

```powershell
python scripts\incremental_price_audit.py recover-remote-harvest `
  --business-date 2026-08-11 `
  --remote-job-id 5 `
  --remote-identity 'mall|task-id|task-number'
```

整份 legacy 结果表因少量缺行或旧的业务字段一致性校验而无法通过时，只能在
`post_attempted`、受控提优、精确 `EXPORTED` 事件、批次/商城/执行编号 provenance
全部通过后执行本地 Partial Exact Harvest：

```powershell
python scripts\incremental_price_audit.py recover-partial-exact-harvest `
  --business-date 2026-08-14 `
  --remote-job-id 5 `
  --remote-identity 'mall|task-id|task-number'
```

该命令不调用远端接口，不增加 `harvest_attempts`。已定位的 result row 写 `SUCCESS`（保留
`price=0`），remote request 确定缺失时，其全部 local consumers 写带
`EXPORTED_NO_MATCHING_ROW` 的 `NO_RESULT`。一个 planner dedupe group 可合法 fan-out 到多个绑定
`crawl_work`，各自保留 source/vCommodityId；未属于该 group 的相同 SKU 不得被带入。
execution 竞争、durable mapping 缺失/冲突或其他真实 provenance 歧义只把对应 crawl 留在 manual。存在任何
未解决 crawl 时 remote job 继续为 `MANUAL_REQUIRED`，不得据此放行 formal final。

## 人工复核表

冻结快照已完成 Detail/Model、但仍有少量 crawl/remote mandatory blocker 时，可以生成完整的人工复核表：

```powershell
python scripts\incremental_price_audit.py export-review `
  --business-date 2026-08-11 `
  --output-path runtime\incremental_price_audit\2026-08-11\saas_audit_review_20260811.xlsx
```

该命令只物化 frozen snapshot 的全部 occurrence，不调用远端接口，不执行 `/add`，也不改变
crawl/remote 状态。输出文件名不得含 `final`；命令不会设置 `finalized=true`、写 final manifest 或占用
formal output path。`NO_RESULT` 价格留空，真实零价保留为 0，未决项标记 `MANUAL_REVIEW` 且不猜价格。
正式 final 的 blocker 和幂等规则不因此放宽。

## 安全开关和当前限制

- Snapshot discovery 使用独立 180 秒总预算，以保留 adapter 现有单请求 60 秒、每页最多 3 次的有限重试；
  Detail、Model、harvest、planner 和 submit 的单次外部 callback 仍保持 60 秒。

- `--shadow` 是默认模式：runtime 装配 SaaS/中核发现、Detail、模型，以及已记录任务的 legacy
  `wait-export-only` / 精确 unknown-submit 对账，但没有 crawl planner 或 submitter，不能创建招商局任务。
  它不是本地网络 dry-run：已有 remote job 满足上文时间、最终远端查询成功和 exact URL 原子 claim 门禁时，
  仍会执行固定 revision 的 8 站本地自动兜底并持久化结果；不得将生产业务日 shadow tick 当作零网络 canary。零本地请求验证只用 fake dependencies / sandbox / fixture。
- `--enable-remote-writes` 才接通受控 legacy `create-only`；旧名 `--enable-zhaoshang` 暂作兼容别名。
  它先把精确成员、模板和 immutable payload 持久化，
  再发起正式建任务。它是写能力，必须有明确授权；注册预览默认不携带该参数，生产注册必须显式加入。
  submit 后若本轮仍有预算，会复用同一个 `_harvest()` 检查一次本轮新任务；`drain-only` 不接通
  planner/submitter，只跨多个有限 batch 做 `wait-export-only` harvest。每批仍最多 6 个，并按
  `(harvest_attempts ASC, id ASC)` 公平选择。超时、进程中断或非确定结果先进入
`SUBMIT_UNKNOWN`，只按精确 task/mall 对账，绝不盲目重发 `/add`。
exact lookup 返回唯一、task/mall/可用的子任务数证据一致且远端已启用时，才允许接管已有任务；接管会以
原 `ADD_INTENT` 时间补写 durable `CREATED`/`ENABLED` equivalent evidence，再继续 priority/harvest。
零匹配保持 unknown，多匹配、身份冲突、未启用或证据写入失败均 fail closed。`remote_identity` 在同一 daily
run 内由部分唯一索引约束，不同业务日可以合法复用；旧库迁移前若发现 collision，初始化直接拒绝继续。
task name 使用业务日和 immutable 请求集生成同日稳定、跨日不同的 14 位标记，防止次日对账命中前日任务。
重启后的 harvest 只从已持久化的 payload、模板和路径重建同一 legacy job；任一证据不一致即转
`MANUAL_REQUIRED`，不尝试“猜测性恢复”。
- runner 将 Python stdout/stderr 都通过单个共享 UTF-8 文件句柄记录到同一业务日日志；日志读取者
  不能在输出行之间抢占写句柄，stderr 本身也不会让 Windows PowerShell 5.1 提前终止，Scheduler
  成败只由 Python 的真实退出码决定。runner 使用 .NET `Process` 并发 drain 两个流，并把进程放入
  kill-on-close Windows Job Object；内部 deadline 小于注册的 ExecutionTimeLimit（默认 PT32M），
  超时返回 124 并终止 child/grandchild，外层 ExecutionTimeLimit 继续作为第二道外部保险。wrapper 被强制终止时，Job Object 关闭也会清理整个进程树。
- `--zhaoshang-backend legacy|batch` 选择新任务策略，默认 `legacy`。`legacy` 保持全量 legacy；`batch`
  仅对 SQL result allowlist 内商城使用公司 Skill 已证实的 `/batch/autoAddTask` 与
  `queryTaskProgress`，其余商城逐商城回退 legacy。batch 按商城和最多 100 条稳定分片；create 阶段不生成
  临时 Excel、不开每商城子进程，也不依赖 Cookie/cURL。它仍必须显式传 `--enable-remote-writes`。
  当前 Skill 没有 batch 结果 export/download HTTP 契约；runtime 改用已验证的只读 SQL 路径，按
  exact `executeNumber` 查询同商城 price/goods，并要求唯一 source_id/mall/SKU/URL/backend 对应唯一 linked
  `crawl_work`；相同 SKU/URL 的不同 SaaS 来源文档分别生成 executeNumber，不能结果扇出。后到的新 source
  不跟随或复制既有 batch job/result，只为仍 pending 的 source 重建精确分片；旧 durable payload仍按旧 identity
  恢复。progress、物化 probe、结果 SQL 共用同一个 callback 绝对 deadline，每次数据库阻塞操作前重算剩余
  timeout；deadline 前未开始的 job 退还 harvest claim，不消耗 retry budget 或清除旧诊断。`price>0` 必须有
  title；`price=0` 固定写 `PRICE_IS_ZERO + 已下架`；无效价格 fail closed。
  `task_status=02` 且 SQL 零行仍需 spider-log 等终态证据，不能直接 `NO_RESULT`。
- batch create 成功或 `SUBMIT_UNKNOWN` 经 exact `batchNumber` 找回后，job 先进入 durable
  `PRIORITY_PENDING`，不得直接 query progress。priority service 只在
  `central_control_sub` / `central_control_sub_zsz` allowlist 内按本 slice 持久化的 exact
  `executeNumber`（每片最多 100 条）查询；每次 SQL/commit 都重算同一个 callback deadline，
  0/N、partial N/M 和明确 transient DB 错误才有界重试，其他异常立即 fail-closed。
- 只有 exact set 在单一 schema 全部物化后才冻结真实 `task_id` 集；写事务内会对两个 schema
  再做 exact `exec_num FOR UPDATE` 重验，随后 UPDATE 只按这些 PK，且限定
  `task_status IN ('00','01') AND is_valid='1' AND weight_type<>'-1'`。事务内与 commit 后都回读
  exact identity；仍活跃的行必须为 `-1`，已为 `-1` 视为幂等成功，竞态变成 `02`（完成）或
  `03`（失败）的终态行均不强改，分别计入 `already_completed` / `already_failed`。
  确认后才从 `PRIORITY_PENDING` 进入 `SUBMITTED` 并在后续 sweep 查询 progress。重启只从 SQLite
  immutable payload 恢复 backend 与 `execute_numbers`。CLI backend 只决定新任务的 planner/submitter；
  当前 18 个生产商城均可逐链接走 batch；`mymro / ofs` 只有在提交 URL 能按已证明的严格格式解析 product code 时才标记 `batch-url-v1`，否则该链接自动回退 `legacy-v1`。这两个商城以 exact executeNumber 加 submitted URL product code 作为请求身份，唯一 SQL result 允许后续 SKU/goods_no/result URL code 变化，多条 result 仍 fail closed。新建 `jslink.com`
  及其子域 URL 先规范为 `b2b.nbdeli.com` 并按 `nbdeli` 路由；vendor-only、空 URL、hostless URL
  一律保留 legacy，Batch adapter 只接受绝对 HTTP(S) URL。历史 frozen `jslink` policy 按原 mall/source/URL
  恢复，不改写旧 SQLite。已有 legacy 与 batch job 在同一 SQLite
  内按各自 durable payload 路由到对应 harvester，损坏 payload
  立即转人工。未启用 remote writes 的 batch priority 不 claim、不烧重试次数；已有 SUBMITTED/RUNNING
  batch 仍继续只读 SQL harvest，绝不重复 POST。
- `PRIORITY_PENDING` 独占 `priority_attempts/priority_elapsed_seconds`；`SUBMIT_UNKNOWN` 仍属于普通
  harvest 对账预算，`SUBMITTED/RUNNING/EXPORTED` 使用 `harvest_attempts/harvest_elapsed_seconds`。
  旧 SQLite 打开时原位补列且默认 0，不得要求清库。batch 的 `SUBMIT_UNKNOWN -> PRIORITY_PENDING`
  和 `PRIORITY_PENDING -> SUBMITTED` 都在一个 SQLite transaction 内同时推进 `remote_jobs` 与 linked
  `crawl_work`；identity 必须由 durable payload 与 JSON remote identity 联合验证 exact contract、
  logical key、payload hash、batch、mall 和无重复 executeNumber 集，任一漂移 fail closed。
- 增量 Tick 的模型批次基线是 10、并发为 1，与第一步生产稳定口径一致；不要把
  `.env.example` 中的 30 当作本项目生产基线。adapter 的默认上限是 3 attempts / 300 秒，
  但 tick 传入单次 callback 的 external cap 默认 60 秒，故每个批次实际最多 3 次 HTTP，
  总 wall time 不超过 `min(60 秒, stage 剩余, tick 剩余)`。adapter 内部重试耗尽后，只有
  `TimeoutError`、429、5xx、connection reset/timeout 等 transport/infrastructure failure 会进入
  `RETRYABLE_FAILED` 并由下一 tick 使用第二次 durable claim；永久 4xx、业务校验和 parser/contract
  error 直接 terminal。两次 durable claims 或累计 300 秒后仍失败会转 terminal，不会无限重试；
  进程在 `CLAIMED` 后硬中断也共用同一个 durable ceiling。同一业务日的 retryable MODEL 即使已不在
  后续缩小的最新快照中，仍会从 durable backlog 精确领取；既有 SUCCESS 不会重新 claim，重试成功后
  才进入正常 planner，已有 crawl/remote intent 仍按 identity 幂等复用。
- POST 之前持久化 immutable intent；超时或进程中断进入 `SUBMIT_UNKNOWN`，后续只能精确对账，
  不自动重发 `/add`。harvest 到 attempts/elapsed 上限进入人工处理。
- RUNNING 任务有 fail-closed 的停滞自愈：tick 会对创建超过 30 分钟且已 harvest 两次以上的
  RUNNING 任务做只读 progress/export 探测并持久化证据（`remote_jobs.recovery_evidence_json`）。
  只有同时满足 harvest 达 8 次、运行超 90 分钟、连续 4 次以上 progress signature 完全静止跨
  60 分钟、且 export 持续返回 `count=0`「未检索到商品价格信息」，才认定为 stalled；旧库缺证据
  一律视为未证明 stalled，继续正常 harvest。认定 stalled 后（仅 `--enable-remote-writes` 的
  tick）对精确 remote identity 执行一次、且仅一次禁用→启用：phase 先持久化再写远端，任何写
  结果未知都先重新精确查询真实状态，绝不盲重发；15 分钟确认窗内未确认保持等待，超窗或身份
  冲突 fail-closed 转 `MANUAL_REQUIRED`。重启确认后任务回归 RUNNING 交回正常 harvest；重启
  30 分钟后若两次以上探测仍静止且无价格，本地进入终态 `RECOVERY_EXHAUSTED`（不伪造远端
  COMPLETED），linked crawl 以 `REMOTE_STALL_RECOVERY_EXHAUSTED` 证据收口为 `NO_RESULT`
  （价格留空），ordinary final 视为可接受终态收敛，`SUBMIT_UNKNOWN`/`MANUAL_REQUIRED`/
  普通 RUNNING 的 blocker 语义不变。该状态在 `status` 的 `remote.recovery_exhausted` 计数。
- SQLite 始终是状态权威。每次命令另外在该业务日的
  `batch_state_snapshots/BATCH_STATE_<time_ns>_<pid>_<n>.md` 写一份不可变的人类可读状态快照；
  `BATCH_STATE.md` 继续保留给既有招商局事件证据和旧读取方，并做 best-effort 兼容刷新。Windows
  上若外部只读工具长期占用固定文件、导致 `os.replace` 返回 `PermissionError/WinError 5`，
  新快照仍可用且不得把已写入 SQLite/remote checkpoint 的命令反转成失败；查看当前 managed 状态时
  优先读最新快照，核对 ADD/启用/提优等历史证据仍读固定 `BATCH_STATE.md`。重叠实例正常返回 0，
  可恢复 pending/defer 返回 0，`FAILED`、人工项或正式产物阻塞返回非零。
- 不要在此调度中加入审批、驳回、通过、保存、提交、推送、任何 SQL，或内建固定 8 站策略之外的本地 crawler 入口；自动本地兜底只能遵守上文 exact URL、最终远端查询成功、原子 claim 和 fail-closed 门禁。

## 每日核对

```powershell
python scripts\incremental_price_audit.py status --business-date 2026-08-11
python scripts\incremental_price_audit.py doctor --business-date 2026-08-11
python -m pytest tests\test_incremental_audit_runtime.py tests\test_incremental_price_audit.py `
  tests\test_incremental_price_audit_finalize.py tests\test_incremental_audit_materializer.py -q
```

关注 `latest_snapshot`、`counts`、`detail`、`model`、`crawl`、`remote`、`final` 和 `next_actions`。
遇到
`SUBMIT_UNKNOWN`、`MANUAL_REQUIRED`、terminal stage、快照不完整、manifest conflict 或认证限制时
暂停并记录；不得靠新 task name、删除 state 或重发写请求“恢复”。

## 客户参考链接预爬扫描器(Issue #9)

- 每 1 分钟一次的旁路 sidecar(`scripts\customer_prewarm_scan.py`,Phase A-D 有界单轮,预算默认 50 秒);
  重入由 `<runtime_root>\customer_prewarm.lock` 非阻塞防重叠,冲突即打印 `SKIPPED_ALREADY_RUNNING` 退出 0。
- 状态落在独立跨日库 `runtime\incremental_price_audit\customer_prewarm.sqlite3`;共享缓存为
  `runtime\incremental_price_audit\shared_crawl_cache.sqlite3`。STEP1 tick 通过 `prewarm_intake` 旁路入队,
  任何异常只计数,绝不改变 tick 结果、trace、审批或收口。
- dry-run 一行用法:`python scripts\customer_prewarm_scan.py --dry-run`(prewarm 状态与缓存写入全部落在
  `--sandbox-root`,默认 `<runtime_root>\prewarm_dry_run`)。dry-run 会先把生产共享缓存以只读快照
  (SQLite `mode=ro` + backup API,零写入生产)复制进 sandbox,当日 daily internet 成功行真实写入 sandbox
  缓存,因此命中/MISS 分布接近部署后真实状态;`--cache-path` 在 dry-run 下指只读快照的**来源**缓存。
- 历史 daily-internet 回填:`--backfill-history N`(N≤90,只读过去 N 天 state 库,幂等可续跑);
  legacy 招商局导出结果的历史导入归 Issue #10 契约,本票不实现。
- 注册脚本 `scripts\register_customer_prewarm_scan_tasks.ps1` 默认只预览;必须显式 `-Apply` 且经用户授权后
  才能注册(1 分钟重复、IgnoreNew、Hidden、Limited)。Issue #9 本身不注册任何计划任务。

### 客户预爬派发生命周期

- `scripts\customer_prewarm_dispatch.py` 的正常 live pass 从 `--budget-seconds` 中预留最多 24 秒（且不超过 60%）给后续生命周期；当天 dispatch lock/lease 完全释放后，才依次运行跨日 identity/priority、跨日 historical harvest/cache sync、当天 harvest。**预留窗口从 dispatch 实际返回时重新起算**，而不是继续使用进程启动时的绝对 deadline；因此某个临近 dispatch 截止点的远端调用即使拖长，也不能把跨日 reconcile/harvest 的保底时间吃成 0。若当天 STEP1 日锁导致 dispatch 返回 `LOCK_BUSY`，本轮仍运行两个历史阶段（它们只申请各自 owning-day lock），但明确不尝试当天 harvest；若是 `SKIPPED_ALREADY_RUNNING`，说明已有另一 dispatch 实例，整轮 lifecycle 继续跳过，防止双实例。正常无超时时，dispatch 预算 + lifecycle reserve 仍等于原 `--budget-seconds`；只有 dispatch 单次调用自身越过其 deadline 时，超出的部分会占用 Scheduler 的 hard-timeout grace。预留至少 20 秒时跨日 reconcile 最多分 10 秒，余量由历史和当天 harvest 平分；小预算按预留时间动态划分。严禁持有当天锁时申请历史日锁，也不得在 reconcile 的历史锁内嵌套调用 harvest 原语。
- A3 重排在远端 POST claim 前先于全局预爬库原子记录 candidate→新 daily intent marker；marker 写入失败绝不 POST。预算不足或 max-jobs 限额在 marker 前判断；明确 `NOT_ATTEMPTED` 后清除 marker。若 POST 已可能发生而进程中断、candidate 尚未完成新 job 绑定，次日 marker 会阻断再次创建，并将该候选标记为需人工核对，最终失败行保留这一不确定性，不能猜成 `NOT_FOUND`。同日未发送 intent 复用原有 daily job；若原组员已命中缓存，只有在 `post_attempted=0`、无远端身份且所有原始预爬 crawl 行仍为未提交状态并与 immutable payload 精确一致时，才在单个事务中退役旧 intent、解绑原 crawl 行，将退出组员的 crawl 标为 `SUPERSEDED`（不残留 `PENDING` 阻塞 STEP1），并为剩余候选重排。任何证据不符转人工，不发送旧大组。已发送 intent 只按精确 crawl/payload 证据重新绑定，不另建任务。
- discovery 只从 `customer_prewarm.sqlite3` 中仍为 `REMOTE_DISPATCHED`/`REMOTE_REUSED`
  的 durable candidate 出发，并按 `(owning_day, remote_job_id)` 去重。执行前在历史 DB 锁内再次验证：
  job 仍为无 identity 且未到 dead threshold 的 `SUBMIT_UNKNOWN`，或带 exact identity 的
  `PRIORITY_PENDING`，且至少一条 crawl 的 `sku_id LIKE 'zhpq-%'`；普通 STEP1 job fail-closed 跳过。历史 harvest 另以同一最新 linkage 筛选所属日 `SUBMITTED`/`RUNNING`/`EXPORTED`，并对已有 SUCCESS 的 terminal job 重试 cache sync；严格命中 nonretryable `PriorityScopeSafetyError` collapsed MANUAL family 的 job 即使尚无 SUCCESS crawl，也进入同一 exact live-proof + existing-harvest 恢复链。历史 DB 缺失不创建。
- 每个历史 job 使用所属日期自己的 `state.sqlite3`、`ProcessLock` 和 durable lease。
  `LOCK_BUSY` 或历史 DB 缺失只计 deferred，零远程调用、零 attempt 变化；每轮最多 4 job，
  durable fairness cursor 从上一轮最后 attempt 后继续，持续 pending 的前四项不会饿死后续 job；单 job 最多 30 秒，
  跨日阶段使用 dispatch 返回后的独立 reserve deadline；因此 dispatch 超时拖长只会消耗 Scheduler grace，不会把跨日阶段饿死。
- `FOUND` 只恢复历史 DB identity 并沿用既有 priority confirm；若当轮未证明 `weight_type=-1`，job 保持
  `PRIORITY_PENDING`，下一轮只重试 priority confirm，不重复 reconcile、submit 或 create。priority 结果写回前再次验证
  candidate linkage；priority 远程写前还须在 lease 更新后最后检查 linkage。最终 linkage 验证通过 `customer_prewarm.sqlite3` 的短 `BEGIN IMMEDIATE` fence
  一直覆盖到历史 DB commit，失配则丢弃结果；job 与 crawl_work 的 `SUBMITTED` 推进为同一事务。
- priority 更新期间 `task_status` 发生变化只记录为远端执行进度，不再作为 scope safety 失败条件；exact task id 集、
  `is_valid` 和提交后 `weight_type=-1` 回读仍是硬门。只有 durable nonretryable
  `PriorityScopeSafetyError`、exact customer-prewarm producer/link/payload identity、live terminal 六计数严格证明
  `total=finished=request count` 且其余计数全为 0 时，生命周期才可对原任务直接 harvest；该恢复路径不 `/add`、不提优、
  不 disable/enable。任一证据缺失、冲突或失败继续保留 `MANUAL_REQUIRED`，但临时 probe 失败下轮仍可重试；同日正常 `SUBMITTED`/`RUNNING`/`EXPORTED` 队列保留每轮 3 个 slot，并用独立 durable cursor 轮转，MANUAL 恢复使用独立每轮 1 个 slot、durable cursor 和预留时间片，既不抢占正常 harvest，也不会因正常 remote call 耗尽 deadline 而永久饥饿。跨日恢复复用完全相同的 proof primitive 和 harvest core。
  只有无诊断的确切 `NOT_FOUND` 消耗 recovery attempt；诊断为不确定的 NOT_FOUND 保留待重试。`MANUAL`/不完整 FOUND 原子记为带 provenance blocker 的 `MANUAL_REQUIRED`，不进入 A3；无 identity 且已 POST 或已有 harvest attempt 的 MANUAL_REQUIRED 也不得自动重建，但仍进入最终失败链接投影。A3 必须先确认 candidate 当前 job id 与最新 producer linkage 完全一致、owning day 是不晚于本轮的规范日期，避免跨日同号误判；重绑时 candidate 与 linkage 同事务提交，POST 前与客户扫描互斥且再次确认候选未变；POST 后绑定失败只按同一 durable intent 的既有远端身份重接，不重发。确切 NOT_FOUND 到 4 次后仍只由既有 A3 创建。历史 harvest 每轮最多 3 job、单 job 90 秒、独立 durable cursor；复用既有 ProcessLock+lease 原语，SUCCESS 按 exact job 镜像 shared cache，所有 scanner/backfill/harvest 通过同一 prewarm-store 同步锁串行，避免同一事件重复写缓存；下轮 candidate 吸收 CACHE_HIT。MANUAL 和确定性缓存同步失败都显式报阻断。`--plan-only` 用 `mode=ro` 统计两个跨日阶段，保持四库零写、零锁、零 adapter 和零远程 I/O。
- 新产生的 `SUBMIT_UNKNOWN` 在持久化和 phase summary 中保留经脱敏的 submit diagnostic；无新事实的 generic
  harvest 不清空原诊断，FOUND 后才按既有状态迁移清除。每次 exact reconcile 将最近状态、脱敏原因、时间和
  recovery attempts 原子写入 recovery evidence；不确定结果不消耗 attempt，确切无诊断 `NOT_FOUND` 才计数。
  Dashboard drawer 展示错误阶段、分类、具体原因、更新时间、实时远端进度、自动对账次数/最近结果和下一步；
  历史诊断确已丢失时明确显示“原始提交诊断不可恢复”，不得猜测。投影层对 Token/Cookie/Authorization/API Key、
  密码、任意 token/secret/API-key/Authorization 后缀字段（含 camel/snake/hyphen 与常见前缀）、JWT、多值 Cookie/Set-Cookie、非 Bearer Authorization、Windows（含空格或无扩展名）与 `/home`、`/tmp` 绝对路径做脱敏；`SUBMIT_UNKNOWN` 在 durable 写边界即脱敏，不向 SQLite 或浏览器新增原始 secret/path。collapsed MANUAL 只有行内通用诊断而无可供看板验证的 durable priority reason 时，显示“提优确认 / 优先级未确认”，不得猜成 `PriorityScopeSafetyError`。

## 启用与回滚门槛

按顺序推进，不跨级：

1. `doctor` 全部 required check 通过，确认 status 无旧的 unknown/manual。
2. 完整工作日只跑 shadow，核对请求量、缓存命中、backlog 和 18:00 的 final pending。
3. 在明确授权下手工跑一次有限 tick；确认 POST intent、提交后退出和后续 harvest。
4. 手工重复执行 finalize，确认 freeze 与正式 Excel 幂等。
5. 先预览注册脚本；未完成生产 canary 前只允许注册 shadow。
6. 生产 canary 已确认远端 identity、priority 和导出契约后，再经确认使用
   `-EnableRemoteWrites -Apply` 注册正式任务。

回滚只禁用增量 Scheduler，不删除 SQLite、Excel、日志或其他历史：

```powershell
powershell -NoProfile -File scripts\unregister_incremental_price_audit_tasks.ps1
powershell -NoProfile -File scripts\unregister_incremental_price_audit_tasks.ps1 -Apply
```

随后按 `STEP_1_PRICE_AUDIT.md` 运行 legacy 全量流程。未完成的增量远端任务仍保留 durable identity；
恢复增量前先 `status` / `doctor` 并完成精确对账，不能因回滚而盲目创建新任务。
