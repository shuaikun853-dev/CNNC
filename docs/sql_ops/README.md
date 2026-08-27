# CNNC SQL 排查知识库（AI 优先）

> **目标读者：AI Agent / 中核运维。** 本目录只沉淀有来源、可复核的中核数据库知识。固定业务范围：`tenant_id=188605`、`commodity_pool_id=222`。
>
> **安全边界：** Agent 默认不得直接执行生产 SQL；这里只保存只读 `SELECT / SHOW` 排查模板。SaaS 运维手册中虽然存在生产写操作和优先级调整示例，但这里只记录其语义和适用条件，不把写 SQL 作为 Agent 默认操作。

## 1. 证据规则（强制）

任何进入本知识库的结论必须标注来源类型，不能靠字段名、表名或经验猜测：

- **[MANUAL]**：`SaaS运维手册.docx` 明文定义或 SQL 示例。
- **[FRONTEND]**：SaaS 同款审批前端代码/接口返回字段的直接映射。
- **[SCHEMA]**：生产库 `SHOW COLUMNS` / 表结构输出，由运营人员只读查询后回传。
- **[LIVE-SQL]**：生产库只读 SQL 的真实结果，由运营人员执行并回传；数量和时间只属于当时快照，不能当永久规则。
- **[OPERATOR]**：运营人员明确确认的人工操作事实，只用于解释案例，不替代字段语义。

证据优先级：**当前系统代码/前端直接映射 + 当前生产 schema/live SQL > 当前 SaaS 运维手册 > 二次整理文档 > 推断。** 若不同来源冲突，必须把冲突写出来，禁止静默选一个。

### 已使用的主要来源

1. `SaaS运维手册.docx`
   - `1. 手动调整任务优先级`
   - `5.8. 基于商品id、链接、名称采集爬虫侧任务插队`
   - `6.8. 中核首次爬取流程核查及问题解决`
   - `11. POC流程数据状态查询`
2. SaaS 同款审批前端 bundle（2026-08-12 保存的页面代码片段）：直接展示 `syncState`、`modelAuditResult` 的 UI 映射。
3. `approve_saas_audit_excel.py` / `reject_saas_audit_excel.py`：中核互联网同款审批脚本，当前只接受 `modelAuditResult='1'` 的互联网同款候选。
4. 2026-08-26 ~ 2026-08-27 中核生产只读 SQL 和 `SHOW COLUMNS` 实测。

## 2. 固定业务范围与最重要的计数规则

- tenant：`188605`
- commodity pool：`222`
- 商品池：`saas_onecode_price.commodity_pool_188605`
- 首次采集商品列表：`saas_compare_price.commodity_list_table_188605`
- 首次采集任务：`saas_compare_price.task_table_188605`
- 同款搜索/模型/审批候选：`saas_compare_price.search_table_188605`
- 最终同款关系：`saas_onecode_price.commodity_pool_relation_188605`
- 分控任务：`central_control_sub_demo.s_task_info`
- 通用异步任务：`onecode_base3.task_execute`

**[LIVE-SQL] 一个客户商品可以对应多条 SaaS 同款候选，因此 `客户商品数 != SaaS 页面行数`。** 页面 377 行不能解释成 377 个客户商品；2026-08-27 实测 377 条互联网待审批记录只涉及 88 个客户商品。具体快照见当日 case。

## 3. 已验证表关系

### 3.1 商品池 → commodity_list

**[LIVE-SQL]**

```text
commodity_pool_188605.MY_COMMODITY_ID
        ↓
commodity_list_table_188605.commodity_id
```

`commodity_list_table_188605.id` 是 task/search 的关联键。

### 3.2 commodity_list → task

**[LIVE-SQL]**

```text
commodity_list_table_188605.id
        ↓
task_table_188605.reference_id
```

中核首次采集常用条件：

```sql
t.table_name = '188605'
```

**[MANUAL] `task_status`：**

- `1`：爬取中
- `2`：爬取完成/成功
- `3`：爬取失败

### 3.3 commodity_list → search

**[LIVE-SQL]**

```text
commodity_list_table_188605.id
        ↓
search_table_188605.commodity_list_id
```

也可用 `search_table_188605.v_commodity_id` 与 `commodity_pool_188605.MY_COMMODITY_ID` 做商品级核对；当前 `SHOW COLUMNS` 已确认 `v_commodity_id` 存在且有普通索引。

### 3.4 SaaS task → 分控 batch

**[LIVE-SQL]** 首次采集 `task_id` 示例：

```text
SAAS-188605_704827316150657024_279774
```

提取 batch：

```sql
SUBSTRING_INDEX(REPLACE(t.task_id, 'SAAS-', ''), '_', 2)
```

得到：

```text
188605_704827316150657024
```

再关联：

