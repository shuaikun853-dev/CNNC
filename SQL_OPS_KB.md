# SQL_OPS_KB.md

> AI 入口文件。凡是处理中核项目的 SQL 排查、SaaS 数据未收口、首次采集/同款/分控/回调/模型结果问题，**先读 `docs/sql_ops/README.md`**，再按索引读取对应 case。

## 固定约定

- SQL 知识专栏：`docs/sql_ops/`
- 长期可复用知识：`docs/sql_ops/README.md`
- 已验证 schema：`docs/sql_ops/SCHEMA_VERIFIED.md`
- 首次流程快速排查链：`docs/sql_ops/CRAWL_STATUS_DIAGNOSTIC.md`
- 历史真实案例：`docs/sql_ops/cases/`
- 新 SQL 排查完成后：把新的表关系、字段语义、有效 SELECT、错误 JOIN/误区沉淀到该目录。
- 文档主要写给 AI，不追求面向业务人员的叙述性；优先保存可验证事实、JOIN 关系、状态语义、决策树和最短排查路径。
- 数量/日期属于案例事实，不能当永久规则；未来排查必须重跑基础分类。
- **任何知识必须有来源**：当前代码/前端、SaaS 运维手册、开发明确确认、生产 `SHOW COLUMNS`、生产只读 SQL、明确的运营人工操作确认。禁止凭字段名和经验猜。
- 来源冲突时必须显式记录冲突，并优先当前系统代码/前端 + 当前生产 schema/live SQL + 开发明确确认。
- Agent 仍受 `AGENTS.md` 约束：默认不得直接执行生产 SQL。本专栏保存只读 SELECT/SHOW 模板，通常由用户手工运行并返回结果。

## 当前推荐读取顺序

1. `docs/sql_ops/README.md` — 当前稳定语义、来源规则、排查决策树、已证伪误区。
2. `docs/sql_ops/CRAWL_STATUS_DIAGNOSTIC.md` — **SaaS 页面/首次流程排查优先读取。** 开发已确认：`IS_SYNC_STANDARD=0` 只代表整体未收口；先看 `commodity_list.crawl_status`，`1` 走 task/exact batch/中控/callback，`2` 直接转 search/model/审批链。该规则已由 2026-08-27 生产只读 SQL 交叉验证。
3. `docs/sql_ops/SCHEMA_VERIFIED.md` — 直接 schema/字段证据。
4. `docs/sql_ops/cases/2026-08-27_FIRST_CRAWL_SAME_AUDIT_EVIDENCE.md` — 截至 2026-08-27 13:36 的最新完整证据链。
5. 需要回看分控队列中途状态时，再读 `2026-08-27_NODE_10000000003_QUEUE_BACKLOG.md`；注意该文件是中途快照，后续已证明 comix/node 并非全局不可用。
6. 需要回看 8/26 早期“task 已建但分控尚无 exact batch”阶段时，读 `2026-08-26_FIRST_CRAWL_MISSING_SAAS.md`；其中早期对 `sync_state` 的“后处理消费”解释已被当前前端映射修正。

## 案例索引

- `docs/sql_ops/cases/2026-08-26_FIRST_CRAWL_MISSING_SAAS.md`
- `docs/sql_ops/cases/2026-08-27_NODE_10000000003_QUEUE_BACKLOG.md`
- `docs/sql_ops/cases/2026-08-27_FIRST_CRAWL_SAME_AUDIT_EVIDENCE.md`
