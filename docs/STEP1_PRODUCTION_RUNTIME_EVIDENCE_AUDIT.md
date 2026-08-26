# STEP_1 生产运行数据只读取证报告（Production Runtime Evidence Audit）

> 审计日期：2026-08-26（ Asia/Shanghai ）。
> 审计人：自动化只读取证流程（本任务 PHASE 1）。
> 纪律声明：全程零生产写入。生产目录 `E:\.A中核运维\cnnc-ops-agent` 仅执行 Git 只读命令与文件只读检查；
> 生产 SQLite 通过 `mode=ro` URI + `PRAGMA query_only=ON` + SQLite Online Backup API 复制到仓外审计目录后分析；
> 未使用 `immutable=1`；未执行任何 STEP_1 业务命令（tick/closeout/finalize/recover-* 等全部未运行）；
> 未调用任何真实 SaaS/中核/招商局接口；未修改 Windows 计划任务（仅 Get-*/wevtutil gl 只读查询）。
>
> **PRODUCTION_WRITES_PERFORMED = 0**

## 1. Audit scope

针对 `STEP1_STABILITY_AUDIT.md`（2026-08-25，`E:\.A中核运维\audit\`）中的 P1-1 / P1-2 / H-1..H-5 / P2-1 / P2-8，
对真实生产运行时数据做离线取证：

- 生产 runtime：`E:\.A中核运维\cnnc-ops-agent\runtime\incremental_price_audit\`
- Windows 计划任务：`CNNC-PriceAudit-Incremental`（只读查询）
- Windows 事件日志：`Microsoft-Windows-TaskScheduler/Operational`（只读查询）
- 生产源码：仅只读核对（源码推导与运行时证据在下文严格分开表述）

## 2. Current production SHA

```text
BASE_BRANCH            = origin/prod/closeout-20260818
CURRENT_PROD_SHA       = 6efdc79c5bb10605f05f2931066a833f986952e5
EXPECTED_PROD_SHA      = 6efdc79c5bb10605f05f2931066a833f986952e5
PRODUCTION_BASE_MOVED  = NO
PRODUCTION_WORKTREE_STATUS = clean（git status --short 无输出；git fetch origin 后复查）
```

## 3. Inspected business dates

runtime 目录下全部业务日：2026-08-11 … 2026-08-26（其中 08-15/16/22/23 为周末或无数据日，无目录）。
重点取证窗口：

| 业务日 | state.sqlite3 | wal/shm | saas_audit_final_*.xlsx | manifest | daily_status.json | BATCH_STATE.md | daily_runs.state |
|---|---|---|---|---|---|---|---|
| 2026-08-11 | 不存在（目录仅有 daily_status.json） | — | 不存在 | — | 是 | 否 | 无 DB |
| 2026-08-12 | 是 | 是 | 是 | 是 | 是 | 是 | FINALIZED |
| 2026-08-13 | 是 | 是 | 是 | 是 | 是 | 是 | FINALIZED |
| 2026-08-14 | 是 | 是 | 是 | 是 | 是 | 是 | FINALIZED |
| 2026-08-17 | 是 | 是 | 是 | 是 | 是 | 是 | FINALIZED |
| 2026-08-18 | 是 | 是 | 是 | 是 | 是 | 是 | FINALIZED |
| 2026-08-19 | 是 | 是 | 是 | 是 | 是 | 是 | FINALIZING（daily_status 2026-08-19T12:30:01Z，最终表次日 10:06 产出） |
| 2026-08-20 | 是 | 是 | 是（另有 2026-08-20_superseded_final5_1907 变体目录） | 是 | 是 | 是 | FINALIZED |
| 2026-08-21 | 是 | 是 | 是 | 是 | 是 | 是 | FINALIZED（08-23 16:23 补出表） |
| 2026-08-24 | 是 | 是 | 是 | 是 | 是 | 是 | FINALIZED |
| 2026-08-25（最近完整已结束业务日） | 是 | 是 | 是 | 是 | 是 | 是 | FINALIZED |
| 2026-08-26（进行中） | 是 | 是 | 否（未到 closeout） | 否 | 是 | 是 | OPEN（取证时 12:45） |

`scheduler_logs/` 存在，按日一个 `YYYY-MM-DD.log`（2026-08-13 起；08-11/08-12 无日志文件）。

## 4. Production DB copy method（生产数据库复制方法）

- 审计副本目录：`E:\.A中核运维\cnnc-ops-agent-worktrees\_audit_tmp\`（仓外，非生产 runtime）
- 方法（对每个业务日）：
  1. 源连接：`sqlite3.connect("file:<生产 state.sqlite3>?mode=ro", uri=True)`
  2. 源连接执行 `PRAGMA query_only=ON;`、`PRAGMA busy_timeout=5000;`
  3. `source.backup(dest)`（SQLite Online Backup API，WAL 中未 checkpoint 数据一并进入副本）
  4. 之后全部 SQL 仅在副本上执行（副本同样以 mode=ro 打开）
- 未对生产 `state.sqlite3` / `-wal` / `-shm` 执行任何写、删、改名、覆盖。
- 未使用 `immutable=1`。

## 5. Scheduler evidence（H-4 / H-5）

### 5.1 计划任务只读查询结果（Get-ScheduledTask / Get-ScheduledTaskInfo）

```text
TaskName             = CNNC-PriceAudit-Incremental（全机唯一 CNNC 相关任务）
State                = Ready
MultipleInstances    = IgnoreNew
StartWhenAvailable   = False          ← 关机/错过触发不补跑（与审计 P2-7b 风险一致）
ExecutionTimeLimit   = PT32M
Principal            = UserId 13013, LogonType Interactive, RunLevel Limited
Triggers（4 个 WeeklyTrigger，StartBoundary 均为 2026-08-24）：
  09:00 每 15min 持续 7h46m（09:00–16:46）
  17:00 每 5min  持续 56m  （17:00–17:56）
  18:00 每 5min  持续 2h1m （18:00–20:01）
  20:35 单次（safety closeout）
