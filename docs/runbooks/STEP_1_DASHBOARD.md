# 第一步:实时看板与本机受控操作台(STEP_1 Dashboard)

> 这是 CNNC STEP_1 看板的运行手册。看板通过 FastAPI 提供 `http://127.0.0.1:8765/dashboard/`
> 单页视图,展示当日审计总状态、阶段进展、招商局任务表(含远端实时子任务进度)、近 14 天日历
> 与计划任务状态;并提供**仅本机、带严格门禁、可审计**的人工操作(立即收口/强制收口/重新导出/重建任务)。
> 仓库提供登录自启的注册/卸载脚本,但两者默认预览,必须显式传 `-Apply` 才会改系统;
> 自启任务只新增/删除 `CNNC-Dashboard-ReadOnly`,不读取、不修改现有审计计划任务。

## 用途与读写红线(2026-08-28 操作台升级后)

- 用途:在生产运行之外,随时用浏览器查看 STEP_1 当日状态,不需要登录任何业务系统;
  出现"任务卡住 0/18、爬虫修好后需要重新导出"等情况时,直接在页面完成受控人工操作。
- **读红线(不变)**:
  - 主 Dashboard 状态投影只读取 `runtime/incremental_price_audit/<业务日>/` 下的
    `daily_status.json` 与 `state.sqlite3`;SaaS Flow Diagnostic 使用指定只读数据库账号
    执行后端预定义、参数化、固定 scope 的 SELECT。
  - 招商局实时子任务进度(queryTaskProgress)是只读远端观测:服务器按 durable
    task_name(=批次号)查询,带缓存/限流/single-flight/超时;查询结果**不写回**
    SQLite 业务状态,单任务查询失败只降级该行显示,绝不拖垮 `/api/summary`。
  - 不触碰现有审计计划任务:自启注册/卸载脚本只操作 `CNNC-Dashboard-ReadOnly` 一个任务名;
    页面 Scheduler 区对现有审计计划任务也只做 PowerShell Get-ScheduledTask/Get-ScheduledTaskInfo 只读查询(10 秒超时、30 秒缓存),查询失败即降级显示"不可用"。
  - 只绑定 `127.0.0.1:8765`,不监听其他网卡,不提供远程访问。
- **写边界(新增,仅本机受控 operator actions)**:
  - Dashboard 主状态读取仍为只读;**仅**以下明确 operator actions 允许本机受控写:
    manual closeout(立即收口)、bounded force-closeout(强制收口)、exact re-export(重新导出)、explicit rebuild(重建任务)。
  - **页面轮询本身不会产生任何业务写**;所有业务写都要求用户在页面上明确点击按钮。
  - POST 路由全部要求:Host/Origin 本机校验 + `X-Dashboard-Action-Token`(进程启动时
    生成,页面经 `/dashboard/api/operator-context` 获取)+ 严格 JSON schema
    (不接受客户端提交任何 taskName/batchNumber/路径/远端身份;服务器一律从 durable
    state 重新取权威值)。
  - 写操作经现有 CLI 子进程执行(`closeout` / `force-finalize` /
    `reexport-existing-remote` / `rebuild-remote-task`),完全复用既有 ProcessLock、durable lease、exact identity、
    immutable payload、fail-closed 语义;每次操作落审计文件
    `<业务日>/dashboard_actions.jsonl`。
  - 重新导出绝不创建新招商局任务;重建必须先安全停用旧任务(确认不会双跑)并生成
    新的 taskName==batchNumber(≠旧值),eligible 采集行原子迁移,旧任务转 SUPERSEDED
    保留全部证据、不再阻塞 finalize。

## 手动启动

在仓库根目录运行:

```powershell
cd E:\.A中核运维\cnnc-dashboard-dev
E:\Python314\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

然后浏览器访问 `http://127.0.0.1:8765/dashboard/`。停止服务用 `Ctrl+C`。

## 登录自启注册(先预览,后 -Apply)

```powershell
cd E:\.A中核运维\cnnc-dashboard-dev
# 第一步:预览(默认,不做任何更改,打印 JSON 与"预览模式"提示)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_dashboard_autostart.ps1
# 第二步:确认预览内容无误后,显式 -Apply 才真正注册
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_dashboard_autostart.ps1 -Apply
```

