# 中核 SaaS 运维知识库（纯中核版）

> 文件用途：作为“中核 SaaS 价格监测运维 Agent”的知识底座，用于 RAG 检索、问题分类、SQL 模板选择、巡检报告生成和只读诊断。  
> 适用范围：仅限中核项目相关运维，不包含宏伟、南水北调、华盛、其他租户专项内容。  
> 建议版本：v1.0  
> 建议文件名：`cnnc_ops_knowledge.md`

---

## 0. 范围约束

### 0.1 允许使用的内容

本知识库只允许包含以下中核相关内容：

- 中核租户：`tenant_id = 188605`
- 中核商品池：`commodity_pool_id = 222`
- 中核相关表：
  - `commodity_pool_188605`
  - `standard_commodity_relation_188605`
  - `standard_commodity_188605`
  - `commodity_pool_relation_188605`
  - `monitor_commodity_price_188605`
  - `monitor_commodity_price_last_change_188605`
  - `monitor_update_price_record`
  - `commodity_guide_price_188605`
  - `saas_compare_price.search_table_188605`
  - `onecode_base3.task_execute`
- 中核相关运维章节：
  - 中核线上化运维操作记录
  - 中核监控商品对标链接替换并推送
  - 中核监控商品类目信息补齐
  - 中核项目手工配合修改价格指数推送数据
  - 中核 SaaS 运维专项
  - 每周巡检及统计工作
  - 持续监测引起同款不足例行工作
  - HTML 导入同款数据核查
  - 中核每周六价格持续爬取任务排查
  - 中核同款审核线下执行
  - 与价格监控有关的任务执行规则

### 0.2 禁止使用的内容

本知识库明确禁止包含或推理以下内容：

- 宏伟专项内容
- 租户 `874649`
- 所有 `*_874649` 表
- 南水北调、华盛、其他租户专项 SQL
- 与中核无关的同步任务、采集任务、商品池数据
- 非中核的临时表、非中核的清表/导入/重推流程

### 0.3 Agent 行为边界

第一版中核运维 Agent 默认只做**只读诊断**：

- 允许：生成 `SELECT` 查询、解释结果、输出排查步骤、生成报告。
- 禁止：自动执行 `UPDATE`、`DELETE`、`INSERT`、`TRUNCATE`、`DROP`、`ALTER`、`CREATE`、`REPLACE`。
- 第一阶段严格禁止生成或展示任何写库 SQL（UPDATE/DELETE/INSERT/TRUNCATE/DROP/ALTER/CREATE/REPLACE），只允许只读诊断。涉及写库操作时，仅提示“需人工处理”，不展示具体变更 SQL。

---

## 1. 中核项目基础信息

### 1.1 固定参数

| 参数 | 值 | 说明 |
|---|---|---|
| `tenant_id` | `188605` | 中核租户编码 |
| `commodity_pool_id` | `222` | 中核监控商品池 |
| 数据库主库 | `saas_onecode_price` | 价格监控主库 |
| 任务表库 | `onecode_base3` | 异步任务执行表所在库 |
| 同款审核库 | `saas_compare_price` | ES 召回与同款审核相关数据 |

### 1.2 核心业务链路

```text
监控商品
  ↓
标品关系 / 标品状态
  ↓
同款关系
  ↓
持续价格爬取
  ↓
爬虫回调处理
  ↓
价格结果 / 最近价格变化
  ↓
任务推送
  ↓
中核客户侧接收与展示
```

### 1.3 中核运维的核心目标

中核运维不是单纯检查系统是否可用，而是保证下面这条业务链路持续稳定：

```text
中核监控商品存在
→ 商品处于持续监控状态
→ 商品有有效同款
→ 同款能被爬虫持续采价
→ 爬虫能正常回调
→ 价格结果能落库
→ 价格/标品/同款结果能正常推送
```

---

## 2. 核心表说明

### 2.1 `commodity_pool_188605`

中核监控商品表。

常用字段：

| 字段 | 说明 |
|---|---|
| `ID` | 表内主键，部分任务推送使用该值作为 `reference_id` |
| `COMMODITY_ID` | 商品编码 |
| `MY_COMMODITY_ID` | 内部商品 ID |
| `COMMODITY_POOL_ID` | 商品池 ID，中核固定为 `222` |
| `IS_MONITOR` | 是否持续监控 |
| `IS_SYNC` | 是否已同步供应商商品/是否供应商爬取 |
| `IS_SYNC_STANDARD` | 是否成功匹配同款/标准状态 |
| `CATEGORY_ID` | 商品类目 |
| `SYNC_LINK` | 对标链接/推送链接 |
| `CREATED_TIME` | 商品创建时间 |

### 2.2 `standard_commodity_relation_188605`

中核监控商品与价格标准商品关系表。

常用场景：

- 判断监控商品是否有标品。
- 判断 `MY_COMMODITY_ID` 是否能关联到标准商品。

### 2.3 `standard_commodity_188605`

中核标准商品表。

常用场景：

- 与 `standard_commodity_relation_188605` 联查，确认是否存在标准商品。

### 2.4 `commodity_pool_relation_188605`

中核监控商品与同款商品关系表。

常用字段：

| 字段 | 说明 |
|---|---|
| `SRC_COMMODITY_POOL_ID` | 来源商品池 ID，中核固定为 `222` |
| `SRC_MY_COMMODITY_ID` | 中核监控商品内部 ID |
| `DEST_SHOP_CODE` | 同款商品来源商城编码 |
| `DEST_SKU_ID` | 同款商品 SKU |
| `COMMODITY_NAME` | 同款商品名称 |
| `LINK_URL` | 同款商品链接 |
| `SHOP_NAME` | 商城名称 |
| `IS_VALID` | 是否有效，`1` 有效，`0` 下架/失效 |
| `CREATE_TIME` | 关系创建时间 |
| `CRAWL_TIME` | 最近爬取时间 |

