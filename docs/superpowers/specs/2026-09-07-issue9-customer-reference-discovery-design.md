# 客户参考链接发现与既有结果复用(Issue #9)设计

- 日期: 2026-09-07
- 基线: `feat/issue-7-shared-crawl-cache` @ `32457a4`(stacked;生产权威 `prod/closeout-20260818` @ `2bc7cc6`)
- 关联: Issue #6(父规格)、#7(Shared Crawl Cache,已 3 轮 review PASS)、#9(本票)
- 状态: 已实现(旁路发现/复用/每日同步;招商局建任务属 #10,史泰博属 #11,STEP3 消费属 #12,Dashboard 属 #13)

## 目标与边界

客户数据一出现就把 documentId/fourCategoryId 纳入旁路预爬准备;事件触发立即入队
+ 每 1 分钟轻量补扫兜底;对每条客户 referenceLink 做商城路由后查 Shared Crawl Cache,
命中即复用(附 document 溯源),MISS 只落 `PENDING_PREWARM` 候选留给 #10/#11。
正常每日互联网爬取(STEP1 zhaoshang 链路)的成功行自动同步进 Shared Crawl Cache,
同步幂等且失败绝不影响原流程。本票**不**创建招商局任务、不跑史泰博、不做 STEP3
cache-first、不动 Dashboard。

## 架构总览

```
STEP1 tick(app/workflows/incremental_price_audit.py,ingest_snapshot 之后)
  └─ deps.prewarm_intake(business_date, rows)   ← 新增 optional DI seam,try/except 全包裹
        │  仅写 customer_prewarm.sqlite3(独立旁路 DB),失败只计数不抛出
        ▼
customer_prewarm.sqlite3(durable discovery state,跨日)
  prewarm_documents / prewarm_candidates / prewarm_links / prewarm_sync_ledger
        ▲                                    ▲
  scripts/customer_prewarm_scan.py(每 1 分钟,IgnoreNew + ProcessLock 防重入)
  ├─ Phase A daily sync: 只读当日 state.sqlite3 的 crawl_work SUCCESS 行
  │    → SharedCrawlCache.record_success(source='daily-internet',幂等 ledger)
  ├─ Phase B catch-up intake: 只读 state.sqlite3 audit_items,补漏入队 documents
  ├─ Phase C detail 扫描: 全局 detail.lock 下 read-only detail → extract links
  │    → 商城路由 → upsert candidates → lookup_current
  │         CACHE_HIT → 状态+溯源(attach_document_reference),不新建爬取
  │         CACHE_MISS → PENDING_PREWARM(唯一候选,#10 的稳定输入)
  └─ Phase D refresh: 对 PENDING_PREWARM 候选重查缓存(吸收 Phase A 新增)
```

## 关键复用(不重造)

| 能力 | 复用点 |
|---|---|
| referenceLink 提取 | `app/services/customer_reference_links.py` 新纯函数,语义=STEP3 `process_monitor_no_push_zhaoshang.py:365-377`(`URL_RE` 同源、referenceInfos 先、顶层 referenceLink 后、仅 http、按出现序去重);测试用同输入对照证明等价 |
| 商城识别 | `scripts/zhaoshang_mall_mapping.load_zhaoshang_mall_mappings`(in-repo 生产映射)+ host 匹配(`_host_matches` 语义:exact 或 subdomain endswith);映射列表可注入,测试用合成映射,不依赖未跟踪 xlsx |
| jslink rewrite | 唯一确定性规则 `www.jslink.com` → `b2b.nbdeli.com`(STEP3:402 字面规则);original_url 永不改写,另存 routing_url/lookup_url |
| 缓存查询 | `SharedCrawlCache.lookup_current(mall, url)`(#7 三级:exact/alias→mall parser);禁止 SKU-only |
| 溯源 | `SharedCrawlCache.attach_document_reference`(unresolved 不落,返回 None 可容忍) |
| detail 调用 | `scripts/audit_saas_excel_file.call_detail_api` + `load_curl_command(data/detail_request.curl)` + `_apply_cnnc_at` + `shared_cnnc_provider()`,全局 `detail.lock` 串行,`internal_direct_opener` 直连 |
| 成功语义 | 每日同步的行级成功 = `crawl_work.state='SUCCESS'` 且 `result_json.page_price_status=='OK'`(`read_zhaoshang_result_xlsx_by_identity` 既有分类,price>0);无互相冲突的第二定义 |
| 防重入 | `ProcessLock`(msvcrt 非阻塞)+ 计划任务 `MultipleInstances IgnoreNew`(既有约定) |

## Durable discovery state(customer_prewarm.sqlite3)

独立跨日 SQLite,连接/事务约定与 #7/DailyAuditStore 相同(连接不缓存、
foreign_keys=ON、busy_timeout=5000、WAL、写事务 BEGIN IMMEDIATE、短事务)。

- `prewarm_documents`:UNIQUE(document_id);business_date(首见日)、four_category_id、
  source(event|scanner)、first/last_seen、detail_state(PENDING|DONE|FAILED)、
  detail_attempts、last_error、last_checked_at。category 变化→重置 PENDING(attempts 归零)。
- `prewarm_candidates`:UNIQUE(mall, original_url);lookup_url、routing_url(NULL=无改写)、
  cache_status(PENDING_LOOKUP|CACHE_HIT|PENDING_PREWARM)、match_level、resolved_identity_id、
  last_error/last_checked_at、first/last_seen、business_date、source。
  状态迁移:任意→CACHE_HIT 允许(历史 append-only,命中永不回退);
  PENDING_LOOKUP→PENDING_PREWARM;`list_unique_cache_misses()`=#10 稳定 seam。
- `prewarm_links`:UNIQUE(document_id, original_url);mall、routing_url、candidate_id FK、
  source、first/last_seen。多 document 同 URL→各建 link,共享同一 candidate(Case 5)。
- `prewarm_sync_ledger`:UNIQUE(source, source_event_key);cache_result_id(NULL=确定性
  失败或不完整写入)、error、created_at。幂等账本。

## 幂等键与每日同步(§十二)

- producer event key = `daily-internet:{business_date}:{task_name}:{crawl_work_id}:{sha256(result_json)[:12]}`。
  全部来自稳定生产标识(business_date、remote_jobs.task_name、crawl_work 自增 id、
  结果内容指纹);重复 harvest/重启/重扫→同 key→跳过;真实新导出(内容变化)→新 key→新历史。
- 写序:先 ledger claim(cache_result_id NULL)→ record_success → 回填 id。
  崩溃恢复:claim 行无 id 时先 `instr(crawl_results.payload_json, event_key)` 自检
  (event_key 恒嵌入 payload),命中则补链,未命中且 claim 过期才允许重记——单实例
  scanner + IgnoreNew 下重复窗口仅剩进程崩溃瞬间,且自检兜底。
- `succeeded_at` = crawl_work.updated_at(DailyAuditStore `_utcnow()` ISO-8601 含 UTC
  偏移,#7 `_utc_timestamp` 直接接受;比同步时刻的墙钟更能反映真实成功时序)。
- SKU 口径:sku_id 来自 saas 请求行并由 harvest 身份校验(sku,url)回读确认→
  `CONFIRMED`(爬虫取得);无 sku→`UNRESOLVED`(real_sku_id 留空);zhpq-* 占位符
  由 #7 防护拒绝。title/comment 在 app 侧行级结果中不存在→留空,完整 result_json 入 payload。
- 确定性失败(ValueError/AliasConflictError)→ ledger 记 error、不重试;
  瞬时失败(sqlite busy)→ 不记 ledger、下轮重试。
- 同步失败不影响 STEP1:同步完全在 scanner 进程内,STEP1 流程零感知(§十三)。

## STEP1 隔离(§五,最高门禁)

- 唯一改动:`TickDependencies.prewarm_intake: Callable[[str, Sequence[Mapping]], None] | None = None`
  + run_tick 在 `ingest_snapshot` 成功后 try/except 调用;异常只累计
  `counters["prewarm_intake_failures"]`,绝不 raise、绝不动 outcome/trace/审批/收口/价格/模型。
  hook 为 None(默认)时逐字节等价于现状——既有测试零感知。
- 生产装配:`build_default_dependencies`(paths 非 None 时)构造 intake worker,
  store 路径 = `paths.run_dir.parent / "customer_prewarm.sqlite3"`(不扩展 RuntimePaths)。
- intake 只做 SQLite upsert(自身短事务),不同步 detail/缓存/招商局/网络。

## 扫描器(scripts/customer_prewarm_scan.py)

- 每 1 分钟一次、无窗口、不长驻:`ProcessLock(<runtime_root>/customer_prewarm.lock)`
  非阻塞,冲突→打印 `SKIPPED_ALREADY_RUNNING` exit 0;预算默认 50s(<60s 间隔)。
- Phase 顺序 A→B→C→D(A 先于 D,当日新同步当天可被 refresh 吸收);每 Phase 独立
  try/except,单条失败记录后继续(Case 8),bounded(detail_max_attempts 默认 5 → FAILED)。
- detail 只读、走全局 `detail.lock`(非阻塞拿不到则本轮跳过 detail,下一分钟再来)。
- 不注册生产计划任务;`scripts/register_customer_prewarm_scan_tasks.ps1` 仅提供
  installer(PT1M、IgnoreNew、Hidden、Limited,模式复制既有 register 脚本),由用户后续授权执行。

## Dry-run(§十五)

`--dry-run`:prewarm store 写入 `--sandbox-root`(明确测试目录);shared cache 只读
(`--cache-path`,默认 sandbox 新库)——lookup 照常、`record_success` 只计数
`would_record` 不写;detail 真实只读调用(验证发现量);禁招商局/史泰博/SaaS write/
审批/推送。输出统计:发现 document 数、reference URL 数、商城分布、历史/daily 命中数、
MISS 数、重复 document→URL 数、detail 失败数。

## 明确不做(本票)

招商局 create/priority=-1/任务复用(#10)、史泰博(#11)、STEP3 cache-first/zhImport(#12)、
Dashboard/手动重爬(#13)、STEP2 三表(#8)。#9 到 `PENDING_PREWARM` 为止。

## 实现注记(独立验收后)

- 提取器对畸形 `referenceInfos` 项按项跳过(永不抛出);STEP3 原块遇此项整篇失败。
  该偏差在 `customer_reference_links.py` 模块注释与等价性测试中双记录。
- CLI detail fetcher 的 401 单次刷新触发条件仅看 HTTP 401(比 `_detail_worker` 的
  `_auth_rejected`(payload 认证失败或 401)窄);payload 级认证失败交给下一分钟
  重试与 AT 自然轮换自愈。`provider.refresh` 失败上抛由 process_document 计为
  detail 失败(sidecar 降级,不 fail-fast)。
- dry-run 默认不指向生产共享缓存;若操作者显式 `--cache-path` 生产库,
  `SharedCrawlCache` 构造仍会执行幂等 `CREATE TABLE IF NOT EXISTS`(无数据变更)。
- `prewarm_candidates` 同 mall 场景下 lookup_url 不随路由规则变化刷新;跨 mall
  变化在未 CACHE_HIT 时保守复用并更新(见 `upsert_link`)。
- 事件/补扫的 document 集合 = STEP1 已观察并持久化的 joined 行
  (`audit_items.row_json`);当前生产管线不持久化未匹配 saas 的纯客户文档。
- detail lock 拿不到时单条计一次 attempt("detail lock busy");5 次/分钟窗口内
  持续被 STEP1 detail 阶段占锁的极端场景会把文档推入 FAILED,直至 category 变化
  重置——生产 detail 阶段为短突发,风险接受并在此记录。
