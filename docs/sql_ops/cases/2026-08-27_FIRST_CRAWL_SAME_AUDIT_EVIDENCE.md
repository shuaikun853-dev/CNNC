# 2026-08-27 中核首次采集 + 同款审批完整证据链

> **状态：截至 2026-08-27 13:36 +08:00 的生产只读 SQL 快照。** 客户数据仍在持续新增，所有数量都只能作为当时 snapshot。
>
> **安全：** 本文只保存 SELECT/SHOW 和人工回传结果。没有执行任何生产写 SQL。

## 1. 来源清单

本文每个结论只使用以下来源：

- **[MANUAL]** `SaaS运维手册.docx`
  - `1. 手动调整任务优先级`
  - `5.8. 基于商品id、链接、名称采集爬虫侧任务插队`
  - `6.8. 中核首次爬取流程核查及问题解决`
  - `11. POC流程数据状态查询`
- **[FRONTEND]** 2026-08-12 保存的 SaaS 同款审批前端代码片段：直接展示 `syncState` / `modelAuditResult` 的 UI 映射。
- **[SCRIPT]** `approve_saas_audit_excel.py`, `reject_saas_audit_excel.py`：互联网同款审批工具只接受 `modelAuditResult='1'`。
- **[SCHEMA]** 2026-08-27 生产 `SHOW COLUMNS FROM saas_compare_price.search_table_188605` 回传结果。
- **[LIVE-SQL]** 2026-08-26 ~ 2026-08-27 运营人员在生产库执行的只读 SQL 回传结果。
- **[OPERATOR]** 运营人员明确确认：从 2026-08-26 开始人工审批了一些参考链接同款；互联网同款没有审批；互联网同款和参考链接都需要人工审批。

凡本文写“推断/可能”的地方都单独标识；没有日志/源码证据时不写成根因。

---

## 2. 当前最重要结论

### 2.1 “页面 377”不是 377 个客户商品

**[FRONTEND + LIVE-SQL]** 当前 SaaS 页面字段映射：

- `sync_state=1`：待审批
- `sync_state=0`：已审批-同款
- `sync_state=2`：已审批-非同款
- `model_audit_result=0/1`：互联网类型，其中 `1` 为同款、`0` 为非同款
- `model_audit_result=2/3`：参考链接类型，其中 `3` 为同款、`2` 为非同款

当日实测：

```text
sync_state=1, model_audit_result=1
377 条记录 / 88 个客户商品
```

因此页面看到的 **377 是互联网同款待审批行数，不是客户商品数**。

### 2.2 截至 13:36，210 条客户商品记录按链路状态闭合分桶

**[LIVE-SQL] 最新快照：**

| 业务日 | 客户商品记录 | 已产生互联网同款商品 | 互联网同款待审批商品 | 互联网同款已审批商品 | 只有参考链接同款、无互联网同款 | 模型完成但无任何同款 | 采集仍未结束 | 采集结束但无 search | 模型仍处理中 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2026-08-26 | 138 | 57 | 57 | 0 | 68 | 6 | 0 | 7 | 0 |
| 2026-08-27 | 72 | 31 | 31 | 0 | 21 | 1 | 16 | 3 | 0 |
| **合计** | **210** | **88** | **88** | **0** | **89** | **7** | **16** | **10** | **0** |

每一天都闭合：

```text
8/26: 57 + 68 + 6 + 0 + 7 + 0 = 138
8/27: 31 + 21 + 1 + 16 + 3 + 0 = 72
```

因此当前“只有 88 个客户商品产生互联网同款”不能直接解释成剩余 122 个“互联网同款丢失”。剩余 122 个已被进一步分到其它状态。

### 2.3 89 个“只有参考链接同款”的进一步拆分

**[LIVE-SQL + FRONTEND]** 对 `has_reference_same=1 AND has_internet_same=0` 的商品进一步看互联网模型类型：

| 业务日 | 只有参考链接同款 | 至少有 `model_audit_result=0`（互联网候选被判非同款） | 未观察到 `model_audit_result=0/1` 的互联网模型候选 |
|---|---:|---:|---:|
| 2026-08-26 | 68 | 59 | 9 |
| 2026-08-27 | 21 | 16 | 5 |
| **合计** | **89** | **75** | **14** |

因此：

