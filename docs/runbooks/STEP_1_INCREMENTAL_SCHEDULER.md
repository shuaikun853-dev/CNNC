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
python scripts\incremental_price_audit.py doctor --business-date 2026-08-11
```

默认 runtime root 是 `runtime/incremental_price_audit`；每个业务日在各自目录保存
`state.sqlite3`、`incremental_price_audit.lock`、`BATCH_STATE.md`、`daily_status.json` 和正式 Excel。
SQLite 是唯一状态权威；`daily_status.json` 只是每次有限命令结束后的原子便利快照。`status` 和
`doctor` 都只读，缺失状态返回 `MISSING`，不会创建目录、数据库、发网络请求或修改 Scheduler。

`status` 固定报告最新快照、发现/新增/变更/已处理数量、Detail/Model 的 pending/running/completed/
terminal、crawl 与远端任务的 pending/running/exported/unknown/manual、freeze/final pending/finalized，
并给出下一条受控动作。`doctor` 只核对本地文件、模型配置键名、SQLite `quick_check` / schema、
过期 lease 和 finalized artifact；不会显示密钥值。默认只把 shadow 所需依赖当硬要求；带 `--enable-remote-writes` 才把招商局依赖
计入健康判定，避免一个系统的认证问题无条件阻塞其他阶段。

SQLite 仅有七张 checkpoint 表：`daily_runs`、`snapshots`、`audit_items`、`snapshot_members`、
`stage_work`、`remote_jobs`、`crawl_work`。不要手改数据库、删除 lock，或把不同调度任务指向
不同的 runtime root。

## 推荐的 Scheduler 配置

正式启用时设置两个 Windows Task Scheduler 任务：

| 项目 | 白天 tick | cutoff 后 finalize |
|---|---|---|
| Program | 受控 Python 解释器路径 | 同左 |
| Start in | 仓库根目录 | 仓库根目录 |
| Arguments | `scripts\incremental_price_audit.py tick --budget-seconds 720 --shadow` | `scripts\incremental_price_audit.py finalize --budget-seconds 720 --shadow` |
| Trigger | 09:00 起每 15 分钟，最后一次 17:45 | 18:03 起每 15 分钟，最后一次 23:48 |
| Multiple instances | **IgnoreNew** | **IgnoreNew** |
| ExecutionTimeLimit | **14 分钟**（720 秒应用预算之外保留两分钟硬兜底） | 同左 |

两个任务必须使用同一 repo、同一 runtime root。内部还会获取非阻塞 OS lock 和 fenced DB lease；
重叠实例得到 `ALREADY_RUNNING` 后以正常 overlap 退出，而不是等待。CLI 和脚本默认 720 秒；
相对 15 分钟触发间隔仍保留三分钟，其中 Scheduler 在第 14 分钟作硬兜底。预算接近末尾 reserve
时不会领取新重工作，下一 tick 继续 backlog。Python 的 wall-clock budget 无法强行中断卡在操作
系统或第三方库里的单次调用，因此必须同时配置 14 分钟
`ExecutionTimeLimit`。若硬停止恰逢 POST，已落库的 immutable intent 会在下一 tick 进入精确对账，
而不是重发。

注册脚本默认只打印计划，下面第一条命令不会修改系统。本阶段验证通过且获得启用确认后，才执行
第二条；默认 Scheduler 保持 shadow，不创建远端任务：

```powershell
powershell -NoProfile -File scripts\register_incremental_price_audit_tasks.ps1
powershell -NoProfile -File scripts\register_incremental_price_audit_tasks.ps1 -Apply
powershell -NoProfile -File scripts\status_incremental_price_audit_tasks.ps1
```

只有另行确认无人值守远端写授权后，注册命令才可增加 `-EnableRemoteWrites`。脚本固定
`MultipleInstances=IgnoreNew`、15 分钟间隔和 14 分钟硬上限；两个任务必须共享同一绝对 runtime root。

自定义 root 或重新执行某日时，显式使用同一个绝对路径：

```powershell
python scripts\incremental_price_audit.py tick `
  --business-date 2026-08-11 `
  --runtime-root 'E:\.A中核运维\cnnc-ops-agent\runtime\incremental_price_audit' `
  --budget-seconds 720 `
  --shadow

python scripts\incremental_price_audit.py finalize `
  --business-date 2026-08-11 `
  --runtime-root 'E:\.A中核运维\cnnc-ops-agent\runtime\incremental_price_audit' `
  --budget-seconds 720 `
  --shadow
