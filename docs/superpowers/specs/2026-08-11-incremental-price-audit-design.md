# 增量价格审计：已实施设计

## 目标和边界

第一步价格审计现在有两条并存路径：原 `scripts/audit_saas_excel_file.py`
仍是可运行的全量对照与回滚路径；增量路径由有限 `tick`、SQLite checkpoint 和可重复
`finalize` 组成。它不启动 daemon，不改变招商局 priority 核心，也不自动执行审批、驳回、
提交、推送或本地兜底。

增量路径的 scope 固定为 `tenant_id=188605`、`commodity_pool_id=222`。`business_date`
是 Asia/Shanghai 调度日，绝不从源行 `createTime` 推断。

## 历史证据和身份

对 31 份 2026-07-13 至 2026-08-11 的 SaaS 快照（10,014 行）验证：`vCommodityId`
和 `skuId` 均不缺失；URL 有 8 行缺失；一个 v 可对应多个 SKU、一个 SKU 或 URL 也可被
多个 v 复用；47 组同一快照 `(v, sku)` 是完整相同的重复行。17/17 配对 legacy
final Excel 保持源快照的行顺序和重复次数。完整数据与计数见
[identity evidence](../../architecture/INCREMENTAL_PRICE_AUDIT_IDENTITY_EVIDENCE.md)。

因此实现区分三类业务 identity：

1. source identity：`vCommodityId`，用于来源商品；缺失 v 或 SKU 时拒绝自动 identity。
2. audit relationship：`vCommodityId + skuId`；版本由 `source_fingerprint + customer_fingerprint`
   表示。输出 occurrence 另以 `snapshot_id + row_ordinal + duplicate_ordinal` 保存，不能 DISTINCT。
3. crawl identity：`mall_key + source_id + skuId + exact-trimmed URL + backend_version`。结果先按
   exact `(skuId, trimmed URL)` 对应，并可 fan-out 到多个 v；仅在该 job 内 SKU 对应唯一 URL 时
   允许 SKU fallback；URL-only 绝不跨 SKU。

所有 fingerprint 是 canonical JSON SHA-256。source 指纹覆盖规范化 SaaS 字段和解析后的 crawl
policy（required/mall/source/backend），customer 指纹覆盖其实际消费字段；
detail 包含 document、分类、customer、业务日和 schema；model 包含四个标题输入及
model/prompt/schema 版本；crawl 包含 crawl identity、业务日和 backend。snapshot hash 是按
权威 API 顺序保存的 `(source_fingerprint, customer_fingerprint)` 有序多重集。同一 hash 在不同
`observed_at` 是两次独立观察，使 unchanged 内容也能提供 cutoff 后的权威证据。

## 最小持久状态

`DailyAuditStore` 只有七张表，而不是工作流平台：

| 表 | 最小职责 |
|---|---|
| `daily_runs` | 日、scope、cutoff、freeze/final artifact、metrics、lease/fencing |
| `snapshots` | 完整权威发现的有序 hash、headers 和证据 |
| `audit_items` | 一个 audit relationship version 的规范化源行 |
| `snapshot_members` | frozen/普通 snapshot 的有序多重集成员 |
| `stage_work` | Detail/Model 的 fingerprint、attempt、elapsed、状态和结果 |
| `remote_jobs` | immutable logical key/task/payload、POST 证据、remote identity、harvest 限额 |
| `crawl_work` | crawl identity、绑定 remote job 和已解析结果 |

SQLite 使用 WAL；每次状态操作打开独立连接。`daily_runs` 内置 generation lease，不另建
`leases`/`events`/`remote_job_items` 表。所有写入校验 owner、generation 与 expiry；远端
任务 identity 一经记录不可替换。

## 有限 Tick

每次 tick 只运行一次：

```text
acquire non-blocking ProcessLock + fenced DB lease
-> recover interrupted claims / uncertain POST
-> harvest previous remote work
-> discover a complete authoritative snapshot
-> diff NEW / CHANGED / UNCHANGED
-> advance eligible Detail and Model
-> persist remote intent, submit create-only once
-> checkpoint metrics, release, exit
```

默认预算为 1200 秒，末尾保留 120 秒；接近 reserve 不领取新的 Detail、Model 或提交工作，
而是保留 backlog 给下一 tick。回调只收到 external timeout、stage 剩余时间和 tick 剩余时间
三者的最小值。默认 Detail 为 3 attempts / 300 seconds；模型普通失败只做 1 次 durable claim，
内部 model adapter 最多 3 attempts / 300 seconds；只有进程在 `CLAIMED` 后硬中断时允许下一 tick
再做 1 次 crash replay，避免普通 retry 相乘；harvest 为 120 attempts /
21600 seconds，到限转 `MANUAL_REQUIRED`。

崩溃后 `CLAIMED` stage 回到可恢复失败；POST 前先写 `SUBMIT_INTENT`，再写
`post_attempted=1`，所以 POST 超时、退出码异常或进程中断都进入 `SUBMIT_UNKNOWN`，只可按
精确 task identity 对账，绝不盲目重发。已提交任务由后续 tick 的 `wait-export-only` harvest。
Legacy task name 的 14 位标记同时 hash 业务日和 immutable 请求集，因此同日重跑稳定、跨日 rollover
不同；auth refresh、只读对账请求和子进程共享本次 callback 的 aggregate deadline。

两个本机实例先竞争同一非阻塞 Windows `ProcessLock`；未取到直接退出。DB lease 是第二道
跨进程 fencing，旧 owner 即使继续运行也无法写回新 generation。Task Scheduler 必须使用
同一 runtime root/lock 文件，并设置 MultipleInstances 为 IgnoreNew。应用 budget 不能抢占卡死的
系统调用，因此 900 秒调度预算还要配 20 分钟 `ExecutionTimeLimit` 作进程级硬兜底。

## Freeze 和 Finalize

`finalize` 复用 tick 的发现、阶段推进、远端提交与 harvest，不维护第二套业务规则。18:00
后只冻结第一份完整权威快照；它保存 snapshot id/hash、headers、证据和有序成员，晚到行
不能混入。冻结后仍可多次 finalize，在预算内 drain 必须工作。

当 frozen 成员的 Detail、Model、crawl 都成功且价格有效时，finalize coordinator 从 frozen
顺序构建 legacy audit response，先持久化唯一 output intent，再调用 `LegacyWorkbookMaterializer`
写临时 Excel、校验完整
`record_results` source matrix 和 summary/record projection hash、原子 publish sidecar manifest，
最后 `mark_finalized`。同一 frozen artifact 重跑复用已验证 workbook；不同输入、output 或
manifest fail closed。物化仅在命令预算至少剩余 60 秒时开始，Task Scheduler 的硬时限负责
不可抢占调用的最后兜底。第二天创建新的 `daily_run` 和远端 task identity，前日 state 不会
rollover 覆盖或被次日对账误认。

## 兼容和安全

招商局 adapter 此阶段只接 legacy `create-only`/`wait-export-only`；模板有 immutable payload
证据，导出按请求 identity 多重集校验。公司 batch backend 仍是后续可替换 adapter seam。
priority 代码未复制、未修改。所有测试使用 fake adapter、临时 SQLite 或历史只读 fixture，
不调用真实写接口或生产 SQL。
