# CNNC 已验证 Schema / 字段证据

> 本文件只记录直接来自生产 `SHOW COLUMNS`、SaaS 运维手册或生产只读 SQL 的 schema/字段事实。禁止根据字段名猜语义。

## 1. 来源标签

- **[SCHEMA-20260827]**：2026-08-27 运营人员在生产库执行 `SHOW COLUMNS FROM saas_compare_price.search_table_188605` 后回传的完整结果。
- **[MANUAL]**：`SaaS运维手册.docx`。
- **[LIVE-SQL]**：2026-08-26 ~ 2026-08-27 生产只读 SQL 实测。

## 2. `saas_compare_price.search_table_188605`

### 2.1 完整字段列表

**[SCHEMA-20260827]**

| 字段 | 类型 | NULL | Key | 默认/额外 |
|---|---|---|---|---|
| `id` | int | NO | PRI | auto_increment |
| `batch_number` | varchar(32) | YES |  |  |
| `commodity_list_id` | bigint | YES | MUL |  |
| `task_id` | varchar(255) | YES |  |  |
| `query_str` | text | YES |  |  |
| `search_result` | text | YES |  |  |
| `error_message` | text | YES |  |  |
| `commodity_num` | int | YES |  |  |
| `category_name` | varchar(512) | YES |  |  |
| `sku_name` | text | YES | MUL |  |
| `sku_id` | varchar(255) | YES |  |  |
| `price` | varchar(32) | YES |  |  |
| `vendor_name` | varchar(255) | YES |  |  |
| `v_commodity_id` | varchar(255) | YES | MUL |  |
| `v_category_id` | varchar(255) | YES |  |  |
| `v_commodity_spec` | text | YES |  |  |
| `commodity_spec` | text | YES |  |  |
| `audit_result` | varchar(255) | YES | MUL |  |
| `similarity` | varchar(255) | YES |  |  |
| `sku_code` | varchar(255) | YES |  |  |
| `ext_link` | text | YES |  |  |
| `create_time` | datetime | YES |  | CURRENT_TIMESTAMP |
| `update_time` | datetime | YES |  | on update CURRENT_TIMESTAMP |
| `is_match` | varchar(1) | YES |  |  |
| `model_result` | varchar(2) | YES |  |  |
| `score_remark` | varchar(4096) | YES |  |  |
| `is_package_spec` | varchar(1) | YES |  |  |
| `annotation_result` | varchar(1) | YES |  |  |
| `label_desc` | varchar(512) | YES |  |  |
| `weighting_scheme` | varchar(128) | YES |  |  |
| `sync_state` | int | YES | MUL | `0` |
| `is_self` | varchar(1) | YES |  |  |
| `shop_name` | varchar(32) | YES |  |  |
| `supplier_shop_name` | varchar(255) | YES |  |  |
| `crawler_time` | timestamp | YES |  |  |
| `pic_url` | text | YES |  |  |
| `comment_num` | varchar(32) | YES |  |  |
| `comment` | varchar(255) | YES |  |  |
| `is_flag_ship_store` | varchar(255) | YES |  |  |
| `category_code` | varchar(255) | YES |  |  |
| `package_list` | longtext | YES |  |  |
| `sales_price` | varchar(32) | YES |  |  |
| `brand_name` | varchar(255) | YES |  |  |
| `description` | longtext | YES |  |  |
| `screenshot_url` | varchar(255) | YES |  |  |
| `detail_pic_url` | text | YES |  |  |
| `sku_status` | varchar(2) | YES |  |  |
| `model_audit_result` | varchar(255) | YES |  |  |
| `reference_error` | varchar(255) | YES |  |  |
| `sales_spec` | varchar(255) | YES |  |  |
| `tenant_spec` | varchar(255) | YES |  |  |
| `brand_id` | varchar(255) | YES |  |  |
| `pic_list` | varchar(255) | YES |  |  |
| `screenshots_url` | varchar(255) | YES |  |  |
| `stock` | varchar(255) | YES |  |  |
| `sub_title` | varchar(255) | YES |  |  |
| `is_flagship` | varchar(255) | YES |  |  |

### 2.2 直接可确认的索引信息

**[SCHEMA-20260827]** `commodity_list_id`, `sku_name`, `v_commodity_id`, `audit_result`, `sync_state` 的 `Key` 均为 `MUL`。这只能确认存在非唯一索引标记，不能据此猜索引组合或选择性。

### 2.3 字段语义（有来源部分）

**[MANUAL] `audit_result`：**

- `NULL` 未处理
- `-1` 治理中
- `0` 已治理未识别同款商品
- `-2` 同款识别中
- `2` 同款
- `3` 非同款

**[FRONTEND，详见 README 来源说明] `sync_state`：** `1` 待审批，`0` 已审批-同款，`2` 已审批-非同款。

