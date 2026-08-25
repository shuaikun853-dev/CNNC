# 增量价格审计 Identity 历史证据

这是实施前的只读历史验证，不含认证信息，也不是运行状态。

## 样本和基数

- 31 份 `runtime/price_audit_*` SaaS 快照，2026-07-13 至 2026-08-11，共 10,014 行。
- 23 份配套 customer JSON，共 5,837 行；17 份可精确配对 legacy final Excel，共 5,718 行。
- `vCommodityId` 缺失 0，`skuId` 缺失 0，`extLink` 缺失 8。
- 10,014 条 SaaS pending 的 `documentId` 均为空；5,837 条 customer 的 `documentId` 均存在，
  跨文件唯一值 2,516 个。23 个配对批次的 7,792 条 SaaS 观察中，6,624 条可按
  `SaaS.vCommodityId = customer.documentId` 关联，1,168 条未关联。
- 在 6,624 条已关联观察中，`customer.referenceId = SaaS.skuId` 为 0 条，
  `customer.referenceLink = SaaS.extLink` 仅 83 条。因此 customer `documentId` 是关联键，
  不是 SKU、URL 或 SaaS 行 identity 的替代品。
- 唯一 v 为 1,218，唯一 SKU 为 7,045，唯一 exact URL 为 7,025，唯一 `(v, sku)` 为 7,400。
- 696 个 v 对应多个 SKU（最大 92）；307 个 SKU 被多个 v 使用；304 个 URL 被多个 v 使用；
  14 个 URL 对应多个 SKU。
- `(v, sku)` 不对应多个 exact URL；47 个快照内重复 `(v, sku)` 组的完整源行相同。

结论：不能把单独的 v、SKU 或 URL 当作一行的唯一键，也不能按 URL-only 合并 crawl。
同一业务日的相邻快照还观察到 50 个共有 v 的 SKU 集发生增删；`createTime` 早于调度日的行有 2,342，
所以业务日必须来自调度，不来自源字段。

## 实际采用的 Identity

```text
source identity      = vCommodityId
audit relationship   = vCommodityId + skuId
audit version        = source_fingerprint + customer_fingerprint
source occurrence    = snapshot_id + row_ordinal
duplicate occurrence = source occurrence + duplicate_ordinal
crawl request        = mall_key + source_id + skuId + exact-trimmed URL + backend_version
crawl result         = exact (skuId, trimmed URL), then fan-out to matching v rows;
                       SKU fallback only when that job maps the SKU to one URL;
                       URL-only never crosses SKU
```

`source occurrence` 才是最终 Excel 行的身份：它保留顺序和重复次数。`audit_items` 可复用同一
relationship version 的 Detail/Model 结果，但 `snapshot_members` 绝不去重。缺少 v 或 SKU 的
未来记录显式失败，不能以空字符串拼接身份，也不能自动提交 crawl。

## Fingerprint 和 Cutoff

所有 hash 为 canonical JSON SHA-256：

- `source_fingerprint`：规范化 SaaS 当前业务行，加上已解析的 crawl policy
  （required、mall key、mall source id、backend version）；金额是 Decimal 字符串，字符串仅 trim，URL 不改写。
- `customer_fingerprint`：`fourCategoryId`、`fourCategoryName`、`referencePrice`、`commodityName`、`referenceLink`。
- `detail_fingerprint`：document、分类、customer fingerprint、business date 和 detail schema。
- `model_fingerprint`：`queryStr`、`skuName`、`modelAuditResult`、`isMatch` 与 model/prompt/schema version。
- `crawl_fingerprint`：crawl identity、business date 和 backend version。
- `snapshot ordered_hash`：权威 API 返回顺序中的 `(source_fingerprint, customer_fingerprint)` 多重集；
  相同内容在不同 `observed_at` 仍保存为独立观察，才能证明内容在 cutoff 后确实再次完整出现。

历史上观察到 52 组 `updateTime` 不变而 `specs` 变化，但当前消费的五个 customer fingerprint
字段未出现这种变化；内容 hash 是面向实际消费字段的防御性选择，不把该样本夸大为直接证明。
17/17 配对 final Excel 的 5,718 行保留源快照 `(v, sku, URL)` 顺序和重复次数；另有一组
80 行真实源/customer 边界样本验证多 tick 与 legacy writer 的业务投影一致，其中强制包含同 URL
多 SKU 和跨 v 的同 `(SKU, URL)`。这两项不等同于对 5,718 行历史模型/页面价/颜色的全量重放。
cutoff 定义为 Asia/Shanghai 18:00 后第一份实际行数等于
权威 `count` 的完整快照，而不是 `createTime <= 18:00` 过滤：2026-08-10 从 17:52 的 332 行
增长到 18:13 的 401 行，新增 69 行中有 3 行创建于 18:00 后；2026-07-24 正式表 84 行中有
41 行也创建于 18:00 后。