### 2.5 `monitor_commodity_price_188605`

中核持续价格爬取结果表。

常用字段：

| 字段 | 说明 |
|---|---|
| `ID` | 价格爬取记录 ID |
| `COMMODITY_POOL_ID` | 商品池 ID，中核固定为 `222` |
| `MY_COMMODITY_ID` | 监控商品内部 ID |
| `SHOP_CODE` | 同款商品商城编码 |
| `SKU_ID` | 同款商品 SKU |
| `STATUS` | 爬取状态 |
| `CREATED_TIME` | 任务/记录创建时间 |
| `MONITOR_TIME` | 监控时间 |
| `GOODS_NO` | 可能参与拼接 `executeNumber` 的商品编号 |

#### `monitor_commodity_price_188605.STATUS` 枚举

| status | 含义 | 运维判断 |
|---:|---|---|
| `0` | 待爬取 | 定时任务还未扫到，或还未推送到爬虫 |
| `1` | 爬取中 | 已推送到爬虫，但爬虫尚未回调 |
| `2` | 爬取成功 | 爬虫已经回调，属于正常状态 |
| `3` | 爬取失败 | 需要导出失败任务给爬虫侧排查或重跑 |
| `4` | 有回调但未爬取到状态/价格 | 异常情况，可先观察或单独统计 |

### 2.6 `monitor_commodity_price_last_change_188605`

中核同款商品最近价格变化/最近监控结果表。

常用场景：

- 判断同款商品最近是否有有效价格变化。
- 检查 HTML 导入同款后是否出现重复数据。
- 清理重复数据时需要与 `commodity_pool_188605` 联查。

### 2.7 `monitor_update_price_record`

爬虫回调价格更新记录表。

常用字段：

| 字段 | 说明 |
|---|---|
| `tenant_id` | 中核固定为 `188605` |
| `commodity_pool_id` | 中核固定为 `222` |
| `check_status` | 回调价格更新处理状态 |
| `create_time` | 回调记录创建时间 |

#### `monitor_update_price_record.check_status` 枚举

| check_status | 含义 | 运维判断 |
|---:|---|---|
| `0` | 未处理 | 数据还未写入价格表，价格表状态可能仍为 `status=1` |
| `1` | 处理中 | 回调正在处理 |
| `2` | 处理完成 | 正常 |
| `3` | 处理失败 | 需要进一步排查回调处理异常 |

### 2.8 `commodity_guide_price_188605`

中核指导价/价格指数表。

常用场景：

- 手工配合修改指定商品价格指数。
- 修改后触发 `REFERENCE_PRICE_SYNC` 推送。

### 2.9 `saas_compare_price.search_table_188605`

中核 ES 召回与同款审核相关表。

常用字段：

| 字段 | 说明 |
|---|---|
| `id` | 待审核记录 ID |
| `v_commodity_id` | 监控商品内部 ID |
| `sync_state` | 待审核类型/同步状态 |
| `vendor_name` | 供应商编码 |
| `sku_id` | 待审核商品 SKU |
| `sku_name` | 待审核商品名称 |
| `ext_link` | 待审核商品链接 |
| `audit_result` | 审核结果 |

常见待审核类型解释：

| sync_state | 类型 |
|---:|---|
| `1` | 普通同款待审核 |
| `3` | 参考链接同款待审核 |
| `4` | 参考链接非同款待审核 |

### 2.10 `onecode_base3.task_execute`

异步任务执行表。

常用字段：

| 字段 | 说明 |
|---|---|
| `TENANT_ID` | 租户 ID，中核为 `188605` |
| `COMMODITY_POOL_ID` | 商品池 ID，中核为 `222` |
| `TYPE` | 任务类型 |
| `STATE` | 任务状态 |
| `REFERENCE_ID` | 关联业务记录 ID |
| `CNT_RETRY` | 已重试次数 |
| `MAX_CNT_RETRY` | 最大重试次数 |
| `NEXT_TIME` | 下次执行时间 |
| `CREATED_TIME` | 任务创建时间 |
| `LAST_RUN` | 最近执行时间 |

#### `task_execute.STATE` 枚举

| state | 含义 |
|---|---|
| `NULL` | 待推送 / 待执行 |
| `0` | 推送中，待重试 |
| `1` | 推送成功 |
| `2` | 推送失败 |
| `4` | 失败，无需推送，数据不合格 |
| 其他数字 | 临时状态，可能由人工干预产生 |

#### 中核相关任务类型

| 任务类型 | 说明 |
|---|---|
| `CATEGORY_RECOGNITION_FOR_MODEL` | 类目识别；POC 无类目识别时会调用爬虫爬取供应商商品 |
| `ZH_GOVEN_RESULT_SYNC_URL` | 标品推送任务，对接中核客户 |
| `REFERENCE_PRICE_SYNC` | 指导价/价格指数推送任务 |
| `MATCH_INTERNET_GOODS_SYNC` | 互联网商品匹配结果推送 |
| `MATCH_PRICE_RESULT_SYNC` | 比价链接推送任务 |

> 注意：虽然系统存在其他任务类型，但中核 Agent 默认只关注与中核链路相关的任务类型。

---

## 3. 监控商品状态处理规则

### 3.1 商品状态组合

中核监控商品可以通过 `IS_SYNC` 与 `IS_SYNC_STANDARD` 判断状态。

| `IS_SYNC` | `IS_SYNC_STANDARD` | 含义 | 处理建议 |
|---|---|---|---|
| `1` | `1` | 有标品、有同款商品、状态正确 | 不处理，后续正常进行价格持续爬取 |
| `1` | `0` | 已推送治理，但没有同款；部分可能没有标品 | 对没有标品的数据先补齐标品，再将 `IS_SYNC` 调整为 `0`，重新走供应商商品爬取与同款匹配流程 |
| `NULL` | `1` | 有标品、有同款商品，但状态错误 | 将 `IS_SYNC` 调整为 `1` |
| `NULL` | `NULL` | 无标品、无同款异常数据 | 补齐标品，并将状态置为 `0` |