- 任务名固定 `CNNC-Dashboard-ReadOnly`;触发器为登录时(当前用户)。
- 动作:隐藏窗口,在仓库根目录以 `E:\Python314\python.exe -m uvicorn app.main:app
  --host 127.0.0.1 --port 8765` 启动只读服务。
- 可选参数:`-RepoRoot`(默认脚本上级目录)、`-PythonExe`(默认 `E:\Python314\python.exe`)、
  `-Port`(默认 `8765`)。
- 注册经 `Register-ScheduledTask` 显式设置长期可用性(2026-08-27 Reliability Fix;同日
  Final Quote Fix 简化 Action 载荷并显式当前用户 Interactive Principal):
  无执行时限(ExecutionTimeLimit=PT0S,覆盖默认 72 小时)、电池供电允许启动且不中断
  (AllowStartIfOnBatteries/DontStopIfGoingOnBatteries)、多实例 IgnoreNew、
  当 Task Scheduler 将该任务判定为失败时最多自动重启 3 次、每次间隔 1 分钟。
- 注册后可用 `Export-ScheduledTask -TaskName CNNC-Dashboard-ReadOnly`(或
  `schtasks /query /tn CNNC-Dashboard-ReadOnly /xml`)核对动作与 settings;该注册不影响现有审计计划任务。

## 卸载(先预览,后 -Apply)

```powershell
cd E:\.A中核运维\cnnc-dashboard-dev
# 预览:打印将执行的两条命令,不做任何更改
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\unregister_dashboard_autostart.ps1
# 确认后显式 -Apply:先 schtasks /End(任务未在运行时失败属预期,不阻塞),后 schtasks /Delete /F
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\unregister_dashboard_autostart.ps1 -Apply
```

`/Delete` 失败(例如任务本就不存在)时脚本以非零退出码报错。卸载同样不影响现有审计计划任务。

## 可用性边界(V1.3,2026-08-27 Reliability Fix 后)

- 登录自启 + 受限自愈:注册时显式设置无执行时限、电池可启、当 Task Scheduler 将该任务
  判定为失败时最多自动重启 3 次(间隔 1 分钟)——服务崩溃通常可自动恢复。
- 仍**不是无限 watchdog**:自动重启额度(3 次)用尽、服务停止后不再被 Task Scheduler
  判定为失败、或长时间无人登录时,页面服务不会自行恢复,需重新登录或按"手动启动"重启
  (页面打不开只表示看板服务不在,不代表审计本身异常)。
- 演进记录:V1 原为"登录自启一次、无任何自愈"(spec §8.2 ACCEPTED_RISK,2026-08-27
  Review Fix Round 1 裁定);Reliability Fix 以 `Register-ScheduledTask` 显式 settings
  收窄该风险(见"登录自启注册"节),仍保留预览 → 显式 `-Apply` 门控。

## 数据来源与绑定

| 项目 | 说明 |
|---|---|
| 监听地址 | 仅 `127.0.0.1:8765`(本机回环) |
| 状态来源 | 主看板:`runtime/incremental_price_audit/<业务日>/daily_status.json`、`state.sqlite3`(只读);SaaS Flow 面板:生产 DB 只读诊断(见"SaaS 首次采集链路"节) |
| Scheduler 区 | 对现有审计计划任务做 PowerShell Get-ScheduledTask/Get-ScheduledTaskInfo 只读查询(10 秒超时、30 秒缓存) |
| 刷新方式 | 前端按指纹条件轮询(ETag/304);页面不可见时暂停轮询 |

## SaaS 首次采集链路(生产 DB 只读诊断)

