# 招商局提优集成约束

在 `auto_crawl_task.py` 的正式链路中保持以下顺序：

```text
create task
-> 按精确 task_name 查询唯一主任务
-> 校验主任务商城与 MALL_CONFIG 一致
-> enable task
-> 轮询 s_task_info 当前商城子任务
-> 按子任务 task_id 主键更新 weight_type=-1
-> 验证
-> wait/export
```

调用 helper 时必须传递完整复合身份：

```python
priority_result = boost_zhaoshang_task_priority_after_create(
    task_name=task_name,
    task_number=task_number,
    mall_code=MALL_CONFIG[mall_key]["identification"],
    expected_subtask_count=len(subtasks),
)
```

严禁把 `task_number` 当唯一任务标识。同一 `RW-...` 可以属于多个商城。查询必须先使用 `idx_opt_distinctmall` 的首列 `mall`，再以 `task_num` 和任务 marker 对应的 `create_time` 窗口过滤；更新只能使用查得的 `task_id` 主键。不得使用 `batch_num LIKE`、task_num-only UPDATE、`start_time`、宽范围回退、`CREATE INDEX` 或 `ALTER TABLE`。

子任务可能延迟或分批落表，调用方必须传入 add payload 的有效子任务数；保留最多约 60 次、每次约 2 秒的轮询，只有命中数恰好等于预期数才进入写事务，少于预期继续等待，多于预期失败。写事务先按已确认的主键 `SELECT ... FOR UPDATE` 做当前读，不能用宽范围 mall 查询加锁。0 行不得执行 UPDATE。已全为 `-1` 返回独立幂等状态；真正执行 UPDATE 后没有正影响行且任务未全部完成时必须暂停。

日志必须包含 task_name、task_number、mall_code、marker_time、轮询次数、命中 ID 数、已为 -1 数、更新候选数和 affected_rows。数据库凭据只从 `ZHAOSHANG_DB_*` 环境变量读取，禁止写入代码或文档。