### 3.2 商品状态统计 SQL

```sql
SELECT
  IS_SYNC,
  IS_SYNC_STANDARD,
  COUNT(1) AS nums
FROM saas_onecode_price.commodity_pool_188605
WHERE COMMODITY_POOL_ID = 222
  AND IS_MONITOR = 1
GROUP BY IS_SYNC, IS_SYNC_STANDARD;
```

### 3.3 有标品、有同款的正常商品统计

```sql
SELECT COUNT(1)
FROM saas_onecode_price.commodity_pool_188605 c
JOIN saas_onecode_price.standard_commodity_relation_188605 r
  ON c.MY_COMMODITY_ID = r.MY_COMMODITY_ID
JOIN saas_onecode_price.standard_commodity_188605 s
  ON r.V_COMMODITY_ID = s.V_COMMODITY_ID
WHERE c.COMMODITY_POOL_ID = 222
  AND c.IS_MONITOR = 1
  AND c.IS_SYNC = 1
  AND c.IS_SYNC_STANDARD = 1;
```

### 3.4 有效同款统计

```sql
SELECT COUNT(DISTINCT c.MY_COMMODITY_ID) AS nums
FROM saas_onecode_price.commodity_pool_188605 c
JOIN saas_onecode_price.commodity_pool_relation_188605 r
  ON c.MY_COMMODITY_ID = r.SRC_MY_COMMODITY_ID
WHERE c.COMMODITY_POOL_ID = 222
  AND c.IS_MONITOR = 1
  AND c.IS_SYNC = 1
  AND c.IS_SYNC_STANDARD = 1
  AND r.IS_VALID = 1;
```

---

## 4. 每周巡检及统计工作

### 4.1 巡检背景

中核每周会触发价格的持续更新及爬取。持续更新会对所有同款商品进行爬取，运维需要定期查看：

- 已经回调多少；
- 仍在爬取中多少；
- 失败多少；
- 是否存在异常积压；
- 本周新增监控商品、同款、下架同款、增补同款数量。

### 4.2 价格爬取状态分布查询

```sql
SELECT
  STATUS,
  COUNT(1) AS nums
FROM saas_onecode_price.monitor_commodity_price_188605
WHERE COMMODITY_POOL_ID = 222
  AND CREATED_TIME > '{{start_time}}'
GROUP BY STATUS;
```

### 4.3 过去一周新增监控商品数

```sql
SELECT COUNT(*) AS nums
FROM saas_onecode_price.commodity_pool_188605
WHERE COMMODITY_POOL_ID = 222
  AND CREATED_TIME BETWEEN '{{start_time}}' AND '{{end_time}}';
```

### 4.4 过去一周新增同款商品数

```sql
SELECT COUNT(*) AS nums
FROM saas_onecode_price.commodity_pool_relation_188605
WHERE CREATE_TIME BETWEEN '{{start_time}}' AND '{{end_time}}'
  AND IS_VALID = '1'
  AND SRC_MY_COMMODITY_ID IN (
    SELECT MY_COMMODITY_ID
    FROM saas_onecode_price.commodity_pool_188605
    WHERE COMMODITY_POOL_ID = 222
      AND CREATED_TIME BETWEEN '{{start_time}}' AND '{{end_time}}'
  );
```

### 4.5 过去一周下架同款商品数

```sql
SELECT COUNT(*) AS nums
FROM saas_onecode_price.commodity_pool_relation_188605
WHERE CRAWL_TIME BETWEEN '{{start_time}}' AND '{{end_time}}'
  AND IS_VALID = '0';
```

### 4.6 过去一周增补同款数

```sql
SELECT COUNT(*) AS nums
FROM saas_onecode_price.commodity_pool_relation_188605
WHERE CREATE_TIME BETWEEN '{{start_time}}' AND '{{end_time}}'
  AND IS_VALID = '1';
```

### 4.7 巡检结果解释规则

| 现象 | 判断 |
|---|---|
| `status=2` 占绝大多数 | 本轮持续爬取基本正常 |
| `status=1` 数量较大且长时间不变 | 爬虫未回调、回调积压或爬虫任务阻塞 |
| `status=3` 数量较大 | 爬取失败较多，需要导出 `executeNumber` 给爬虫侧 |
| `status=4` 数量较大 | 有回调但未采到状态/价格，作为异常数据观察或补充排查 |
| 新增同款数低、下架同款数高 | 同款池质量下降，需要启动同款不足治理 |

---

## 5. 持续监测引起同款不足例行工作

### 5.1 场景说明

SaaS 持续监测是对已经梳理好的同款商品做爬虫持续监测。随着每周持续监测推进，可能出现：

- 同款商品全部下架；
- 同款商品只剩 1 个；
- 有效同款数量不足，导致价格监测稳定性下降。

当某个监控商品有效同款只剩 1 个或更少时，需要阶段性重新走：

```text
采集 → 召回 → 匹配 → 过滤 → 补充同款
```

在自动化流程上线前，该工作可作为固定数据运营工作：周五执行一次，并在下一次周三前补充新增同款。

### 5.2 有效同款数量统计