- **这是什么**:一级可见的折叠面板(`SaaS 首次采集链路`,默认收起、展开才显示阶段树),回答"18:00 前客户侧持续新增、但 SaaS 页面数量不涨时,新增数据主要停在哪个阶段"。页面顶部"客户/SaaS"计数只来自权威增长观察;SQL 商品池计数单独标注为"当日商品池记录",不冒充权威客户数。
- **只读范围**:仅通过指定只读数据库账号(`CNNC_DIAG_DB_*`)执行后端预定义、参数化 SELECT(`app/services/saas_flow_diagnostic.py` 的 SQL registry);scope 永久固定 `tenant_id=188605`、`commodity_pool_id=222`,不接受浏览器/API 提交的任何 SQL、tenant、pool、schema、表名;无任何写操作、无 priority 调整、无审批/驳回/重试/建任务入口。诊断链以 `IS_SYNC_STANDARD=0` → `crawl_status`(1=爬虫未回调走 task/分控链,2=已回调走 search/model/审批链)为准,字段语义来自 `docs/sql_ops` KB。
- **18:00 前增长分叉**:增长判断只复用当日 `state.sqlite3` 中已持久化的权威观察序列(`snapshots.observed_at` + `evidence_json` 的 customer/saas 计数),不额外调用客户/SaaS 业务 API;上海时间 18:00 后不再评估。同一业务日连续一个区间"客户增、SaaS 不增"为 `增长落后观察`,连续两个及以上为 `持续增长落后`;触发时面板并列展示各阶段分布,不会仅凭计数差异断言"SaaS 丢数据"。
- **缓存与限流**:后端 ≥60 秒缓存(CNNC_DIAG_CACHE_SECONDS,下限 60);"刷新 SaaS 链路"按钮触发受限强制读取,间隔 <30 秒(CNNC_DIAG_FRESH_MIN_INTERVAL_SECONDS,下限 30,env 配更低被钳制)按限流返回缓存;强制刷新另有服务端 single-flight,并发刷新只触发一次生产查询。前端 SaaS Flow 有独立 ~60 秒轮询周期(visible 轮询/hidden 停止/回前台补刷),不依赖 summary 的 200/304 生命周期,也绝不形成 5 秒级生产 DB 轮询。查询均带 connect/read 超时、当日固定窗口、有界结果集;核心查询失败时相关计数显示"不可用",不冒充业务 0。
- **降级**:`CNNC_DIAG_DB_*` 未配置时面板显示"数据库诊断未配置"(增长判断仍可用);连接失败/超时/查询失败只降级本面板(整体 unavailable 或部分查询 partial),`/dashboard/api/summary` 与页面其余区域完全不受影响。DB 凭据只从进程环境变量或 ignored 的 `data/CNNC_DIAG_DB.env` 读取,不进 Git、日志或 API 响应。

## 本机人工操作台(2026-08-28)

招商局任务 Drawer 与最终收口卡提供五个受控操作。所有操作只在用户点击后发生;
按钮按任务状态启用;每次操作的启动/结果都写入 `<业务日>/dashboard_actions.jsonl`。

| 操作 | 入口 | 语义 | 关键规则 |
|---|---|---|---|
| 刷新任务进度 | 任务 Drawer | 对该任务 exact batchNumber 执行一次受限 queryTaskProgress | 只读;同任务 10 秒限流;结果不写业务状态 |
| 重新导出 | 任务 Drawer | 对当前已有任务再次执行 wait/export/harvest | 绝不创建新任务;执行前强制 progress 预检(TERMINAL 立即导出/ACTIVE 有界探测并如实提示/UNKNOWN 拒绝并返回 `REMOTE_PROGRESS_UNKNOWN`);此前导出 0 条不阻止再次执行。普通 `MANUAL_REQUIRED` 仍 fail closed;历史版本若因 `PriorityScopeSafetyError` 产生非重试型 `PRIORITY_UNCONFIRMED`，并被压成 `MANUAL_REQUIRED + error_text=highest priority was not confirmed + result_json NULL/空`，只有 durable CREATED/ENABLED/PRIORITY_UNCONFIRMED 三者精确同一 remote identity、该 unconfirmed 事件 `reason=PriorityScopeSafetyError && retryable=false`，且 live probe 已证明 `REMOTE_TERMINAL` 时，才允许对原任务执行一次 wait-export-only 收割;该例外不证明 priority 成功、不允许 ACTIVE、不调用 create/prioritize/stop |
| 重建任务 | 任务 Drawer(二次确认) | 停用旧任务→迁移未完成采集→创建 replacement | 新 taskName==新 batchNumber(自动生成,≠旧值,`cnnc_current_opt_<商城>_<14位数字>` 形态);旧任务 ACTIVE 时必须先 disable 并确认(否则 `OLD_TASK_STILL_ACTIVE`/`OLD_TASK_STOP_UNCONFIRMED`);SUCCESS/终态采集行绝不迁移;旧任务转 SUPERSEDED 不再阻塞 finalize,证据全部保留;同一动作幂等,绝不双发 create |
| 立即收口 | 最终收口卡 | 正常生产 closeout(非 force-finalize) | 完全复用 `run_runtime_command("closeout", invocation="manual")`:ProcessLock 冲突→`ALREADY_RUNNING`,已出表→`ALREADY_FINALIZED`,阻塞未清→`BLOCKED`(绝不绕过 blocker);页面显示"收口中…"并轮询结果,不阻塞 HTTP |
| 强制收口 | 最终收口卡(二次确认) | 按冻结快照生成正式最终表,接受不超过 20 条最终业务行缺少页面价格 | 页面与执行前都按同一 final projection 重算 `missing_page_price_rows`:冻结业务行与当日 customer-prewarm 失败行先按 `documentId+互联网链接` 投影/去重,已有 frozen identity 原位标记失败,只有缺席 identity 才追加;价格为 0 不算缺价,NO_RESULT/未完成/人工采集行算缺价。请求携带用户确认时看到的精确数量,数量变化或 >20 即拒绝。只放宽采集/远端未决状态,Detail、模型、客户价和无效价格证据仍阻塞;不新建、不重试、不修改招商局任务,原 durable 状态完整保留 |

