# 中核推送报文 SQL 排查：快速上下文

> 这是后续 AI 和开发者的首选读取入口。  
> 完整历史规范仍保存在 `E:\.A中核运维\20260727-20260801\周二\中核推送报文SQL排查经验.md`。  
> 默认只读取本文件；需要 SQL 模板或追溯证据时，再按主题读取完整规范或 `EVIDENCE_LOG.md`，不要每次全文加载。

## 固定范围

- 数据库：MySQL。
- 租户：`tenant_id = 188605`。
- 商品池：`commodity_pool_id = 222`。
- 长整型 `document_id`、`sku_id`、`commodity_id` 必须作为字符串处理并加单引号。

## 核心表与字段

### `saas_onecode_price.commodity_pool_188605`

- `ID`：商品池记录主键。
- `MY_COMMODITY_ID`：客户商品编码。
- `COMMODITY_ID`：文档 ID。
- `COMMODITY_NAME`：商品名称。

### `onecode_base3.task_execute`

- 任务字段：`ID`、`COMMODITY_POOL_ID`、`TENANT_ID`、`TYPE`、`STATE`、`FLOW_ID`、`REFERENCE_ID`。
- 执行字段：`CNT_RETRY`、`MAX_CNT_RETRY`、`TIME_INTERVAL`、`NEXT_TIME`、`LAST_RUN`。
- 时间字段必须使用 `CREATED_TIME`、`UPDATED_TIME`。

### `saas_onecode_price.saas_sync_record`

- 主要字段：`id`、`sync_type`、`tenant_id`、`serialNo`、`req`、`res`、`is_success`、`error_msg`、`sku_id`、`task_id`。
- 时间字段使用 `create_time`、`update_time`。
- 已确认关联：`saas_sync_record.task_id = task_execute.ID`。

## 已确认关联

- 对 2026-01-06 晚间以后带 `task_id` 的正常同步记录：
  - `task_execute.REFERENCE_ID = commodity_pool_188605.ID` 是主关联。
  - `REFERENCE_PRICE_SYNC` 有 2 条已知例外，任务 ID 为 `53998125`、`53998123`。
- 2026-01-06 14:34:17 以前的历史同步记录普遍没有 `task_id`：
  - `REFERENCE_PRICE_SYNC`：1,475,520 条。
  - `ZH_GOVEN_RESULT_SYNC`：161,329 条。
- 因此，历史记录 `task_id IS NULL` 不能解释为“没有生成任务”，也不能通过任务表判断是否执行。

## 两类推送

### `REFERENCE_PRICE_SYNC`

- 业务字段：`monitorStatus`。
- 参数配置截图中存在 `SAAS_PRICE_SYNC_URL_188605`，说明为“指导价推送地址配置”，配置值显示为中核商城 `.../dataExchange/commodity/` 路径前缀。
- 用户补充的完整指导价接口为 `https://www.cnncmall.com/emp/onecode/improxy/receive/iio/dataExchange/commodity/sassGuidePriceApply/187453`。因此此前查询结果中 `sync_type=REFERENCE_PRICE_SYNC` 的记录属于该接口的业务推送报文；同时查出的 `ZH_GOVEN_RESULT_SYNC` 不属于该接口。
- `saas_sync_record` 本身未确认保存 URL，因此上述关系能确认报文业务归属，不能单凭同步记录证明某一次请求实际命中的完整 URL；逐次 URL 证据仍以完整运行配置或网关日志为准。
- 实际观察值：`1`、`2`，另有 1 条空值异常。
- 当前业务理解：
  - `1`：价格监测成功。
  - `2`：价格监测失败或没有有效价格结果。
- `monitorStatus=2` 不等于接口调用失败。

### `ZH_GOVEN_RESULT_SYNC`

- `task_execute.TYPE`：`ZH_GOVEN_RESULT_SYNC_URL`。
- `saas_sync_record.sync_type`：`ZH_GOVEN_RESULT_SYNC`。
- 参数配置截图中存在 `ZH_GOVEN_RESULT_SYNC_URL_188605`，说明为“监控商品对应标品推送地址配置”，配置值显示为中核商城 `.../dataExchange/commodity/` 路径前缀。
- 业务字段：`verificationResult`、`verificationRemark`。
- 当前数据只观察到 `verificationResult=1 / 校验通过`。
- 截图未完整展示 URL 后缀，不能仅凭截图确认完整地址是否为 `sassCommodityCheckResultApply/187453`；完整配置值或网关日志仍需补证。

## 状态与接口判断