```sql
SELECT
  c.MY_COMMODITY_ID,
  c.COMMODITY_ID,
  c.COMMODITY_NAME,
  COUNT(r.ID) AS valid_relation_count
FROM saas_onecode_price.commodity_pool_188605 c
LEFT JOIN saas_onecode_price.commodity_pool_relation_188605 r
  ON c.MY_COMMODITY_ID = r.SRC_MY_COMMODITY_ID
 AND c.COMMODITY_POOL_ID = r.SRC_COMMODITY_POOL_ID
 AND r.IS_VALID = 1
WHERE c.COMMODITY_POOL_ID = 222
  AND c.IS_MONITOR = 1
GROUP BY c.MY_COMMODITY_ID, c.COMMODITY_ID, c.COMMODITY_NAME
ORDER BY valid_relation_count ASC;
```

### 5.3 同款不足商品查询

```sql
SELECT *
FROM (
  SELECT
    c.MY_COMMODITY_ID,
    c.COMMODITY_ID,
    c.COMMODITY_NAME,
    COUNT(r.ID) AS valid_relation_count
  FROM saas_onecode_price.commodity_pool_188605 c
  LEFT JOIN saas_onecode_price.commodity_pool_relation_188605 r
    ON c.MY_COMMODITY_ID = r.SRC_MY_COMMODITY_ID
   AND c.COMMODITY_POOL_ID = r.SRC_COMMODITY_POOL_ID
   AND r.IS_VALID = 1
  WHERE c.COMMODITY_POOL_ID = 222
    AND c.IS_MONITOR = 1
  GROUP BY c.MY_COMMODITY_ID, c.COMMODITY_ID, c.COMMODITY_NAME
) t
WHERE t.valid_relation_count <= 1
ORDER BY t.valid_relation_count ASC;
```

### 5.4 处理建议

| 有效同款数量 | 处理建议 |
|---:|---|
| `0` | 必须补充同款；重新走采集、召回、匹配、过滤 |
| `1` | 同款不足；进入周五例行补充清单 |
| `2-3` | 可观察，视业务要求决定是否补充 |
| `>=4` | 通常可正常持续监测 |

---

## 6. HTML 导入同款数据核查

### 6.1 场景说明

HTML 导入同款数据对接的接口是同步接口。导入数据量大时，接口可能超时，运营人员无法确认数据是否已经导入成功，可能会再次导入，导致同款关系表或最近价格变化表出现重复数据。

需要核查：

- `commodity_pool_relation_188605` 是否有重复；
- `monitor_commodity_price_last_change_188605` 是否有重复；
- 删除重复数据前必须备份，并人工确认。

### 6.2 relation 表重复核查

```sql
SELECT *
FROM (
  SELECT
    r.SRC_MY_COMMODITY_ID,
    r.DEST_SHOP_CODE,
    r.DEST_SKU_ID,
    COUNT(1) AS total
  FROM saas_onecode_price.commodity_pool_relation_188605 r
  JOIN saas_onecode_price.commodity_pool_188605 c
    ON r.SRC_MY_COMMODITY_ID = c.MY_COMMODITY_ID
   AND r.SRC_COMMODITY_POOL_ID = c.COMMODITY_POOL_ID
  WHERE c.IS_MONITOR = 1
    AND c.COMMODITY_POOL_ID = 222
  GROUP BY r.SRC_MY_COMMODITY_ID, r.DEST_SHOP_CODE, r.DEST_SKU_ID
) a
WHERE a.total > 1;
```

### 6.3 last_change 表重复核查

```sql
SELECT *
FROM (
  SELECT
    c.MY_COMMODITY_ID,
    l.SHOP_CODE,
    l.SKU_ID,
    COUNT(1) AS total
  FROM saas_onecode_price.commodity_pool_188605 c
  JOIN saas_onecode_price.monitor_commodity_price_last_change_188605 l
    ON c.MY_COMMODITY_ID = l.MY_COMMODITY_ID
   AND c.COMMODITY_POOL_ID = l.COMMODITY_POOL_ID
  WHERE c.IS_MONITOR = 1
    AND c.COMMODITY_POOL_ID = 222
  GROUP BY c.MY_COMMODITY_ID, l.SHOP_CODE, l.SKU_ID
) a
WHERE a.total > 1;
```

### 6.4 删除重复数据风险提示

删除重复数据属于高风险操作，Agent 不允许自动执行。必须满足：

1. 已查询重复明细；
2. 已导出重复记录；
3. 已生成备份 SQL；
4. 已确认保留规则，如保留最新 `create_time` 或最新 `crawl_time`；
5. 人工确认后再执行删除；
6. 删除后复查重复数量是否为 0。

---

## 7. 中核每周六价格持续爬取任务排查

### 7.1 场景说明

中核每周六会有大批量同款商品价格持续爬取任务发起。周一需要排查任务数据是否有异常，并将异常反馈给爬虫侧及时调整。

排查内容：

1. 任务数量查询；
2. 回调情况查询；
3. 爬取失败数据导出，供爬虫侧再次爬取。

### 7.2 周六零点后爬虫调用数量

```sql
SELECT SUM(num) AS call_num
FROM crawler_call_record
WHERE tenant_id = '188605'
  AND call_time > '{{start_time}}';
```

### 7.3 周六后价格爬取状态分布

```sql
SELECT
  STATUS,
  COUNT(1) AS nums
FROM saas_onecode_price.monitor_commodity_price_188605
WHERE COMMODITY_POOL_ID = 222
  AND CREATED_TIME > '{{start_time}}'
GROUP BY STATUS;
```

### 7.4 爬取失败/异常 executeNumber 导出

```sql
SELECT
  CASE
    WHEN p.GOODS_NO IS NULL THEN CONCAT('saas-', p.ID, '-188605-222-', p.MY_COMMODITY_ID)
    ELSE CONCAT('saas-', p.ID, '-188605-222-', p.MY_COMMODITY_ID, '-', p.GOODS_NO)
  END AS executeNumber
FROM saas_onecode_price.monitor_commodity_price_188605 p
JOIN saas_onecode_price.commodity_pool_relation_188605 r
  ON p.MY_COMMODITY_ID = r.SRC_MY_COMMODITY_ID
 AND p.COMMODITY_POOL_ID = r.SRC_COMMODITY_POOL_ID
 AND p.SHOP_CODE = r.DEST_SHOP_CODE
 AND p.SKU_ID = r.DEST_SKU_ID
WHERE p.CREATED_TIME > '{{start_time}}'
  AND p.COMMODITY_POOL_ID = 222
  AND p.STATUS IN (3, 4)
  AND r.IS_VALID = 1;
```