- **75/89**：不是“互联网链路完全没结果”，而是至少存在互联网候选，模型结果为互联网非同款 (`model_audit_result=0`)，同时没有互联网同款 (`1`)。
- **14/89**：在这次查询条件下没有观察到 `model_audit_result=0/1`；只能写“未观察到互联网模型候选证据”，不能在没有更细日志前断言爬虫完全没采到。

这 75 个是解释“为什么互联网同款页只覆盖 88 个客户商品”的最大已验证群体。

---

## 3. 同款审批字段：纠正前期误读

### 3.1 前期错误假设

排查中曾把：

```text
audit_result=2 + sync_state=1
```

误解释成“模型结果已出来，等待某个 sync consumer 做后处理”。

**该解释已被当前 SaaS 前端字段映射证伪。**

### 3.2 当前正确解释

**[FRONTEND]**：

```text
sync_state=1 -> 待审批
sync_state=0 -> 已审批-同款
sync_state=2 -> 已审批-非同款
```

```text
model_audit_result=0 -> 互联网 + 非同款
model_audit_result=1 -> 互联网 + 同款
model_audit_result=2 -> 参考链接 + 非同款
model_audit_result=3 -> 参考链接 + 同款
```

### 3.3 当时状态矩阵

**[LIVE-SQL]**：

```text
sync_state=0, model_audit_result=3 : 80 行 / 80 商品
sync_state=1, model_audit_result=1 : 377 行 / 88 商品
sync_state=1, model_audit_result=3 : 89 行 / 89 商品
```

**[OPERATOR]**：

- 80 条 `sync_state=0 + model_audit_result=3` 与运营人员从 8/26 开始手工审批参考链接同款的操作相符。
- 运营人员明确确认互联网同款没有人工审批；live SQL 也确实没有 `model_audit_result=1 + sync_state=0`。

因此此前把 80 条 `sync_state=0` 的分批更新时间（14:58 / 16:34 / 16:39 / 16:45）解释为“定时 consumer”是错误推断，应废弃。

### 3.4 80 条参考链接人工审批的更新时间分布

**[LIVE-SQL]**：

```text
2026-08-26 14:58:00  42
2026-08-26 16:34:00  29
2026-08-26 16:39:00   8
2026-08-26 16:45:00   1
```

合计 80。结合 [OPERATOR] 人工操作确认，这些时间点应按人工审批批次理解，不再推断为系统定时器。

---

## 4. `search_table` schema 实测

完整字段见 `docs/sql_ops/SCHEMA_VERIFIED.md`。

特别重要的生产 schema 事实：

- `commodity_list_id`：`bigint`, `MUL`
- `v_commodity_id`：`varchar(255)`, `MUL`
- `audit_result`：`varchar(255)`, `MUL`
- `sync_state`：`int`, `MUL`, schema 默认值 `0`
- `model_audit_result`：`varchar(255)`
- `update_time`：`on update CURRENT_TIMESTAMP`

**注意：** `sync_state` 默认值为 0 只属于 schema 事实，不能用来推导审批状态为什么变化。

---

## 5. 8/26 → 8/27 首次采集：中控入队问题

### 5.1 早期 8/26 快照

**[LIVE-SQL]** 8/26 早期：138 个客户商品全部进入 `commodity_list`。当时 `IS_SYNC_STANDARD=0` 商品曾被分成：

```text
1  个：没有生成采集 task
47 个：仍有 SaaS task_status=1
43 个：已有模型同款但尚未收口（该组后续审批语义已按本 case 修正）
```

47 个 running 商品每个有 18 个 SaaS task，共 846；提取 47 个 exact batch 后，在当时查询窗口的 `central_control_sub_demo.s_task_info` 中全部为 0 条。该阶段故障边界只能定位为：

```text
SaaS task 已创建
↓
分控任务尚未出现
```

不能仅凭数据库说明中间哪个服务具体报错。

### 5.2 8/27 后续：分控任务已经生成但大量未入队

后续同批业务进入 `s_task_info` 后，曾观察到 42 个 batch × 11 个 mall 的：

```text
task_status=00
queue_status=00
enqueue_time IS NULL
```

共 462 个子任务。

11 个 mall：

```text
colipu, comix, ehsy, guangbo, jd,
lxwl, lxwlmall, mymro, nbdeli, ofs, zkh
```

全部位于：

```text
node_id=10000000003
```

该状态直接证明“分控子任务已经存在，但尚未入队”。

### 5.3 node 级大盘

