# 2026-08-26 中核首次采集未进入 SaaS 页面排查

> **用途：给未来 AI 复盘和复用。** 本文记录 2026-08-26 一次真实 SQL 排查的完整证据链、有效 SQL、被证伪的思路和最终定位。
>
> 所有 SQL 均为只读 SELECT。Agent 不应自行执行生产 SQL；由用户手工执行并回传结果。

## 1. 背景与最终结论

当天客户侧：

- 客户商品：138 个，`documentId` 唯一 138 个。
- SaaS 同款页面：210 行。
- 210 行实际只覆盖 43 个客户商品 ID。
- 210 行全部可以匹配回客户商品，不存在 SaaS 页面孤儿 ID。

**不要把 138 vs 210 当异常。** 一个客户商品可以有多个同款行。

当时商品池状态：

```text
IS_SYNC_STANDARD=0 : 91
IS_SYNC_STANDARD=1 : 43
IS_SYNC_STANDARD=2 : 4
```

对 91 个 `IS_SYNC_STANDARD=0` 商品进一步分类，得到：

```text
A_没有生成采集任务         1
C_仍有爬虫运行            47
F_已有同款但仍未收口       43
```

即：

```text
91 = 47 + 43 + 1
```

最终定位：

1. **47 个商品：** SaaS 已为每个商品生成 18 个 task，共 846 个，全部 `task_status=1`；但对应 47 个 exact batch 在 `central_control_sub_demo.s_task_info` 中全部为 0。正常 batch 对照有 18 条中控任务且 `02/02`。故障边界是 **SaaS task 创建后 → demo 分控投递/创建之前**。
2. **43 个商品：** 已无 running task、模型无 pending、均有 `audit_result=2`；但没有有效 `commodity_pool_relation`，并且 43/43 都仍存在 `sync_state=1` 的同款候选，共 210 行。进一步分类：**38 个“部分已处理、仍有待处理” + 5 个“全部仍待处理”**。故障边界是 **模型同款结果 → 后续结果消费/收口/relation 生成**。
3. **1 个商品：** 已进入商品池/commodity_list，但没有生成采集 task。故障边界是 **首次采集 task 生成逻辑**。

## 2. 第一阶段：确认商品池状态

### 2.1 当天商品的 `IS_SYNC_STANDARD` 分布

```sql
SELECT
    IS_SYNC_STANDARD,
    COUNT(*) AS 商品数
FROM saas_onecode_price.commodity_pool_188605
WHERE COMMODITY_POOL_ID = 222
  AND IS_MONITOR = 1
  AND CREATED_TIME >= '2026-08-26 00:00:00'
GROUP BY IS_SYNC_STANDARD
ORDER BY IS_SYNC_STANDARD;
```

本次过程中的实测曾从：

```text
0 92
1 43
2 3
```

变化为：

```text
0 91
1 43
2 4
```

说明后台状态在排查过程中仍有少量变化。未来 AI 引用数量前必须重跑，不要把历史数字写死。

### 2.2 确认全部客户商品是否进入首次同款流程

```sql
SELECT
    COUNT(DISTINCT c.MY_COMMODITY_ID) AS 客户商品数,
    COUNT(DISTINCT cl.commodity_id) AS 已进入同款流程数,
    COUNT(DISTINCT c.MY_COMMODITY_ID) - COUNT(DISTINCT cl.commodity_id) AS 尚未进入同款流程数
FROM saas_onecode_price.commodity_pool_188605 c
LEFT JOIN saas_compare_price.commodity_list_table_188605 cl
  ON cl.commodity_id = c.MY_COMMODITY_ID
WHERE c.COMMODITY_POOL_ID = 222
  AND c.IS_MONITOR = 1
  AND c.CREATED_TIME >= '2026-08-26 00:00:00';
```

实测：

```text
客户商品数 = 138
已进入同款流程数 = 138
尚未进入同款流程数 = 0
```

结论：不是 `commodity_list` 漏入导致的大面积缺失。

## 3. 第二阶段：看 task / search 大盘

### 3.1 task 状态

本次实测：

```text
task_status=1 爬取中     846 tasks，影响 47 个客户商品
task_status=2 爬取成功  1620 tasks，影响 90 个客户商品
```

**注意：47 和 90 的商品集合可能重叠，不能相加。**

通用 SQL：