### 7.5 排查结论模板

```text
本轮中核周六持续爬取任务巡检结果：

1. 本轮发起爬虫调用数量：{{call_num}}
2. 价格爬取状态分布：
   - 待爬取 status=0：{{status_0}}
   - 爬取中 status=1：{{status_1}}
   - 爬取成功 status=2：{{status_2}}
   - 爬取失败 status=3：{{status_3}}
   - 未爬取到价格 status=4：{{status_4}}

初步判断：
- 若 status=1 数量较大且持续不变，优先判断爬虫未回调或回调积压。
- 若 status=3 数量较大，需导出 executeNumber 给爬虫侧重跑。
- 若 status=4 数量较大，说明有回调但未采到价格或状态，需单独观察。
```

---

## 8. 中核同款审核线下执行

### 8.1 场景说明

模型可能识别不出同款，或召回结果未形成有效同款。此时可以把 ES 召回数据导出，重新进行同款识别或人工审核。

### 8.2 待审核数据导出字段

建议导出字段：

| 字段 | 说明 |
|---|---|
| 待审核记录 id | `search_table_188605.id` |
| 监控商品编码 | `commodity_pool_188605.COMMODITY_ID` |
| 监控商品名称 | `commodity_pool_188605.COMMODITY_NAME` |
| 待审核类型 | 根据 `sync_state` 解释 |
| 供应商编码 | `search_table_188605.vendor_name` |
| 待审核商品 SKU | `search_table_188605.sku_id` |
| 待审核商品名称 | `search_table_188605.sku_name` |
| 待审核商品链接 | `search_table_188605.ext_link` |

### 8.3 待审核类型解释

```sql
CASE
  WHEN s.sync_state = 1 THEN '普通同款待审核'
  WHEN s.sync_state = 3 THEN '参考链接同款待审核'
  WHEN s.sync_state = 4 THEN '参考链接非同款待审核'
  ELSE '未知类型'
END AS audit_type
```

### 8.4 待审核记录导出 SQL 模板

```sql
SELECT
  CONCAT(s.ID, '\t') AS '待审核记录id',
  CONCAT(c.COMMODITY_ID, '\t') AS '监控商品编码',
  c.COMMODITY_NAME AS '监控商品名称',
  CASE
    WHEN s.SYNC_STATE = 1 THEN '普通同款待审核'
    WHEN s.SYNC_STATE = 3 THEN '参考链接同款待审核'
    WHEN s.SYNC_STATE = 4 THEN '参考链接非同款待审核'
    ELSE '未知类型'
  END AS '待审核类型',
  s.VENDOR_NAME AS '供应商编码',
  CONCAT(s.SKU_ID, '\t') AS '待审核商品skuId',
  s.SKU_NAME AS '待审核商品名称',
  s.EXT_LINK AS '待审核商品链接'
FROM saas_compare_price.search_table_188605 s
JOIN saas_onecode_price.commodity_pool_188605 c
  ON s.V_COMMODITY_ID = c.MY_COMMODITY_ID
WHERE c.COMMODITY_POOL_ID = 222
  AND c.IS_MONITOR = 1
  AND s.CREATE_TIME BETWEEN '{{start_time}}' AND '{{end_time}}';
```

### 8.5 AI 同款判断输出格式

| 输出字段 | 取值 |
|---|---|
| 是否同款 | `是` / `否` / `存疑` |
| 判断理由 | 简要说明关键依据 |
| 风险等级 | `低` / `中` / `高` |
| 关键冲突字段 | 品牌、型号、规格、功率、容量、尺寸、包装数量等 |

### 8.6 同款判断原则

优先比较：

1. 品牌是否一致；
2. 型号是否一致；
3. 规格参数是否一致；
4. 功率、容量、尺寸、电压、防护等级等关键属性是否冲突；
5. 单位、包装数量、套装关系是否冲突；
6. 商品名称是否存在明显不同品类。

判断规则：

| 情况 | 判断 |
|---|---|
| 品牌、型号、核心规格一致 | 是 |
| 型号冲突、品类冲突、规格明显冲突 | 否 |
| 信息不足但无明显冲突 | 存疑 |
| 仅颜色、轻微描述差异，不影响型号规格 | 可判是，需说明 |

---

## 9. 中核监控商品对标链接替换并推送

### 9.1 场景说明

当中核某个监控商品的对标链接需要替换时，需要更新 `commodity_pool_188605.sync_link`，并触发 `ZH_GOVEN_RESULT_SYNC_URL` 推送任务。

该场景属于写库操作，Agent 不能自动执行，只能生成操作方案与 SQL。

### 9.2 操作前查询

```sql
SELECT *
FROM saas_onecode_price.commodity_pool_188605
WHERE COMMODITY_POOL_ID = 222
  AND COMMODITY_ID = '{{commodity_id}}';
```

### 9.3 查询存量推送任务

```sql
SELECT *
FROM onecode_base3.task_execute
WHERE REFERENCE_ID = '{{commodity_pool_table_id}}'
  AND TYPE = 'ZH_GOVEN_RESULT_SYNC_URL';
```

### 9.4 变更逻辑

如果存在存量任务：

```sql
-- 高风险：人工确认后执行
UPDATE onecode_base3.task_execute
SET STATE = NULL,
    CNT_RETRY = 0,
    NEXT_TIME = NOW()
WHERE ID = '{{task_execute_id}}';
```

