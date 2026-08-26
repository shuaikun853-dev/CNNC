# SQL_OPS_KB.md

> AI 入口文件。凡是处理中核项目的 SQL 排查、SaaS 数据未收口、首次采集/同款/分控/回调/模型结果问题，**先读 `docs/sql_ops/README.md`**，再按索引读取对应 case。

## 固定约定

- SQL 知识专栏：`docs/sql_ops/`
- 长期可复用知识：`docs/sql_ops/README.md`
- 历史真实案例：`docs/sql_ops/cases/`
- 新 SQL 排查完成后：把新的表关系、字段语义、有效 SELECT、错误 JOIN/误区沉淀到该目录。
- 文档主要写给 AI，不追求面向业务人员的叙述性；优先保存可验证事实、JOIN 关系、状态语义、决策树和最短排查路径。
- 数量/日期属于案例事实，不能当永久规则；未来排查必须重跑基础分类。
- Agent 仍受 `AGENTS.md` 约束：默认不得直接执行生产 SQL。本专栏保存只读 SELECT 模板，通常由用户手工运行并返回结果。

当前案例：

- `docs/sql_ops/cases/2026-08-26_FIRST_CRAWL_MISSING_SAAS.md`
