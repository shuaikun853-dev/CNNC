# 2026-08-27 中核首次采集待入队排查

> 目标读者：AI Agent。只记录只读 SQL、已验证表关系、实测结果和推理边界。

## 1. 现象

2026-08-27 上午，中核 SaaS 同款页面仍停留在 210 条。当天 `commodity_pool_188605` 已有 39 条新商品进入，但未带来页面新增。

## 2. 当天采集链路实测

- 当天 SaaS `task_table_188605` 存在 `task_status=1` 的采集任务，数量约 666。
- 将 `task_id` 提取为 batch 后查 `central_control_sub_demo.s_task_info`，当天 batch 已进入 demo 分控；这与 2026-08-26 的“batch 根本未进入分控”不同。
- batch 完整性检查显示：`还没全部爬完 = 42 batch / 42 商品`。
- 对未完成子任务统计：
  - `task_status='00'`
  - `queue_status='00'`
  - 子任务数：462
  - 影响 batch：42
  - 最早创建：2026-08-27 08:42:08
  - 最晚创建：2026-08-27 10:44:09

## 3. 商城级规律

462 = 42 batch × 11 mall。按 `mall` 分组后，以下 11 个 mall 每个都恰好有 42 条 `00/00` 任务，影响全部 42 个 batch：

- colipu
- comix
- ehsy
- guangbo
- jd
- lxwl
- lxwlmall
- mymro
- nbdeli
- ofs
- zkh

每个 mall 的统计均为：

- 待入队任务数 = 42
- 影响 batch 数 = 42
- node 数 = 1
- `enqueue_time IS NULL` 数 = 42

因此不是随机商品或个别任务慢，而是固定 11 个商城在全部 42 个 batch 上都没有入队。

## 4. 当前最强结论

这些任务不是“爬虫正在慢慢执行”，而是**尚未进入执行队列**：

```text
s_task_info 已创建
  ↓
task_status=00
queue_status=00
enqueue_time IS NULL
  ↓
【入队调度 / 节点可用性 / mall 调度配置】 ← 当前故障边界
  ↓
实际爬虫执行
```

页面不新增的当前直接原因是：42 个商品对应的整批采集都被这 11 个 mall 的未入队任务阻塞，batch 无法整体完成，因此后续回调、模型、同款收口未能继续。

## 5. 下一步只读 SQL

### 5.1 看 11 个 mall 是否实际落在同一个 node_id

```sql
WITH today_batch AS (
    SELECT DISTINCT
        SUBSTRING_INDEX(REPLACE(t.task_id, 'SAAS-', ''), '_', 2) AS batch_num
    FROM saas_onecode_price.commodity_pool_188605 c
    JOIN saas_compare_price.commodity_list_table_188605 cl
      ON cl.commodity_id = c.MY_COMMODITY_ID
    JOIN saas_compare_price.task_table_188605 t
      ON t.reference_id = cl.id
     AND t.table_name = '188605'
    WHERE c.COMMODITY_POOL_ID = 222
      AND c.IS_MONITOR = 1
      AND c.CREATED_TIME >= '2026-08-27 00:00:00'
      AND c.CREATED_TIME <  '2026-08-28 00:00:00'
)
SELECT
    s.node_id,
    COUNT(*) AS 待入队任务数,
    COUNT(DISTINCT s.mall) AS mall数,
    GROUP_CONCAT(DISTINCT s.mall ORDER BY s.mall) AS malls,
    COUNT(DISTINCT s.batch_num) AS batch数,
    SUM(s.enqueue_time IS NULL) AS enqueue_time为空数,
    MIN(s.create_time) AS 最早创建,
    MAX(s.create_time) AS 最晚创建
FROM central_control_sub_demo.s_task_info s
JOIN today_batch b ON b.batch_num = s.batch_num
WHERE s.is_valid='1'
  AND s.task_status='00'
  AND s.queue_status='00'
GROUP BY s.node_id
ORDER BY 待入队任务数 DESC;
```

### 5.2 同时对比各 mall 已完成/未执行情况

```sql
WITH today_batch AS (
    SELECT DISTINCT
        SUBSTRING_INDEX(REPLACE(t.task_id, 'SAAS-', ''), '_', 2) AS batch_num
    FROM saas_onecode_price.commodity_pool_188605 c
    JOIN saas_compare_price.commodity_list_table_188605 cl
      ON cl.commodity_id = c.MY_COMMODITY_ID
    JOIN saas_compare_price.task_table_188605 t
      ON t.reference_id = cl.id
     AND t.table_name = '188605'
    WHERE c.COMMODITY_POOL_ID = 222
      AND c.IS_MONITOR = 1
      AND c.CREATED_TIME >= '2026-08-27 00:00:00'
      AND c.CREATED_TIME <  '2026-08-28 00:00:00'
)
SELECT
    s.mall,
    s.node_id,
    s.task_status,
    s.queue_status,
    COUNT(*) AS 任务数,
    COUNT(DISTINCT s.batch_num) AS batch数,
    SUM(s.enqueue_time IS NULL) AS enqueue_time为空数
FROM central_control_sub_demo.s_task_info s
JOIN today_batch b ON b.batch_num=s.batch_num
WHERE s.is_valid='1'
GROUP BY s.mall,s.node_id,s.task_status,s.queue_status
ORDER BY s.mall,s.task_status,s.queue_status;
```

## 6. 推理边界

当前还不能仅凭数据库断言具体是“某个进程挂了”还是“节点不可用/商城调度配置异常”。可以确认的是：固定 11 个 mall 的任务在全部 42 个 batch 中从未入队。下一步应优先检查这些 mall 的 `node_id` 是否一致，以及对应入队调度/节点状态，而不是继续查模型或 SaaS 页面。