如果不存在存量任务：

```sql
-- 高风险：人工确认后执行
INSERT INTO onecode_base3.task_execute
  (COMMODITY_POOL_ID, TENANT_ID, TYPE, STATE, REFERENCE_ID, CNT_RETRY, MAX_CNT_RETRY, TIME_INTERVAL, NEXT_TIME, CREATED_TIME)
VALUES
  (222, '188605', 'ZH_GOVEN_RESULT_SYNC_URL', NULL, '{{commodity_pool_table_id}}', 0, 3, 300, NOW(), NOW());
```

### 9.5 操作后复查

```sql
SELECT *
FROM onecode_base3.task_execute
WHERE REFERENCE_ID = '{{commodity_pool_table_id}}'
  AND TYPE = 'ZH_GOVEN_RESULT_SYNC_URL';
```

判断：

- `STATE = 1`：推送成功；
- `STATE = 0`：推送中或待重试；
- `STATE = 2`：推送失败，需要看推送记录和返回报文；
- `STATE IS NULL`：待执行。

---

## 10. 中核监控商品类目信息补齐

### 10.1 场景说明

中核监控商品类目信息补齐依赖从价格标品库导出的标准商品信息。需要确认 SaaS 监控商品默认类目层级；文档记录中当前商品表类目信息为四级类目。

### 10.2 临时表与映射表

常见临时表：

| 表 | 说明 |
|---|---|
| `commodity_pool_188605_0825` | 更新类目之后的商品数据 |
| `commodity_pool_188605_0825_1` | 更新类目之前的商品数据 |
| `tmp_commodity_category_188605` | Excel 导入的类目数据 |
| `category_info_188605` | 中核类目映射临时表 |

### 10.3 校验 SQL

#### 监控商品总数

```sql
SELECT COUNT(1)
FROM commodity_pool_188605_0825_1;
```

#### 持续监控商品总数

```sql
SELECT COUNT(1)
FROM commodity_pool_188605_0825_1
WHERE IS_MONITOR = 1;
```

#### 有类目的监控商品数量

```sql
SELECT COUNT(1)
FROM commodity_pool_188605_0825
WHERE CATEGORY_ID IS NOT NULL
  AND LENGTH(CATEGORY_ID) > 0;
```

#### 不在类目映射内的有类目监控商品

```sql
SELECT COUNT(1)
FROM commodity_pool_188605_0825_1
WHERE CATEGORY_ID IS NOT NULL
  AND LENGTH(CATEGORY_ID) > 0
  AND CATEGORY_ID NOT IN (
    SELECT l4_category_id
    FROM category_info_188605
  );
```

#### 现有商品类目信息与提供数据不一致商品数量

```sql
SELECT COUNT(c1.COMMODITY_ID)
FROM commodity_pool_188605_0825_1 c1
JOIN tmp_commodity_category_188605 c2
  ON c1.COMMODITY_ID = c2.COMMODITY_ID
WHERE c1.CATEGORY_ID IS NOT NULL
  AND LENGTH(c1.CATEGORY_ID) > 0
  AND c1.CATEGORY_ID != c2.CATEGORY_ID;
```

### 10.4 风险提示

类目补齐会影响商品匹配、过滤、统计和后续展示。Agent 第一阶段只生成校验 SQL（只读），不展示变更 SQL，不允许自动执行生产更新。

---

## 11. 中核价格指数修改并推送

### 11.1 场景说明

当项目要求手工配合修改指定商品的价格指数时，需要更新 `commodity_guide_price_188605.price_value`，然后通过 `REFERENCE_PRICE_SYNC` 任务触发价格指数推送。

这是高风险写库操作，必须人工确认。

### 11.2 操作前查询商品

```sql
SELECT *
FROM saas_onecode_price.commodity_pool_188605
WHERE COMMODITY_POOL_ID = 222
  AND COMMODITY_ID = '{{commodity_id}}';
```

### 11.3 操作前查询价格指数

```sql
SELECT *
FROM saas_onecode_price.commodity_guide_price_188605
WHERE MY_COMMODITY_ID = (
  SELECT MY_COMMODITY_ID
  FROM saas_onecode_price.commodity_pool_188605
  WHERE COMMODITY_POOL_ID = 222
    AND COMMODITY_ID = '{{commodity_id}}'
);
```

### 11.4 高风险变更 SQL 模板

```sql
-- 高风险：必须人工确认商品、旧值、新值后执行
UPDATE saas_onecode_price.commodity_guide_price_188605
SET PRICE_VALUE = {{price_value}},
    CALC_TIME = NOW()
WHERE MY_COMMODITY_ID = (
  SELECT MY_COMMODITY_ID
  FROM saas_onecode_price.commodity_pool_188605
  WHERE COMMODITY_POOL_ID = 222
    AND COMMODITY_ID = '{{commodity_id}}'
);
```

### 11.5 查询价格指数推送任务

```sql
SELECT *
FROM onecode_base3.task_execute
WHERE COMMODITY_POOL_ID = '222'
  AND TENANT_ID = '188605'
  AND TYPE = 'REFERENCE_PRICE_SYNC'
  AND REFERENCE_ID = (
    SELECT CONCAT(ID, '')
    FROM saas_onecode_price.commodity_pool_188605
    WHERE COMMODITY_POOL_ID = 222
      AND COMMODITY_ID = '{{commodity_id}}'
  );
```

### 11.6 高风险重推 SQL 模板

```sql
-- 高风险：必须人工确认后执行
UPDATE onecode_base3.task_execute
SET STATE = NULL
WHERE ID = '{{task_execute_id}}';
```

---

## 12. 任务执行与积压排查

### 12.1 待执行任务按租户和任务类型聚合