- 完整时间线：`ORDER BY create_time ASC, id ASC`。
- 最新状态：按 `sku_id + sync_type` 分组，使用 `ORDER BY create_time DESC, id DESC`。
- 客户接口入口成功证据：
  - `is_success = 1`；
  - `respCode = 0000`；
  - `rspDesc = success`；
  - 请求、响应和记录字段的 `serialNo` 一致。
- 上述证据不能证明客户业务表落库、页面展示、统计更新或异步消费成功。

## 已观察到的状态变化

- `1 -> 2`：119 次，119 次返回 `0000`。
- `2 -> 2`：110 次，其中 105 次返回 `0000`。
- `无前置状态 -> 2`：2,190 次，其中 2,186 次返回 `0000`。
- 最新状态仍为 `2`：526 个商品。
  - 历史同时出现过 `1` 和 `2`：4 个。
  - 历史只有 `2`：522 个。
- 历史出现过 `2`、最新状态为 `1`：1,783 个商品。

这些数据不能证明客户只对 `1 -> 2` 增加失败数量，但可作为客户侧规则排查线索。

## 尚未确认

- `monitorStatus`、`verificationResult` 的官方完整枚举。
- `task_execute.STATE` 各值的官方定义。
- `LAST_RUN`、`CNT_RETRY` 的完整状态机语义。
- 手动推送与自动推送的可靠区分字段；当前 `FLOW_ID` 全部为 `NULL`。
- 客户收到 `0000` 后的落库、异步消费、幂等、过滤和失败数量统计规则。
- 两条 `REFERENCE_ID` 例外记录的具体原因。

## 当前排查案例

- 商品：`692769252154302464`。
- 最新价格推送：2026-07-29 10:13:40，流水号 `150330273907113723`。
- 我方任务、请求、任务关联和三个流水号正常，客户返回 `0000 success`。
- 该商品三次推送的 `monitorStatus=1`、指导价和渠道价格相同，客户可能按 SKU、状态或价格结果去重。
- 当前查询未发现该商品的 `ZH_GOVEN_RESULT_SYNC` 记录；是否构成客户处理前置条件尚未确认。
- 详细证据见 `EVIDENCE_LOG.md` 对应案例。

### 五个客户反馈商品

- `687324288284909568`、`687674773336514560`、
  `687674825354272768`、`689196703512289280`：
  - 最新价格推送均为 `monitorStatus=1`；
  - 均有 `verificationResult=1 / 校验通过`；
  - 最新价格请求均包含指导价和两个渠道价格。
- `691765053350105088`：
  - 只查到两次 `REFERENCE_PRICE_SYNC`，最新仍为 `monitorStatus=2`；
  - 两次请求均无指导价和渠道价格；
  - 当前结果未发现 `ZH_GOVEN_RESULT_SYNC`。
- 五个商品共 18 条同步记录均为 `is_success=1`、客户返回
  `0000 success`，记录、请求、响应流水号一致。
- 因此五条均无客户接口调用失败证据，但
  `691765053350105088` 尚无价格检测成功报文，不能与前四条作相同结论。
- 详细时间、流水号和价格证据见 `EVIDENCE_LOG.md`。

## 强制排查规则

1. 先区分业务状态失败与接口调用失败。
2. 再区分接口入口成功与客户业务落库成功。
3. 不用历史任意记录代替最新状态。
4. 两类接口分别判断，不混用状态字段。
5. 查询为空时依次放宽时间、`TYPE`、`REFERENCE_ID`、租户、商品池和库表条件。
6. 报文优先使用 `JSON_VALID`、`JSON_EXTRACT`、`JSON_UNQUOTE`。
7. 字段不确定时先查询 `information_schema.COLUMNS`，不得虚构。
8. 默认只生成 `SELECT`，未经明确要求不执行写操作。

## SQL 性能规则

- 按文档 ID 查询实际推送报文时，优先直接使用
  `saas_sync_record.tenant_id = 188605 AND sku_id IN (...)`。
- 不要在 `sku_id`、`task_id` 等过滤或关联字段上使用 `CAST`，否则可能无法使用索引。
- “实际报文查询”和“无同步记录时的任务补查”分两步执行；不要默认把商品池、同步表、任务表和反向 `NOT EXISTS` 全部塞进一条查询。

## 文档维护规则

1. 每次 SQL 结果带来新事实，先更新本文件和 `EVIDENCE_LOG.md`，再输出结论。
2. 本文件只保留当前有效的短上下文，不堆放原始结果。
3. 原始数量、时间范围、异常 ID 和推断依据写入 `EVIDENCE_LOG.md`。
4. 被新证据推翻的结论应直接修正，并在证据日志记录变更，不并列保留冲突结论。
5. 后续会话默认读取本文件；只有需要具体 SQL 模板或证据追溯时才读取对应章节。
