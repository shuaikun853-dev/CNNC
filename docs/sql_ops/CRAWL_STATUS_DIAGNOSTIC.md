# 中核 SaaS 首次流程：`crawl_status` 优先排查链

> **适用范围：** tenant `188605`、commodity pool `222` 的 SaaS 首次采集/同款页面排查。
>
> **来源约束：** 本文只记录开发明确确认 + 2026-08-27 生产只读 SQL 实测。数量仅是当时快照，不可当永久值。

## 1. 来源

- **[DEV-CONFIRMED]** 开发明确说明：
  - `IS_SYNC_STANDARD=0` 表示商品整个首次同款流程还没有最终收口。
  - 优先查看 `saas_compare_price.commodity_list_table_188605.crawl_status`。
  - `crawl_status=1`：爬虫还没有回调。
  - `crawl_status=2`：爬虫已经回调，随后进入后续 search / 模型 / 审批流程。
- **[DEV-CONFIRMED]** 2026-08-27 用户把下述排查链发给开发确认，开发回复“没问题”，要求后续 SaaS 页面数据按此链路排查。
- **[LIVE-SQL]** 2026-08-27 生产只读 SQL 交叉验证：
  - 快照 A：`crawl_status=1` 7 个商品，`crawl_status=2` 96 个商品。
  - 快照 B：
    - `crawl_status=1`：7 个商品；7 个仍有 `task_status=1`；0 个有 search。
    - `crawl_status=2`：97 个商品；1 个仍有某条 `task_status=1`；94 个已有 search。
  - 后续时点，针对 `crawl_status=1` 的明细查询曾返回 0 行；另一时点 `crawl_status=2` 为 105 个，其中 104 个已有 search。说明业务状态在排查期间持续推进，必须按当前快照判断。

## 2. 权威排查链

```text
IS_SYNC_STANDARD = 0
= 整个首次同款流程还没最终收口
        ↓
先看 commodity_list_table_188605.crawl_status
        ↓
┌─────────────────────────────┐
│                             │
crawl_status = 1              crawl_status = 2
│                             │
爬虫还没回调                  爬虫已经回调
│                             │
查 task_table                 不要继续纠结中控
↓                             ↓
提取 exact batch              查 search_table
↓                             ↓
查 s_task_info                audit_result
↓                             ↓
查中控 / 爬虫 / callback      model_audit_result
                              ↓
                              sync_state
                              ↓
                              人工审批 / 后续收口
```

## 3. 关键语义

### 3.1 `IS_SYNC_STANDARD=0`

**[DEV-CONFIRMED + MANUAL]** 只表示“整个流程尚未最终收口”。

不能据此直接判断：

- 爬虫还在跑；
- 数据没到 SaaS 同款审批页面；
- 模型没有结果；
- handler/stream 有积压。

商品可能已经完成爬虫和模型，并已经以 `sync_state=1` 的待审批同款显示在 SaaS 页面，但因为人工审批/最终收口未完成，`IS_SYNC_STANDARD` 仍为 `0`。

### 3.2 `crawl_status=1`

**[DEV-CONFIRMED]**：爬虫还没有回调。

**[LIVE-SQL]**：一次生产快照中，7 个 `crawl_status=1` 商品全部仍有 `task_status=1`，且 0 个有 search，和开发口径完全吻合。

因此当 `crawl_status=1` 时，才继续往上游追：

1. `task_table_188605`
2. 从 `task_id` 提取 exact `batch_num`
3. `central_control_sub_demo.s_task_info`
4. 看 `00/00`、`00/01`、`01/02`、`02/02` 等状态
5. 必要时查 Spider callback

### 3.3 `crawl_status=2`

**[DEV-CONFIRMED]**：爬虫已经回调，后面应继续进入 search / 模型 / 审批流程。

**[LIVE-SQL]**：一次生产快照中，97 个 `crawl_status=2` 商品有 94 个已经存在 search；后续时点 105 个中有 104 个存在 search。说明 `crawl_status=2 → search` 是当前生产链路的稳定方向。

`crawl_status=2` 后，如果暂时还没有 search，不应立刻判故障；应允许短暂后续处理窗口。只有同一商品长时间维持 `crawl_status=2` 且没有 search，才继续定位“爬虫回调 → search 生成”之间的问题。

### 3.4 `crawl_status=2` 与某条 `task_status=1` 可以同时出现