**[LIVE-SQL]** 在 `node_id=10000000003`、2026-08-26 19:00 以后的一次快照中，14 个 mall 的 `00/00` 总量达到 168,716，其中：

```text
jd       107326
comix     17972
zkh       12320
lxwl       9386
nbdeli     7489
...
```

这只是当时快照，不是永久队列规模。

### 5.4 不能把根因写成“comix 全局坏了”

中途快照曾显示 comix 大量 `00/00`，但后续 [LIVE-SQL] 查到：

- 同一个 `node_id=10000000003`
- 同一个 `mall=comix`
- 其它租户（例如 199177）的旧任务

在 2026-08-27 10:59~11:01 能从长期等待进入队列并秒级完成。

因此已证伪：

```text
“comix crawler 全局不可用”
“整个 node 完全死掉”
```

更准确的数据库结论只能是：**当时存在大量入队前积压，且不同 mall/租户/任务类的释放节奏不同。** exact 根因仍需要分控日志/调度源码。

---

## 6. `weight_type` 与 11:30 左右的恢复

### 6.1 手册来源

**[MANUAL]**：

- `weight_type` 是分控任务等级/优先级字段。
- 一处手册写：`-1` 预留给运营人员 SQL 修改；`0` 紧急；`1` 重要；`2` 普通。
- `5.8` “对未执行任务进行加急”示例把目标未执行任务设置为 `weight_type=-1`。

手册文本本身存在不同操作片段，所以这里只确认优先级用途与 `-1` 的运营加急用途，不推导完整 scheduler 排序算法。

### 6.2 现场优先级变化后的事实

**[OPERATOR + LIVE-SQL]** 运营人员在 11:30 左右再次调整目标任务优先级后，多 mall 任务快速入队/完成，页面开始增长。

当天 `weight_type=-1` 的目标 54 batch 中：

- `colipu, ehsy, guangbo, jd, lxwl, lxwlmall, mymro, nbdeli, ofs, zkh`：54/54 很快完成。
- `comix` 当时一度为：38 完成 + 15 已入队 + 1 尚未入队。

前一天核心 11 mall 的 48 个 `-1` batch 中，大部分旧任务也在 2026-08-27 11:30~11:32 集中入队并快速结束。

**重要限制：** 这些旧任务在前一天就已是 `-1`，却直到今天 11:30 才集中释放。因此不能只写“设置 -1 必然立即执行”。数据库只能确认“11:30 左右发生了批量释放，且与再次人工优先级操作时间重合”；到底是重新触发、调度器恢复、缓存/队列状态刷新还是其它原因，需要分控日志确认。

### 6.3 普通 `weight_type=0` 任务并非全部卡死

**[LIVE-SQL]** 前一天 `weight_type=0, weight=1` 的多数 mall 任务已在 8/26 正常执行完成。

有 3 行一度显示 `task_status=02, queue_status=01`：

- `lxwlmall` 1 行
- `zjmi` 2 行

三行都有 `enqueue_time` 和 `end_time`，执行在约 1 秒内结束。因此不能把 `queue_status!=02` 自动判定成“爬虫没完成”。

---

## 7. `task_execute`：当前并不存在可用于“让路”的积压

### 7.1 手册的待执行条件

**[MANUAL]**：

```sql
STATE IS NULL
AND NEXT_TIME <= NOW()
AND CNT_RETRY < MAX_CNT_RETRY
```

手册的“让目标租户先跑”办法，是暂时把其它租户满足条件的待执行任务设置为临时 `STATE`，只留下目标租户，然后完成后恢复。

### 7.2 2026-08-27 当前全局查询

**[LIVE-SQL]** 执行：

```sql
SELECT TENANT_ID, TYPE, COUNT(1)
FROM onecode_base3.task_execute
WHERE STATE IS NULL
  AND CNT_RETRY < MAX_CNT_RETRY
  AND NEXT_TIME < NOW()
GROUP BY TENANT_ID, TYPE;
```

结果：**0 行**。

因此在这个查询时点：

```text
全系统 eligible pending task_execute = 0
```

不能用“其它租户 task_execute 占队列”解释当前同款审批页数据量，也没有可供手册“其它租户让路”机制调度的中核目标任务。

### 7.3 8/26~8/27 中核 task_execute 历史分布

**[LIVE-SQL]**：