Action               = powershell.exe -File scripts\run_incremental_price_audit_scheduled.ps1
                       -RepoRoot E:\.A中核运维\cnnc-ops-agent -PythonExe E:\Python314\python.exe
                       -BudgetSeconds 720 -CloseoutBudgetSeconds 1680 -HardTimeoutMinutes 32
                       -ZhaoshangBackend legacy -EnableRemoteWrites
LastRunTime          = 2026-08-26 12:45:45
LastTaskResult       = 0
NextRunTime          = 2026-08-26 13:00:00
NumberOfMissedRuns   = 0
```

### 5.2 Windows 事件日志

`wevtutil gl Microsoft-Windows-TaskScheduler/Operational` → **enabled: false**。
TaskScheduler 操作日志未启用 → 「Task Scheduler 有执行但 scheduler_log 没有」「LastTaskResult 非 0 无日志」这类
对照无法用 Windows 事件证明。**INSUFFICIENT_RUNTIME_EVIDENCE（事件日志层）**。

### 5.3 scheduler_logs 时间线核对（08-17 … 08-26）

| 日 | RUN/END | 最后日志时刻 | 20:35 closeout | 备注 |
|---|---|---|---|---|
| 08-17 | 20/20 | 20:00:49 | 无（当日尚无 20:35 触发器，StartBoundary=08-24） | 14 次 phase exit≠0（tick exit=2 等，均有 STOP reason=phase_failed，非静默） |
| 08-18 | 20/20 | 20:00:06 | 无（同上） | 14 次 exit≠0（可见失败） |
| 08-19 | 29/29 | 17:36:18 | 无（同上；当日 18:00 后无调度记录，daily_status 20:30 由非调度写入） | 4 次 exit≠0 |
| 08-20 | 39/39 | 20:11:35 | 无（同上） | 全部 exit 0 |
| 08-21 | 45/45 | 20:01:14 | 无（同上） | 29 次 exit≠0 |
| 08-24 | 39/38 | 20:35:03 | **20:35:01 RUN → 20:35:03 END exit=0** | 1 个 RUN 无 END：13:45:00 harvest-only 触发被 IgnoreNew 跳过（前实例运行中，属策略性跳过，非静默失败） |
| 08-25 | 60/60 | 20:35:04 | **20:35:01 RUN → 20:35:04 END exit=0** | 18:42–19:10 closeout 5 次 exit=2（其中 18:42 一次耗时 726.3s 超 720s tick 预算口径），19:15 起 exit=0 |
| 08-26（进行中） | 16/16 | 12:45:04 | 未到 | 全部 exit 0 |

final.xlsx 落盘时刻常在调度 RUN 窗口之外（08-17 21:00:44、08-24 20:20:12、08-25 20:34:50 等），
且全机只有这一个计划任务 → 这些物化来自**人工命令**（与 08-19~08-21 人工补救的运维史吻合），非调度静默行为。

### 5.4 结论（H-4 / H-5）

- **H4_SAFETY_CLOSEOUT_MISS：NOT_OBSERVED（in inspected window）**。20:35 触发器仅自 08-24 存在（StartBoundary），
  仅有的两个触发日（08-24、08-25）均实际执行且 exit_code=0。08-17..08-21 无 20:35 行是旧调度形态，不是漏跑。
  StartWhenAvailable=False + 事件日志禁用 → 「机器恰好错过 20:35」的未来风险无法从事件层排除（定性为风险因素，不是已发生事实）。
- **H5_SCHEDULER_EARLY_FAILURE：NOT_OBSERVED（in inspected window）/ 事件层 INSUFFICIENT_RUNTIME_EVIDENCE**。
  日志内 RUN:START:END 全配对（唯一例外为 08-24 13:45 IgnoreNew 策略性跳过）；LastTaskResult=0、MissedRuns=0；
  所有非零退出都有 `STOP reason=phase_failed` 且写日志，未见「有执行无日志」或「空 phases 整日空转」形态。

## 6. P1-1 runtime evidence（乱码）

### 6.1 生产 DB（crawl_work / remote_jobs / stage_work / audit_items 全量 TEXT 字段扫描）

乱码特征字节（鎷/鎴/灞/浠/鏈 等 GBK 误解码形态）在 `page_price_source` 的分布：

| 业务日 | crawl_work 总数 | source="招商局"（正确） | source="鎷涘晢灞€"（乱码） | 分类分布 |
|---|---|---|---|---|
| 08-11 | 无 DB | — | — | — |
| 08-12 | 558 | 0 | **549**（OK 490 / PRICE_IS_ZERO 59） | SUCCESS 549 / NO_RESULT 9 |
| 08-13 | 589 | 0 | **587**（OK 543 / PRICE_IS_ZERO 44） | SUCCESS 587 / NO_RESULT 2 |
| 08-14 | 988 | 876 | **74**（OK 70 / PRICE_IS_ZERO 4） | SUCCESS 950 / NO_RESULT 38 |
| 08-17 | 345 | 345 | 0 | 同左 |
| 08-18 | 392 | 392 | 0 | 〃 |
| 08-19 | 526 | 526 | 0 | 〃 |
| 08-20 | 622 | 622 | 0 | 〃 |
| 08-21 | 281 | 281 | 0 | 〃 |
| 08-24 | 479 | 477 | 0 | 〃 |
| 08-25 | 564 | 562 | 0 | 〃 |
| 08-26（进行中） | 151 | 151 | 0 | 〃 |

- backend_version 全部为 `legacy-v1`（含乱码日），乱码在 08-14 当天中途消失（生产在 08-14 中午切换了读取路径/部署）。
- `remote_jobs.result_json`、`stage_work.result_json`、`audit_items.row_json`、所有 `error_text`：**乱码 0 命中**。
- **分类层面**：全部 inspected days 中 `crawl_work.state` 只有 SUCCESS / NO_RESULT；
  `page_price_status` 只有 OK / PRICE_IS_ZERO / NO_RESULT；**FAILED 与 PRICE_MISSING 从未出现**；
  「OK 但 page_price_value 为空」的行 = 0。即「成功+空价 → 误判 FAILED」的触发前提在已检查窗口内**从未发生**。

### 6.2 生产交付工作簿（saas_audit_final_*.xlsx，openpyxl 只读全单元格扫描）

| 业务日 | 乱码单元格 | 页面价格来源分布 |
|---|---|---|
| 08-12 | 0 | 该日工作簿无「页面价格来源」列 |
| 08-13 | **590** | 鎷涘晢灞€ 590 / 招商局（无采集结果）2 / None 6 |
| 08-14 | **4** | 招商局 386 / 鎷涘晢灞€ 4 / 招商局（无采集结果）11 / None 6 |
| 08-17…08-25 | 0 | 招商局 / 招商局（无采集结果）/ None |

### 6.3 P1-1 结论

```text
P1_1_CODE_BUG          = CONFIRMED（源码只读核对：zhaoshang_result_adapter.py
                          read_zhaoshang_result_xlsx_by_identity 分类分支使用乱码字面量
                          "鎴愬姛"/"鎷涘晢灞€"/"浠锋牸涓嶅彲瑙?"/"鏈煡"，
                          同文件 read_zhaoshang_result_xlsx 为正确中文——与稳定性审计一致）
