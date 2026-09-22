# 招商局 batch backend 本地契约核验

## 已有证据

- 本地技能只定义了两个接口：`POST /batch/autoAddTask` 和按 `batchNumber` 查询的 `GET /queryTaskProgress`。
- 建任务为 URL 模式 `crawlMode=3`，单次同一 `mallLogo`、最多 100 条；每条包含稳定的 `executeNumber`、`url`、`productCode`。
- 技能明确固定 `commodityPoolId=777`、`tenantId=000000`、`taskSource=mrwang`、`taskWeight=0`，成功证据是 `rspCode=0000` 且 `success=true`。
- improxy 被描述为无需 token。adapter 不读取 CNNC/SaaS/招商局 cURL，不构造 `AuthManager`，HTTP client 使用 `trust_env=False`。
- 技能没有任务详情、结果导出或结果下载 HTTP 接口。2026-09-22 生产只读核验已证明结果落在 `shopping_spider`：exact `<mall>_price.exe_number` 可关联同商城 `<mall>_goods.sku_id`；SQL adapter 仅允许已通过表结构、索引、历史 Batch identity、价格语义和查询时延验证的 `colipu / comix / ehsy / gongpin / guangbo / haoxiny / jd / lxwl / lxwlmall / mymro / nbdeli / ofs / qstbg / suning / tima / xfs / xhgj / ycw / zjmi / zkh`，其余商城继续 fail closed。CNNC 当前 18 个生产商城均可逐链接进入 batch。`mymro / ofs` 的请求身份是 exact executeNumber 加提交 URL 中按严格已证明格式解析出的 product code；exact executeNumber 恰好一条 SQL result 时允许 result SKU、goods_no 和 URL code 变化，多条结果仍转 identity conflict，提交 URL 无法解析则该链接回退 legacy。其他商城继续校验 durable expected SKU 与 URL；`qstbg` 只接受同 host、同 goods_id 的严格 detail/init 变体（含精确 `count=1` 兼容），`lxwlmall` 只接受同 host 的 `/detail` 与 `/detail/inner` 变体。原始 26 商城 UNION 仍只是人工样例，不进入 runtime。`nbdeli` 唯一允许的 SKU 特例是只对纯数字后缀 `PCS` 做等价归一；91 条成功历史样本均表现为数据库 SKU 去除 `PCS`，无其他变换或 identity collision。新建请求中的 `jslink.com` 及其子域统一规范为 `b2b.nbdeli.com` 并按 `nbdeli` 路由，历史 durable identity 保持兼容。

## 采用的身份

`PreparedBatchPlan.parent_job_key` 是业务日期、契约版本和规范化请求全集的 SHA-256。runtime 规划时把 SaaS `source_id` 一并冻结；即使两个来源文档指向相同 SKU/URL，也按 `(request identity, source_id)` 生成两个稳定 `executeNumber`，保证每个 executeNumber 只对应一个 `crawl_work`。每个商城按最多 100 条分片；每片有独立的 `logical_job_key`、`batchNumber=taskName` 和请求体 SHA-256。旧 durable payload 没有 `source_ids` 时仍按原 hash/identity 恢复。

每片使用独立 `batchNumber`，而不是沿用技能脚本“多片共用一个批次号”的做法。原因是现有只读核对接口只能按 `batchNumber` 返回汇总数；独立编号才能在 POST 响应超时后用“准确编号 + 准确条数”证明该片已存在，避免盲目重发和历史 16 变 32。

## 安全边界

- adapter 默认 `write_enabled=False`；shadow/status/doctor 不会 POST。
- 一次进程内同一 logical job 最多 POST 一次；跨进程必须继续依赖 SQLite 的 `SUBMIT_INTENT/post_attempted/SUBMIT_UNKNOWN`。
- reconciliation 要求进度总数等于持久化片大小；数量或 identity 冲突进入人工状态。
- create payload 仍按接口契约发送 `taskWeight=0`；item row 物化后，独立 priority service 仅按 exact
  `executeNumber` 冻结真实 PK，并把仍为 `00/01` 的行受控更新为 `weight_type=-1`。`02/03` 均为
  合法终态，不做 UPDATE。
- batch 创建、精确 priority、进度和 SQL result 已接入 runtime。新任务的每条 durable `executeNumber` 必须与唯一 linked `crawl_work` 的 source_id/mall/SKU/URL/backend 全部一致；零匹配、多匹配或额外 linked row 均 fail closed。后到的相同 SKU/URL、不同 source_id 不复用既有 batch job/result，而是只为 pending source 重建一个精确分片。progress、materialization probe 和结果 SQL 共用同一个绝对 callback deadline，数据库每次阻塞操作前重算剩余 timeout；批内尚未开始的 job 退还 claim，不消耗 retry budget 或清除旧诊断。
- `price>0` 只有在 title 非空时转换为 `OK`；`price=0` 统一转换为 `PRICE_IS_ZERO + 已下架`；NULL、空、非数字、非有限数和负值都不是下架，必须 `INVALID_RESULT`。`task_status=02` 且 SQL 零行仍需 spider-log 等终态证据，不能自动记为 `NO_RESULT`；`03` 且零行是 crawler terminal failure。
- CLI backend 只选择新任务策略：`legacy` 仍全部走 legacy；`batch` 对 allowlist 内商城走 batch，对未验证商城逐商城回退 legacy。已有 legacy/batch job 始终按 immutable payload 路由各自 harvester；切回 legacy 不会遗弃已经进入 SQLite 的 batch job，也不会重复 POST。
