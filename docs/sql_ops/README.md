# CNNC SQL 排查知识库（AI 优先）

> **目标读者：AI Agent。** 本目录用于沉淀中核 `tenant_id=188605`、`commodity_pool_id=222` 的只读 SQL 排查知识、已验证表关系、字段语义、排查路径、误区和历史案例。
>
> **重要安全边界：** 本仓库 Agent 仍遵守 `AGENTS.md`：Agent 默认不得直接执行生产 SQL。这里保存的是**只读 SELECT 排查模板和推理知识**，由用户在数据库客户端手工执行并把结果返回给 Agent。除已有受控招商局优先级工具外，不在这里新增 UPDATE/DELETE/INSERT SQL。

## 1. AI 使用规则

处理任何“查 SQL / SaaS 数据为什么没到页面 / 中核首次采集卡住 / task 为什么没完成 / 同款为什么没显示”的问题时：

1. 先读本文件。
2. 再按问题读取 `docs/sql_ops/cases/` 下最近且同类的案例。
3. 优先复用已经验证的表关系和 SQL，不要凭字段名猜 JOIN。
4. 先缩小到目标商品集合，再 JOIN 大表；不要先全表聚合。
5. 每一步只证明一层事实，避免从“大盘状态”直接推断具体商品状态。
6. 用户返回结果后再决定下一条 SQL，不一次性让用户跑十几条重 SQL。
7. 本次排查得到新的、可复用的表关系/字段语义/坑点时，应追加到本目录；事故细节放 `cases/`，长期稳定知识放本 README。
8. 不记录数据库账号、密码、连接串、Token、Cookie 或其他凭据。

## 2. 固定业务范围

- tenant: `188605`
- commodity pool: `222`
- 当前常用日过滤：`CREATED_TIME >= '<business_date> 00:00:00'`
- 同一个客户商品可能对应多条 SaaS 同款记录，因此：
  - **客户商品数 ≠ SaaS 页面同款行数**
  - 不要把 `customer_count` 与 SaaS 同款行数直接做一一对应异常判断。

## 3. 已验证核心表关系

### 3.1 商品池

`saas_onecode_price.commodity_pool_188605`

关键字段：

- `MY_COMMODITY_ID`：客户商品 ID，后续排查的主业务键。
- `COMMODITY_POOL_ID`：中核固定为 `222`。
- `IS_MONITOR`：当前排查一般限定 `1`。
- `CREATED_TIME`：商品进入池时间。
- `IS_SYNC_STANDARD`：首次同款流程收口状态。当前运维口径中常见：
  - `0`：尚未完成最终收口；
  - `1`：已完成并存在可用同款结果；
  - `2`：已完成但无同款。

> AI 不应仅凭 `IS_SYNC_STANDARD=0` 判断“爬虫还在跑”。必须继续看 task/search/relation。

### 3.2 首次采集商品列表

`saas_compare_price.commodity_list_table_188605`

关键关系：

```text
commodity_pool_188605.MY_COMMODITY_ID
        ↓
commodity_list_table_188605.commodity_id
```

`commodity_list_table_188605.id` 是 task/search 的关联键。

### 3.3 首次采集任务

`saas_compare_price.task_table_188605`

已验证关系：

```text
commodity_list_table_188605.id
        ↓
task_table_188605.reference_id
```

常用条件：

```sql
t.table_name = '188605'
```

当前运维手册/实测使用的 `task_status`：

- `1`：爬取中
- `2`：完成
- `3`：失败

`task_id` 示例：

```text
SAAS-188605_704827316150657024_279774
```

首次采集的 batch 可从 `task_id` 提取：

```sql
SUBSTRING_INDEX(REPLACE(t.task_id, 'SAAS-', ''), '_', 2)
```

得到：

```text
188605_704827316150657024
```

### 3.4 模型/同款搜索结果

`saas_compare_price.search_table_188605`

关系：

```text
commodity_list_table_188605.id
        ↓
search_table_188605.commodity_list_id
```

当前运维口径：

- `audit_result=2`：模型识别为同款
- `audit_result=3`：模型识别为非同款
- `audit_result IS NULL`、`-1`、`-2`：排查时按未完全处理/处理中候选关注

`sync_state` 是**模型结果后的后续处理状态**，不能与 `audit_result` 混为一谈。2026-08-26 实测案例中：

- 同一个客户商品可以同时存在 `sync_state=0` 和 `sync_state=1` 的 `audit_result=2` 行；
- 商品只要仍有 `sync_state=1` 的候选，就可能尚未完成最终收口。

### 3.5 最终同款关系

`saas_onecode_price.commodity_pool_relation_188605`

本次已验证判断有效同款关系的字段：

```sql
r.SRC_COMMODITY_POOL_ID = 222
AND r.SRC_MY_COMMODITY_ID = c.MY_COMMODITY_ID
AND r.IS_VALID = 1
```

