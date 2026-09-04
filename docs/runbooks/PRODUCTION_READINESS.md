# 生产就绪只读检查（PRODUCTION READINESS）

## 用途

在部署或交接前，用一条命令确认本机生产运行前提全部就绪：操作系统、git 仓库状态、
Python 版本与依赖、时区一致性、计划任务配置、认证/凭据文件、招商局外部工具。
本检查完全只读，不改变任何生产状态，结论分级为 PASS / WARN / FAIL / UNKNOWN，
全部 PASS 才输出 READY。

## 运行命令

```powershell
cd E:\.A中核运维\cnnc-ops-agent
$env:PYTHONIOENCODING='utf-8'

# 人读输出（每项一行，尾部 Overall 结论）
python scripts/production_readiness_check.py

# 机器可读 JSON（UTF-8，ensure_ascii=False；供脚本或 Dashboard 消费）
python scripts/production_readiness_check.py --json

# 可选参数
python scripts/production_readiness_check.py --repo-root <路径> --expected-branch prod/closeout-20260818 --timeout 10
```

## READ-ONLY 承诺清单

- 不注册、修改、删除任何计划任务（只执行 `Get-ScheduledTask` 只读查询）。
- 不连接任何数据库（不读不写 MySQL / SQLite）。
- 不登录、不刷新认证；`data/` 下敏感文件只检查存在性 / 大小 / mtime，
  绝不读取或打印内容、token、cookie。
- 不发起任何网络请求。
- 不写入、不移动、不删除任何文件。

## 检查项

| 检查名 | 内容 | FAIL 条件示例 | WARN 条件示例 | UNKNOWN 条件示例 |
|---|---|---|---|---|
| Operating system | 生产平台 | - | 非 Windows（仅设计/测试环境） | - |
| Git repository | 仓库可用性、分支、脏行 | - | 分支不是 `prod/closeout-20260818`；存在非 .pyc 脏行（仅列前 5 条） | git 不可用 / 查询非零 |
| Python runtime | 版本与依赖包 | Python < 3.11；fastapi/uvicorn/pydantic/yaml/pytest/httpx/openpyxl/pymysql 缺失 | - | - |
| Timezone | 系统时区、UTC+8、上海业务日期 | - | tzutil 非 `China Standard Time`；本地偏移非 +08:00；上海业务日期与本地日期不一致（Scheduler 与 Python 语义分裂风险） | tzutil 不可用 |
| Task scheduler | 任务存在性与配置漂移 | `CNNC-PriceAudit-Incremental` 不存在 | trigger 数/时刻、IgnoreNew、PT32M、runner 脚本任一漂移；LastTaskResult 非 0（267009=从未运行记 INFO）；`CNNC-Dashboard-ReadOnly` 缺失 | 查询超时/异常；拿不到的字段不假装验证过 |
| Auth state | data/ 凭据文件 | auth_state.json 缺失/非 JSON；model.env、detail_request.curl、latest_saas_sameAudit_queryAll.curl 缺失 | auth_state.json 超过 24 小时未刷新；cnnc_auth_ledger.jsonl 缺失 | - |
| External tools | 外部工具与运行时目录 | 招商局外部工具（`EXTERNAL_TOOL_PATH` 常量指向的文件）缺失；powershell/git 不在 PATH | runtime/incremental_price_audit 缺失（首跑可接受） | 常量定义模块不可导入 |

注意：`.pyc` 脏行属运行稳态（pytest 重新编译），不算真实脏，备注为
"仅 .pyc 假脏(运行稳态)"。

## 状态分级与退出码

- 状态严重度：`PASS < WARN < UNKNOWN < FAIL`；`worst_status` 汇报当前最差项。
- `Overall: READY` 当且仅当全部 PASS，否则 `NOT READY`。
- 退出码：`0` = READY；`1` = NOT READY（含 WARN/FAIL/UNKNOWN）；`2` = 程序意外错误。
- UNKNOWN 表示"无法验证"（工具缺失、查询失败、字段拿不到），与 FAIL（明确不合格）
  严格区分；报告绝不把拿不到的字段当作已验证。

## Dashboard 复用规划

服务层 `app/services/production_readiness.py` 的
`build_readiness_report(...).to_dict()` 已输出 JSON 可序列化结构，CLI `--json`
即消费该结构。未来 Dashboard 可直接加一个只读端点（如 `GET /api/readiness`）
复用同一服务层，建议加 60 秒缓存，避免每次页面请求都真实查询调度器。

## 时区结论摘要

- Task Scheduler = 本地钟表：`register_incremental_price_audit_tasks.ps1` 注册的
  09:00/17:00/18:00/20:35 触发时刻按本机时区解释，因此期望系统时区为
  `China Standard Time`。
- Python = Asia/Shanghai：业务日期权威来自
  `app/services/incremental_audit_runtime.py` 的 `business_date_in_shanghai()`
  （显式 Asia/Shanghai）。
- SQLite = UTC：审计存储层按 UTC 时间戳记录。
- Dashboard 前端 = 显式上海时区展示。

已知漂移点（本任务只报告不修改）：
`app/services/legacy_zhaoshang_adapter.py:1045` 存在 naive 本地时间
（`datetime.now()`）与 +8 规整 marker 直接比较的潜在漂移。最小修复方案：该处
改用上海 aware now（与 `business_date_in_shanghai` 同源的 Asia/Shanghai 时钟），
再与 marker 比较；修复前若本机时区偏离 +8，优先级窗口上界可能被放大或缩小。
