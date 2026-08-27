# 2026-08-27 中核首次采集卡在分控待入队（AI 排查记录）

> 目标读者：AI Agent。本文记录 2026-08-27 中核 `tenant_id=188605`、`commodity_pool_id=222` 页面无新增时的只读 SQL 排查过程。数量是当时事实，后续不可直接复用，必须重跑基础查询。

## 1. 当前已确认现象

- 当天有新商品进入商品池，但 SaaS 同款页面仍未新增。
- SaaS 首次采集 `task_table_188605` 中存在大量 `task_status=1` 任务。
- 从 `task_id` 提取 exact batch 后，42 个 batch 全部“还没全部爬完”。
- 对 42 个 batch 的未完成分控子任务统计结果：
  - `task_status='00'`
  - `queue_status='00'`
  - 子任务数：462
  - 影响 batch：42
  - 最早 `create_time`：2026-08-27 08:42:08
  - 最晚 `create_time`：2026-08-27 10:44:09
- `462 / 42 = 11`，说明每个 batch 平均且恰好剩 11 个 `00/00` 子任务，优先怀疑固定商城/固定调度槽位未入队，而非随机单任务慢。

## 2. `central_control_sub_demo.s_task_info` 已确认字段

用户执行 `DESC central_control_sub_demo.s_task_info` 后确认字段包括：

- `task_id` bigint PK
- `center_task_id`
- `node_id`
- `rel_code`
- `commodity_pool_id`
- `exec_num`
- `task_num`
- `batch_num`
- `mall` varchar(64)（有索引）
- `busi_params`
- `busi_params_sha`
- `other_params`
- `task_type`
- `weight`
- `weight_type`
- `start_time`
- `end_time`
- `task_status`
- `queue_status`
- `enqueue_time`
- `push_status`
- `pull_status`
- `is_notice`
- `is_retry`
- `is_main`
- `retry_count`
- `is_valid`
- `is_collect`
- `create_time`
- `update_time`
- `remark`
- `reserved_field1`
- `reserved_field2`

注意：`start_time` 字段非空，不应仅凭字段名把它解释为“真实开始执行时间”；对 `task_status=00/queue_status=00` 的任务应以状态字段和 `enqueue_time` 共同判断是否真正入队/执行。

## 3. 下一步优先 SQL：按 mall 统计 00/00

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
    COUNT(*) AS 待入队任务数,
    COUNT(DISTINCT s.batch_num) AS 影响batch数,
    COUNT(DISTINCT s.node_id) AS node数,
    SUM(s.enqueue_time IS NULL) AS enqueue_time为空数,
    MIN(s.create_time) AS 最早创建时间,
    MAX(s.create_time) AS 最晚创建时间,
    MIN(s.update_time) AS 最早更新时间,
    MAX(s.update_time) AS 最晚更新时间
FROM central_control_sub_demo.s_task_info s
JOIN today_batch b
  ON b.batch_num = s.batch_num
WHERE s.is_valid = '1'
  AND s.task_status = '00'
  AND s.queue_status = '00'
GROUP BY s.mall
ORDER BY 待入队任务数 DESC, s.mall;
```

### AI 解读规则

- 若恰好 11 个 mall，每个 mall 都是 `42` 个任务、影响 `42` 个 batch：高度支持“固定 11 个商城任务统一未入队”。继续查 mall 维度的 node/weight/更新时间和调度服务，不要再归因于商品级爬虫慢。
- 若少数 mall 占绝大多数任务：优先定位对应商城调度/资源。
- 若 `enqueue_time` 基本全 NULL：进一步支持任务尚未真正入队。
- 若 `enqueue_time` 非 NULL 但 `queue_status=00`：需要开发确认状态机语义，不能仅凭字段名推断。

## 4. 安全边界

仅保存只读 SELECT。不得把排障需要变成生产 UPDATE/DELETE/INSERT。任何状态修复、重试、重启或提优都需单独确认和对应 runbook/工具约束。