```sql
SELECT
    t.task_status,
    COUNT(*) AS 任务数,
    COUNT(DISTINCT cl.commodity_id) AS 商品数
FROM saas_onecode_price.commodity_pool_188605 c
JOIN saas_compare_price.commodity_list_table_188605 cl
  ON cl.commodity_id = c.MY_COMMODITY_ID
JOIN saas_compare_price.task_table_188605 t
  ON t.reference_id = cl.id
 AND t.table_name = '188605'
WHERE c.COMMODITY_POOL_ID = 222
  AND c.IS_MONITOR = 1
  AND c.CREATED_TIME >= '2026-08-26 00:00:00'
GROUP BY t.task_status
ORDER BY t.task_status;
```

### 3.2 search / 模型结果状态

本次实测：

```text
audit_result=2 识别为同款       290 rows，影响 85 个商品
audit_result=3 识别为非同款   25993 rows，影响 89 个商品
```

未看到明显 NULL/-1/-2 模型 pending 大盘。

```sql
SELECT
    s.audit_result,
    COUNT(*) AS 行数,
    COUNT(DISTINCT cl.commodity_id) AS 商品数
FROM saas_onecode_price.commodity_pool_188605 c
JOIN saas_compare_price.commodity_list_table_188605 cl
  ON cl.commodity_id = c.MY_COMMODITY_ID
JOIN saas_compare_price.search_table_188605 s
  ON s.commodity_list_id = cl.id
WHERE c.COMMODITY_POOL_ID = 222
  AND c.IS_MONITOR = 1
  AND c.CREATED_TIME >= '2026-08-26 00:00:00'
GROUP BY s.audit_result
ORDER BY s.audit_result;
```

## 4. 核心分类 SQL：把 `IS_SYNC_STANDARD=0` 拆成真实阶段

这条是本次最有价值的分类 SQL之一。

```sql
WITH base AS (
    SELECT
        c.MY_COMMODITY_ID
    FROM saas_onecode_price.commodity_pool_188605 c
    WHERE c.COMMODITY_POOL_ID = 222
      AND c.IS_MONITOR = 1
      AND c.CREATED_TIME >= '2026-08-26 00:00:00'
      AND c.IS_SYNC_STANDARD = 0
),

task_stat AS (
    SELECT
        cl.commodity_id,
        COUNT(t.reference_id) AS total_tasks,
        SUM(t.task_status = 1) AS running_tasks,
        SUM(t.task_status = 2) AS success_tasks,
        SUM(t.task_status = 3) AS failed_tasks
    FROM base b
    JOIN saas_compare_price.commodity_list_table_188605 cl
      ON cl.commodity_id = b.MY_COMMODITY_ID
    LEFT JOIN saas_compare_price.task_table_188605 t
      ON t.reference_id = cl.id
     AND t.table_name = '188605'
    GROUP BY cl.commodity_id
),

search_stat AS (
    SELECT
        cl.commodity_id,
        COUNT(s.commodity_list_id) AS search_rows,
        SUM(s.audit_result = 2) AS same_rows,
        SUM(s.audit_result = 3) AS non_same_rows,
        SUM(
            s.audit_result IS NULL
            OR s.audit_result IN (-1, -2)
        ) AS model_pending_rows
    FROM base b
    JOIN saas_compare_price.commodity_list_table_188605 cl
      ON cl.commodity_id = b.MY_COMMODITY_ID
    LEFT JOIN saas_compare_price.search_table_188605 s
      ON s.commodity_list_id = cl.id
    GROUP BY cl.commodity_id
)

SELECT
    CASE
        WHEN COALESCE(t.total_tasks, 0) = 0
            THEN 'A_没有生成采集任务'

        WHEN COALESCE(t.failed_tasks, 0) > 0
            THEN 'B_存在爬虫失败'

        WHEN COALESCE(t.running_tasks, 0) > 0
            THEN 'C_仍有爬虫运行'

        WHEN COALESCE(t.success_tasks, 0) > 0
         AND COALESCE(s.search_rows, 0) = 0
            THEN 'D_爬虫成功但没有search结果'

        WHEN COALESCE(s.model_pending_rows, 0) > 0
            THEN 'E_模型未处理完成'

        WHEN COALESCE(s.same_rows, 0) > 0
            THEN 'F_已有同款但仍未收口'

        WHEN COALESCE(s.search_rows, 0) > 0
         AND COALESCE(s.same_rows, 0) = 0
            THEN 'G_全部非同款但仍未收口'

        ELSE 'H_其他'
    END AS 当前阶段,

    COUNT(*) AS 商品数

FROM base b
LEFT JOIN task_stat t
  ON t.commodity_id = b.MY_COMMODITY_ID
LEFT JOIN search_stat s
  ON s.commodity_id = b.MY_COMMODITY_ID
GROUP BY 当前阶段
ORDER BY 当前阶段;
```