```sql
SELECT
  TENANT_ID,
  TYPE,
  COUNT(1) AS nums
FROM onecode_base3.task_execute
WHERE STATE IS NULL
  AND NEXT_TIME <= NOW()
  AND CNT_RETRY < MAX_CNT_RETRY
GROUP BY TENANT_ID, TYPE
ORDER BY nums DESC;
```

### 12.2 中核待执行任务查询

```sql
SELECT
  TYPE,
  COUNT(1) AS nums
FROM onecode_base3.task_execute
WHERE TENANT_ID = '188605'
  AND STATE IS NULL
  AND NEXT_TIME <= NOW()
  AND CNT_RETRY < MAX_CNT_RETRY
GROUP BY TYPE
ORDER BY nums DESC;
```

### 12.3 中核指定任务推送状态统计

```sql
SELECT
  STATE,
  COUNT(1) AS nums
FROM onecode_base3.task_execute
WHERE TENANT_ID = '188605'
  AND COMMODITY_POOL_ID = 222
  AND TYPE = '{{task_type}}'
  AND CREATED_TIME > '{{start_time}}'
GROUP BY STATE;
```

### 12.4 任务状态解释

| 状态 | 判断 |
|---|---|
| `STATE IS NULL` | 待执行；如果长时间不执行，可能有任务积压 |
| `STATE = 0` | 推送中或待重试 |
| `STATE = 1` | 推送成功 |
| `STATE = 2` | 推送失败，需要查报文和返回 |
| `STATE = 4` | 失败且无需推送，可能数据不合格 |

### 12.5 任务积压判断

如果任务一直卡着不执行，常见原因：

1. 某个租户的任务一直失败，阻塞了其他租户任务；
2. 中核自身某类任务大量堆积；
3. `NEXT_TIME` 已到但 `STATE` 长时间为 `NULL`；
4. `CNT_RETRY` 达到上限；
5. handle 应用处理异常。

---

## 13. 单商品问题标准排查链路

### 13.1 适用问题

用户或客户反馈：

- 某商品没有价格；
- 某商品价格不更新；
- 某商品一直采集中；
- 某商品没有同款；
- 某商品推送失败；
- 某商品客户侧没收到。

### 13.2 排查步骤

```text
1. 查商品是否存在于中核商品池 222。
2. 查商品是否 IS_MONITOR = 1。
3. 查 IS_SYNC / IS_SYNC_STANDARD 状态。
4. 查是否存在有效同款。
5. 查本轮价格爬取状态。
6. 查爬虫回调处理状态。
7. 查最近价格变化结果。
8. 查 task_execute 推送任务。
9. 根据状态给出结论和下一步建议。
```

### 13.3 商品基础信息查询

```sql
SELECT *
FROM saas_onecode_price.commodity_pool_188605
WHERE COMMODITY_POOL_ID = 222
  AND COMMODITY_ID = '{{commodity_id}}';
```

### 13.4 有效同款查询

```sql
SELECT *
FROM saas_onecode_price.commodity_pool_relation_188605
WHERE SRC_COMMODITY_POOL_ID = 222
  AND SRC_MY_COMMODITY_ID = '{{my_commodity_id}}'
  AND IS_VALID = 1;
```

### 13.5 最近价格爬取状态查询

```sql
SELECT
  STATUS,
  COUNT(1) AS nums
FROM saas_onecode_price.monitor_commodity_price_188605
WHERE COMMODITY_POOL_ID = 222
  AND MY_COMMODITY_ID = '{{my_commodity_id}}'
  AND CREATED_TIME > '{{start_time}}'
GROUP BY STATUS;
```

### 13.6 回调处理状态查询

```sql
SELECT
  CHECK_STATUS,
  COUNT(1) AS nums
FROM saas_onecode_price.monitor_update_price_record
WHERE TENANT_ID = '188605'
  AND COMMODITY_POOL_ID = 222
  AND CREATE_TIME > '{{start_time}}'
GROUP BY CHECK_STATUS;
```

### 13.7 最近价格变化查询

```sql
SELECT *
FROM saas_onecode_price.monitor_commodity_price_last_change_188605
WHERE COMMODITY_POOL_ID = 222
  AND MY_COMMODITY_ID = '{{my_commodity_id}}'
ORDER BY CRAWL_TIME DESC
LIMIT 50;
```

---

## 14. Agent 场景分类规则

### 14.1 场景枚举

| 场景 | 触发问题 |
|---|---|
| `weekly_check` | 周巡检、本周情况、每周统计、跑批情况 |
| `single_item_check` | 单商品没价格、价格不更新、商品异常 |
| `crawler_check` | 采集中、爬虫未回调、爬取失败、回调异常 |
| `relation_check` | 无同款、同款不足、同款下架、同款重复 |
| `duplicate_check` | HTML 导入、重复导入、重复同款、重复 last_change |
| `task_check` | 推送失败、客户没收到、任务积压、task_execute 异常 |
| `match_review` | 同款审核、标题匹配、是否同款 |
| `category_fill` | 类目补齐、类目缺失、类目映射 |
| `sync_link_replace` | 对标链接替换、sync_link 修改 |
| `price_index_modify` | 价格指数修改、指导价修改、REFERENCE_PRICE_SYNC |

### 14.2 路由关键词

```yaml
weekly_check:
  keywords:
    - 周巡检
    - 本周
    - 每周
    - 跑批
    - 持续更新
    - 数据统计

single_item_check:
  keywords:
    - 没价格
    - 价格不更新
    - 商品异常
    - 单商品
    - 查这个商品

crawler_check:
  keywords:
    - 采集中
    - 爬虫
    - 回调
    - 爬取失败
    - status

relation_check:
  keywords:
    - 没有同款
    - 同款不足
    - 同款下架
    - 有效同款
    - relation

duplicate_check:
  keywords:
    - HTML导入
    - 重复导入
    - 重复同款
    - 重复数据

task_check:
  keywords:
    - 推送失败
    - 客户没收到
    - 任务积压
    - task_execute
    - 重推

match_review:
  keywords:
    - 同款审核
    - 是否同款
    - 标题匹配
    - 待审核

category_fill:
  keywords:
    - 类目补齐
    - 类目缺失
    - 类目映射

sync_link_replace:
  keywords:
    - 对标链接
    - 替换链接
    - sync_link

price_index_modify:
  keywords:
    - 价格指数
    - 指导价
    - 修改价格
    - REFERENCE_PRICE_SYNC
```

