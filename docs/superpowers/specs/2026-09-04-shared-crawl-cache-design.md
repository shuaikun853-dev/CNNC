# 共享爬虫缓存与 URL 身份模型设计(Issue #7)

- 日期: 2026-09-04
- 状态: 已实现(纯数据层基础:存储/身份/查询/测试;未接入任何生产业务流)
- 关联: Issue #6(URL 身份模型)、Issue #7(Shared Crawl Cache and URL Identity Model)
- 新增文件: `app/services/crawl_url_identity.py`、`app/services/shared_crawl_cache.py`、
  `tests/test_shared_crawl_cache.py`、`tests/test_shared_crawl_cache_migration.py`

## 目标

为多次爬取同一商品 URL 的场景提供跨日的共享缓存:同一条 (mall, URL) 链路的成功
结果只爬一次、多次复用;历史 append-only 保留,`current` 行代表最新有效证据。

## 独立 DB 决策理由

STEP1 生产 store 是 `app/services/daily_audit_store.py` 的 `DailyAuditStore`,
库路径 `<runtime_root>/<business_date>/state.sqlite3`,每个业务日一个文件。
共享缓存必须跨日存活,放进 per-day 文件会随日期切换被天然隔离甚至归档清理,
因此缓存是独立 SQLite 文件(如 `<runtime_root>/shared_crawl_cache.sqlite3`,
由构造方传入路径)。连接/事务风格与 `daily_audit_store` 一致:连接不缓存、
`PRAGMA foreign_keys=ON`、`busy_timeout=5000`、WAL、写事务 `BEGIN IMMEDIATE`、
contextmanager `_transaction(immediate=True)` / `_reader()`。

## 三级 identity 解析(顺序固定)

1. Level1/Level2(EXACT): `crawl_urls` alias 表按 `(mall, url)` 精确命中
   (alias 表同时涵盖 exact URL 与已学习的历史链路 URL)。
2. Level3(MALL_PARSER): 商城隔离解析器把 URL 解析为 product_identity
   (v1 仅 `ofs`:`path` 以 `/product-<纯数字>.html` 结尾,大小写不敏感、允许尾斜杠),
   再按 `(mall, product_identity)` 查 `crawl_identities`。
3. 全 MISS ⇒ `None`,保守不命中。永不模糊匹配,永不把 SKU 当 identity 键。

`record_success` 写入时解析顺序 returned → canonical → submitted → original,
取第一个 parser 命中;全 MISS 且输入 URL 未注册过时新建 `product_identity=NULL`
的 identity;输入 URL 已注册则认领原 identity(同 URL 重复成功追加同一身份历史)。

## SKU evidence 三态

`CONFIRMED` / `URL_PARSED_UNVERIFIED` / `UNRESOLVED`,校验后原样落库:
UNRESOLVED 必须 无 real_sku_id;后两者必须提供 real_sku_id。URL 解析出的 SKU
只允许 `URL_PARSED_UNVERIFIED`,不存在任何把状态静默升级为 CONFIRMED 的 API。

## placeholder 防护

real_sku_id 若匹配 placeholder 前缀(默认 `zhpq-`,strip 后小写比较)或等于
crawl_id,一律 `ValueError` 拒绝落库——即使调用方声称 CONFIRMED。

## current 唯一性

`ux_crawl_results_current ON crawl_results(identity_id) WHERE is_current=1`
部分唯一索引硬约束每个 identity 至多一行 current。选举规则:新行
`(succeeded_at, id)` 字典序大于现有 current 行才接管,乱序迟到的历史不夺 current。

## alias fail-closed 冲突策略

`(mall, url)` 永久归属唯一 identity(`UNIQUE(mall, url)`)。写入时发现某 URL
已属于另一 identity ⇒ `AliasConflictError`,整个事务回滚、零残留(fail-closed)。
`document_references` 的 UNIQUE 冲突同理:identity 相同幂等,不同则冲突。

## migration 策略

全部 `CREATE TABLE IF NOT EXISTS` 幂等、纯新增;对 STEP1 的 `state.sqlite3`
零接触(独立文件,测试断言表集合与行数不变、`DailyAuditStore` 重开可用)。
缓存自身重开/表被删后重建均幂等:重跑 `record_success` 会重新注册 alias,
历史行永不丢失。

## 明确不做(本票范围外)

不接 producer、不加 scheduler、不做 Dashboard。以下业务编排均不在本票:
#9 客户链接实时发现+1分钟补扫、#10 招商局预爬+priority=-1、
#11 史泰博本地脚本接入、#12 STEP3 cache-first+zhImport Excel 标色、
#13 Dashboard 预爬页+手动重爬。
