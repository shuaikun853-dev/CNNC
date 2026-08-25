# 中核 Agent 架构重构设计方案

## 1. 为什么需要重构

### 1.1 当前能力边界

第一阶段 Agent 是"问答式 SQL 诊断助手"：

```
用户提问 → 识别场景 → 查知识库/给SQL模板 → 返回诊断建议
```

能做：识别 6 种诊断场景、生成安全 SQL 模板、返回知识片段和解读提示、校验参数渲染 SQL

不能做：处理批量数据(Excel/CSV)、执行业务规则判断、生成处理清单/异常清单/导入模板、输出客户反馈话术、按工作流程组织操作步骤

### 1.2 业务真实需求

中核价格指数运营有 8 项核心工作，每项有明确的输入、规则、输出、截止时间。Agent 应辅助**完成工作**，而非只回答"应该怎么做"。

| 维度 | 当前 | 目标 |
|---|---|---|
| 输入 | 自然语言问题 | 问题 + Excel/CSV/表格 + 编码 + 链接 |
| 处理 | 场景识别→SQL模板 | 任务识别→workflow→规则引擎→处理清单 |
| 输出 | SQL模板+知识片段 | 处理明细+异常清单+导入模板+客户话术 |
| 动作 | 无（只读） | 建议动作(APPROVE/REJECT/WAIT等)，停在人工确认 |

## 2. 重构目标和非目标

### 2.1 目标

- 从"问答式 SQL 诊断助手"升级为"中核价格指数运营工作处理 Agent"
- 新增 workflow 层处理 8 项核心运营工作
- 支持批量数据输入(Excel/CSV)和结构化输出
- 支持业务规则引擎，输出建议动作和人工操作清单
- 生成 SARS 导入模板、客户反馈话术、异常清单
- 保留第一阶段 /diagnose 和 /health 不受影响

### 2.2 非目标

- 不推倒重写
- 不修改第一阶段已通过的 P0/P1 安全逻辑
- 不接真实数据库（当前阶段）
- 不接模型（当前阶段）
- 不新增任何写库能力
- 不设计自动推送、自动解绑、自动驳回、自动重算
- 不允许 raw_sql
- 不允许模型生成 SQL
- 不处理非中核项目
- 不跨租户处理

## 3. 不推倒重写原则

### 3.1 核心原则

第一阶段安全底座(scope_guard、safety_guard、sql_selector、knowledge_loader)经 Codex P0/P1 审查，244 测试覆盖。必须保留，重构在其上增加 workflow 层。

### 3.2 旁路新增、渐进重构

- 不在 R1/R2 阶段搬迁旧模块文件
- 新增模块通过适配层(wrapper/adapter)调用现有安全模块
- 旧模块保持原位置、原接口、原测试不变
- 等工作流稳定后再考虑目录迁移，迁移必须单独设计、单独测试、单独提交

### 3.3 所有动作都是建议

- APPROVE 不是自动审批
- REJECT 不是自动驳回
- READY_TO_PUSH 不是自动推送
- RECALCULATE_INDEX 不是自动重算
- UNBIND_WRONG_RELATIONS 不是自动解绑
- IMPORT_CUSTOMER_LINKS 不是自动导入生产系统
- 所有这些都只是建议动作，必须人工在系统中操作

## 4. 第一阶段保留模块

以下模块保持原位置不动，通过适配层调用：