本次结果：

```text
A_没有生成采集任务    1
C_仍有爬虫运行       47
F_已有同款但仍未收口  43
```

## 5. 47 个“仍有爬虫运行”商品的精确定位

### 5.1 确认不是历史残留 task

```sql
SELECT
    DATE(t.create_time) AS 任务日期,
    COUNT(*) AS 任务数,
    COUNT(DISTINCT c.MY_COMMODITY_ID) AS 商品数,
    MIN(t.create_time) AS 最早任务时间,
    MAX(t.create_time) AS 最晚任务时间
FROM saas_onecode_price.commodity_pool_188605 c
JOIN saas_compare_price.commodity_list_table_188605 cl
  ON cl.commodity_id = c.MY_COMMODITY_ID
JOIN saas_compare_price.task_table_188605 t
  ON t.reference_id = cl.id
WHERE c.COMMODITY_POOL_ID = 222
  AND c.IS_MONITOR = 1
  AND c.CREATED_TIME >= '2026-08-26 00:00:00'
  AND c.IS_SYNC_STANDARD = 0
  AND t.table_name = '188605'
  AND t.task_status = 1
GROUP BY DATE(t.create_time)
ORDER BY 任务日期 DESC;
```

实测：

```text
2026-08-26  846  47  2026-08-26 16:55:32  2026-08-26 17:44:07
```

结论：846 条全部是当天新任务，不是历史脏状态。

### 5.2 看 task_id 格式

样例：

```text
SAAS-188605_704827316150657024_279774
SAAS-188605_704827316150657024_469542
...
```

同一个商品的 `reference_id` 相同，一个 batch 下有多个 task。

### 5.3 从 task_id 提取 batch，并确认每批任务数

```sql
SELECT
    SUBSTRING_INDEX(
        REPLACE(t.task_id, 'SAAS-', ''),
        '_',
        2
    ) AS batch_num,
    COUNT(*) AS saas_running_tasks,
    COUNT(DISTINCT c.MY_COMMODITY_ID) AS 商品数,
    MIN(t.create_time) AS 最早任务,
    MAX(t.create_time) AS 最晚任务
FROM saas_onecode_price.commodity_pool_188605 c
JOIN saas_compare_price.commodity_list_table_188605 cl
  ON cl.commodity_id = c.MY_COMMODITY_ID
JOIN saas_compare_price.task_table_188605 t
  ON t.reference_id = cl.id
WHERE c.COMMODITY_POOL_ID = 222
  AND c.IS_MONITOR = 1
  AND c.CREATED_TIME >= '2026-08-26 00:00:00'
  AND c.IS_SYNC_STANDARD = 0
  AND t.table_name = '188605'
  AND t.task_status = 1
  AND t.create_time >= '2026-08-26 00:00:00'
GROUP BY batch_num
ORDER BY saas_running_tasks DESC;
```

实测规律：

```text
47 个 batch
每个 batch = 18 个 task
每个 batch = 1 个客户商品
47 × 18 = 846
```

这是关键证据：不是 846 个随机散点，而是 47 个商品整批没有收口。

### 5.4 exact batch 查 demo 分控

```sql
WITH pending_batch AS (
    SELECT DISTINCT
        SUBSTRING_INDEX(
            REPLACE(t.task_id, 'SAAS-', ''),
            '_',
            2
        ) AS batch_num
    FROM saas_onecode_price.commodity_pool_188605 c
    JOIN saas_compare_price.commodity_list_table_188605 cl
      ON cl.commodity_id = c.MY_COMMODITY_ID
    JOIN saas_compare_price.task_table_188605 t
      ON t.reference_id = cl.id
    WHERE c.COMMODITY_POOL_ID = 222
      AND c.IS_MONITOR = 1
      AND c.CREATED_TIME >= '2026-08-26 00:00:00'
      AND c.IS_SYNC_STANDARD = 0
      AND t.table_name = '188605'
      AND t.task_status = 1
      AND t.create_time >= '2026-08-26 00:00:00'
)
SELECT
    p.batch_num,
    COUNT(s.task_id) AS 中控任务数,
    SUM(CASE WHEN s.task_status = '00' THEN 1 ELSE 0 END) AS 待执行,
    SUM(CASE WHEN s.task_status = '01' THEN 1 ELSE 0 END) AS 执行中,
    SUM(CASE WHEN s.task_status = '02' THEN 1 ELSE 0 END) AS 执行成功,
    SUM(CASE WHEN s.task_status = '03' THEN 1 ELSE 0 END) AS 执行失败,
    SUM(CASE WHEN s.queue_status = '00' THEN 1 ELSE 0 END) AS 等待入队,
    SUM(CASE WHEN s.queue_status = '01' THEN 1 ELSE 0 END) AS 已入队,
    SUM(CASE WHEN s.queue_status = '02' THEN 1 ELSE 0 END) AS 已出队
FROM pending_batch p
LEFT JOIN central_control_sub_demo.s_task_info s
  ON s.batch_num = p.batch_num
 AND s.commodity_pool_id = '222'
 AND s.is_valid = '1'
GROUP BY p.batch_num
ORDER BY p.batch_num;
```