P1_1_HISTORICAL_IMPACT = CONFIRMED（限定范围）：
  a) 乱码来源字符串确实进入生产 DB（08-12 549 行 / 08-13 587 行 / 08-14 74 行）；
  b) 确实进入已交付 final.xlsx（08-13 工作簿 590 个乱码单元格；08-14 4 个）；
  c) 但「采集成功+价格不可见 → 误分类 FAILED」未曾发生（窗口内无任何 FAILED/PRICE_MISSING 行，
     分类均为 OK/PRICE_IS_ZERO，价格值均在）。
P1_1_AFFECTED_ROWS     = DB result_json 1210 行（549+587+74）；交付工作簿 594 个单元格（590+4）；
                          误分类行 0
P1_1_AFFECTED_DATES    = 2026-08-12 / 2026-08-13 / 2026-08-14（08-14 后消失，08-17 起干净）
```

## 7. P1-2 runtime evidence（partial-exact）

### 7.1 历史触发证据（BATCH_STATE.md + DB 交叉核对）

`recover-partial-exact-harvest` 的使用痕迹：`PARTIAL_EXACT_HARVEST_COMPLETED` / `PARTIAL_EXACT_HARVEST_MANUAL`
事件共 **7 次**，全部能在 DB crawl_work 中逐一对应到 NO_RESULT 行：

| 记录日(行号) | remote_identity | job | expected | matched | absent→NO_RESULT | 事件 remote_state |
|---|---|---|---|---|---|---|
| 08-14(:207) | jd\|1235646\|RW-20260814093819 | 3 | 273 | 272 | 1（crawl 142） | COMPLETED |
| 08-14(:208) | zjmi\|1235647\|RW-20260814093828 | 4 | 58 | 55 | 3（crawl 367/371/374） | COMPLETED |
| 08-14(:209) | lxwlmall\|1235648\|RW-20260814093835 | 5 | 21 | 20 | 1（crawl 393） | COMPLETED |
| 08-14(:210) | nbdeli\|1235644\|RW-20260814093801 | 1 | 34 | 32 | 1（crawl 7）+1 manual | MANUAL_REQUIRED |
| 08-14(:211) | zkh\|1235649\|RW-20260814093842 | 6 | 26 | 23 | 3（crawl 419/426/435）+5 manual | MANUAL_REQUIRED |
| 08-25(:634) | colipu\|1271327\|RW-20260825172047 | 88 | 5 | 4 | 1（crawl 554） | COMPLETED |

合计 **written_no_result = 10 行**（08-14 9 行 + 08-25 1 行）。DB 侧核对：这 10 行 crawl_work 均
`state=NO_RESULT`，result_json 带 `evidence_type=EXPORTED_NO_MATCHING_ROW`、`remote_state=COMPLETED`。
remote_jobs 侧核对：6 个 job 现 state=COMPLETED，harvest_attempts=2（08-25 job 88 为 14）。

### 7.2 当时是否「远端确实无结果」——离线可证明性

离线证据只能证明：**命令确实以本地 workbook 缺行为依据写入了不可逆 NO_RESULT（历史触发 CONFIRMED）**，
且事件记录的 remote_state 是工具自身状态机值。无法从离线工件反查当时远端 progress 是否
total==finished==expected（代码路径无该查询，无此证据落盘）——因此「其中是否有行实际远端有价而被抹空」
**UNKNOWN — historical outcome cannot be proven offline**。

### 7.3 P1-2 结论

```text
P1_2_CODE_RISK         = CONFIRMED（源码只读核对：run_partial_exact_harvest_recovery 对
                          exact_absent 直接 complete_partial_exact_no_result，
                          无远端完成活性证明、无 absent 上限——与稳定性审计一致）
