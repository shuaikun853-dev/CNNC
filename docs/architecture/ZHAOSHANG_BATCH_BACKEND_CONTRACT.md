# 招商局 batch backend 本地契约核验

## 已有证据

- 本地技能只定义了两个接口：`POST /batch/autoAddTask` 和按 `batchNumber` 查询的 `GET /queryTaskProgress`。
- 建任务为 URL 模式 `crawlMode=3`，单次同一 `mallLogo`、最多 100 条；每条包含稳定的 `executeNumber`、`url`、`productCode`。
- 技能明确固定 `commodityPoolId=777`、`tenantId=000000`、`taskSource=mrwang`、`taskWeight=0`，成功证据是 `rspCode=0000` 且 `success=true`。
- improxy 被描述为无需 token。adapter 不读取 CNNC/SaaS/招商局 cURL，不构造 `AuthManager`，HTTP client 使用 `trust_env=False`。
- 技能没有任务详情、结果导出或结果下载接口；随附 SQL 只是历史查询样例，项目红线禁止把它变成运行时 SQL。

## 采用的身份

`PreparedBatchPlan.parent_job_key` 是业务日期、契约版本和规范化请求全集的 SHA-256。每个商城按最多 100 条分片；每片有独立的 `logical_job_key`、`batchNumber=taskName` 和请求体 SHA-256。`executeNumber` 由片 identity、序号和 item identity 稳定派生。

每片使用独立 `batchNumber`，而不是沿用技能脚本“多片共用一个批次号”的做法。原因是现有只读核对接口只能按 `batchNumber` 返回汇总数；独立编号才能在 POST 响应超时后用“准确编号 + 准确条数”证明该片已存在，避免盲目重发和历史 16 变 32。

## 安全边界

- adapter 默认 `write_enabled=False`；shadow/status/doctor 不会 POST。
- 一次进程内同一 logical job 最多 POST 一次；跨进程必须继续依赖 SQLite 的 `SUBMIT_INTENT/post_attempted/SUBMIT_UNKNOWN`。
- reconciliation 要求进度总数等于持久化片大小；数量或 identity 冲突进入人工状态。
- `taskWeight=-1` 没有本地证据。当前只保留技能证实的 `taskWeight=0`，不修改 priority 核心。
- batch 创建和进度契约可接入；正式切换仍缺结果 export/download 契约以及真实 canary 对自定义稳定 `batchNumber` 的接受性验证。未补齐前应继续用 legacy backend 收获结果。