| 模块 | 原位置 | 调用方式 |
|---|---|---|
| scope_guard | app/agent/router.py | workflow 通过 import 调用 check_scope() |
| safety_guard | app/agent/safety_guard.py | workflow 通过 import 调用 validate_sql() |
| sql_selector | app/agent/sql_selector.py | /diagnose 链路专用，workflow 不直接调用 |
| knowledge_loader | app/services/knowledge_loader.py | 仅供第一阶段 /diagnose 使用，接口和缓存保持不变 |
| planner | app/agent/planner.py | /diagnose 链路专用 |
| result_analyzer | app/agent/result_analyzer.py | /diagnose 链路专用 |
| YAML SQL模板 | app/templates/*.yaml | /diagnose 链路专用 |
| /diagnose | app/main.py | 保持不变 |
| /health | app/main.py | 保持不变 |

长期阶段可以考虑将旧模块从 app/agent 迁移到 app/core、app/diagnosis，但：
- 非当前阶段
- 单独实施
- 需完整回归测试
- 不能破坏第一阶段 P0/P1

## 5. 新增 workflow 层

| 模块 | 位置 | 职责 |
|---|---|---|
| workflow router | app/workflows/router.py | 任务识别和路由 |
| workflow base | app/workflows/base.py | WorkflowBase 抽象类 |
| work_01 | app/workflows/work_01_same_product_approval.py | 同款审批处理 |
| work_02 | app/workflows/work_02_price_index_feedback.py | 价格指数反馈处理 |
| work_03~08 | app/workflows/work_0x_*.py | 其余 6 项工作流占位 |
| human_approval_guard | app/workflows/human_approval_guard.py | 校验所有动作仅为人工建议，强制 executed=false |
| workflow_scope_guard | app/workflows/scope_guard.py | 校验请求及每条记录均为 CNNC/188605/222 |
| excel_reader | app/services/excel_reader.py | 读取 Excel/CSV/JSON |
| excel_writer | app/services/excel_writer.py | 导出 Excel/CSV/MD/TXT |
| sars_template_builder | app/services/sars_template_builder.py | SARS 导入模板 |
| customer_reply_builder | app/services/customer_reply_builder.py | 客户反馈话术 |
| code_mapping_service | app/services/code_mapping_service.py | 编码映射 |
| audit_logger | app/services/audit_logger.py | 审计日志 |
| workflow_knowledge_adapter | app/services/workflow_knowledge_adapter.py | 按白名单加载拆分后的 workflow 知识文件 |
| workflow schemas | app/schemas/workflow.py | Workflow Pydantic 模型 |
| export schemas | app/schemas/export_result.py | 导出结果模型 |

## 6. 新旧架构对比

### 6.1 旧架构

```
main.py
  /health
  /diagnose
    scope_guard → planner → sql_selector → safety_guard
    knowledge_loader → result_analyzer
```

### 6.2 新架构

```
main.py
  /health                              ← 保留
  /diagnose                            ← 保留
  /diagnose/preview                    ← 保留
  /workflows/route                     ← 新增
  /workflows/same-product-approval/analyze  ← 新增
  /workflows/price-index-feedback/analyze   ← 新增
  /workflows/export                    ← 新增
```

### 6.3 关键区别

| 维度 | 旧 | 新 |
|---|---|---|
| 入口 | 仅 /diagnose | /diagnose + /workflows/* |
| 处理 | 场景→SQL模板 | 任务→workflow→规则引擎→处理清单 |
| 输入 | 问题+参数 | 问题+参数+Excel+编码+链接 |
| 输出 | SQL+知识片段 | 处理明细+异常清单+导入模板+客户话术 |
| 动作 | 无 | 建议动作枚举，停在人工确认 |
| 安全层 | scope+safety | 同上+human_approval_guard |

### 6.4 调用关系

```
/workflows/*
  → scope_guard.check_scope()         # 复用，中核范围校验
  → workflow_router.route()            # 新增，任务路由
  → workflow.analyze()                 # 新增，规则引擎
  → human_approval_guard.check()       # 新增，动作拦截
  → workflow_knowledge_adapter.search_workflow_knowledge() # 独立加载 workflow 知识

/diagnose（不受影响）
  → scope_guard → safety_guard → sql_selector → knowledge_loader → result_analyzer
```

## 7. 短期目标目录结构：旁路新增，不移动旧模块

**核心原则：R2-R6 阶段不移动任何现有模块，仅旁路新增。**

```
cnnc-ops-agent/
+-- app/
|   +-- main.py                          # FastAPI 入口（仅新增路由，不改旧路由）
|   +-- config.py                        # 配置
|   +-- agent/                           # 第一阶段模块，原位置不动
|   |   +-- router.py                    # scope_guard + route_question
|   |   +-- safety_guard.py              # SQL 安全校验
|   |   +-- sql_selector.py              # SQL 模板选择和参数渲染
|   |   +-- planner.py                   # 诊断规划
|   |   +-- result_analyzer.py           # 结果解读
|   +-- services/
|   |   +-- knowledge_loader.py          # 知识库加载（原位置不动）
|   +-- workflows/                       # [NEW] 工作流层
|   |   +-- __init__.py
|   |   +-- router.py                    # 任务路由
|   |   +-- base.py                      # WorkflowBase 抽象类
|   |   +-- work_01_same_product_approval.py
|   |   +-- work_02_price_index_feedback.py
|   +-- schemas/                         # [NEW] 数据模型
|   |   +-- __init__.py
|   |   +-- workflow.py                  # Workflow Pydantic 模型
|   +-- templates/                       # YAML SQL 模板（原位置不动）
|   +-- db/
|       +-- mock_client.py               # 占位 mock（原位置不动）
+-- data/docs/                           # 知识库（拆分为多文件）
+-- tests/                               # 测试
+-- docs/                                # 设计文档
+-- outputs/                             # 导出输出
+-- requirements.txt
+-- .env.example
+-- README.md
```

**新增模块通过适配层调用现有安全模块：**

- workflow 调用 app.agent.router.check_scope() 做中核范围校验
- workflow 调用 app.agent.safety_guard.validate_sql() 做 SQL 安全校验
- workflow 调用 app.services.knowledge_loader 获取业务规则片段
- 不在 R2-R6 阶段搬迁任何文件

## 8. 长期目标目录结构：稳定后再考虑迁移

**非当前阶段。以下仅在 workflow 层稳定后、经完整回归测试后才可实施。**

迁移条件：
- workflow 层已通过 Codex 安全审查
- work_01、work_02 已端到端验证
- 244 回归测试全部通过
- 迁移单独设计、单独测试、单独提交

```
cnnc-ops-agent/
+-- app/
|   +-- main.py
|   +-- config.py
|   +-- core/                            # 从 app/agent/ 迁移
|   |   +-- scope_guard.py               # <-- 原 app/agent/router.py check_scope
|   |   +-- safety_guard.py              # <-- 原 app/agent/safety_guard.py
|   |   +-- execution_sql_policy.py      # 新增
|   |   +-- human_approval_guard.py      # 新增
|   +-- diagnosis/                       # 从 app/agent/ 迁移
|   |   +-- planner.py                   # <-- 原 app/agent/planner.py
|   |   +-- sql_selector.py              # <-- 原 app/agent/sql_selector.py
|   |   +-- result_analyzer.py           # <-- 原 app/agent/result_analyzer.py
|   |   +-- query_executor.py            # 新增
|   +-- workflows/                       # 已有
|   |   +-- router.py
|   |   +-- base.py
|   |   +-- work_01_same_product_approval.py
|   |   +-- work_02_price_index_feedback.py
|   |   +-- work_03_same_product_shortage.py
|   |   +-- work_04_failed_bus.py
|   |   +-- work_05_weekly_index_update.py
|   |   +-- work_06_index_fluctuation.py
|   |   +-- work_07_push_check.py
|   |   +-- work_08_ad_hoc.py
|   +-- knowledge/                       # 从 app/services/ 迁移
|   |   +-- loader.py                    # <-- 原 app/services/knowledge_loader.py
|   |   +-- retriever.py                 # 拆分
|   +-- services/
|   |   +-- excel_reader.py
|   |   +-- excel_writer.py
|   |   +-- sars_template_builder.py
|   |   +-- customer_reply_builder.py
|   |   +-- code_mapping_service.py
|   |   +-- audit_logger.py
|   +-- schemas/
|   |   +-- diagnose.py                  # <-- 原 main.py 模型
|   |   +-- workflow.py
|   |   +-- db_result.py
|   |   +-- export_result.py
|   +-- templates/
|   |   +-- sql/                         # <-- 原 templates/*.yaml
|   |   +-- exports/
|   +-- db/
|       +-- db_config.py
|       +-- readonly_client.py
|       +-- mock_client.py
+-- data/docs/
+-- tests/
+-- docs/
+-- outputs/
+-- requirements.txt
+-- .env.example
+-- README.md
```

**迁移必须：**
- 单独设计、单独测试、单独提交
- 不能破坏第一阶段 P0/P1
- 需完整回归测试（244 测试 + 新增测试全通过）
- 迁移不是当前优先事项

## 9. workflow 层设计

### 9.1 WorkflowBase

```
WorkflowBase (ABC):
  workflow_name: str           # work_01_same_product_approval
  display_name: str            # 新增监控商品同款审批处理
  required_inputs: list[str]   # 必需输入字段
  optional_inputs: list[str]   # 可选输入字段
  possible_actions: list[str]  # 可能输出的动作枚举

  analyze(inputs) -> WorkflowAnalyzeResponse
  get_knowledge_refs() -> list[str]
```

### 9.2 workflow router

路由规则（关键词 + 文件类型组合）：

| workflow | 触发关键词 | 文件线索 |
|---|---|---|
| work_01 | 同款审批、新增监控、同款匹配 | 含标准商品编码+同款链接的表格 |
| work_02 | 价格异常、勘误、反馈、价格偏高/偏低、客户反馈 | 含客户反馈+商品编码 |
| work_03 | 同款不足、补同款、无同款 | 同款统计表 |
| work_04 | 失败班车、处理失败 | 失败任务列表 |
| work_05 | 价格指数更新、周度计算 | 周度数据表 |
| work_06 | 波动、正负10%、异常波动 | 波动数据 |
| work_07 | 推送、推送失败、推送后核查 | 推送结果表 |
| work_08 | 临时、导数据、分析 | 任意 |

router 同时返回 confidence 值，低于阈值时要求人工确认。

### 9.3 适配层调用方式

```
from app.agent.router import check_scope
from app.agent.safety_guard import validate_sql
from app.services.workflow_knowledge_adapter import search_workflow_knowledge
```

workflow 层不修改现有模块代码。自然语言问题继续调用 `check_scope()`；结构化记录必须先经过新增的 `workflow_scope_guard`，不能仅依赖问题文本判断范围。

每个 workflow 请求必须包含不可缺省的作用域上下文：

```text
project = "CNNC"
tenant_id = 188605
commodity_pool_id = 222
```

缺少上下文、上下文不匹配、同一批次混入其他租户记录时，整批返回 `OUT_OF_SCOPE`，不得进入规则引擎。`workflow_knowledge_adapter` 只服务拆分后的 workflow 知识文件，不改变第一阶段 `knowledge_loader` 的单文件加载和缓存行为。

## 10. human_approval_guard 设计

### 10.1 RECOMMENDATION_ONLY_ACTIONS

`WorkflowAction` 中的所有动作都只是建议标签，不是可执行命令。包括 `APPROVE`、`REJECT`、`READY_TO_PUSH`、`IMPORT_CUSTOMER_LINKS`、`UNBIND_WRONG_RELATIONS`、`RECALCULATE_INDEX`、`PUSH_INDEX`、`RETRY_PUSH` 等。

系统不设计动作执行器、人工确认后执行路径、生产系统客户端或写库接口。人工确认只表示人工已阅读建议，不会解锁任何系统动作。

### 10.2 检查逻辑

```
1. 动作必须属于 WorkflowAction 枚举，否则阻止输出
2. 所有动作强制 execution_allowed=false、executed=false
3. 涉及审批、驳回、解绑、导入、重算、推送、重推时，needs_human_approval=true
4. 输出文案必须明确包含"建议人工"，不得表述为已执行或将自动执行
5. guard 只校验建议输出，不调用任何外部系统
```

### 10.3 核心保证

- 所有动作默认 need_approval=True
- 当前不存在可自动执行动作，也不存在用于解锁执行的确认参数
- Agent 永远不调用数据库写入或系统操作接口
- 涉及解绑、驳回、立即推送、重新计算价格指数、导入生产系统、重推任务等动作，只输出人工操作建议

## 11. 第 1 项 workflow：商城新增监控商品同款审批处理和价格指数同步

### 11.1 基本信息
- workflow_name: work_01_same_product_approval
- display_name: 中核商城新增监控商品同款审批处理和价格指数同步
- 周期: 每日，日清

### 11.2 输入模型 SameProductRecord

`SameProductRecord` 表示一个待处理商品，`SameProductCandidate` 表示该商品的一条同款候选。规则引擎必须根据候选明细自行计算有效同款数、有效商城数、最高价、最低价和价差，不接受调用方直接提供这些汇总值。

**SameProductRecord**

| 字段 | 类型 | 含义 |
|---|---|---|
| scope | WorkflowScope | 记录所属范围，必须为 CNNC/188605/222 并与请求 scope 一致 |
| standard_commodity_code | str | 标准商品编码 |
| variant_code | str="" | 潜在变异编码 |
| sars_monitor_code | str="" | SARS 监控商品编码/Document ID |
| commodity_name | str="" | 商品名称 |
| supplier_product_name | str="" | 供应商推品名称 |
| mall_recommended_name | str="" | 商城推荐标准商品名称 |
| current_status | str="" | 同款匹配中/待审批/已审批/校验成功/校验失败 |
| reference_price | Decimal or None | 参考价（标准商品价格） |
| candidates | list[SameProductCandidate] | 同款候选明细 |

**SameProductCandidate**

| 字段 | 类型 | 含义 |
|---|---|---|
| link | str | 参考链接或互联网采集链接 |
| mall_name | str | 商城名称 |
| crawl_price | Decimal or None | 采集价格 |
| page_actual_price | Decimal or None | 页面实际价格 |
| comment_count | int or None | 评论数 |
| same_product_type | str | 参考链接/互联网采集 |
| is_link_valid | bool=True | 链接是否有效 |
| is_product_on_shelf | bool=True | 商品是否上架 |
| is_same_product | bool or None | 是否已确认同款；未知时不得计入有效同款 |

### 11.3 规则引擎（按顺序判断，命中第一条即输出）

**核心业务规则：至少 2 条及以上不同商城的有效同款数据，才能生成价格指数。**

| # | 条件 | 动作 | pushable | 原因 |
|---|---|---|---|---|
| 1 | 非中核范围数据 | REJECT | false | 不处理非中核范围数据 |
| 2 | current_status 为"同款匹配中"或"采集匹配中" | WAIT_CRAWLER | false | 同款仍在采集回流中，不能直接推送，否则可能进入校验失败 |
| 3 | candidates 为空 | SUPPLEMENT_SAME_PRODUCT | false | 无同款数据，无法生成价格指数 |
| 4 | 计算得到 valid_same_count = 0 | SUPPLEMENT_SAME_PRODUCT | false | 无有效同款，无法生成价格指数 |
| 5 | 计算得到 valid_same_count = 1 | SUPPLEMENT_SAME_PRODUCT | false | 仅 1 条同款，无法生成价格指数；附加建议 DO_NOT_PUSH |
| 6 | valid_same_count >= 2 但 valid_mall_count < 2 | MANUAL_CHECK | false | 同一商城重复链接，不满足不同商城要求，一物多码风险 |
| 7 | is_link_valid = False 或 is_product_on_shelf = False | REJECT | false | 链接失效/商品下架，仅输出建议人工驳回，不自动驳回 |
| 8 | crawl_price 与 page_actual_price 不一致 | MANUAL_CHECK | false | 采集价格与页面实际价格不一致，直接通过会导致价格指数错误 |
| 9 | 最高价 > reference_price x 1.3 | MANUAL_CHECK | false | 价格显著偏高，偏离参考价 |
| 10 | 最低价 < reference_price x 0.7 | MANUAL_CHECK | false | 价格显著偏低，偏离参考价 |
| 11 | 有效同款价差 > 30% | MANUAL_CHECK | false | 价差过大；可附加 FEEDBACK_CUSTOMER 建议 |
| 12 | 系统去极值异常（如剔除低价合理项、保留高价项） | MANUAL_CHECK | false | 去极值逻辑异常 |
| 13 | 当前状态为"校验失败" | MANUAL_CHECK | false | 校验失败需人工核查原因 |
| 14 | 所有条件满足：valid_same_count>=2、valid_mall_count>=2、链接有效、商品上架、采集价=页面价、价差<=30%、价格未偏离参考价 | APPROVE | true | 条件全部满足，建议审批通过；若流程上可推送，额外标记 ready_to_push=true |
| 15 | 其他 | MANUAL_CHECK | false | 默认人工核查 |

**特别说明：**
- `valid_same_count`、`valid_mall_count`、最高价、最低价和 `price_diff_pct` 均为规则引擎派生字段，不得由请求直接覆盖
- 有效候选必须同时满足：链接有效、商品上架、明确确认为同款、价格存在且大于 0
- 商城名称标准化后去重计算 `valid_mall_count`
- `price_diff_pct = (最高有效价 - 最低有效价) / 最低有效价 × 100%`；最低有效价为空或小于等于 0 时进入 MANUAL_CHECK
- 金额和百分比使用 Decimal，统一保留规则和舍入方式
- APPROVE 不是自动审批，仅表示"建议审批通过"，人工需在系统中操作
- REJECT 不是自动驳回，仅表示"建议人工驳回"
- READY_TO_PUSH 不作为单独主动作，对满足审批通过且流程上可推送的数据，在 APPROVE 基础上额外标记 ready_to_push=true
- ready_to_push 仅表示"可人工推送"，不是自动推送
- 规则 14 是最后才判断的审批通过条件，确保数据不会被轻易判为 APPROVE

### 11.4 输出动作枚举

| 动作 | 含义 | 是否自动执行 | 是否需要人工操作 | Agent 输出 |
|---|---|---|---|---|
| APPROVE | 建议审批通过 | 否 | 是 | 审批依据、风险提示 |
| REJECT | 建议驳回 | 否 | 是 | 驳回原因、异常链接 |
| WAIT_CRAWLER | 等待采集回流 | 否 | 否 | 等待原因、建议复查时间 |
| SUPPLEMENT_SAME_PRODUCT | 需补同款 | 否 | 是 | 缺口说明、补同款建议 |
| DO_NOT_PUSH | 标记不可推送 | 否 | 否 | 不可推送原因 |
| MANUAL_CHECK | 人工核查 | 否 | 是 | 核查原因和证据 |
| FEEDBACK_CUSTOMER | 反馈客户重新提供 | 否 | 是 | 客户反馈话术 |
| READY_TO_PUSH | 附加标记（非单独动作），在 APPROVE 基础上标记 ready_to_push=true | 否 | 是 | 推送前核查清单 |

### 11.5 输出文件

**1. same_product_approval_result.xlsx**

| 字段 | 含义 |
|---|---|
| standard_commodity_code | 标准商品编码 |
| variant_code | 潜在变异编码 |
| sars_monitor_code | SARS 监控商品编码/Document ID |
| commodity_name | 商品名称 |
| current_status | 当前状态 |
| same_product_type | 同款类型 |
| mall_name | 商城名称 |
| link | 链接 |
| crawl_price | 采集价格 |
| page_actual_price | 页面实际价格 |
| reference_price | 参考价 |
| valid_same_count | 有效同款数 |
| valid_mall_count | 有效商城数 |
| price_diff_pct | 价差比例 |
| action | 建议动作（primary_action） |
| pushable | 是否可人工推送 |
| ready_to_push | 可人工推送标记（仅 APPROVE 且流程上可推送时为 true） |
| reason | 判断原因 |
| risk_notice | 风险提示 |

**2. same_product_exception_list.xlsx**

| 字段 | 含义 |
|---|---|
| standard_commodity_code | 标准商品编码 |
| commodity_name | 商品名称 |
| exception_type | 异常类型 |
| exception_link | 异常链接 |
| exception_price | 异常价格 |
| exception_reason | 异常原因 |
| suggested_action | 建议处理 |

**3. same_product_ready_to_push.xlsx**

| 字段 | 含义 |
|---|---|
| standard_commodity_code | 标准商品编码 |
| commodity_name | 商品名称 |
| valid_same_count | 有效同款数 |
| valid_mall_count | 有效商城数 |
| min_price | 最低价 |
| max_price | 最高价 |
| avg_price | 平均价 |
| ready_to_push | 是否建议人工推送 |
| pre_push_checklist | 推送前检查项 |

**4. same_product_wait_crawler.xlsx**

| 字段 | 含义 |
|---|---|
| standard_commodity_code | 标准商品编码 |
| commodity_name | 商品名称 |
| current_status | 当前状态 |
| wait_reason | 等待原因 |
| suggested_recheck_time | 建议复查时间 |
| need_crawler_check | 是否需要排查爬虫 |

**5. same_product_manual_check.xlsx**

| 字段 | 含义 |
|---|---|
| standard_commodity_code | 标准商品编码 |
| commodity_name | 商品名称 |
| check_reason | 人工核查原因 |
| evidence | 证据 |
| suggested_action | 建议动作 |

## 12. 第 2 项 workflow：价格指数勘误和运营反馈问题处理

### 12.1 基本信息

- workflow_name: work_02_price_index_feedback
- display_name: 中核价格指数勘误和运营反馈问题处理
- 周期: 按需，收到客户反馈后处理

### 12.2 业务场景

客户每日通过运营群或工单反馈问题，Agent 需要：
- 根据标准商品编码/潜在变异编码定位商品
- 编码转换成 SARS 监控商品编码/Document ID
- 客户可能提供新同款链接
- 判断客户链接是否有效、是否来自不同商城、价差是否过大
- 生成 SARS 导入模板
- 可能需要建议人工解绑旧同款
- 可能需要建议人工重新计算价格指数
- 可能需要建议人工推送
- 生成客户反馈话术
- 加急任务优先处理，但仍不自动推送或自动导入

### 12.3 输入模型 PriceFeedbackRecord

| 字段 | 类型 | 含义 |
|---|---|---|
| scope | WorkflowScope | 记录所属范围，必须为 CNNC/188605/222 并与请求 scope 一致 |
| standard_commodity_code | str | 标准商品编码 |
| variant_code | str="" | 潜在变异编码 |
| sars_monitor_code | str="" | SARS 监控商品编码/Document ID |
| document_id | str="" | Document ID |
| commodity_name | str="" | 商品名称 |
| feedback_source | str | 客户/供应商/内部核查 |
| feedback_type | str | 价格偏高/价格偏低/缺失/异常波动/链接错误/商品不匹配/其他 |
| feedback_detail | str="" | 反馈详情 |
| customer_links | list[CustomerLinkRecord] | 客户提供的链接列表 |
| current_same_products | list[SameProductRecord] | 当前系统已有同款列表 |
| current_index_price | Decimal or None | 当前价格指数 |
| customer_expected_price | Decimal or None | 客户期望价格 |
| actual_market_price | Decimal or None | 实际市场价格（客户提供） |
| need_code_mapping | bool=False | 是否需要编码映射 |
| mapped_sars_monitor_code | str="" | 映射后的 SARS 监控商品编码 |
| priority | str="normal" | normal/urgent/critical |
| current_status | str="" | 同款匹配中/采集匹配中/校验失败/校验成功/待审批 |

`price_diff_pct`、同款数量、有效商城数和各类 `need_*` 均为分析输出，不作为调用方可控制的输入。

### 12.4 新增 CustomerLinkRecord

| 字段 | 类型 | 含义 |
|---|---|---|
| url | str | 客户提供的链接 |
| mall_name | str="" | 商城名称 |
| product_name | str="" | 商品名称 |
| price | Decimal or None | 链接对应价格 |
| page_actual_price | Decimal or None | 页面实际价格 |
| is_valid_link | bool or None | 链接是否有效 |
| is_same_product | bool or None | 是否确认为同款 |
| comment_count | int or None | 评论数 |
| issue | str="" | 问题说明 |

### 12.5 规则引擎（按顺序判断，命中第一条即输出）

| # | 条件 | 动作 | 原因 |
|---|---|---|---|
| 1 | 非中核范围数据 | REJECT | 不处理非中核范围数据 |
| 2 | 客户反馈未提供商品编码（standard_commodity_code 和 variant_code 均为空） | MANUAL_VERIFY | 缺少标准商品编码或潜在变异编码，无法定位商品 |
| 3 | 商品编码未映射到 SARS 监控商品编码 | MANUAL_VERIFY | 需要补充编码映射 |
| 4 | current_status 为"同款匹配中"或"采集匹配中" | WAIT_COLLECTION | 同款仍在采集回流中 |
| 5 | 客户提供 0 条链接，且计算得到当前有效同款不足 2 条 | WAIT_COLLECTION | 无可用同款；可附加 SUPPLEMENT_SAME_PRODUCT 建议 |
| 6 | 客户提供 1 条有效链接 | IMPORT_CUSTOMER_LINKS + SUPPLEMENT_SAME_PRODUCT | 只有 1 条，不足以生成价格指数 |
| 7 | 客户提供 >=2 条有效链接，且来自不同商城，价差合理 | IMPORT_CUSTOMER_LINKS | 可附加 RECALCULATE_INDEX、PUSH_INDEX 建议，全部为人工操作 |
| 8 | 客户提供链接中一条正确、一条错误 | MANUAL_VERIFY | 正确链接可附加 IMPORT_CUSTOMER_LINKS，错误链接附加 ASK_CUSTOMER_RECHECK |
| 9 | 客户提供多个链接但价差 > 30% | ASK_CUSTOMER_RECHECK | 价差过大，不建议直接推送 |
| 10 | 当前系统同款明显错误 | UNBIND_WRONG_RELATIONS | 仅建议人工解绑，不自动执行 |
| 11 | feedback_type=异常波动 | MANUAL_VERIFY | 可能是采集问题，需核查 |
| 12 | 当前价格指数偏高/偏低，且前述规则未命中 | MANUAL_VERIFY | 先核查同款质量，再输出附加建议 |
| 13 | feedback_type=价格偏高/偏低，偏差 <= 10% | ASK_CUSTOMER_RECHECK | 差异在合理范围，建议客户重新确认 |
| 15 | 关联同款审批问题 | FORWARD_TO_WORK01 | 转第 1 项同款审批流程 |
| 16 | 其他 | MANUAL_VERIFY | 默认人工核查 |

**特别说明：**
- IMPORT_CUSTOMER_LINKS 不是自动导入，仅生成 SARS 导入模板供人工操作
- UNBIND_WRONG_RELATIONS 不是自动解绑，仅列出建议解绑的错误同款清单
- RECALCULATE_INDEX 不是自动重算，仅输出重算前检查清单
- PUSH_INDEX 不是自动推送，仅输出可推送清单
- 加急任务优先处理方案，但仍不自动执行任何高风险操作
- `priority` 只影响队列排序和响应标记，不参与主动作规则判定
- 每次分析必须返回唯一 `primary_action`、可选 `suggested_actions`、命中的 `rule_id` 和结构化 `evidence`

### 12.6 输出动作枚举

| 动作 | 含义 | 是否自动执行 | 是否需要人工操作 | Agent 输出 |
|---|---|---|---|---|
| IMPORT_CUSTOMER_LINKS | 建议导入客户提供同款 | 否 | 是 | SARS 导入模板 |
| UNBIND_WRONG_RELATIONS | 建议解绑错误同款 | 否 | 是 | 错误同款清单 |
| RECALCULATE_INDEX | 建议重新计算价格指数 | 否 | 是 | 重算前检查清单 |
| PUSH_INDEX | 建议推送价格指数 | 否 | 是 | 可推送清单 |
| SUPPLEMENT_SAME_PRODUCT | 需补同款 | 否 | 是 | 缺口说明、补同款建议 |
| WAIT_COLLECTION | 等待采集 | 否 | 否 | 等待原因 |
| ASK_CUSTOMER_RECHECK | 建议客户重新确认 | 否 | 是 | 客户反馈话术 |
| MANUAL_VERIFY | 人工核查 | 否 | 是 | 核查原因和证据 |
| FORWARD_TO_WORK01 | 转第 1 项同款审批流程 | 否 | 否 | 内部流转 |
| NO_ACTION | 无需处理 | 否 | 否 | 说明原因 |

### 12.7 输出文件

**1. price_feedback_result.xlsx**

| 字段 | 含义 |
|---|---|
| standard_commodity_code | 标准商品编码 |
| variant_code | 潜在变异编码 |
| sars_monitor_code | SARS 监控商品编码/Document ID |
| commodity_name | 商品名称 |
| feedback_source | 反馈来源 |
| feedback_type | 反馈类型 |
| feedback_detail | 反馈内容摘要 |
| current_index_price | 当前价格指数 |
| customer_expected_price | 客户期望价格/页面价格 |
| price_diff_pct | 价格偏差比例 |
| same_product_count | 当前同款数 |
| valid_mall_count | 有效商城数 |
| customer_link_count | 客户链接数 |
| valid_customer_link_count | 有效客户链接数 |
| action | 建议动作 |
| need_supplement | 是否需要补录同款 |
| need_unbind | 是否需要人工解绑旧同款 |
| need_recalculate | 是否建议人工重算 |
| need_push | 是否建议人工推送 |
| reason | 判断原因 |
| risk_notice | 风险提示 |

**2. sars_import_template.xlsx**

| 字段 | 含义 |
|---|---|
| sars_monitor_code | SARS 监控商品编码/Document ID |
| standard_commodity_code | 标准商品编码 |
| variant_code | 潜在变异编码 |
| supplier_product_name | 供应商推品名称 |
| mall_recommended_name | 商城推荐标准商品名称 |
| skv | SKV |
| mall_name | 商城名称 |
| same_product_link | 同款链接 |
| price | 价格 |
| comment_count | 评论数 |
| source | 来源：客户提供/采集补充 |
| is_valid | 是否有效 |
| exception_reason | 异常原因 |

**3. customer_reply_draft.txt**

| 字段 | 含义 |
|---|---|
| customer_question_summary | 客户问题摘要 |
| processing_conclusion | 处理结论 |
| customer_confirmation_items | 需客户确认事项 |
| generated_suggestions | 已生成的处理建议 |
| manual_operation_nodes | 后续人工处理节点 |
| risk_statement | 风险说明 |

**4. price_feedback_exception_list.xlsx**

| 字段 | 含义 |
|---|---|
| standard_commodity_code | 标准商品编码 |
| commodity_name | 商品名称 |
| exception_type | 异常类型 |
| involved_link | 涉及链接 |
| involved_price | 涉及价格 |
| exception_reason | 异常原因 |
| suggested_customer_reply | 建议反馈客户内容 |


## 13. 其他 6 项 workflow 占位设计

| workflow | display_name | 核心输入 | 核心动作 |
|---|---|---|---|
| work_03 | 同款不足补充处理 | 同款统计表 | SUPPLEMENT_SAME_PRODUCT / MANUAL_CHECK |
| work_04 | 失败班车异常处理 | 失败任务列表 | RETRY_PUSH / MANUAL_CHECK / FEEDBACK_CUSTOMER |
| work_05 | 价格指数周度更新 | 周度数据表 | CONFIRM / MANUAL_CHECK / RECALCULATE_INDEX |
| work_06 | 指数波动异常处理 | 波动数据 | ALERT / MANUAL_CHECK / EXEMPTION |
| work_07 | 推送后核查处理 | 推送结果表 | CONFIRM / RETRY_PUSH / MANUAL_CHECK |
| work_08 | 临时查询与分析 | 任意 | QUERY / EXPORT / MANUAL_CHECK |

**说明**：详细规则引擎和输出文件在对应实现阶段设计。所有动作均为建议动作，不自动执行。

## 14. API 契约

### 14.0 访问和作用域约束

- `/workflows/*` 默认只允许绑定 `127.0.0.1`
- HTTP 调用必须校验 `LOCAL_WORKFLOW_TOKEN`；未配置或不匹配时拒绝请求
- token 只控制 workflow API 访问，不解锁任何自动动作或数据库写操作
- 所有分析请求必须携带 `scope`，且只能是 `project=CNNC`、`tenant_id=188605`、`commodity_pool_id=222`
- 批量记录发现任一非中核或作用域不明记录时整批拒绝
- 文件下载只允许访问当前请求生成的文件 ID，不接受客户端提供本地路径

### 14.1 POST /workflows/route

**Request：**

```json
{
  "scope": {
    "project": "CNNC",
    "tenant_id": 188605,
    "commodity_pool_id": 222
  },
  "question": "今天的新增监控商品同款审批",
  "file_hint": "same_product_approval_table.xlsx"
}
```

**Response：**

```json
{
  "workflow_name": "work_01_same_product_approval",
  "confidence": 0.95
}
```

### 14.2 POST /workflows/same-product-approval/analyze

**Request：**

```json
{
  "scope": {
    "project": "CNNC",
    "tenant_id": 188605,
    "commodity_pool_id": 222
  },
  "records": [
    {
      "scope": {
        "project": "CNNC",
        "tenant_id": 188605,
        "commodity_pool_id": 222
      },
      "standard_commodity_code": "SPC-20240615-001",
      "current_status": "PENDING_APPROVAL",
      "reference_price": 115.00,
      "candidates": [
        {
          "link": "https://example.com/a",
          "mall_name": "商城A",
          "crawl_price": 118.50,
          "page_actual_price": 118.50,
          "is_link_valid": true,
          "is_product_on_shelf": true,
          "is_same_product": true
        },
        {
          "link": "https://example.com/b",
          "mall_name": "商城B",
          "crawl_price": 116.00,
          "page_actual_price": 116.00,
          "is_link_valid": true,
          "is_product_on_shelf": true,
          "is_same_product": true
        }
      ]
    }
  ]
}
```

**Response：**

```json
{
  "primary_action": "APPROVE",
  "suggested_actions": ["APPROVE"],
  "reason": "有效同款>=2，有效商城>=2，价差<=30%，价格接近参考价",
  "pushable": true,
  "ready_to_push": true,
  "needs_human_approval": true,
  "execution_allowed": false,
  "executed": false
}
```

### 14.3 POST /workflows/price-index-feedback/analyze

**Request：**

```json
{
  "scope": {
    "project": "CNNC",
    "tenant_id": 188605,
    "commodity_pool_id": 222
  },
  "records": [
    {
      "scope": {
        "project": "CNNC",
        "tenant_id": 188605,
        "commodity_pool_id": 222
      },
      "standard_commodity_code": "SPC-20240615-001",
      "feedback_type": "价格偏高",
      "current_index_price": 260.00,
      "customer_expected_price": 198.00,
      "customer_links": [
        {
          "url": "https://example.com/product/123",
          "mall_name": "京东",
          "product_name": "某品牌电线电缆",
          "price": 200.00,
          "page_actual_price": 198.00,
          "is_valid_link": true,
          "is_same_product": true,
          "comment_count": 5,
          "issue": null
        }
      ]
    }
  ]
}
```

**Response：**

```json
{
  "primary_action": "RECALCULATE_INDEX",
  "suggested_actions": ["IMPORT_CUSTOMER_LINKS", "RECALCULATE_INDEX", "PUSH_INDEX"],
  "reason": "偏差>30%，有>=2同款来自不同商城，建议人工重算价格指数",
  "needs_human_approval": true,
  "execution_allowed": false,
  "executed": false
}
```

### 14.4 GET /workflows/{workflow_name}/knowledge

**work_01 引用：**

```json
{
  "workflow_name": "work_01_same_product_approval",
  "knowledge_files": [
    "cnnc_work_01_same_product_approval.md",
    "cnnc_base_scope.md",
    "cnnc_forbidden_actions.md"
  ]
}
```

**work_02 引用：**

```json
{
  "workflow_name": "work_02_price_index_feedback",
  "knowledge_files": [
    "cnnc_work_02_price_index_feedback.md",
    "cnnc_sars_import_rules.md",
    "cnnc_base_scope.md",
    "cnnc_forbidden_actions.md"
  ]
}
```

**SQL 诊断引用：**

```json
{
  "workflow_name": "diagnose",
  "knowledge_files": [
    "cnnc_sql_diagnosis_knowledge.md"
  ]
}
```

## 15. 数据模型
### 15.1 WorkflowAction 枚举

```python
from enum import Enum

class WorkflowAction(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    WAIT_CRAWLER = "WAIT_CRAWLER"
    SUPPLEMENT_SAME_PRODUCT = "SUPPLEMENT_SAME_PRODUCT"
    DO_NOT_PUSH = "DO_NOT_PUSH"
    MANUAL_CHECK = "MANUAL_CHECK"
    FEEDBACK_CUSTOMER = "FEEDBACK_CUSTOMER"
    READY_TO_PUSH = "READY_TO_PUSH"
    IMPORT_CUSTOMER_LINKS = "IMPORT_CUSTOMER_LINKS"
    UNBIND_WRONG_RELATIONS = "UNBIND_WRONG_RELATIONS"
    RECALCULATE_INDEX = "RECALCULATE_INDEX"
    PUSH_INDEX = "PUSH_INDEX"
    ASK_CUSTOMER_RECHECK = "ASK_CUSTOMER_RECHECK"
    MANUAL_VERIFY = "MANUAL_VERIFY"
    FORWARD_TO_WORK01 = "FORWARD_TO_WORK01"
    NO_ACTION = "NO_ACTION"
    WAIT_COLLECTION = "WAIT_COLLECTION"
    RETRY_PUSH = "RETRY_PUSH"
    CONFIRM = "CONFIRM"
    ALERT = "ALERT"
    EXEMPTION = "EXEMPTION"
    QUERY = "QUERY"
    EXPORT = "EXPORT"
```

### 15.2 ExportFormat 枚举

```python
class ExportFormat(str, Enum):
    XLSX = "XLSX"
    CSV = "CSV"
    MD = "MD"
    TXT = "TXT"
```

### 15.3 Workflow 请求/响应模型

```python
from decimal import Decimal
from typing import Generic, TypeVar
from pydantic import BaseModel, ConfigDict, Field
from typing import List, Optional

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class WorkflowScope(StrictModel):
    project: str = Field(..., pattern="^CNNC$")
    tenant_id: int = Field(..., ge=188605, le=188605)
    commodity_pool_id: int = Field(..., ge=222, le=222)

class BaseWorkflowRecord(StrictModel):
    scope: WorkflowScope

# SameProductRecord 和 PriceFeedbackRecord 必须继承 BaseWorkflowRecord

class WorkflowRouteRequest(StrictModel):
    scope: WorkflowScope
    question: str = Field(..., description="用户自然语言问题")
    file_hint: Optional[str] = Field(None, description="文件名提示")

class WorkflowRouteResponse(BaseModel):
    workflow_name: str = Field(..., description="匹配的 workflow 名称")
    confidence: float = Field(..., ge=0.0, le=1.0, description="路由置信度")

class WorkflowRecordResult(BaseModel):
    standard_commodity_code: str
    primary_action: WorkflowAction = Field(..., description="主要建议动作")
    suggested_actions: List[WorkflowAction] = Field(default_factory=list, description="全部建议动作列表（含 primary_action）")
    action: Optional[WorkflowAction] = Field(None, description="兼容字段，等于 primary_action")
    reason: str
    pushable: bool = False
    ready_to_push: bool = Field(False, description="可人工推送标记（仅 APPROVE 且流程上可推送时为 true）")
    needs_human_approval: bool = True
    execution_allowed: bool = False
    executed: bool = False
    rule_id: str
    evidence: List[str] = Field(default_factory=list)

class ExportFileInfo(BaseModel):
    filename: str
    format: ExportFormat
    sheet_name: Optional[str] = None
    row_count: int

RecordT = TypeVar("RecordT", bound=StrictModel)

class WorkflowAnalyzeRequest(StrictModel, Generic[RecordT]):
    scope: WorkflowScope
    records: List[RecordT] = Field(..., min_length=1, description="强类型待分析记录")

class SameProductAnalyzeRequest(WorkflowAnalyzeRequest[SameProductRecord]):
    records: List[SameProductRecord]

class PriceFeedbackAnalyzeRequest(WorkflowAnalyzeRequest[PriceFeedbackRecord]):
    records: List[PriceFeedbackRecord]

class WorkflowAnalyzeResponse(BaseModel):
    primary_action: WorkflowAction = Field(..., description="主要建议动作")
    suggested_actions: List[WorkflowAction] = Field(default_factory=list, description="全部建议动作列表")
    action: Optional[WorkflowAction] = Field(None, description="兼容字段，等于 primary_action")
    reason: str
    pushable: bool = False
    ready_to_push: bool = Field(False, description="可人工推送标记")
    needs_human_approval: bool = True
    execution_allowed: bool = False
    executed: bool = False
    record_results: Optional[List[WorkflowRecordResult]] = None
    export_files: Optional[List[ExportFileInfo]] = None
```

### 15.4 CustomerLinkRecord

```python
class CustomerLinkRecord(StrictModel):
    url: str = Field(..., description="客户提供的商品链接")
    mall_name: Optional[str] = Field(None, description="商城名称")
    product_name: Optional[str] = Field(None, description="商品名称")
    price: Optional[Decimal] = Field(None, ge=0, description="链接标价")
    page_actual_price: Optional[Decimal] = Field(None, ge=0, description="页面实际成交价")
    is_valid_link: bool = Field(False, description="链接是否有效（未下架/可访问）")
    is_same_product: bool = Field(False, description="是否为同款商品")
    comment_count: int = Field(0, description="评论数")
    issue: Optional[str] = Field(None, description="存在的问题描述")
```
## 16. 输出文件结构

### 16.1 文件命名规范

```
outputs/{date}/{workflow_name}_{file_type}.{ext}
```

示例：

- `outputs/2024-06-15/work_01_approval_result.xlsx`
- `outputs/2024-06-15/work_02_recalculate_list.xlsx`
- `outputs/2024-06-15/work_02_customer_reply_template.md`

所有导出文件必须包含或伴随以下元数据：

- `DRAFT / 未经人工复核 / 不可直接导入`
- request_id、生成时间、workflow 名称、规则版本
- 来源文件 SHA-256 或输入批次哈希
- scope：CNNC / 188605 / 222
- review_status，默认 `PENDING_MANUAL_REVIEW`

### 16.2 Excel 读写服务

```python
class ExcelReader:
    def read_file(self, file_bytes: bytes, original_filename: str, sheet_name: str | None = None) -> DataTable:
        ...

    def validate_columns(self, table: DataTable, required_columns: list[str]) -> bool:
        ...

    def map_columns(self, table: DataTable, column_mapping: dict[str, str]) -> DataTable:
        ...

class ExcelWriter:
    def write_file(self, request_id: str, table: DataTable, sheet_name: str = "Sheet1") -> ExportFileInfo:
        ...

    def write_multi_sheet(self, request_id: str, sheets: dict[str, DataTable]) -> ExportFileInfo:
        ...
```

### 16.3 文件安全约束

- 只接受 `.xlsx`、`.csv`、`.json`；拒绝 `.xlsm`、含宏文件和未知压缩格式
- 业务文件必须包含 project、tenant_id、commodity_pool_id 列；缺失或任一行不匹配 CNNC/188605/222 时整批拒绝
- 限制上传字节数、解压后大小、行数、列数、Sheet 数、单元格文本长度和批次记录数
- 检测损坏文件、XLSX ZIP bomb、重复列名和隐藏 Sheet；异常时结构化拒绝
- 不接受客户端提供的读取或写入路径，服务端使用随机 request_id 生成隔离目录
- 文件名使用安全化后的 basename，不允许 `..`、绝对路径或路径分隔符
- CSV/XLSX 导出时，对以 `= + - @` 开头的文本做公式注入转义
- 临时文件和导出文件设置保留期限，超期清理；不同请求不得覆盖同名文件
- 下载接口校验访问 token 与 request_id，只返回当前请求生成的文件

## 17. 知识库结构

### 17.1 拆分后的 MD 文件列表

共 13 个文件：

| 序号 | 文件名 | 内容 |
|---|---|---|
| 1 | cnnc_ops_index.md | 知识库索引 |
| 2 | cnnc_base_scope.md | 基础作用域与安全边界 |
| 3 | cnnc_work_01_same_product_approval.md | work_01 同款审批规则 |
| 4 | cnnc_work_02_price_index_feedback.md | work_02 价格指数反馈规则 |
| 5 | cnnc_work_03_supplement_same_product.md | work_03 同款不足补充 |
| 6 | cnnc_work_04_failed_task_exception.md | work_04 失败班车异常 |
| 7 | cnnc_work_05_weekly_index_update.md | work_05 价格指数周度更新 |
| 8 | cnnc_work_06_index_volatility_exception.md | work_06 指数波动异常 |
| 9 | cnnc_work_07_post_push_verification.md | work_07 推送后核查 |
| 10 | cnnc_work_08_ad_hoc_query.md | work_08 临时查询与分析 |
| 11 | cnnc_sql_diagnosis_knowledge.md | SQL 诊断知识 |
| 12 | cnnc_sars_import_rules.md | SARS 导入规则 |
| 13 | cnnc_forbidden_actions.md | 禁止动作清单 |

### 17.2 workflow 与知识库引用关系表

| workflow | 引用的知识库文件 |
|---|---|
| work_01 | cnnc_work_01_same_product_approval.md, cnnc_base_scope.md, cnnc_forbidden_actions.md |
| work_02 | cnnc_work_02_price_index_feedback.md, cnnc_sars_import_rules.md, cnnc_base_scope.md, cnnc_forbidden_actions.md |
| work_03 | cnnc_work_03_supplement_same_product.md, cnnc_base_scope.md, cnnc_forbidden_actions.md |
| work_04 | cnnc_work_04_failed_task_exception.md, cnnc_base_scope.md, cnnc_forbidden_actions.md |
| work_05 | cnnc_work_05_weekly_index_update.md, cnnc_base_scope.md, cnnc_forbidden_actions.md |
| work_06 | cnnc_work_06_index_volatility_exception.md, cnnc_base_scope.md, cnnc_forbidden_actions.md |
| work_07 | cnnc_work_07_post_push_verification.md, cnnc_base_scope.md, cnnc_forbidden_actions.md |
| work_08 | cnnc_work_08_ad_hoc_query.md, cnnc_base_scope.md, cnnc_forbidden_actions.md |
| diagnose | cnnc_sql_diagnosis_knowledge.md, cnnc_base_scope.md, cnnc_forbidden_actions.md |

workflow 知识库由独立的 `workflow_knowledge_adapter` 按允许文件清单加载。第一阶段 `knowledge_loader` 继续读取原单体文件，R2-R6 不修改其接口、缓存和脱敏逻辑。


## 18. 安全边界

### 18.1 分层安全

**workflow 调用链：**

```
/workflows/*
  -> local_access_guard     # 第一层：本地绑定和 token 校验
  -> scope_guard            # 第二层：问题文本范围校验
  -> workflow_scope_guard   # 第三层：结构化 scope 和批次记录校验
  -> workflow_router        # 第四层：意图路由
  -> workflow.analyze       # 第五层：规则引擎分析
  -> human_approval_guard   # 第六层：强制仅输出建议
  -> workflow_knowledge_adapter
```

**diagnose 调用链：**

```
/diagnose
  -> scope_guard           # 第一层：作用域校验
  -> safety_guard          # 第二层：安全守卫（SQL注入等）
  -> sql_selector          # 第三层：SQL模板选择
  -> knowledge_loader      # 第四层：知识库加载
  -> result_analyzer       # 第五层：结果分析
```

### 18.2 建议动作矩阵

| 动作 | 含义 | 是否自动执行 | 是否需要人工操作 | Agent 输出 |
|---|---|---|---|---|
| APPROVE | 建议审批通过 | 否 | 是 | 审批建议清单 |
| REJECT | 建议驳回 | 否 | 是 | 驳回建议及理由 |
| WAIT_CRAWLER | 等待爬虫补充数据 | 否 | 是（人工跟进） | 等待清单 |
| SUPPLEMENT_SAME_PRODUCT | 建议补充同款 | 否 | 是 | 同款补充建议 |
| DO_NOT_PUSH | 建议不推送 | 否 | 是 | 不推送建议清单 |
| MANUAL_CHECK | 建议人工核查 | 否 | 是 | 核查清单 |
| FEEDBACK_CUSTOMER | 建议反馈客户 | 否 | 是 | 客户反馈话术模板 |
| READY_TO_PUSH | 建议可推送 | 否 | 是 | 推送建议清单 |
| IMPORT_CUSTOMER_LINKS | 建议导入客户链接 | 否 | 是 | 导入模板 |
| UNBIND_WRONG_RELATIONS | 建议解绑错误关联 | 否 | 是 | 解绑建议清单 |
| RECALCULATE_INDEX | 建议重算价格指数 | 否 | 是 | 重算建议及数据 |
| PUSH_INDEX | 建议推送价格指数 | 否 | 是 | 推送建议清单 |
| ASK_CUSTOMER_RECHECK | 建议请客户复核 | 否 | 是 | 客户复核话术模板 |

**核心保证：**

- LOCAL_EXEC_TOKEN 只用于未来本地只读查询执行，不用于解锁写操作
- Agent 永远不需要 DB 写权限
- 本设计不包含任何写操作接口

**重要澄清**：
- APPROVE 不是自动审批，仅生成建议审批清单
- REJECT 不是自动驳回，仅生成建议驳回清单
- READY_TO_PUSH 不是自动推送，仅生成建议推送清单
- RECALCULATE_INDEX 不是自动重算，仅生成建议重算清单
- UNBIND_WRONG_RELATIONS 不是自动解绑，仅生成建议解绑清单
- IMPORT_CUSTOMER_LINKS 不是自动导入，仅生成导入模板

### 18.3 execution_sql_policy

仅用于只读查询执行前的额外校验，不用于任何写操作。

- 本 Agent 不设计、不暴露、不执行任何 DB 写操作
- 即使未来接入只读数据库，也只允许 SELECT/WITH 类查询，且必须来自 YAML 模板
- db_readonly_verified=false -> 一切执行禁止
- bind_host != 127.0.0.1 -> 一切执行禁止
- LOCAL_EXEC_TOKEN 未配置 -> effective_execute=false
- SHOW GRANTS 仅允许 USAGE + SELECT 权限

**文档中不得以肯定或含糊方式出现以下表述（仅允许在否定声明中作为"不是"的反面引用）：**
- 动作涉及 DB 写操作时
- 需要 DB 写权限
- 自动重算
- 自动推送
- 自动解绑
- 自动驳回
- 自动导入生产系统


## 19. 渐进式实施计划

| 阶段 | 内容 | 约束 |
|---|---|---|
| R1 | 只修订架构设计文档 | 不写代码，不移动模块，不改 /diagnose，不改 P0/P1 |
| R2 | 旁路新增 workflow 基础层 | 新增强类型 scope、local_access_guard、workflow_scope_guard、schemas、base、router 和知识库适配层；不移动旧模块 |
| R2-GATE | 基础层安全复审 | 验证非中核批次拒绝、未授权接口拒绝、动作永不执行、第一阶段回归通过 |
| R3 | 实现 work_01 离线规则版 | JSON/mock，不接 Excel，不接 DB，不接模型 |
| R4 | 实现 work_02 离线规则版 | 同上 |
| R4-GATE | 规则与业务复审 | 验证派生指标、规则优先级、证据和人工建议语义 |
| R5 | 接入 Excel/CSV 读取和导出 | 完成文件大小、ZIP bomb、路径、公式注入、隔离和清理测试后方可通过 |
| R5-GATE | 文件安全复审 | 验证恶意文件、并发隔离、下载授权和草稿标识 |
| R6 | 接入个人本地只读查询能力 | 默认关闭，不写库 |
| R6-GATE | 独立数据库安全复审 | 必须通过执行条件、只读权限和结果脱敏审查 |
| R7 | 再评估是否迁移目录 | 等 workflow 稳定后，单独设计/测试/提交 |
| R8 | 封版安全复审 | 汇总各阶段门禁结果，不替代前置安全复审 |

## 20. 测试策略

### 20.1 单元测试

| 测试对象 | 测试内容 |
|---|---|
| 规则引擎 | 逐条规则覆盖：同款数边界、商城数边界、价差阈值、链接有效性 |
| workflow_scope_guard | scope 缺失、非 188605、非 222、混合租户批次全部拒绝 |
| excel_reader / excel_writer | 文件限制、损坏文件、ZIP bomb、路径穿越、公式注入、隔离和清理 |
| router | 意图匹配、confidence 阈值、兜底路由 |
| human_approval_guard | 所有动作 executed=false、execution_allowed=false，高风险建议 needs_human_approval=true |
| local_access_guard | 非 loopback、token 缺失或错误时拒绝 workflow API |
| execution_sql_policy | 只读校验、bind_host 校验、token 校验 |

### 20.2 集成测试

- work_01 端到端：输入审批表 -> 路由 -> 分析 -> 输出审批建议清单
- work_02 端到端：输入反馈数据 -> 路由 -> 分析 -> 输出客户话术 + 重算建议
- 安全拦截端到端：尝试绕过 access/scope/human_approval_guard -> 被拦截
- 批次中混入一条其他租户记录 -> 整批 OUT_OF_SCOPE
- 导出文件含 DRAFT、规则版本、输入哈希和待人工复核状态

### 20.3 回归测试

- 现有 244 条测试必须全通过
- /diagnose 和 /health 接口不受影响
- 不改变现有 P0/P1 逻辑


## 21. 风险与缓解

| 风险 | 缓解措施 |
|---|---|
| 规则引擎遗漏 | 逐条测试覆盖 + 知识库审查 + 兜底 MANUAL_CHECK |
| Excel 格式不统一 | 列映射配置 + validate_columns 强校验 + 人工确认 |
| 非中核或混合租户数据进入 workflow | 强制 WorkflowScope + 批次逐条范围校验 + 失败关闭 |
| 调用方伪造汇总计数 | 仅接受候选明细，由规则引擎计算计数、商城去重和价差 |
| Workflow API 未授权访问 | loopback 默认绑定 + LOCAL_WORKFLOW_TOKEN + 下载请求隔离 |
| 恶意 Excel/CSV | 文件容量限制 + ZIP bomb/路径/公式注入防护 + 临时文件清理 |
| router 误路由 | confidence 阈值 + 低于阈值兜底 MANUAL_CHECK + 人工确认 |
| 安全层绕过 | 分层校验 + Codex 审查 + 无写操作接口 |
| 重构引入回归 | 244 条回归测试 + /diagnose 和 /health 不动 + 渐进式发布 |
| 本地 DB 意外写入 | execution_sql_policy + 只允许 SELECT + bind_host 限制 + 默认关闭 |

## 22. Codex 审查清单

### P0 审查项（17 项）

| 序号 | 审查项 | 要求 |
|---|---|---|
| 1 | 保留 P0/P1 底座 | 不破坏现有 P0/P1 逻辑 |
| 2 | 没破坏 /diagnose | /diagnose 功能和接口不变 |
| 3 | 没破坏 /health | /health 功能和接口不变 |
| 4 | 没有 raw_sql 入口 | 不暴露原始 SQL 输入接口 |
| 5 | 没有模型生成 SQL 执行 | 模型不可生成 SQL 并执行 |
| 6 | 没有任何 DB 写操作 | 无 INSERT/UPDATE/DELETE 等写操作 |
| 7 | 没自动解绑 | UNBIND_WRONG_RELATIONS 仅生成建议 |
| 8 | 没自动驳回 | REJECT 仅生成建议 |
| 9 | 没自动推送 | READY_TO_PUSH / PUSH_INDEX 仅生成建议 |
| 10 | 没自动重算 | RECALCULATE_INDEX 仅生成建议 |
| 11 | 没自动导入 | IMPORT_CUSTOMER_LINKS 仅生成模板 |
| 12 | 没自动重推 | 不存在自动重推逻辑 |
| 13 | 没跨租户 | scope_guard 限制仅中核 |
| 14 | 仍只处理中核 | 不扩展到其他租户 |
| 15 | 高风险动作全需人工确认 | human_approval_guard 拦截 |
| 16 | workflow 不会绕过 scope_guard | workflow 在 scope_guard 之后 |
| 17 | Excel 导出不会生成可自动写库脚本 | 导出仅为数据清单 |

### P1 审查项（11 项）

| 序号 | 审查项 | 要求 |
|---|---|---|
| 1 | work_01 规则符合会议纪要 | 同款审批规则与业务会议一致 |
| 2 | work_02 规则符合会议纪要 | 价格指数反馈规则与业务会议一致 |
| 3 | 有效同款<2 时不会误判可推送 | 规则引擎正确处理边界 |
| 4 | 同商城重复链接识别一物多码 | 同商城去重逻辑正确 |
| 5 | 价差>30% 进入人工核查 | 阈值判断正确 |
| 6 | 链接失效/下架/价格不一致不会被审批通过 | 链接有效性校验正确 |
| 7 | 客户链接保留流程留痕 | 操作记录可追溯 |
| 8 | SARS 导入模板字段完整 | 模板字段与业务要求一致 |
| 9 | 客户话术不会承诺已自动处理 | 话术明确标注"建议" |
| 10 | router 误路由有 confidence 和人工确认 | 低置信度走兜底 |
| 11 | 默认兜底 MANUAL_CHECK / MANUAL_VERIFY | 未匹配时走人工 |

### P2 审查项（6 项）

| 序号 | 审查项 | 要求 |
|---|---|---|
| 1 | 文档结构清晰 | 章节划分合理 |
| 2 | API 示例自洽 | 请求/响应可对应 |
| 3 | 字段命名统一 | 全文命名风格一致 |
| 4 | 输出文件命名统一 | 遵循命名规范 |
| 5 | 知识库引用用拆分后 MD | 不引用旧的单体文件 |
| 6 | 明确未来企业版升级方向 | 标注可扩展点 |
## 23. 最终结论

| 序号 | 问题 | 结论 |
|---|---|---|
| 1 | 是否建议推倒重写 | **不建议推倒重写。** 现有 /diagnose 底座稳定，应在保留基础上扩展。 |
| 2 | 是否建议保留第一阶段诊断底座 | **必须保留。** /diagnose 是 P0 能力，所有后续扩展不得影响其功能和安全性。 |
| 3 | 是否建议优先实现第 1、2 项 workflow | **建议优先实现。** work_01（同款审批）和 work_02（价格指数反馈）是当前最高频人工操作，价值最大。 |
| 4 | 是否允许直接进入代码实施 | **不允许直接大规模改代码。** 仅允许进入"旁路新增 workflow 基础层"的 R2 小步实施，且前提是本设计通过 Codex 审查。 |
| 5 | 是否允许接真实数据库 | **不允许。** 真实数据库只读查询能力放到后续阶段（R6），默认关闭。不设计、不暴露、不执行任何 DB 写操作。 |
| 6 | 是否允许接模型 | **不允许在当前阶段接模型。** 未来模型只能用于意图识别、分类、话术润色，不允许生成 SQL 或执行动作。 |
| 7 | 是否允许 Agent 自动处理推送/解绑/驳回/重算 | **不允许。** Agent 只能生成建议、清单、模板、话术。所有动作均需人工确认后方可执行。 |
