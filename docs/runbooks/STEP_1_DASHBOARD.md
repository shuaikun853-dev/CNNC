# 第一步:只读实时看板(STEP_1 Dashboard)

> 这是 CNNC STEP_1 只读看板的运行手册。看板通过 FastAPI 提供 `http://127.0.0.1:8765/dashboard/`
> 单页视图,展示当日审计总状态、阶段进展、招商局任务表、近 14 天日历与计划任务状态。
> 仓库提供登录自启的注册/卸载脚本,但两者默认预览,必须显式传 `-Apply` 才会改系统;
> 自启任务只新增/删除 `CNNC-Dashboard-ReadOnly`,不读取、不修改现有审计计划任务。

## 用途与只读红线

- 用途:在生产运行之外,随时用浏览器查看 STEP_1 当日状态,不需要登录任何业务系统。
- **只读红线**:
  - 零写入:服务不写任何文件、不发任何业务写请求;只读取
    `runtime/incremental_price_audit/<业务日>/` 下的 `daily_status.json` 与 `state.sqlite3`。
  - GET-only:看板路由只接受 GET;POST/PUT/DELETE/PATCH 一律 405。
  - 不触碰现有审计计划任务:自启注册/卸载脚本只操作 `CNNC-Dashboard-ReadOnly` 一个任务名;
    页面 Scheduler 区对现有审计计划任务也只做只读 `schtasks /query`,查询失败即降级显示"不可用"。
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
- 注册后用 `schtasks /query /tn CNNC-Dashboard-ReadOnly /xml`(或 `/v /fo list`)核对动作里
  `-Command "Set-Location ..."` 的内层双引号完整(脚本已按 PS 5.1 原生传参规则转义,此处为复核);该注册不影响现有审计计划任务。

## 卸载(先预览,后 -Apply)

```powershell
cd E:\.A中核运维\cnnc-dashboard-dev
# 预览:打印将执行的两条命令,不做任何更改
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\unregister_dashboard_autostart.ps1
# 确认后显式 -Apply:先 schtasks /End(任务未在运行时失败属预期,不阻塞),后 schtasks /Delete /F
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\unregister_dashboard_autostart.ps1 -Apply
```

`/Delete` 失败(例如任务本就不存在)时脚本以非零退出码报错。卸载同样不影响现有审计计划任务。

## 数据来源与绑定

| 项目 | 说明 |
|---|---|
| 监听地址 | 仅 `127.0.0.1:8765`(本机回环) |
| 状态来源 | `runtime/incremental_price_audit/<业务日>/daily_status.json`、`state.sqlite3`(只读) |
| Scheduler 区 | 对现有审计计划任务做只读 `schtasks /query`,10 秒超时 |
| 刷新方式 | 前端按指纹条件轮询(ETag/304);页面不可见时暂停轮询 |

## 排障

| 症状 | 含义 | 处理 |
|---|---|---|
| `HTTP 503`(提示"状态读取失败") | 只读投影读取当日状态抛异常 | 稍后重试;确认 `runtime/incremental_price_audit/<业务日>/` 存在且可读;持续失败看服务端日志 |
| 页面顶部出现断线横幅 | 轮询请求连续失败(服务停止或网络断开) | 前端会自动重试,恢复后横幅自动消失;必要时按"手动启动"重启服务 |
| Scheduler 区显示"不可用" | 对现有审计计划任务的 `schtasks` 只读查询失败或超时,已降级 | 不影响页面其余区域;可手动 `schtasks /query` 核对计划任务本身 |
| `HTTP 400`(日期参数) | `?date=` 不是 `YYYY-MM-DD` | 使用 `YYYY-MM-DD` 格式或省略参数(默认上海业务日) |
| 浏览器打不开页面 | 服务未启动或未注册自启 | 按"手动启动"启动,或按"登录自启注册"先预览再 `-Apply`;核对 `http://127.0.0.1:8765/dashboard/` 地址 |