```text
提取出的 batch_num
        ↓
central_control_sub_demo.s_task_info.batch_num
```

**已证伪：** 首次采集不要把 `task_table.task_id = s_task_info.exec_num` 当主 JOIN。2026-08-26 实测该关联会得到 `NO_MATCH`，但 exact `batch_num` 关联可以找到真实分控任务。

### 3.5 search → 最终 relation

**[LIVE-SQL]** 当前确认的商品级有效 relation 判断：

```sql
r.SRC_COMMODITY_POOL_ID = 222
AND r.SRC_MY_COMMODITY_ID = c.MY_COMMODITY_ID
AND r.IS_VALID = 1
```

候选级精确回连曾使用：

```sql
r.DEST_SHOP_CODE = s.vendor_name
AND CAST(r.DEST_SKU_ID AS CHAR) = CAST(s.sku_id AS CHAR)
```

2026-08-27 实测 `sync_state=1 AND audit_result=2` 的候选没有匹配到有效 relation；`sync_state=0 AND audit_result=2` 中已有一部分形成有效 relation。该关系是当日生产实测，不应扩展成未验证的数据库约束。

## 4. 商品池状态语义

### 4.1 `IS_SYNC_STANDARD`

**[MANUAL - 6.8]**：

- `0`：同款匹配/拉取尚未最终完成；同时手册提醒，`0` 不能单独证明“模型还没走完”，也可能已经匹配完成但没有拉到同款，必须继续查 task/search。
- `1`：完成同款拉取。
- `2`：已完成无同款。

**禁止：** 看到 `IS_SYNC_STANDARD=0` 就直接说“爬虫还在跑”。

## 5. `search_table_188605`：模型结果与人工审批

### 5.1 `audit_result`

**[MANUAL - POC流程数据状态查询]**：

- `NULL`：未处理
- `-1`：治理中
- `0`：已治理、未识别同款商品
- `-2`：同款识别中
- `2`：同款
- `3`：非同款

手册说明：Portal 拉取最终同款时会做极值过滤，因此最终同款数据量可能少于模型匹配同款数据量。

### 5.2 `sync_state` 与 `model_audit_result`：以当前前端映射为准

**[FRONTEND] 当前 SaaS 同款审批页面直接映射：**

`syncState`：

- `1`：待审批
- `0`：已审批-同款
- `2`：已审批-非同款

`modelAuditResult`：

- `0`：非同款 + 互联网
- `1`：同款 + 互联网
- `2`：非同款 + 参考链接
- `3`：同款 + 参考链接

因此当前页面语义可直接组合为：

| model_audit_result | sync_state | 当前含义 |
|---:|---:|---|
| `1` | `1` | 互联网同款，待人工审批 |
| `1` | `0` | 互联网同款，已审批通过 |
| `0` | `2` | 互联网候选，已审批为非同款（若出现） |
| `3` | `1` | 参考链接同款，待人工审批 |
| `3` | `0` | 参考链接同款，已审批通过 |
| `2` | `2` | 参考链接候选，已审批为非同款（若出现） |

**来源冲突提示：** 较早的二次整理文档曾把 `sync_state=3/4` 写成“参考链接待审核”类型，但当前 SaaS 前端和 2026-08-27 生产数据明确使用 `sync_state=1` 表示统一“待审批”，由 `model_audit_result` 区分互联网/参考链接。未来以当前前端 + live SQL 为准，不再把 `sync_state=3/4` 当当前稳定规则。

### 5.3 当前 schema 已确认的关键字段

**[SCHEMA - 2026-08-27 `SHOW COLUMNS saas_compare_price.search_table_188605`]**：

`id`, `batch_number`, `commodity_list_id`, `task_id`, `query_str`, `search_result`, `error_message`, `commodity_num`, `category_name`, `sku_name`, `sku_id`, `price`, `vendor_name`, `v_commodity_id`, `v_category_id`, `v_commodity_spec`, `commodity_spec`, `audit_result`, `similarity`, `sku_code`, `ext_link`, `create_time`, `update_time`, `is_match`, `model_result`, `score_remark`, `is_package_spec`, `annotation_result`, `label_desc`, `weighting_scheme`, `sync_state`, `is_self`, `shop_name`, `supplier_shop_name`, `crawler_time`, `pic_url`, `comment_num`, `comment`, `is_flag_ship_store`, `category_code`, `package_list`, `sales_price`, `brand_name`, `description`, `screenshot_url`, `detail_pic_url`, `sku_status`, `model_audit_result`, `reference_error`, `sales_spec`, `tenant_spec`, `brand_id`, `pic_list`, `screenshots_url`, `stock`, `sub_title`, `is_flagship`。