```text
REFERENCE_PRICE_SYNC        pool 222 state=1     50
SAAS_GOODS_CRAWL            pool 222 state=1    193
SAAS_GOODS_CRAWL            pool 222 state=NULL   1  (CNT_RETRY=MAX_CNT_RETRY=3)
ZH_GOVEN_RESULT_SYNC_URL    pool 222 state=1     49
ZH_GOVEN_RESULT_SYNC_URL    pool 222 state=2      1
```

那条 `SAAS_GOODS_CRAWL state=NULL` 已经重试耗尽，不属于上述 eligible pending 条件。

**结论：** `search_table.sync_state=1` 是 SaaS 审批状态，不是 `task_execute.STATE=NULL` 队列积压。

---

## 8. 页面 / relation / IS_SYNC_STANDARD 的已验证边界

### 8.1 当时同款候选与 relation

一次精确对照结果：

```text
sync_state=0 + audit_result=2
80 候选 / 80 商品 -> 42 商品精确匹配有效 relation

sync_state=1 + audit_result=2
466 候选 / 135 商品 -> 0 商品精确匹配有效 relation
```

另一次商品级 relation 总量查询为：

```text
43 条有效 relation / 43 个商品
```

42 vs 43 的 1 个差异来自“候选级 vendor_name+sku_id 精确回连”与“商品级有效 relation 总数”口径不同；没有继续查出那 1 条的具体来源，因此不得猜。

### 8.2 `IS_SYNC_STANDARD` 快照

曾查到：

```text
IS_SYNC_STANDARD=0 : 146
IS_SYNC_STANDARD=1 : 43
IS_SYNC_STANDARD=2 : 5
```

该快照对应当时 194 条客户商品记录。后续客户继续新增，所以这不是 13:36 的最终数量。

**[MANUAL]**：`0` 未最终完成/未拉取到，`1` 完成同款拉取，`2` 已完成无同款。`0` 不能单独证明是哪一层没完成。

---

## 9. 当前真正需要继续排的两个上游缺口

截至 13:36 的最新闭合分类中：

```text
16 个：采集仍未结束
10 个：采集结束但无 search
```

这 26 个是真正还没走到“模型完成结果”阶段的客户商品记录。

另外 7 个是“模型完成但无任何同款”，不能自动视为异常；需要业务抽样核验模型判断是否合理。

对于“只有参考链接同款”的 89 个：

- 75 个已经有互联网非同款模型证据，主要需要核验召回质量/模型判定是否过严。
- 14 个没有观察到互联网 `model_audit_result=0/1`，可继续排互联网候选生成链。

---

## 10. 推荐的高性能商品级分类 SQL

> 目的：避免对每个商品重复执行 5~6 个相关 `EXISTS`。先缩小商品集合，再对 task/search 各聚合一次。

