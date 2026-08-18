# 招商局当前任务提优说明

> 生产流程以 `AGENTS.md`、`docs/runbooks/STEP_1_PRICE_AUDIT.md`、`docs/runbooks/STEP_3_NO_PUSH_PROCESSING.md` 和当前代码为准。本文只说明提优身份与 SQL 边界。

## 正式调用链

```text
scripts/audit_saas_excel_file.py 或 scripts/process_monitor_no_push_zhaoshang.py
-> E:\.A2026\招商局自动建任务\external_tools\crawl_task_tool\auto_crawl_task.py
-> 同目录 zhaoshang_priority_boost.py
```

仓库内 `zhaoshang_priority_boost_integration/zhaoshang_priority_boost.py` 是生产 helper 的镜像，两份必须保持一致。

## 复合身份

同一个 `task_number` 可以同时属于多个商城，禁止把它当唯一任务标识。主流程必须先用精确 `task_name` 查询唯一主任务，并得到：

- `task_name`
- `task_number`
- 从现有 `MALL_CONFIG[mall_key]["identification"]` 读取的 `mall_code`
- 本次 add payload 的有效子任务数量 `expected_subtask_count`

任务名固定为 `cnnc_current_opt_<商城名>_<至少14位纯数字marker>`。marker 前 14 位解析为创建基准时间，安全窗口为 `[marker-2分钟, marker+15分钟)`。

## SQL 边界

先使用 `mall` 首列的现有索引定位当前商城候选：

```sql
SELECT task_id, task_num, mall, create_time, task_status, weight_type, is_valid
FROM central_control_sub_zsz.s_task_info
FORCE INDEX (idx_opt_distinctmall)
WHERE mall = ?
  AND task_status IN ('00', '01')
  AND is_valid = '1'
  AND task_num = ?
  AND create_time >= ?
  AND create_time < ?;
```

子任务可能分批落表，只有查询命中数与 `expected_subtask_count` 完全一致时才进入写事务；少于预期继续轮询，多于预期失败。写事务先按已确认的主键 `SELECT ... FOR UPDATE` 做当前读和精确锁定。严格校验后，只按子任务主键分批更新：

```sql
UPDATE central_control_sub_zsz.s_task_info
SET weight_type = '-1'
WHERE task_id IN (?, ...)
  AND task_status IN ('00', '01')
  AND is_valid = '1'
  AND weight_type <> '-1';
```

禁止 task_num-only UPDATE、`batch_num LIKE`、`start_time` 定位、宽范围 fallback、通用 SQL 入口、`CREATE INDEX` 或 `ALTER TABLE`。数据库配置仅通过 `ZHAOSHANG_DB_*` 环境变量和项目受控加载器提供，文档与日志不得保存实际值。

## 结果语义

- 只有 `affected_rows>0` 可称本次已提优。
- 预查确认所有目标行已为 `-1` 时，返回 `already_highest_priority`，作为幂等成功单独记录。
- 若定位后所有目标行已完成，返回 `task_completed_unconfirmed`，不声称已提优。
- 已有活动任务却没有本次提优证据时记录 `ACTIVE_UNCONFIRMED` 并暂停。
- 其他 0 行、身份不一致、窗口越界、验证失败或数据库异常必须回滚并暂停，不得放宽 SQL。

## 验证

自动化测试使用内存数据库替身，不执行生产 SQL：

```powershell
python -m pytest tests\test_zhaoshang_priority_boost.py tests\test_zhaoshang_resume_modes.py -q
```

测试同时覆盖仓库镜像和生产加载副本，至少验证单商城、同 RW 多商城、延迟/分批落表、当前读幂等、已为 `-1`、历史窗口、商城映射失败、混商城拒绝、参数化主键分批更新和精确主任务查询。