P1_2_HISTORICAL_TRIGGER= CONFIRMED（7 次命令执行、10 行不可逆 NO_RESULT，见 7.1）
P1_2_WRONG_PRICE_LOST  = UNKNOWN（离线无法证明当时远端 progress 状态）
```

## 8. H-3：同 SKU 多 URL（P2-4 触发前提）

对 crawl_work（run 内）统计 `同一 sku_id 对应多个不同 exact_url`：

| 业务日 | sku_id 多 URL 组数 | source_id 多 URL 组数 |
|---|---|---|
| 08-12 | 0 | （未统计） |
| 08-13 | 0 | 24 |
| 08-14 | 0 | 61 |
| 08-17 | 0 | 24 |
| 08-18 | 0 | 61 |
| 08-19 | 0 | 75 |
| 08-20 | 0 | 74 |
| 08-21 | 0 | 18 |
| 08-24 | 0 | 48 |
| 08-25 | 0 | 41 |
| 08-26 | 0 | 13 |

- **sku_id（采集编码）级多 URL：全窗口 0 组 → P2-4 的「同 SKU 多 URL 且导出省略 URL」前提未观察到。**
- source_id（SaaS 商品 id）级多 URL 常见（同一商品多编码/多链接），但每个 sku_id 唯一对应一个 exact_url，
  code_matches 多命中扇出（P2-4）需要同 sku_id 多 URL，因此未发生。
- 结论：**H3_MULTI_URL_SKU = NOT_OBSERVED（inspected window）**（本任务只调查不修改）。

## 9. H-4：20:35 safety closeout 缺失

见 5.4。**NOT_OBSERVED（in inspected window）**；事件日志禁用导致机器级错过场景 INSUFFICIENT_RUNTIME_EVIDENCE。

## 10. H-5：Scheduler 静默早失败

见 5.3/5.4。**NOT_OBSERVED（in inspected window）**；LastTaskResult=0；唯一 RUN 无 END 案例（08-24 13:45）
是 IgnoreNew 策略性跳过；非零退出均有日志与 STOP reason。事件层 INSUFFICIENT_RUNTIME_EVIDENCE。

## 11. P2-1：RECOVERED_PENDING_REVIEW

stage_work 全窗口（08-12…08-26）stage/state 分布只有：
`DETAIL/SUCCESS`、`MODEL/SUCCESS`、`MODEL/PENDING`（仅 2026-08-21 有 113 行 MODEL/PENDING 残留，当日
最终仍 FINALIZED）。**RECOVERED_PENDING_REVIEW = 0 次 → NOT OBSERVED IN INSPECTED WINDOW。**

## 12. P2-8：SQLite lock

只读搜索（全部 scheduler_logs/*.log + 各业务日 BATCH_STATE.md + DB 副本全部 error_text）：
`database is locked` / `SQLITE_BUSY` / `OperationalError` / `locked` / `busy` —— **0 命中**。

**SQLITE_LOCK_OBSERVED = NOT OBSERVED IN INSPECTED WINDOW**（不据此断言永不发生）。

## 13. Confirmed facts（生产证据，非源码推导）

1. 生产分支 SHA 与预期一致且工作区 clean（PHASE 0）。
2. 乱码「鎷涘晢灞€」在 2026-08-12/13/14 进入生产 crawl_work.result_json（1210 行）与
   08-13/08-14 交付 final.xlsx（594 单元格）；08-14 中途起消失。
3. 全窗口无 FAILED / PRICE_MISSING 分类；乱码期间分类（OK/价格为0）与价格值本身正确。
4. partial-exact 恢复命令历史执行 7 次，写入不可逆 NO_RESULT 10 行（业务日 08-14 ×9、08-25 ×1）。
5. NO_RESULT（含 partial-exact 与正常 harvest 两条路径）全部带 `EXPORTED_NO_MATCHING_ROW` /
   `EXPORT_NO_PRICE_TERMINAL` 证据与 `remote_state=COMPLETED`。
6. 计划任务 20:35 触发器 08-24 起存在且两天实际执行 exit=0；LastTaskResult=0；事件日志禁用。
7. RECOVERED_PENDING_REVIEW 0 次；SQLite lock 字符串 0 命中；sku_id 级多 URL 0 组。
8. final.xlsx 物化时刻多处落在调度窗口外（人工命令写入），全机仅一个 CNNC 计划任务。

## 14. High-probability findings（高概率推断，标注推断依据）

1. 08-12~08-14 乱码由 identity 读取路径产生（时间窗与 identity reader 上线/切换吻合；乱码字符串
   与该函数字面量逐字一致）；08-14 中午的切换使其消失——推断依据：DB 字符串形态，非源码。
2. 08-25 colipu job 88（harvest_attempts=14、433s）与 08-14 jobs 1/6（attempts 后移、06:47 更新）
   属于「先 MANUAL、后人工 partial-exact 放行」的形态——推断依据：BATCH_STATE 事件与 remote_jobs 时间线。
3. 08-19 的 daily_status 更新（20:30）与最终表（08-20 10:06）由人工操作完成——推断依据：调度日志 17:36 后无记录。

## 15. Unknown / insufficient evidence

1. partial-exact 10 行 NO_RESULT 当时远端 progress 是否已完成：UNKNOWN（无落盘证据；代码路径不查询）。
2. Task Scheduler 事件级对照（H5c 早期失败、20:36+ 空退出）：事件日志 disabled → INSUFFICIENT。
3. 2026-08-11 无 state.sqlite3（仅 daily_status.json），该日无从取证。
4. 08-12 final 工作簿无「页面价格来源」列（当日物化列集不同），该列是否曾以其他列名携带乱码未深究（DB 已证实该日 result_json 乱码）。
5. scheduler_logs 中 legacy 工具 stdout 为 GBK 乱码形态（如「[????] Token refresh success」）——
   与稳定性审计 P3-17（子进程未钉 UTF-8）一致，属已知 P3，本任务不处理。

## 16. Exact files inspected（只读）

生产 Git：origin/prod/closeout-20260818、git status。
生产源码（只读）：
`app/services/zhaoshang_result_adapter.py`、`app/services/recovery/harvest.py`、
`app/services/recovery/no_result.py`、`app/services/legacy_zhaoshang_adapter.py`（关键函数段）、
`app/services/incremental_audit_recovery.py`、`tests/test_partial_exact_harvest.py`、
`tests/test_recovery_facade_contract.py`。
运行时数据（只读）：
- `runtime/incremental_price_audit/<2026-08-11…2026-08-26>\{state.sqlite3, saas_audit_final_*.xlsx,
  *.manifest.json, daily_status.json, BATCH_STATE.md}`（state.sqlite3 经备份 API 取副本）
- `runtime/incremental_price_audit/scheduler_logs\2026-08-13…2026-08-26.log`
- 仓外审计脚本与副本：`E:\.A中核运维\cnnc-ops-agent-worktrees\_audit_tmp\`（audit_lib.py、step1…step15 脚本与 audit_*.sqlite3 副本）

## 17. Exact SQL used（全部在审计副本上）

```sql
SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name;
PRAGMA table_info(daily_runs); PRAGMA table_info(snapshots); PRAGMA table_info(audit_items);
PRAGMA table_info(snapshot_members); PRAGMA table_info(stage_work);
PRAGMA table_info(remote_jobs); PRAGMA table_info(crawl_work);
SELECT state, final_output_path, updated_at FROM daily_runs;
SELECT <result_json|error_text|row_json> FROM <crawl_work|remote_jobs|stage_work|audit_items> WHERE ... IS NOT NULL;  -- 全量取回后在 Python 内做乱码/分类统计
SELECT state, COUNT(*) n FROM crawl_work GROUP BY state;
SELECT state, COUNT(*) n, MIN(harvest_attempts), MAX(harvest_attempts) FROM remote_jobs GROUP BY state;
SELECT result_json FROM crawl_work WHERE state='NO_RESULT';
SELECT id, mall_key, task_name, state, remote_identity, harvest_attempts, harvest_elapsed_seconds,
       export_path, result_json, error_text, updated_at FROM remote_jobs WHERE id IN (...);