`sync_state` 的数据库默认值在该 schema 输出中为 `0`。**不要仅凭默认值反推业务状态迁移；审批语义以当前前端映射为准。**

## 6. 分控 `s_task_info` 状态

### 6.1 手册状态

**[MANUAL]**：

`task_status`：

- `00`：待执行
- `01`：执行中
- `02`：执行成功/爬取结束
- `03`：执行失败

`queue_status`：

- `00`：等待入队
- `01`：已入队
- `02`：已出队

### 6.2 2026-08-27 live SQL 对状态判定的修正

**[LIVE-SQL]**：

- `00/00 + enqueue_time IS NULL`：已生成分控子任务，但尚未入队。
- `00/01 + enqueue_time IS NOT NULL`：已入队，但 task 仍处于待执行状态。
- `01/02`：执行路径中。
- `02/02`：常见正常完成。
- `03/02`：失败/异常终态。
- **`start_time` 不能作为真实开始执行的证据**：当日 `00/00` 行也出现了 `start_time`。
- **不要强制要求 `queue_status=02` 才算采集完成**：当日实测存在 `task_status=02, queue_status=01, end_time IS NOT NULL` 的 `lxwlmall/zjmi` 行，已经实际执行并结束，只是队列状态未同步为 `02`。

判断“是否实际爬取结束”时，当前更稳妥的证据是：

```text
task_status='02' + end_time IS NOT NULL
```

`queue_status` 用于描述队列生命周期，不应单独覆盖执行终态。

### 6.3 `weight_type`

**[MANUAL]** 手册资料存在两类相关描述：

- 一处说明 `weight_type` 任务等级：`-1` 预留运营人员 SQL 使用，`0` 紧急，`1` 重要，`2` 普通。
- `5.8` 的“对未执行任务进行加急”示例直接把目标未执行任务设置为 `weight_type=-1`。

因此可以确认：**`weight_type` 是分控侧优先级/任务等级字段，`-1` 有明确运营加急用途。** 但不同手册片段同时出现 `0` 紧急与 `-1` 运营加急，未来不要自行推导完整排序或调度算法；具体写操作必须由人工按当前运维手册执行。

### 6.4 手册对大量 `00/00` 的判断

**[MANUAL]**：某时间段持续大量 `task_status=00 AND queue_status=00` 且一直不变化，手册将其指向分控应用问题，并提到队列满/缓存问题及重启分控。数据库只能定位“入队前积压”，具体技术根因仍需日志确认。

## 7. Spider 回调

**[MANUAL]**：

- 未回调/失败记录：`shopping_spider.spider_sync_log`
- 成功回调记录：`shopping_spider.spider_sync_log_complated`
- `push_state='0000'`：成功回调 SaaS
- `push_state='8888'`：未回调
- 其他状态：回调失败/异常

首次采集优先按 exact `batch_number/batch_num` 对账，不用全局大盘替代目标 batch。

## 8. `onecode_base3.task_execute`

### 8.1 字段与状态

**[MANUAL]**：

关键字段：`TENANT_ID`, `COMMODITY_POOL_ID`, `TYPE`, `STATE`, `REFERENCE_ID`, `CNT_RETRY`, `MAX_CNT_RETRY`, `NEXT_TIME`, `CREATED_TIME`, `LAST_RUN`。

`STATE`：

- `NULL`：待推送/待执行
- `0`：推送中、待重试
- `1`：推送成功
- `2`：推送失败
- `4`：失败、无需推送/数据不合格
- 其他数字：可作为人工临时状态（手册的优先级调整示例）

当前可执行待任务的手册条件：

```sql
STATE IS NULL
AND NEXT_TIME <= NOW()
AND CNT_RETRY < MAX_CNT_RETRY
```

### 8.2 手册中的“让目标租户先跑”机制

**[MANUAL]**：先查询全局满足上述条件的待执行任务；如果目标租户有紧急任务，可以临时把其他租户待执行任务改成不与正式枚举冲突的临时 `STATE`，只留下目标租户正常待执行，完成后再恢复。

**适用前提：目标租户本身必须存在满足条件的 `task_execute` 待执行任务。** 如果全局查询都为 0 行，“其他租户让路”没有可调度目标，不能用来解释/解决另一个表里的审批候选积压。

手册列出的价格监控相关 TYPE 包括：`CATEGORY_RECOGNITION_FOR_MODEL`, `ZH_GOVEN_RESULT_SYNC_URL`, `REFERENCE_PRICE_SYNC`, `MATCH_INTERNET_GOODS_SYNC`, `CRAWL_URL_RESULT_SYNC`, `GOODS_SEARCH_PUSH_SYNC`, `MATCH_PRICE_RESULT_SYNC`。

2026-08-27 live SQL 还实测到 `SAAS_GOODS_CRAWL`，属于生产实际存在的 TYPE；其业务语义只记录“已观察到”，不凭名称扩展定义。