```

`finalize` 在 18:00 前返回未就绪；18:00 后冻结第一份完整权威快照。可重复触发：未 drain
时保持 `FINALIZING`，只有 Detail/Model/crawl pending 时按正常等待退出；terminal、未规划 crawl、
无效价格或产物冲突才作为可操作阻塞返回非零。命令总预算至少还剩 60 秒才开始正式物化，
否则 checkpoint 后交给下一次 finalize。完成后 workbook/manifest 只会复用同一 frozen artifact，
输出路径 intent 在落盘前已唯一登记，冲突不会先写出第二份正式文件。

## 安全开关和当前限制

- `--shadow` 是默认模式：runtime 装配 SaaS/中核发现、Detail、模型，以及已记录任务的 legacy
  `wait-export-only` / 精确 unknown-submit 对账，但没有 crawl planner 或 submitter，不能创建招商局任务。
- `--enable-remote-writes` 才接通受控 legacy `create-only`；旧名 `--enable-zhaoshang` 暂作兼容别名。
  它先把精确成员、模板和 immutable payload 持久化，
  再发起正式建任务。它是写能力，必须有本批次的明确授权；Scheduler 默认任务不要携带该参数。
  submit 后当前 tick 退出，后续 tick 只做有上限的 `wait-export-only` harvest；超时、进程中断或非确定结果先进入
  `SUBMIT_UNKNOWN`，只按精确 task/mall 对账，绝不盲目重发 `/add`。
  task name 使用业务日和 immutable 请求集生成同日稳定、跨日不同的 14 位标记，防止次日对账命中前日任务。
  重启后的 harvest 只从已持久化的 payload、模板和路径重建同一 legacy job；任一证据不一致即转
  `MANUAL_REQUIRED`，不尝试“猜测性恢复”。
- `--zhaoshang-backend legacy|batch` 选择 Adapter，默认 `legacy`。`batch` 使用公司 Skill 已证实的
  `/batch/autoAddTask` 与 `queryTaskProgress`，按商城和最多 100 条稳定分片；create 阶段不生成
  临时 Excel、不开每商城子进程，也不依赖 Cookie/cURL。它仍必须显式传 `--enable-remote-writes`。
  当前 Skill 没有 batch 结果 export/download 契约，因此该 backend 只用于受控 canary；任务完成
  后会进入人工 export 项，不会猜测接口。正式日常运行在 canary 前保持 `legacy`。
- 公司 Skill 只证明 `taskWeight=0`，没有证明它映射数据库 `weight_type=-1`；batch Adapter 不猜测
  `-1`，也没有修改现有受控 priority 核心。
- 增量 Tick 的模型批次基线是 10、并发为 1，与第一步生产稳定口径一致；不要把
  `.env.example` 中的 30 当作本项目生产基线。adapter 的默认上限是 3 attempts / 300 秒，
  但 tick 传入单次 callback 的 external cap 默认 60 秒，故每个批次实际最多 3 次 HTTP，
  总 wall time 不超过 `min(60 秒, stage 剩余, tick 剩余)`。普通可捕获失败在一次 durable claim
  后即终止，不在 tick 层叠加 retry；只有进程在 `CLAIMED` 后硬中断时允许下一 tick 做一次只读
  crash replay。故通常最多 3 次 HTTP；连续两次进程中断的理论硬上限为两组各 3 次，随后转 terminal。
- POST 之前持久化 immutable intent；超时或进程中断进入 `SUBMIT_UNKNOWN`，后续只能精确对账，
  不自动重发 `/add`。harvest 到 attempts/elapsed 上限进入人工处理。
- 每次命令都会安全更新该业务日的 `BATCH_STATE.md`；SQLite 仍是状态权威。重叠实例正常返回 0，
  可恢复 pending/defer 返回 0，`FAILED`、人工项或正式产物阻塞返回非零。
- 不要在此调度中加入审批、驳回、通过、保存、提交、推送、本地 crawler fallback 或任何 SQL。

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

## 启用与回滚门槛

按顺序推进，不跨级：

1. `doctor` 全部 required check 通过，确认 status 无旧的 unknown/manual。
2. 完整工作日只跑 shadow，核对请求量、缓存命中、backlog 和 18:00 的 final pending。
3. 在明确授权下手工跑一次有限 tick；确认 POST intent、提交后退出和后续 harvest。
4. 手工重复执行 finalize，确认 freeze 与正式 Excel 幂等。
5. 先预览注册脚本，再经确认使用 `-Apply`；最初仍保持 shadow。
6. 只有生产 canary 已确认远端 identity/导出契约后，才考虑带 `-EnableRemoteWrites` 重注册。

回滚只禁用增量 Scheduler，不删除 SQLite、Excel、日志或其他历史：

```powershell
powershell -NoProfile -File scripts\unregister_incremental_price_audit_tasks.ps1
powershell -NoProfile -File scripts\unregister_incremental_price_audit_tasks.ps1 -Apply
```

随后按 `STEP_1_PRICE_AUDIT.md` 运行 legacy 全量流程。未完成的增量远端任务仍保留 durable identity；
恢复增量前先 `status` / `doctor` 并完成精确对账，不能因回滚而盲目创建新任务。