**[FRONTEND] `model_audit_result`：**

- `0` 互联网 + 非同款
- `1` 互联网 + 同款
- `2` 参考链接 + 非同款
- `3` 参考链接 + 同款

`sync_state` 的 schema 默认值是 `0`，但默认值本身不能用于解释业务状态迁移。

## 3. `central_control_sub_demo.s_task_info`

### 3.1 已在生产实测/案例中出现的字段

**[LIVE-SQL]**：

`task_id`, `center_task_id`, `node_id`, `rel_code`, `commodity_pool_id`, `exec_num`, `task_num`, `batch_num`, `mall`, `busi_params`, `busi_params_sha`, `other_params`, `task_type`, `weight`, `weight_type`, `start_time`, `end_time`, `task_status`, `queue_status`, `enqueue_time`, `push_status`, `pull_status`, `is_notice`, `is_retry`, `is_main`, `retry_count`, `is_valid`, `is_collect`, `create_time`, `update_time`, `remark`，以及保留字段。

这里只说明这些字段在实测查询/输出中存在；未执行完整 `SHOW COLUMNS` 前，不宣称这是完整 schema。

### 3.2 状态语义

**[MANUAL]**

`task_status`：`00` 待执行，`01` 执行中，`02` 执行成功/爬取结束，`03` 执行失败。

`queue_status`：`00` 等待入队，`01` 已入队，`02` 已出队。

**[LIVE-SQL] 修正：**

- `start_time` 在 `00/00` 未入队行上也可能有值，所以不能作为执行开始证据。
- 生产实测存在 `task_status='02' AND queue_status='01' AND end_time IS NOT NULL` 的已完成行，因此不能把 `02/02` 写成唯一成功条件。

### 3.3 `weight_type`

**[MANUAL]**：手册资料明确把 `weight_type` 定义为任务等级/优先级字段；一处说明 `-1` 为运营预留 SQL 使用、`0` 紧急、`1` 重要、`2` 普通，另一个“未执行任务加急”章节直接用 `weight_type=-1`。因此只确认其优先级用途和 `-1` 的运营加急用途，不自行推导完整调度排序算法。

## 4. `onecode_base3.task_execute`

### 4.1 手册已定义字段

**[MANUAL]**：`TENANT_ID`, `COMMODITY_POOL_ID`, `TYPE`, `STATE`, `REFERENCE_ID`, `CNT_RETRY`, `MAX_CNT_RETRY`, `NEXT_TIME`, `CREATED_TIME`, `LAST_RUN`。

### 4.2 `STATE`

**[MANUAL]**：

- `NULL`：待推送/待执行
- `0`：推送中、待重试
- `1`：推送成功
- `2`：推送失败
- `4`：失败、无需推送/数据不合格
- 其它数字：运维临时状态可能使用

手册用于筛“当前已到期可执行”的条件：

```sql
STATE IS NULL
AND NEXT_TIME <= NOW()
AND CNT_RETRY < MAX_CNT_RETRY
```

## 5. 其它已验证核心表/键

### 5.1 `commodity_pool_188605`

**[MANUAL + LIVE-SQL] 已使用关键字段：** `ID`, `MY_COMMODITY_ID`, `COMMODITY_POOL_ID`, `IS_MONITOR`, `IS_SYNC`, `IS_SYNC_STANDARD`, `CREATED_TIME`。

`MY_COMMODITY_ID` 是当前排查贯穿商品池、commodity_list、search、relation 的客户商品业务键。

### 5.2 `commodity_list_table_188605`

**[LIVE-SQL] 已验证键：** `id`, `commodity_id`。

```text
commodity_pool.MY_COMMODITY_ID = commodity_list.commodity_id
```

### 5.3 `task_table_188605`

**[LIVE-SQL + MANUAL] 已使用字段：** `task_id`, `reference_id`, `table_name`, `task_status`, `create_time`。

```text
commodity_list.id = task_table.reference_id
```

### 5.4 `commodity_pool_relation_188605`

**[LIVE-SQL] 已使用字段：** `SRC_COMMODITY_POOL_ID`, `SRC_MY_COMMODITY_ID`, `DEST_SHOP_CODE`, `DEST_SKU_ID`, `IS_VALID`。

当前有效关系商品级筛法：

```sql
SRC_COMMODITY_POOL_ID = 222
AND SRC_MY_COMMODITY_ID = <MY_COMMODITY_ID>
AND IS_VALID = 1
```

## 6. 后续补 schema 原则

- 只允许把真实 `SHOW COLUMNS / DESCRIBE / SHOW INDEX` 输出补进本文件。
- 如果只在查询结果里见过某字段，要写“已观察字段”，不能冒充完整 schema。
- 字段业务含义必须有手册/当前代码/UI 映射/live SQL 行为证据；仅凭字段名不能入库。