## 9. 推荐排查决策树

遇到“客户数据没有全部进入 SaaS 同款审批页面”时：

```text
commodity_pool
  ↓
commodity_list 是否存在
  ↓
task 是否存在 / 是否还有 task_status=1
  ↓
若 task_status=1：提取 exact batch_num
  ↓
s_task_info：是否未入队 / 已入队 / 已执行 / 失败
  ↓
若分控已完成：查 spider callback / SaaS task 是否闭合
  ↓
search 是否存在
  ↓
audit_result 是否仍 NULL/-1/-2
  ↓
模型完成后：按 model_audit_result 区分
  ├─ 0：互联网非同款
  ├─ 1：互联网同款
  ├─ 2：参考链接非同款
  └─ 3：参考链接同款
  ↓
sync_state 是否仍 1（待人工审批）
  ↓
人工审批后看 0/2 + relation / IS_SYNC_STANDARD
```

关键原则：**“没有出现在互联网同款待审批页面”不等于“互联网数据丢了”。** 可能是：采集未结束、无 search、模型未完成、互联网候选全部被模型判非同款、只有参考链接同款，或确实没有互联网候选。必须分桶后判断。

## 10. 性能规则

2026-08-27 已验证：对每个商品重复写 5~6 个相关 `EXISTS` 会在大 `search_table` 上很慢。优先：

1. 先用商品池日期/池 ID 缩小到 `base`。
2. 用 `base_ids` 限定 `commodity_list`。
3. `task_table` 聚合一次得到 task flags。
4. `search_table` 聚合一次得到 search/model/approval flags。
5. 最后在几百个商品级 flag 上分类。

`search_table_188605.commodity_list_id` 当前 schema 有普通索引（`MUL`），应优先利用目标 `commodity_list.id` 缩小扫描范围。

## 11. 常见误区 / 已证伪思路

1. 客户商品数不能和 SaaS 页面行数一一比较。
2. 不同 `task_status` 的“影响商品数”可能重叠，不能直接相加。
3. `task_status=1` 不证明爬虫正在真实执行；先查 exact batch 的分控状态。
4. `start_time` 不证明已经执行；优先看 `enqueue_time/task_status/end_time`。
5. `02/01` 也可能已实际完成；不要把 `02/02` 写成唯一成功条件。
6. 首次采集不要用 `task_table.task_id = s_task_info.exec_num` 当主 JOIN。
7. `audit_result=2` 只是模型判同款，不代表人工已经审批，也不代表已经形成有效 relation。
8. 当前 `sync_state=1` 是“待审批”，不是 `task_execute` 队列状态；不要把大量 `sync_state=1` 自动解释为异步 consumer 积压。
9. `model_audit_result=1` 是互联网同款；`3` 是参考链接同款。是否进入页面还要看 `sync_state`。
10. `task_execute` 全局没有 eligible pending 时，手册的“其他租户让路”优先级办法没有目标可执行。
11. 任何“comix 全局坏了”“node 全死”“某优先级一定先跑”等结论都必须有当时 exact SQL/日志；2026-08-27 现场曾出现同 node + 同 comix 的其他租户任务能够入队并秒级完成，因此不能把一次旧积压直接定性为商城/爬虫全局故障。
12. 数量随客户持续推送变化；案例数字必须带查询时间，不可写成固定基线。

## 12. 案例索引

- [`2026-08-26 中核首次采集未进入 SaaS 页面排查`](cases/2026-08-26_FIRST_CRAWL_MISSING_SAAS.md) — 早期阶段证据；其中对 `sync_state` 的“后处理消费”解释已被 2026-08-27 前端字段映射修正，引用时以本 README 当前定义为准。
- [`2026-08-27 node 10000000003 queue backlog`](cases/2026-08-27_NODE_10000000003_QUEUE_BACKLOG.md) — 中途快照；其中“comix standout hard blocker”是当时窗口结论，后来同 node/comix 已恢复，不能当永久根因。
- [`2026-08-27 首次采集 + 同款审批完整证据链`](cases/2026-08-27_FIRST_CRAWL_SAME_AUDIT_EVIDENCE.md) — **当前最完整、推荐优先读取**。

## 13. 维护约定

- 长期稳定的表关系/字段语义 → 更新本 README。
- 真实某天数量、时间、事故过程 → 写 `cases/YYYY-MM-DD_*.md`。
- schema 直接输出 → `SCHEMA_VERIFIED.md`。
- 对存在冲突的来源必须同时记录，不得为了“看起来一致”擅自消除冲突。
- 所有生产数量必须标记为 snapshot；未来排查先重跑，不复用旧计数。
- 不记录账号、密码、Cookie、Token、连接串或其它凭据。