SELECT id, mall_key, source_id, sku_id, exact_url, state, result_json, error_text
       FROM crawl_work WHERE remote_job_id = 88;
SELECT COUNT(*) FROM (SELECT sku_id FROM crawl_work GROUP BY sku_id HAVING COUNT(DISTINCT exact_url) > 1);
SELECT COUNT(*) FROM (SELECT source_id FROM crawl_work WHERE source_id <> ''
       GROUP BY source_id HAVING COUNT(DISTINCT exact_url) > 1);
SELECT mall_key, sku_id, COUNT(DISTINCT exact_url) AS urls, COUNT(*) FROM crawl_work
       GROUP BY mall_key, sku_id HAVING COUNT(DISTINCT exact_url) > 1 LIMIT 5;
SELECT stage, state, COUNT(*) n FROM stage_work GROUP BY stage, state;
SELECT backend_version, COUNT(*) n FROM crawl_work GROUP BY backend_version;
```

源连接（生产文件，只读）：`file:...state.sqlite3?mode=ro` + `PRAGMA query_only=ON` + `PRAGMA busy_timeout=5000`
+ `Connection.backup()`。

## 18. Exact PowerShell read-only commands used

```powershell
git -C <prod> fetch origin
git -C <prod> rev-parse origin/prod/closeout-20260818
git -C <prod> log -1 --oneline origin/prod/closeout-20260818
git -C <prod> status --short
Get-ChildItem -LiteralPath <runtime>\incremental_price_audit [-Recurse]
Get-ScheduledTask -TaskName 'CNNC-PriceAudit-Incremental'   # + .Triggers/.Settings/.Principal/.Actions 读取
Get-ScheduledTaskInfo -TaskName 'CNNC-PriceAudit-Incremental'
Get-ScheduledTask | Where-Object { $_.TaskName -match 'CNNC|PriceAudit|price|audit' }
wevtutil gl Microsoft-Windows-TaskScheduler/Operational
Select-String -Pattern 'partial[-_]exact','MANUAL_REQUIRED','database is locked','SQLITE_BUSY',
  'OperationalError','NO_RESULT','[鎷鎴灞浠鏈]' -Path <scheduler_logs\*.log + 各日 BATCH_STATE.md>
```

（配套审计脚本为 Python，仅 openpyxl 只读读取 xlsx 与上文 SQL；无任何写调用。）

## 19. Production write confirmation

```text
PRODUCTION_WRITES_PERFORMED = 0
```

- 生产源码/配置/Git：未修改（生产工作区 status clean 复核）。
- 生产 SQLite：未写（ro URI + query_only + 备份 API）。
- 生产 Excel/日志/BATCH_STATE：未写（只读打开）。
- Windows 计划任务：未注册/未修改（仅 Get-* 查询）。
- 真实远端（SaaS/中核/招商局）：零调用。
- STEP_1 业务命令（tick/closeout/finalize/recover-* 等）：零执行。