---

## 15. Agent 结果解释规则

### 15.1 价格爬取状态解释

| 查询结果 | 解释 |
|---|---|
| `status=2` 高 | 爬虫回调正常，价格爬取基本成功 |
| `status=1` 高 | 爬虫未回调或回调积压，需继续查回调记录 |
| `status=3` 高 | 爬虫失败较多，应导出 executeNumber 给爬虫侧 |
| `status=4` 高 | 有回调但未采到价格/状态，可作为异常数据观察 |
| `status=0` 高 | 定时任务未扫到、未推送到爬虫或任务积压 |

### 15.2 回调状态解释

| 查询结果 | 解释 |
|---|---|
| `check_status=0` 高 | 回调未处理，价格表可能仍显示爬取中 |
| `check_status=1` 高 | 回调处理中，若长时间不变需排查处理线程 |
| `check_status=2` 高 | 回调处理正常 |
| `check_status=3` 高 | 回调处理失败，需排查失败原因 |

### 15.3 同款数量解释

| 有效同款数 | 解释 |
|---:|---|
| `0` | 无有效同款，无法稳定做持续价格监测 |
| `1` | 同款不足，应进入例行补充流程 |
| `2-3` | 较少，建议观察 |
| `>=4` | 同款数量相对健康 |

### 15.4 任务推送解释

| `task_execute.STATE` | 解释 |
|---|---|
| `NULL` | 待执行，需看是否积压 |
| `0` | 推送中或待重试 |
| `1` | 推送成功 |
| `2` | 推送失败，需要查报文和返回 |
| `4` | 失败且无需推送，可能数据不合格 |

---

## 16. 写库操作风控规则

### 16.1 高风险操作清单

以下操作 Agent 不允许自动执行：

- 更新 `commodity_pool_188605.sync_link`
- 更新 `commodity_guide_price_188605.price_value`
- 更新 `task_execute.state`
- 插入 `task_execute`
- 删除 `commodity_pool_relation_188605` 重复数据
- 删除 `monitor_commodity_price_last_change_188605` 重复数据
- 更新类目信息
- 导入同款审核结果
- 清空临时表或生产表

### 16.2 必须遵循的人工确认流程

```text
1. 查询旧值
2. 提示需人工备份（第一阶段不生成备份 SQL）
3. 提示需人工处理（第一阶段不展示变更 SQL）
4. 展示影响行数
5. 人工确认
6. 执行变更
7. 复查结果
8. 记录操作日志
```

### 16.3 SQL 安全拦截规则

第一版 Agent 只允许执行：

- `SELECT`
- `WITH ... SELECT`

禁止执行：

- `UPDATE`
- `DELETE`
- `INSERT`
- `TRUNCATE`
- `DROP`
- `ALTER`
- `CREATE`
- `REPLACE`
- `MERGE`
- `CALL`
- `EXEC`

---

## 17. Agent 输出模板

### 17.1 单商品排查输出模板

```markdown
## 问题类型
single_item_check

## 排查对象
- commodity_id：{{commodity_id}}
- tenant_id：188605
- commodity_pool_id：222

## 排查步骤
1. 查询商品是否存在于中核商品池。
2. 查询商品是否持续监控。
3. 查询商品标品/同款状态。
4. 查询有效同款数量。
5. 查询最近价格爬取状态。
6. 查询回调处理状态。
7. 查询最近价格变化。
8. 查询推送任务。

## 建议 SQL
{{sql_list}}

## 结果解释
{{analysis}}

## 下一步建议
{{next_action}}

## 风险提示
当前为只读诊断，不执行任何写库操作。
```

### 17.2 每周巡检输出模板

```markdown
## 中核每周巡检结果

### 1. 爬虫调用情况
- 发起调用数量：{{call_num}}

### 2. 价格爬取状态分布
| status | 含义 | 数量 |
|---|---|---:|
| 0 | 待爬取 | {{status_0}} |
| 1 | 爬取中 | {{status_1}} |
| 2 | 爬取成功 | {{status_2}} |
| 3 | 爬取失败 | {{status_3}} |
| 4 | 未爬取到价格 | {{status_4}} |

### 3. 本周数据变化
- 新增监控商品：{{new_commodity_count}}
- 新增同款商品：{{new_relation_count}}
- 下架同款商品：{{invalid_relation_count}}
- 增补同款商品：{{supplement_relation_count}}

### 4. 初步结论
{{summary}}

### 5. 建议动作
{{next_action}}
```

---

## 18. 接手前建议

在正式接手中核项目前，建议先完成以下准备：

1. 整理并固化上述 SQL 模板；
2. 做一个只读版中核运维 Agent；
3. 将 `tenant_id=188605`、`commodity_pool_id=222` 写成固定配置；
4. 默认禁止所有写库操作；
5. 建立周巡检报告模板；
6. 建立单商品排查模板；
7. 建立同款审核 Excel 输出模板；
8. 等拿到只读数据库权限后，再接入自动查询；
9. 等熟悉项目后，再考虑半自动写库，但必须保留人工确认。

---

## 19. 版本记录

| 版本 | 日期 | 说明 |
|---|---|---|
| v1.0 | 2026-06-15 | 纯中核版知识库，已排除宏伟及其他租户专项内容 |