实测：**47 个 batch 全部 `中控任务数=0`。**

### 5.5 正常 batch 对照实验

正常已成功回调 batch：

```text
188605_704824207118622720
```

异常 batch：

```text
188605_704827316150657024
```

SQL：

```sql
SELECT
    '正常已回调batch' AS 类型,
    COUNT(*) AS 中控任务数,
    GROUP_CONCAT(DISTINCT task_status ORDER BY task_status) AS task_status,
    GROUP_CONCAT(DISTINCT queue_status ORDER BY queue_status) AS queue_status,
    MIN(create_time) AS 最早时间,
    MAX(create_time) AS 最晚时间
FROM central_control_sub_demo.s_task_info
WHERE batch_num = '188605_704824207118622720'

UNION ALL

SELECT
    '异常未收口batch' AS 类型,
    COUNT(*) AS 中控任务数,
    GROUP_CONCAT(DISTINCT task_status ORDER BY task_status) AS task_status,
    GROUP_CONCAT(DISTINCT queue_status ORDER BY queue_status) AS queue_status,
    MIN(create_time) AS 最早时间,
    MAX(create_time) AS 最晚时间
FROM central_control_sub_demo.s_task_info
WHERE batch_num = '188605_704827316150657024';
```

实测：

```text
正常已回调batch   18   02   02   2026-08-26 17:01:07 ~ 17:01:17
异常未收口batch    0
```

**结论：47 个商品不是“爬虫还慢慢在跑”，而是 SaaS 已建 task，但 exact batch 没进入 demo 分控。**

### 5.6 Spider 表的辅助证据

直接用这 846 个完整 `task_id` 查：

- `shopping_spider.spider_sync_log`
- `shopping_spider.spider_sync_log_complated`

实测：

```text
总task_id        846
已成功回调         0
当前回调表中       0
两张表都找不到   846
```

结合 exact batch 在 `s_task_info` 为 0，可作为“下游根本没建立这批任务”的辅助证据。

但 AI 应优先使用 **exact batch → central_control_sub_demo.s_task_info** 定位投递层，而不是只看 spider 表。

## 6. 43 个“已有同款但仍未收口”商品的精确定位

### 6.1 先证明它们不是采集/模型 pending

这 43 个能进入分类 F，已经满足：

- task 已生成；
- 无 `task_status=1`；
- 无 `task_status=3`；
- search 有结果；
- 无模型 pending；
- 存在 `audit_result=2`；
- 但 `IS_SYNC_STANDARD=0`。

### 6.2 查是否已经生成有效最终同款关系

```sql
SELECT
    CASE
        WHEN EXISTS (
            SELECT 1
            FROM saas_onecode_price.commodity_pool_relation_188605 r
            WHERE r.SRC_COMMODITY_POOL_ID = 222
              AND r.SRC_MY_COMMODITY_ID = c.MY_COMMODITY_ID
              AND r.IS_VALID = 1
        )
        THEN 'A_已有有效同款关系但状态仍为0'
        ELSE 'B_模型已有同款但未生成有效同款关系'
    END AS 状态,
    COUNT(*) AS 商品数
FROM saas_onecode_price.commodity_pool_188605 c
WHERE c.COMMODITY_POOL_ID = 222
  AND c.IS_MONITOR = 1
  AND c.CREATED_TIME >= '2026-08-26 00:00:00'
  AND c.IS_SYNC_STANDARD = 0
  AND EXISTS (
      SELECT 1
      FROM saas_compare_price.commodity_list_table_188605 cl
      JOIN saas_compare_price.search_table_188605 s
        ON s.commodity_list_id = cl.id
      WHERE cl.commodity_id = c.MY_COMMODITY_ID
        AND s.audit_result = 2
  )
  AND NOT EXISTS (
      SELECT 1
      FROM saas_compare_price.commodity_list_table_188605 cl
      JOIN saas_compare_price.task_table_188605 t
        ON t.reference_id = cl.id
       AND t.table_name = '188605'
      WHERE cl.commodity_id = c.MY_COMMODITY_ID
        AND t.task_status = 1
  )
GROUP BY 状态;
```

