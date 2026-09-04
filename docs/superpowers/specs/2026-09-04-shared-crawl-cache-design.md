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
   (v1 仅 `ofs`:hostname 必须是 `ofs.cn`、`www.ofs.cn` 或任意真实子域
   `*.ofs.cn`(endswith 允许策略),`path` 以 `/product-<纯数字>.html` 结尾,
   大小写不敏感、允许尾斜杠;identity 只取自 path,query/fragment 伪装一律
   MISS;其他域名含 `evilofs.cn`、`ofs.cn.evil.com` 一律 None),
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
部分唯一索引硬约束每个 identity 至多一行 current。选举规则:succeeded_at
归一化为 UTC 并保留微秒精度,新行 `(succeeded_at, id)` 字典序大于现有 current
行才接管(微秒即分出先后,id 仅在完全相等时 tie-break),乱序迟到的历史不夺
current。

## alias fail-closed 冲突策略(Rule A/B/C 证据决策表)

写入时对链路去重后的四个 URL(original/submitted/returned/canonical)**分别**
执行 mall parser,收集非空解析结果的去重集合 `parsed_product_identities`;
再收集已有 identity 证据(各 URL 在 `crawl_urls` 的 `(mall, url)` 归属 ∪
`(mall, product_identity)` 既有行):

- **Rule A**(|parsed| ≥ 2):链路自相矛盾,在任何写入前抛
  `AliasConflictError`——即使这些 identity 库里从未出现过。
- **Rule B**(|parsed| = 1,记 P):按证据集合决策——0 个 → 新建携带 P 的
  identity;恰 1 个 → 复用,且该 identity 自身 product_identity 为 NULL 时
  在本事务内安全 UPDATE 为 P(唯一索引 `ux_crawl_identities_mall_product`
  是并发后盾,IntegrityError 以 AliasConflictError 语义上抛),已携带其他值
  或证据 ≥2 个不同 identity → `AliasConflictError`,绝不合并/覆写已有组;
- **Rule C**(|parsed| = 0):仅 alias 证据——0 个 → 新建 NULL identity,
  1 个 → 复用(脏 submitted URL 经已知 returned/canonical 归并),≥2 → 冲突。

`(mall, url)` 仍永久归属唯一 identity(`UNIQUE(mall, url)`);
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
