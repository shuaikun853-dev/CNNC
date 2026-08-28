# 第一步:只读实时看板(STEP_1 Dashboard)

> 这是 CNNC STEP_1 只读看板的运行手册。看板通过 FastAPI 提供 `http://127.0.0.1:8765/dashboard/`
> 单页视图,展示当日审计总状态、阶段进展、招商局任务表、近 14 天日历与计划任务状态。
> 仓库提供登录自启的注册/卸载脚本,但两者默认预览,必须显式传 `-Apply` 才会改系统;
> 自启任务只新增/删除 `CNNC-Dashboard-ReadOnly`,不读取、不修改现有审计计划任务。

## 用途与只读红线

- 用途:在生产运行之外,随时用浏览器查看 STEP_1 当日状态,不需要登录任何业务系统。
- **只读红线**:
  - 零写入:服务不写任何文件、不发任何业务写请求。主 Dashboard 状态投影只读取
    `runtime/incremental_price_audit/<业务日>/` 下的 `daily_status.json` 与 `state.sqlite3`;
    SaaS Flow Diagnostic 是唯一额外的只读数据源(见下文专节),使用指定只读数据库账号
    执行后端预定义、参数化、固定 scope 的 SELECT。
  - GET-only:看板路由只接受 GET;POST/PUT/DELETE/PATCH 一律 405。
  - 不触碰现有审计计划任务:自启注册/卸载脚本只操作 `CNNC-Dashboard-ReadOnly` 一个任务名;
    页面 Scheduler 区对现有审计计划任务也只做 PowerShell Get-ScheduledTask/Get-ScheduledTaskInfo 只读查询(10 秒超时、30 秒缓存),查询失败即降级显示"不可用"。
  - 只绑定 `127.0.0.1:8765`,不监听其他网卡,不提供远程访问。

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

## 排障

| 症状 | 含义 | 处理 |
|---|---|---|
| `HTTP 503`(提示"状态读取失败") | 只读投影读取当日状态抛异常 | 稍后重试;确认 `runtime/incremental_price_audit/<业务日>/` 存在且可读;持续失败看服务端日志 |
| 页面顶部出现断线横幅 | 轮询请求连续失败(服务停止或网络断开) | 前端会自动重试,恢复后横幅自动消失;必要时按"手动启动"重启服务 |
| Scheduler 区显示"不可用" | 对现有审计计划任务的 `schtasks` 只读查询失败或超时,已降级 | 不影响页面其余区域;可手动 `schtasks /query` 核对计划任务本身 |
| `HTTP 400`(日期参数) | `?date=` 不是 `YYYY-MM-DD` | 使用 `YYYY-MM-DD` 格式或省略参数(默认上海业务日) |
| 浏览器打不开页面 | 服务未启动或未注册自启 | 按"手动启动"启动,或按"登录自启注册"先预览再 `-Apply`;核对 `http://127.0.0.1:8765/dashboard/` 地址 |