实测：

```text
B_模型已有同款但未生成有效同款关系   43
```

结论：不是简单的 `IS_SYNC_STANDARD` 单字段回写遗漏；**连最终有效 relation 都没生成。**

### 6.3 查这 43 个商品的 `sync_state`

```sql
WITH target AS (
    SELECT c.MY_COMMODITY_ID
    FROM saas_onecode_price.commodity_pool_188605 c
    WHERE c.COMMODITY_POOL_ID = 222
      AND c.IS_MONITOR = 1
      AND c.CREATED_TIME >= '2026-08-26 00:00:00'
      AND c.IS_SYNC_STANDARD = 0
      AND EXISTS (
          SELECT 1
          FROM saas_compare_price.commodity_list_table_188605 cl
          JOIN saas_compare_price.search_table_188605 s
            ON s.commodity_list_id = cl.id
          WHERE cl.commodity_id = c.MY_COMMODITY_ID
            AND s.audit_result = 2
      )
      AND NOT EXISTS (
          SELECT 1
          FROM saas_compare_price.commodity_list_table_188605 cl
          JOIN saas_compare_price.task_table_188605 t
            ON t.reference_id = cl.id
           AND t.table_name = '188605'
          WHERE cl.commodity_id = c.MY_COMMODITY_ID
            AND t.task_status = 1
      )
      AND NOT EXISTS (
          SELECT 1
          FROM saas_onecode_price.commodity_pool_relation_188605 r
          WHERE r.SRC_COMMODITY_POOL_ID = 222
            AND r.SRC_MY_COMMODITY_ID = c.MY_COMMODITY_ID
            AND r.IS_VALID = 1
      )
)
SELECT
    s.sync_state,
    COUNT(*) AS 同款候选行数,
    COUNT(DISTINCT t.MY_COMMODITY_ID) AS 商品数
FROM target t
JOIN saas_compare_price.commodity_list_table_188605 cl
  ON cl.commodity_id = t.MY_COMMODITY_ID
JOIN saas_compare_price.search_table_188605 s
  ON s.commodity_list_id = cl.id
WHERE s.audit_result = 2
GROUP BY s.sync_state
ORDER BY s.sync_state;
```

实测：

```text
sync_state=0   38 行   38 个商品
sync_state=1  210 行   43 个商品
```

关键解释：

- 43/43 个商品全部仍有 `sync_state=1` 的同款候选；
- 38 个商品同时已经有至少一部分 `sync_state=0`；
- 因此不能把“页面没显示”简单归因于极值过滤；这些商品后续处理确实还没有完全收口。

### 6.4 再拆成“部分处理”和“全部未处理”

```sql
WITH target AS (
    SELECT c.MY_COMMODITY_ID
    FROM saas_onecode_price.commodity_pool_188605 c
    WHERE c.COMMODITY_POOL_ID = 222
      AND c.IS_MONITOR = 1
      AND c.CREATED_TIME >= '2026-08-26 00:00:00'
      AND c.IS_SYNC_STANDARD = 0
      AND EXISTS (
          SELECT 1
          FROM saas_compare_price.commodity_list_table_188605 cl
          JOIN saas_compare_price.search_table_188605 s
            ON s.commodity_list_id = cl.id
          WHERE cl.commodity_id = c.MY_COMMODITY_ID
            AND s.audit_result = 2
            AND s.sync_state = 1
      )
      AND NOT EXISTS (
          SELECT 1
          FROM saas_compare_price.commodity_list_table_188605 cl
          JOIN saas_compare_price.task_table_188605 t
            ON t.reference_id = cl.id
          WHERE cl.commodity_id = c.MY_COMMODITY_ID
            AND t.table_name = '188605'
            AND t.task_status = 1
      )
)
SELECT
    CASE
        WHEN EXISTS (
            SELECT 1
            FROM saas_compare_price.commodity_list_table_188605 cl
            JOIN saas_compare_price.search_table_188605 s
              ON s.commodity_list_id = cl.id
            WHERE cl.commodity_id = t.MY_COMMODITY_ID
              AND s.audit_result = 2
              AND s.sync_state = 0
        )
        THEN '部分已处理_仍有待处理'
        ELSE '全部仍待处理'
    END AS 状态,
    COUNT(*) AS 商品数
FROM target t
GROUP BY 状态;
```