最终收口卡的“未爬到价格”是**同一状态生成最终表后**无有效页面价格的业务行数,由
`frozen_missing_page_price_rows` 与 day-scoped failed-link projection 统一生成;已 finalized 日优先读取
finalize 时持久化的 immutable metrics,旧批次仅在 workbook/manifest 哈希一致时从最终表回读,绝不随 mutable prewarm 状态漂移。
新批次的 `FINALIZED`、manifest hash 与最终缺价分项及 force metrics 同一 SQLite transaction 提交；
物化前私有 intent 固定最终指标；提交前 workbook/manifest 已落盘但 DB 未 finalized 时，
校验后按首次物化的指标重试（即使晚到采集改变当前计数）。输入变化导致 manifest 冲突时停下人工核对，
不覆盖正式表。文件与 DB 不具备跨系统 ACID 原子性。
“底层采集工作项”来自 `crawl_work` 状态,可能因历史轮次或重复采集显著大于业务缺价行数,两者不得混用。查看历史批次时用页头业务日期选择器;页面以 `?date=YYYY-MM-DD` 将 summary、操作记录和 POST 目标锁到同一业务日。

结果语义(重新导出):`REEXPORT_COMPLETED`(取得有效结果,N 条)/`REEXPORT_COMPLETED_NO_RESULT`
(任务已结束但导出 0 条,可稍后再点)/`REEXPORT_STILL_RUNNING`(任务仍在执行,本次未取得最终导出)/
`REMOTE_PROGRESS_UNKNOWN` 等错误码(未执行导出,页面给出明确类别)。

常见错误码:`TASK_BATCH_CONFLICT`(任务名称/批次号在当日存在重复,无法安全确定远端任务)、
`IDENTITY_MISMATCH`/`INVALID_REMOTE_STATE`(durable 身份与请求不符或证据不完整)、
`STALE_ACTION`(任务状态在打开页面后已变化,刷新后重试)、`ACTION_IN_FLIGHT`(同类操作进行中)、
`LOCK_CONFLICT`(生产 writer 锁被占)、`REBUILD_CONFIRM_REQUIRED`(重建必须二次确认)。

CLI 等价命令(与页面按钮走同一实现,供脚本/应急):
`python scripts/incremental_price_audit.py reexport-existing-remote --remote-job-id N --remote-identity "mall|id|number"`
`python scripts/incremental_price_audit.py rebuild-remote-task --remote-job-id N --remote-identity "mall|id|number"`
`python scripts/incremental_price_audit.py force-finalize --max-incomplete 20 --accept-unresolved-crawl --expected-missing-page-price-rows N`

## 客户预热面板(Issue #13,2026-09-14)

- 页面:`/dashboard/prewarm`,独立的只读投影页(主看板之外)。按业务日期取数
  (`?date=YYYY-MM-DD`,默认上海业务日),支持商城/SKU 状态等过滤;三库
  (customer_prewarm、共享爬取缓存、当日 `state.sqlite3`)一律 `mode=ro` 只读。