AI 应区分：

```text
search_table.audit_result=2
```

只是模型层“识别为同款”，不等于已经形成：

```text
commodity_pool_relation_188605.IS_VALID=1
```

也不等于已经能在页面展示。

### 3.6 Demo 分控

`central_control_sub_demo.s_task_info`

中核首次采集排查的关键关联是：

```text
从 task_table.task_id 提取 batch_num
        ↓
central_control_sub_demo.s_task_info.batch_num
```

不要用：

```text
task_table.task_id = s_task_info.exec_num
```

作为首次采集的主关联。2026-08-26 实测会得到 `NO_MATCH`，但这是错误关联方式。

实测正常 batch：

```text
中控任务数 = 18
task_status = 02
queue_status = 02
```

异常 batch 若 `s_task_info` 为 0 条，而 SaaS `task_table` 已存在完整任务，则故障边界落在：

```text
SaaS task 创建
    ↓
任务投递 / 分控创建
    ↓
central_control_sub_demo.s_task_info
```

### 3.7 Spider 回调

- `shopping_spider.spider_sync_log`
- `shopping_spider.spider_sync_log_complated`

首次采集优先按 `batch_number` 与上面的 `batch_num` 对账。

已见字段：

- `batch_number`
- `execution_number`
- `push_state`
- `task_state`
- `create_time`

运维口径：

- `push_state='0000'`：成功回调 SaaS
- `push_state='8888'`：未回调
- 其他：回调失败/异常

`execution_number` 的格式与 SaaS `task_id` 可以一致，例如：

```text
SAAS-188605_704824207118622720_353583
```

但排查首次采集整批是否进入下游时，**batch 级对账优先于全局大盘或错误的中控 exec_num JOIN**。

## 4. 推荐排查顺序

遇到“客户商品没出现在 SaaS 同款页面”时，按以下顺序：

```text
commodity_pool
  ↓
是否进入 commodity_list
  ↓
是否生成 task
  ↓
task 是否仍 running / failed
  ↓
若 task running：提取 exact batch → 查 demo 分控
  ↓
若采集完成：查 search audit_result
  ↓
若 audit_result=2：查 sync_state
  ↓
查 commodity_pool_relation 是否生成有效关系
  ↓
最后再判断页面展示/过滤
```

不要跳过中间层直接下结论。

## 5. 性能规则

优先写成：

```sql
WITH base AS (
    SELECT ...
    FROM commodity_pool_188605
    WHERE ...今天目标商品...
)
```

然后让 task/search 查询只 JOIN `base`。

对于只需要判断“是否存在”的问题，优先用 `EXISTS / NOT EXISTS`，避免先对整个 `task_table` / `search_table` 做大聚合。

## 6. 常见误区（AI 必读）

1. **不要把 138 个客户商品和 210 条 SaaS 同款行理解成数量不一致。** 一个客户商品可对应多条同款。
2. **不同 task_status 影响的商品数可能重叠，不能直接相加。**
3. **`audit_result=2` 不代表页面必然展示。** 还需看 `sync_state`、有效 relation、后处理/过滤。
4. **不要因为 `task_status=1` 就直接说“爬虫真在跑”。** 先查 exact batch 是否进入分控。
5. **不要用全局 `batch_num LIKE '188605%'` 的大盘结果证明某 47 个商品的具体状态。** 必须从目标 task 提取 exact batch。
6. **不要用 `task_table.task_id = s_task_info.exec_num` 作为首次采集主 JOIN。** 正确主线是 `task_id → batch_num → s_task_info.batch_num`。
7. `temporary_data_crawl.dc_task_execution_info*` 在 2026-08-26 这次 SAAS 首次采集案例里只匹配到极少量记录，**不能作为这条链路的主判定依据**，除非后续开发确认其适用范围。
8. 页面同款行数可能经过后处理/极值过滤；但如果仍存在大量 `sync_state=1`，不能把“页面没显示”简单归因于过滤。
9. 时间变化会导致数量变化。每次引用“91 条/47 条/43 条”前应重跑当前日基础分类，不要把历史数量当永久事实。

## 7. 案例索引

- [`2026-08-26 中核首次采集未进入 SaaS 页面排查`](cases/2026-08-26_FIRST_CRAWL_MISSING_SAAS.md)

## 8. 后续维护约定

今后每次有新的 SQL 排查：

- 可复用的表关系/字段语义/SQL 模板 → 更新本 README；
- 某一天的具体事故、实际数量、逐步证据 → 新建 `docs/sql_ops/cases/YYYY-MM-DD_<topic>.md`；
- 必须记录“哪些 SQL/关联方式被证伪”，避免未来 AI 重走错误路径；
- 只保存 SELECT 模板，不把生产写 SQL 沉淀为普通排查知识。