```sql
WITH base AS (
    SELECT DISTINCT
        c.MY_COMMODITY_ID,
        DATE(c.CREATED_TIME) AS business_date
    FROM saas_onecode_price.commodity_pool_188605 c
    WHERE c.COMMODITY_POOL_ID = 222
      AND c.IS_MONITOR = 1
      AND c.CREATED_TIME >= '2026-08-26 00:00:00'
      AND c.CREATED_TIME <  '2026-08-28 00:00:00'
),
base_ids AS (
    SELECT DISTINCT MY_COMMODITY_ID FROM base
),
cl_scope AS (
    SELECT cl.id, cl.commodity_id
    FROM saas_compare_price.commodity_list_table_188605 cl
    JOIN base_ids b ON b.MY_COMMODITY_ID = cl.commodity_id
),
task_flags AS (
    SELECT
        cl.commodity_id,
        MAX(t.task_status = 1) AS crawl_running
    FROM cl_scope cl
    JOIN saas_compare_price.task_table_188605 t
      ON t.reference_id = cl.id
     AND t.table_name = '188605'
    GROUP BY cl.commodity_id
),
search_flags AS (
    SELECT
        cl.commodity_id,
        1 AS has_search,
        MAX(s.audit_result IS NULL OR s.audit_result IN ('-1','-2')) AS model_pending,
        MAX(s.audit_result='2') AS has_any_same,
        MAX(s.audit_result='2' AND s.model_audit_result='1') AS has_internet_same,
        MAX(s.audit_result='2' AND s.model_audit_result='1' AND s.sync_state=1) AS internet_pending,
        MAX(s.audit_result='2' AND s.model_audit_result='1' AND s.sync_state=0) AS internet_approved,
        MAX(s.audit_result='2' AND s.model_audit_result='3') AS has_reference_same
    FROM cl_scope cl
    JOIN saas_compare_price.search_table_188605 s
      ON s.commodity_list_id = cl.id
    GROUP BY cl.commodity_id
),
flags AS (
    SELECT
        b.business_date,
        b.MY_COMMODITY_ID,
        COALESCE(t.crawl_running,0) AS crawl_running,
        COALESCE(s.has_search,0) AS has_search,
        COALESCE(s.model_pending,0) AS model_pending,
        COALESCE(s.has_any_same,0) AS has_any_same,
        COALESCE(s.has_internet_same,0) AS has_internet_same,
        COALESCE(s.internet_pending,0) AS internet_pending,
        COALESCE(s.internet_approved,0) AS internet_approved,
        COALESCE(s.has_reference_same,0) AS has_reference_same
    FROM base b
    LEFT JOIN task_flags t ON t.commodity_id=b.MY_COMMODITY_ID
    LEFT JOIN search_flags s ON s.commodity_id=b.MY_COMMODITY_ID
)
SELECT
    business_date,
    COUNT(*) AS 客户商品数,
    SUM(has_internet_same=1) AS 已产生互联网同款商品,
    SUM(internet_pending=1) AS 互联网同款待审批商品,
    SUM(internet_approved=1) AS 互联网同款已审批商品,
    SUM(crawl_running=0 AND has_search=1 AND model_pending=0
        AND has_internet_same=0 AND has_reference_same=1)
        AS 只有参考链接同款没有互联网同款,
    SUM(crawl_running=0 AND has_search=1 AND model_pending=0 AND has_any_same=0)
        AS 模型完成但无任何同款,
    SUM(crawl_running=1) AS 采集仍未结束,
    SUM(crawl_running=0 AND has_search=0) AS 采集结束但无search,
    SUM(crawl_running=0 AND has_search=1 AND model_pending=1) AS 模型仍在处理中
FROM flags
GROUP BY business_date
ORDER BY business_date;
```

注意：如果同一 `MY_COMMODITY_ID` 跨不同日期重复进入池，`DISTINCT MY_COMMODITY_ID, business_date` 会按业务日分别统计。这是按日运营视角，不等于全局唯一商品数。

---

## 11. 已证伪 / 不再使用的结论

1. **“377 页面行 = 377 个客户商品”**：错误。377 行只涉及 88 个商品。
2. **“sync_state=1 是后台 sync consumer 积压”**：错误。当前前端直接定义为待人工审批。
3. **“昨天 80 条 sync_state=0 是系统定时批量消费”**：错误。运营人员确认是人工审批参考链接；80 条时间分布与人工批次相符。
4. **“task_execute 其它租户积压导致当前同款审批页不增长”**：当前时点错误。全局 eligible pending 查询为 0 行。
5. **“comix 全局爬虫坏了”**：错误。后续同 node + 同 comix 的其它租户任务可以入队并完成。
6. **“node 10000000003 完全死了”**：错误。多个 mall 在排查窗口内持续有新入队/完成。
7. **“02/02 是唯一爬取完成状态”**：错误。存在 `02/01 + end_time` 的实际完成记录。
8. **“start_time 有值 = 已经实际开始执行”**：错误。`00/00` 未入队也见到 start_time。
9. **“210 个客户商品 - 88 个互联网同款商品 = 122 个互联网同款丢失”**：错误。122 个已经被分到只有参考链接、模型无同款、采集未完、无 search 等不同状态。

---

## 12. 仍未证明的事项

以下内容目前没有足够日志/源码证据，禁止写成确定根因：

- 11:30 批量入队究竟由再次改优先级、scheduler 恢复、队列/缓存刷新还是别的事件触发。
- `weight_type=-1/0/1/2` 在当前生产 scheduler 内部的完整比较/抢占算法。
- 14 个未观察到互联网 `model_audit_result=0/1` 商品究竟卡在爬虫、召回、search 生成还是其它更早步骤。
- 42 vs 43 relation 精确回连差 1 条的具体来源。
- 75 个“互联网候选全部判非同款”是否属于模型过严；需要抽样查看候选标题/规格/模型解释后才能判断。

这些必须继续用源码、服务日志或更细的只读 SQL 证明。