**[LIVE-SQL]**：一次快照中，97 个 `crawl_status=2` 商品里仍有 1 个商品存在某条 `task_status=1`。

因此：

- 判断“商品整体是否已经收到爬虫回调”优先看 `commodity_list.crawl_status`；
- `task_status` 用于深入分析任务明细，不应单独覆盖商品级 `crawl_status`；
- 为什么个别已回调商品仍保留 `task_status=1`，当前没有源码/日志证据，不做根因猜测。

## 4. 轻量排查 SQL

### 4.1 先看当天 `crawl_status` 分布

```sql
SELECT
    cl.crawl_status,
    COUNT(DISTINCT cl.commodity_id) AS 商品数
FROM saas_compare_price.commodity_list_table_188605 cl
JOIN saas_onecode_price.commodity_pool_188605 c
  ON c.MY_COMMODITY_ID = cl.commodity_id
WHERE c.COMMODITY_POOL_ID = 222
  AND c.IS_MONITOR = 1
  AND c.CREATED_TIME >= '<business_date> 00:00:00'
  AND c.CREATED_TIME <  DATE_ADD('<business_date> 00:00:00', INTERVAL 1 DAY)
GROUP BY cl.crawl_status
ORDER BY cl.crawl_status;
```

### 4.2 只对 `crawl_status=1` 深挖 task

```sql
SELECT
    cl.commodity_id,
    cl.id AS commodity_list_id,
    cl.crawl_status,
    COUNT(t.task_id) AS task数,
    SUM(t.task_status = 1) AS task运行中,
    SUM(t.task_status = 2) AS task完成,
    SUM(t.task_status = 3) AS task失败,
    MIN(t.create_time) AS 最早task,
    MAX(t.create_time) AS 最晚task
FROM saas_compare_price.commodity_list_table_188605 cl
JOIN saas_onecode_price.commodity_pool_188605 c
  ON c.MY_COMMODITY_ID = cl.commodity_id
LEFT JOIN saas_compare_price.task_table_188605 t
  ON t.reference_id = cl.id
 AND t.table_name = '188605'
WHERE c.COMMODITY_POOL_ID = 222
  AND c.IS_MONITOR = 1
  AND c.CREATED_TIME >= '<business_date> 00:00:00'
  AND c.CREATED_TIME <  DATE_ADD('<business_date> 00:00:00', INTERVAL 1 DAY)
  AND cl.crawl_status = 1
GROUP BY cl.commodity_id, cl.id, cl.crawl_status
ORDER BY cl.commodity_id;
```

### 4.3 `crawl_status=2` 但没有 search 的例外

```sql
SELECT
    cl.id AS commodity_list_id,
    cl.commodity_id,
    cl.crawl_status
FROM saas_compare_price.commodity_list_table_188605 cl
JOIN saas_onecode_price.commodity_pool_188605 c
  ON c.MY_COMMODITY_ID = cl.commodity_id
WHERE c.COMMODITY_POOL_ID = 222
  AND c.IS_MONITOR = 1
  AND c.CREATED_TIME >= '<business_date> 00:00:00'
  AND c.CREATED_TIME <  DATE_ADD('<business_date> 00:00:00', INTERVAL 1 DAY)
  AND cl.crawl_status = 2
  AND NOT EXISTS (
      SELECT 1
      FROM saas_compare_price.search_table_188605 s
      WHERE s.commodity_list_id = cl.id
  )
LIMIT 20;
```

## 5. 性能规则

不要一开始把 `commodity_list + task_table + search_table` 两个一对多表同时大 JOIN 后再 `COUNT(DISTINCT)`；现场实测会很慢。

优先：

1. 先用 `crawl_status` 把目标商品缩小；
2. `crawl_status=1` 才 JOIN task；
3. `crawl_status=2` 才查 search；
4. 使用 `EXISTS / NOT EXISTS` 做是否存在判断；
5. 数量变化快时，每次结论都标记查询时点。

## 6. 已废弃的旧排查习惯

- `IS_SYNC_STANDARD=0` ≠ “没到 SaaS 页面”。
- `IS_SYNC_STANDARD=0` ≠ “爬虫还在跑”。
- `task_status=1` 不再作为商品级“是否已经爬虫回调”的第一判断字段。
- 对 `crawl_status=2` 的商品，默认不再从中控重新查起；先看 search/model/审批链。

