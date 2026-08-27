# 2026-08-27 — COMIX backlog / priority evidence

> AI-first incident note. Read-only SQL evidence only. Do not execute writes from this document.

## Scope

- Node: `10000000003`
- Mall: `comix`
- Current business tenant under investigation: `188605`
- Commodity pool: `222`

## Confirmed current tenant state

For today's tenant `188605` COMIX rows, a precise query showed:

- `weight_type=0`
- `weight=1`
- `task_status=00`
- `queue_status=00`
- 46 rows at the observation point
- all 46 had `enqueue_time IS NULL`
- create range `2026-08-27 08:42:08` through `2026-08-27 11:00:09`

The count grew from 42 to 46 while troubleshooting because new tasks continued to arrive.

## Important cross-tenant control

Same node + same mall was able to execute another tenant (`199177`). Rows created around `2026-08-26 18:59–19:00` were finally enqueued around `2026-08-27 10:59–11:01` and progressed through `00/01`, `01/02`, and `02/02`.

Therefore do NOT diagnose this as a global COMIX crawler outage or a dead node. The current evidence is consistent with a deep cross-tenant queue backlog.

## Backlog ahead of first tenant-188605 COMIX task

A broad waiting-row query (without narrowing to the same priority tier) returned:

- 104,857 older `00/00` COMIX tasks before the first 188605 task
- 1,194 distinct older batches
- oldest waiting create time: `2026-08-20 02:36:08`
- newest waiting create time before the first 188605 task: `2026-08-27 04:22:21`

This proves severe historical backlog depth, but it does NOT prove strict FIFO ordering because different `weight_type` values coexist.

## Hourly distribution of current COMIX `00/00` backlog (recent slice)

Observed current waiting rows by create hour:

| create hour | waiting rows | min create | max create |
|---|---:|---|---|
| 2026-08-26 19:00 | 2,281 | 19:00:45 | 19:27:51 |
| 2026-08-27 02:00 | 7,870 | 02:01:04 | 02:59:58 |
| 2026-08-27 03:00 | 5,442 | 03:00:01 | 03:59:58 |
| 2026-08-27 04:00 | 2,280 | 04:00:01 | 04:22:21 |
| 2026-08-27 08:00 | 3 | 08:42:08 | 08:52:08 |
| 2026-08-27 09:00 | 20 | 09:04:09 | 09:48:08 |
| 2026-08-27 10:00 | 23 | 10:02:09 | 10:58:13 |
| 2026-08-27 11:00 | 1 | 11:00:09 | 11:00:09 |

Key pattern: the 02:00 + 03:00 + 04:00 waves total 15,592 rows, exactly matching a previously observed `weight_type=2, weight=1` waiting-group count at that earlier snapshot. This strongly suggests the overnight 02:01–04:22 backlog is a distinct priority tier from the current tenant-188605 rows (`weight_type=0, weight=1`). Counts can move between snapshots as rows enqueue, so treat the equality as strong evidence, not an immutable invariant.

The relevant wait ahead of tenant 188605 should therefore be measured within the SAME priority tier (`weight_type=0, weight=1`) rather than using the full 104,857-row cross-priority backlog.

## Recommended exact same-priority queue-ahead query

```sql
WITH cnnc_first AS (
    SELECT MIN(create_time) AS first_time
    FROM central_control_sub_demo.s_task_info
    WHERE node_id = 10000000003
      AND is_valid = '1'
      AND mall = 'comix'
      AND batch_num LIKE '188605\\_%'
      AND create_time >= '2026-08-27 00:00:00'
      AND weight_type = '0'
      AND weight = 1
)
SELECT
    COUNT(*) AS same_priority_waiting_ahead,
    COUNT(DISTINCT batch_num) AS batches_ahead,
    MIN(create_time) AS oldest_waiting,
    MAX(create_time) AS newest_waiting_before_cnnc
FROM central_control_sub_demo.s_task_info s
CROSS JOIN cnnc_first c
WHERE s.node_id = 10000000003
  AND s.is_valid = '1'
  AND s.mall = 'comix'
  AND s.weight_type = '0'
  AND s.weight = 1
  AND s.task_status = '00'
  AND s.queue_status = '00'
  AND s.enqueue_time IS NULL
  AND s.create_time < c.first_time;
```

## Recommended throughput query

Do not estimate ETA from backlog alone. Measure recent same-priority enqueue throughput:

```sql
SELECT
    DATE_FORMAT(enqueue_time, '%Y-%m-%d %H:%i:00') AS enqueue_minute,
    COUNT(*) AS enqueued_rows
FROM central_control_sub_demo.s_task_info
WHERE node_id = 10000000003
  AND is_valid = '1'
  AND mall = 'comix'
  AND weight_type = '0'
  AND weight = 1
  AND enqueue_time IS NOT NULL
  AND enqueue_time >= NOW() - INTERVAL 15 MINUTE
GROUP BY DATE_FORMAT(enqueue_time, '%Y-%m-%d %H:%i:00')
ORDER BY enqueue_minute;
```

Use the same-priority waiting-ahead count plus observed enqueue throughput to estimate when tenant `188605` might be reached. Do not assume strict FIFO or infer ETA from the full cross-priority backlog.