- **GET 零业务写契约**:summary/links/detail 等 GET 绝不修改三个库的内容与哈希;
  WAL 库的只读连接可能物化瞬态 `-shm/-wal` 读辅助文件(SQLite 读端产物,
  同 `customer_prewarm_discovery` 记录的现象),不是业务写、不是内容变化。
- 受控 operator action(单 URL 手动重爬):`POST /dashboard/api/prewarm/links/{id}/recrawl`,
  门禁同主操作台(本机 Host/Origin + 操作令牌),只接受精确候选 id 与期望
  URL/状态(防陈旧)。按 URL 路由:史泰博 → 本机直接爬取;招商 → 派发单个
  招商任务并提优 priority -1。返回 `202 + action_id`,以
  `GET /dashboard/api/prewarm/recrawl-actions/{action_id}` 轮询生命周期:
  `WAITING_REMOTE`(远端在途)/`PRIORITY_PENDING`(提优未证实,需人工重试)/
  `SUCCEEDED`/`FAILED`。`SUCCEEDED` 仅当共享缓存出现 succeeded_at 晚于动作
  开始、且 payload 的 (business_date, remote_job_id) 与本动作派发任务精确
  一致的新 current 行(producer 仅诊断展示,跨日 REUSED 的 auto-pass producer
  行同样算);远端任务终态 FAILED → 诚实 `FAILED`;派发本身绝不是成功。
  同一 URL 非终态期间二次发起 → 409(`ACTION_IN_FLIGHT`)。跨日 REUSED
  (复用更早业务日的未完成任务)时,动作 worker 即该任务的有界 lifecycle
  owner——有界循环执行 prewarm 专属的既有生命周期收割原语
  `harvest_existing_customer_prewarm_job`(共享 harvest core,溯源取自
  durable prewarm payload 的 mapping source;零二次 POST,只写原业务日
  state DB;普通 STEP1 手动重导出对这类任务保持 fail-closed——
  `NO_SNAPSHOT_SCOPE`)+ 幂等镜像进共享缓存;预算耗尽保持
  `WAITING_REMOTE` 并释放同 URL 锁,操作员再次点击即重新探测并重启该循环。

## 排障

| 症状 | 含义 | 处理 |
|---|---|---|
| `HTTP 503`(提示"状态读取失败") | 只读投影读取当日状态抛异常 | 稍后重试;确认 `runtime/incremental_price_audit/<业务日>/` 存在且可读;持续失败看服务端日志 |
| 页面顶部出现断线横幅 | 轮询请求连续失败(服务停止或网络断开) | 前端会自动重试,恢复后横幅自动消失;必要时按"手动启动"重启服务 |
| Scheduler 区显示"不可用" | 对现有审计计划任务的 `schtasks` 只读查询失败或超时,已降级 | 不影响页面其余区域;可手动 `schtasks /query` 核对计划任务本身 |
| `HTTP 400`(日期参数) | `?date=` 不是 `YYYY-MM-DD` | 使用 `YYYY-MM-DD` 格式或省略参数(默认上海业务日) |
| 浏览器打不开页面 | 服务未启动或未注册自启 | 按"手动启动"启动,或按"登录自启注册"先预览再 `-Apply`;核对 `http://127.0.0.1:8765/dashboard/` 地址 |
| 子任务列显示"远端进度暂不可用"/"含 N 条采集" | 招商局实时进度查询失败(登录过期/超时/工具缺失)或从未成功过 | 只影响该行显示;点【刷新任务进度】重试;持续失败核对招商局登录与外部工具;本地"含 N 条采集"是绑定数,不是远端完成数 |
| 操作按钮全部禁用并提示"操作台不可用" | `/dashboard/api/operator-context` 获取令牌失败 | 刷新页面;确认服务来自本仓库(令牌随服务进程生成) |
| 操作返回 403(INVALID_ACTION_TOKEN) | 页面令牌与服务进程不一致(服务重启过) | 刷新页面重新获取令牌 |
| 操作返回 409(STALE_ACTION) | 打开页面期间任务状态已被调度器推进 | 刷新页面后重新点击 |
