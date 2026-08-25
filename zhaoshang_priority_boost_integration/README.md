# 招商局当前任务优先级集成

本目录保存受控提优 helper 的仓库镜像。生产实际加载副本位于：

`E:\.A2026\招商局自动建任务\external_tools\crawl_task_tool\zhaoshang_priority_boost.py`

两份文件必须保持一致。测试会同时运行镜像和生产副本，避免只修样例而漏掉正式链路。

## 任务身份

`task_number` 不是唯一任务标识，同一个 `RW-...` 可以同时属于多个商城。调用方必须传入：

- 精确 `task_name`：`cnnc_current_opt_<中文商城名>_<至少14位纯数字marker>`
- 从精确主任务查询得到的 `task_number`
- 从现有 `MALL_CONFIG[mall_key]["identification"]` 读取的 `mall_code`
- 本次 add payload 中的有效子任务数量 `expected_subtask_count`

禁止按中文商城名猜 code，禁止查询不到时取首条记录或默认成京东。

## 定位和更新

helper 从任务 marker 的前 14 位解析时间，以 `[marker-2分钟, marker+15分钟)` 作为 `create_time` 安全窗口。轮询查询固定为：

```sql
SELECT task_id, task_num, mall, create_time, task_status, weight_type, is_valid
FROM central_control_sub_zsz.s_task_info
FORCE INDEX (idx_opt_distinctmall)
WHERE mall = ?
  AND is_valid = '1'
  AND task_num = ?
  AND create_time >= ?
  AND create_time < ?;
```

查到的每行都要在 Python 中再次验证 taskNumber、mall、create_time 和有效性。`task_status` 只表示采集进度，不参与 priority 过滤。子任务可能分批落表，轮询必须等到命中数量与 `expected_subtask_count` 完全一致；少于预期继续等待，多于预期失败。写事务再按已确认的主键执行 `SELECT ... FOR UPDATE` 当前读，避免 REPEATABLE READ 旧快照，同时不锁定宽范围 mall 扫描。最终只按子任务主键分批更新：

```sql
UPDATE central_control_sub_zsz.s_task_info
SET weight_type = '-1'
WHERE task_id IN (?, ...)
  AND is_valid = '1'
  AND weight_type <> '-1';
```

每批最多 1000 个 ID，所有值必须参数化。禁止 `task_num` 单条件 UPDATE、`batch_num LIKE`、`start_time` 定位、宽范围 fallback、建索引或改表结构。

## 状态语义

- `updated`：本次 `affected_rows>0`，可记录已提优。
- `already_highest_priority`：预查确认目标已全部为 `-1`，不执行 UPDATE；这是幂等成功，但不能称为本次提优。
- `task_status=00/01/02`：都属于相同的 priority 处理范围；完成状态不能跳过更新。
- 其他 0 行、身份越界、回读不全为 `-1`、验证失败或数据库异常：回滚并暂停，不得放宽 SQL。

自动化验证：

```powershell
python -m pytest tests\test_zhaoshang_priority_boost.py tests\test_zhaoshang_resume_modes.py -q
```