实测：

```text
部分已处理_仍有待处理   38
全部仍待处理              5
```

因此 43 条的开发反馈应是：

> 采集和模型识别已经完成，但模型同款结果后的消费/处理/收口没有走完。43 个商品全部仍有 `sync_state=1` 候选，共 210 行；38 个商品只处理了一部分，5 个商品一个同款候选都没推进；最终也没有生成有效 `commodity_pool_relation`。

## 7. 1 个“没有生成采集任务”商品

分类 SQL 已确认有 1 个：

```text
commodity_pool 已存在
commodity_list 已存在
但 task_table 无任务
```

开发应排查：

```text
commodity_list 创建完成
    ↓
首次采集 task 生成条件 / handler
    ↓
task_table
```

本次没有继续追到具体代码/日志，因此不要在未来文档里把这一条写成已确定某个服务 bug。

## 8. 本次被证伪/不应复用的排查思路

### 8.1 错误：`task_table.task_id = s_task_info.exec_num`

曾执行类似：

```sql
... LEFT JOIN central_control_sub_demo.s_task_info s
ON s.exec_num = x.task_id
```

得到：

```text
NO_MATCH / NO_MATCH / 846
```

这不能证明“846 个任务没有中控”。对于首次采集，正确关联应从 `task_id` 提取 batch，再 JOIN：

```text
s_task_info.batch_num
```

### 8.2 不够精确：全局 `batch_num LIKE '188605%'`

全局大盘曾看到：

```text
02/01 3
02/02 1668
```

它只能说明今天中核大盘中控状态，不足以证明目标 47 个商品的 exact batch 已进入分控。

最终 exact batch 查询才发现目标 47 个 batch 全部为 0。

### 8.3 不要优先用 `temporary_data_crawl.dc_task_execution_info*`

曾对目标 846 个 task_id 查到极少量 `1 / 2` 匹配，无法解释这条 SaaS 首次采集链路。除非后续研发明确字段/流程映射，否则不要把它作为主证据。

### 8.4 不要把 `audit_result=2` 直接等价为页面同款

本次有 43 个商品已经有 `audit_result=2`，但：

- 无有效 relation；
- 仍有 210 条 `sync_state=1`；
- `IS_SYNC_STANDARD` 仍为 0；
- 页面不显示。

所以未来 AI 必须继续查 `sync_state + relation + IS_SYNC_STANDARD`。

## 9. 对开发的简短反馈模板

不要写得像正式审计报告，可以直接说：

```text
今天中核还有 91 个商品没正常到 SaaS 页面，我这边拆了一下：

1）47 个：SaaS 已经给每个商品生成了 18 个采集 task，共 846 个，task_status 都还是 1；但是把 task_id 对应的 batch 提出来以后，这 47 个 batch 在 central_control_sub_demo.s_task_info 里全是 0。正常 batch 对照是 18 条，状态 02/02。看起来是 task 建出来了，但没有成功进入 demo 分控，麻烦查下 task 创建之后到分控投递这段。

2）43 个：已经没有 running task，模型也没有 pending，而且都有 audit_result=2；但是没有生成有效 commodity_pool_relation，43 个商品都还有 sync_state=1 的同款结果，一共 210 条。其中 38 个是处理了一部分还有待处理，5 个是全部还没处理。麻烦看下模型结果后面的 handler/stream/收口是不是积压了。

3）还有 1 个商品进了 commodity_list，但连采集 task 都没生成，也麻烦一起看下 task 生成条件。
```

## 10. 下次同类问题的最短路径

AI 下次看到“客户商品没到 SaaS 页面”，推荐直接：

1. 重跑当天 `IS_SYNC_STANDARD` 分布；
2. 跑第 4 节核心分类 SQL；
3. 对 C 类：提取 exact batch → `central_control_sub_demo.s_task_info`；
4. 对 F 类：`audit_result=2` → `sync_state` → `commodity_pool_relation`；
5. 对 A 类：直接查 task 生成链路；
6. 数量随时间变化，禁止照搬 47/43/1。
