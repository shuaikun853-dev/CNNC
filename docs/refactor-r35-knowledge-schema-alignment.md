# R3.5 设计文档：知识库-Schema 对齐与 work_03 独立 Workflow

> 版本：2.2 | 日期：2026-06-16 | 状态：设计阶段（P1 二次修订稿，待 Codex 复审）
> 前置依赖：R3 work_01 离线规则版已通过（14 条规则，315 tests passing）
>
> 修订记录：
> - v2.0：收窄 R3.5 范围（只做对齐+占位，不实现 work_03 规则引擎）；保留 work_03_same_product_shortage workflow_name；ManualOperationNode 强类型化；sars_monitor_code 非必填；有效同款数必须派生；CollectionConfig 字段改名；去掉 export_format=XLSX；补充 PriceFeedbackRecord 扩展方案；P1 审查清单去重
> - v2.1：P1 修订 — 12.3~12.6 明确为 R4.5 预留设计，R3.5 不输出任何建议；shared_rules.py 不纳入 R3.5 必做；T08 修正 manual_operation_nodes 矛盾；R6 删除"SQL 自动执行"表述；补充 work_03 API 安全测试 14 项清单；增加 R3.5 允许/不允许清单
> - v2.2：修正 2.3 节 SQL 建议口径冲突（R3.5 不输出 SQL 建议）；修正 12.3 节 evidence 示例中 R5+ 改为 R6 + 安全审查表述

---

## 目录

1. 当前状态评估
2. 三份 MD 对 Agent 搭建的影响
3. 哪些架构不需要改
4. 哪些地方必须改
5. 哪些地方建议改
6. 哪些地方后续阶段再改
7. workflow_knowledge_adapter 调整方案
8. schema 扩展方案
9. ManualOperationNode 设计
10. work_01 对齐方案
11. work_02 R4 设计边界
12. work_03 独立 workflow 设计
13. workflow router 触发词调整
14. 新的阶段规划（R3.5 / R4 / R5 / R6）
15. 安全边界
16. 测试策略
17. Codex 审查清单
18. 最终结论

---

## 1. 当前状态评估

### 1.1 代码资产盘点

| 模块 | 位置 | 状态 | 测试 |
|---|---|---|---|
| scope_guard（Agent） | app/agent/router.py | R1 P0/P1 审查通过 | OK |
| safety_guard | app/agent/safety_guard.py | R1 P0/P1 审查通过 | OK |
| sql_selector | app/agent/sql_selector.py | R1 /diagnose 专用 | OK |
| knowledge_loader | app/services/knowledge_loader.py | R1 /diagnose 专用 | OK |
| planner / result_analyzer | app/agent/ | R1 /diagnose 专用 | OK |
| /health | app/main.py | R1 完成 | OK |
| /diagnose | app/main.py | R1 完成 | OK |
| workflow_scope_guard | app/workflows/scope_guard.py | R2 完成 | OK |
| local_access_guard | app/workflows/local_access_guard.py | R2 完成 | OK |
| human_approval_guard | app/workflows/human_approval_guard.py | R2 完成 | OK |
| workflow schemas | app/schemas/workflow.py | R2 完成 | OK |
| workflow base + registry | app/workflows/base.py | R2 完成 | OK |
| workflow router | app/workflows/router.py | R2 完成 | OK |
| workflow_knowledge_adapter | app/services/workflow_knowledge_adapter.py | R2 完成 | OK |
| work_01 规则引擎 | app/workflows/work_01_same_product_approval.py | R3 14条规则 | 315 tests |
| work_02 WorkflowBase | app/workflows/base.py:PriceIndexFeedbackWorkflow | R2 占位 | OK |
| work_03 WorkflowBase | **不存在** | **未实现** | **N/A** |

### 1.2 work_01 规则引擎现状（14 条）

| 规则 ID | 条件 | 动作 | pushable |
|---|---|---|---|
| R01_WAIT_CRAWLER | current_status 为"同款匹配中"/"采集匹配中" | WAIT_CRAWLER | false |
| R12_VALIDATE_FAILED | current_status 为"校验失败" | MANUAL_CHECK | false |
| R02_NO_CANDIDATES | candidates 为空 | SUPPLEMENT_SAME_PRODUCT | false |
| R06A_ALL_LINK_INVALID_OR_OFFSHELF | 所有候选链接失效或下架 | REJECT | false |
| R06B_PARTIAL_LINK_INVALID_OR_OFFSHELF | 部分候选链接失效或下架 | MANUAL_CHECK | false |
| R08_PRICE_INVALID | 已确认同款但价格缺失或<=0 | MANUAL_CHECK | false |
| R03_VALID_COUNT_ZERO | valid_same_count=0 | SUPPLEMENT_SAME_PRODUCT | false |
| R04_VALID_COUNT_ONE | valid_same_count=1 | SUPPLEMENT_SAME_PRODUCT | false |
| R05_SAME_MALL_DUPLICATE | valid_mall_count<2 | MANUAL_CHECK | false |
| R07_PRICE_INCONSISTENT | crawl_price!=page_actual_price | MANUAL_CHECK | false |
| R09_REF_PRICE_TOO_HIGH | max_price>reference_price*1.3 | MANUAL_CHECK | false |
| R10_PRICE_DIFF_TOO_LARGE | price_diff_pct>30% | MANUAL_CHECK | false |
| R13_APPROVE | 全部条件满足 | APPROVE | true |
| R99_MANUAL_CHECK | 兜底 | MANUAL_CHECK | false |

### 1.3 三处关键缺口

| # | 问题 | 严重程度 | 位置 |
|---|---|---|---|
| 1 | router 中 work_03 workflow_name 为 `work_03_same_product_shortage`，知识库文件名为 `cnnc_work_03_supplement_same_product.md` — 已重新评估，workflow_name 保留不变，通过映射关联 | 已解决 | app/workflows/router.py:7 |
| 2 | `_WORKFLOW_KNOWLEDGE_MAP` 没有 work_03 条目，白名单已包含其文件名 | P1 | app/services/workflow_knowledge_adapter.py |
| 3 | `WORKFLOW_REGISTRY` 只有 work_01 和 work_02 | P1 | app/workflows/base.py:106-109 |

### 1.4 work_02 占位现状

`PriceIndexFeedbackWorkflow.analyze()` 对每条记录返回 `MANUAL_VERIFY`，reason 固定为 "R2 only: work_02 business rules not implemented yet"，`execution_allowed=False`，`executed=False`。无任何业务逻辑判断。

### 1.5 Schema 现状

| Schema | 字段数 | 状态 |
|---|---|---|
| WorkflowScope | 3 | 完成，锁定 CNNC/188605/222 |
| WorkflowAction | 23 | 完成，枚举已覆盖 work_01~work_08 |
| SameProductCandidate | 8 | 完成 |
| SameProductRecord | 10 | 完成 |
| CustomerLinkRecord | 8 | 完成 |
| PriceFeedbackRecord | 15 | 完成 |
| WorkflowRecordResult | 12 | 完成 |
| ExportFileInfo | 4 | 完成 |
| WorkflowAnalyzeResponse | 6 | 完成 |

---

## 2. 三份 MD 对 Agent 搭建的影响

### 2.1 work_01 MD（cnnc_work_01_same_product_approval.md）的影响

| 规则 | MD 描述 | 代码已实现 | 对齐状态 |
|---|---|---|---|
| 至少2条有效同款 | "至少2条有效同款" | R03/R04/R13 | 已对齐 |
| 来自不同商城 | "2条有效同款需来自不同商城" | R05 | 已对齐 |
| 链接可访问+商品上架 | "链接失效、下架、页面打不开，驳回" | R06A/R06B | 已对齐 |
| 型号/规格一致 | "型号、规格、关键参数一致" | 依赖 is_same_product 输入 | 部分对齐 |
| 价格偏高约30% | "高于参考价/价格指数约30%以上，需谨慎" | R09（仅判>1.3，不判<0.7） | 已对齐（R3决策） |
| 低价优先保留 | "优先选择较低且合理的同款价" | 保留低价不剔除 | 已对齐（R3决策） |
| 参考链接默认可信 | "通常优先认为可信，但仍需人工核对" | same_product_type不影响规则 | 已对齐 |
| 同款匹配中不能推送 | "不要直接点击推送" | R01 | 已对齐 |
| 页面价格与SaaS价格一致 | "页面价格与SaaS返回价格需一致" | R07 | 已对齐 |
| 同一商城一物多码 | "同一商城多条链接不能同时作为最终两条同款" | R05 | 已对齐 |
| 校验失败需人工 | MD异常处理 | R12 | 已对齐 |
| 同款来源分类 | "参考链接"和"互联网" | same_product_type字段存在但未参与规则 | 不影响判定 |

**结论：work_01 已基本完整覆盖 MD 核心规则，无需大改。**

### 2.2 work_02 MD（cnnc_work_02_price_index_feedback.md）的影响

| 规则 | MD 描述 | 代码现状 | 缺口 |
|---|---|---|---|
| 编码转换是关键 | "不能把标准商品编码直接当SaaS监控商品编码导入" | Schema 有 need_code_mapping | R4需实现映射检查规则 |
| 不能盲信反馈链接 | "客户提供的链接通常可信，但仍要核对" | CustomerLinkRecord 有 is_valid_link | R4需实现反馈链接校验 |
| 只修正有问题的同款 | "原来SaaS采集的同款如果确实正确，不必全部解绑" | 无 | R4需实现 |
| 错款必须解绑 | "型号/规格/核心参数不一致时，解绑或驳回" | UNBIND_WRONG_RELATIONS 已定义 | R4需规则触发 |
| 高溢价约30%不作为有效同款 | "高于参考价/当前指数约30%以上不作为有效同款" | work_01 R09 同类逻辑 | R4需复用 |
| 低价优先 | "倾向保留价格较低的数据" | work_01已保留 | R4需复用 |
| 制作同款导入模板 | zhImport模板12个字段 | IMPORT_CUSTOMER_LINKS 已定义 | R4需实现模板字段映射 |
| zhImport导入SaaS | "租户ID=188605，商品池ID=222" | Scope已锁定 | R4需在输出标注 |
| 解绑后重算推送 | "解绑->重新计算->推送" | 动作链已定义 | R4需实现动作链判定 |
| 价格太高场景 | "解绑高价错款+补入低价+重算推送" | 无 | R4需实现 |
| 价格太低场景 | "检查是否绑了非同款" | 无 | R4需实现 |

**结论：work_02 需从占位骨架升级为完整规则引擎，是 R4 核心任务。**

### 2.3 work_03 MD（cnnc_work_03_supplement_same_product.md）的影响

| 规则 | MD 描述 | 代码现状 | 缺口 |
|---|---|---|---|
| 周期：每周一~三 | "每周工作，周一启动周三前完成" | 无 | 需Schema表示时间约束 |
| SQL筛查同款不足 | 查 saas_onecode_price 三张表 | 无 | R3.5 仅做 Schema/知识映射/占位 workflow；SQL 建议输出放到 R4.5，SQL 只读查询能力放到 R6 单独安全审查 |
| 编码关系表匹配 | "商城标识对应关系表" | Schema 有 need_code_mapping | work_03需独立编码映射步骤 |
| 品牌清洗 | "中英文品牌提取中文主品牌" | 无Schema字段 | 需新增BrandInfo schema |
| 搜索词生成 | "完整/精简/兜底搜索词" | 无Schema字段 | 需新增SearchTermConfig schema |
| 采集任务配置 | "ES互联网召回60、京东召回30/60" | 无Schema字段 | 需新增CollectionConfig schema |
| 模型同款匹配 | "启用模型同款匹配" | 无 | 不接模型，仅建议人工操作 |
| 去一物多码 | "同一商城只保留一条" | work_01 R05 逻辑可复用 | work_03规则中引用 |
| 去极值 | "高于参考价/价格指数约30%剔除" | work_01 R09 逻辑可复用 | work_03规则中引用 |
| 制作导入模板 | zhImport模板12个字段 | IMPORT_CUSTOMER_LINKS 已定义 | work_03需独立模板逻辑 |
| 导入SaaS | "租户ID=188605，商品池ID=222" | Scope已锁定 | 不自动导入，仅生成模板 |
| 周三全量计算 | "周三价格指数全量计算" | 无 | 不自动触发，仅输出时间提示 |

**结论：work_03 是完整独立工作流，需6个新Schema，与 work_01 共享去一物多码/去极值逻辑但触发条件和流程完全不同。**

---

## 3. 哪些架构不需要改

### 3.1 安全底座不动

| 层 | 模块 | 理由 |
|---|---|---|
| L1 | local_access_guard | loopback+token校验已完成 |
| L2 | scope_guard（Agent） | 自然语言scope判定不变 |
| L3 | workflow_scope_guard | CNNC/188605/222锁定不变 |
| L4 | human_approval_guard | 建议动作拦截逻辑不变 |
| L5 | safety_guard | SQL安全校验不变 |
| L6 | execution_sql_policy | 只读策略不变 |

### 3.2 核心抽象不动

| 抽象 | 理由 |
|---|---|
| WorkflowBase ABC | 接口稳定，6个属性+2个方法 |
| WorkflowAnalyzeResponse | 结构稳定，6个字段 |
| WorkflowRecordResult | 结构稳定，12个字段，rule_id/evidence已支持新规则 |
| WorkflowAction 枚举 | 23个值已覆盖work_03场景 |
| WorkflowScope | 锁定 CNNC/188605/222 |
| StrictModel 基类 | extra="forbid"策略不变 |

### 3.3 /diagnose 和 /health 不动

- /diagnose 完整调用链不变
- /health 端点不变
- knowledge_loader 单文件加载和缓存逻辑不变

### 3.4 work_01 规则不动

- 14条规则逻辑和优先级不变
- _is_valid_candidate 不变（含 mall_name 非空校验，R3 P1 已修复）
- 参考价偏离仅判高于1.3倍、不判低于0.7倍的R3业务决策保留
- _derive_metrics 计算逻辑不变
- 所有阈值常量不变（PRICE_DIFF_THRESHOLD=0.30, HIGH_PRICE_RATIO=1.30, MIN_VALID_SAME_COUNT=2, MIN_VALID_MALL_COUNT=2）

### 3.5 不推倒重写原则不变

- 不移动旧模块文件
- 新增模块通过适配层调用现有安全模块
- 旧模块保持原位置、原接口、原测试不变

---

## 4. 哪些地方必须改

### 4.1 router 中 work_03 的 workflow_name 保留不变

**现状：** `app/workflows/router.py:7`

```python
(["同款不足", "补同款", "无同款"], "work_03_same_product_shortage", 0.85),
```

**评估结论：保留 `work_03_same_product_shortage` 不变。**

workflow_name 是系统稳定接口，知识库文件名只是资源映射，不必完全一致。通过 `_WORKFLOW_KNOWLEDGE_MAP` 将 `work_03_same_product_shortage` 映射到 `cnnc_work_03_supplement_same_product.md` 即可。如果强行改名，需要引入 alias 兼容机制，复杂度不值得。

### 4.2 _WORKFLOW_KNOWLEDGE_MAP 必须增加 work_03 条目

**现状：** `_WORKFLOW_KNOWLEDGE_MAP` 只有 work_01、work_02、diagnose。

**必须新增：**

```python
"work_03_same_product_shortage": [
    "cnnc_work_03_supplement_same_product.md",
    "cnnc_sars_import_rules.md",
    "cnnc_base_scope.md",
    "cnnc_forbidden_actions.md",
],
```

work_03 引用 `cnnc_sars_import_rules.md` 是因为补同款最终步骤也是制作 zhImport 模板导入 SaaS。

### 4.3 WORKFLOW_REGISTRY 必须增加 work_03

必须新增 `SupplementSameProductWorkflow` 类并注册到 `WORKFLOW_REGISTRY`：

```python
class SupplementSameProductWorkflow(WorkflowBase):
    workflow_name = "work_03_same_product_shortage"
    display_name = "中核监控商品同款不足数据筛查和线下流程补同款"
    required_inputs = ["commodity_name"]
    optional_inputs = [
        "sars_monitor_code", "standard_commodity_code",
        "variant_code", "mapped_sars_monitor_code",
        "current_same_products", "reference_price",
    ]
    possible_actions = [
        WorkflowAction.SUPPLEMENT_SAME_PRODUCT,
        WorkflowAction.IMPORT_CUSTOMER_LINKS,
        WorkflowAction.UNBIND_WRONG_RELATIONS,
        WorkflowAction.EXPORT,
        WorkflowAction.QUERY,
        WorkflowAction.MANUAL_CHECK,
    ]
    def analyze(self, records): ...  # R3.5 占位，R4.5 实现真实规则
    def get_knowledge_refs(self): ...
```

> **R3.5 注意：** analyze() 在 R3.5 阶段只返回占位响应（全部 MANUAL_CHECK），与 work_02 占位对称。真实规则引擎在 R4.5 实现。
>
> **编码缺失处理：** sars_monitor_code 已改为非必填（`str = ""`）。编码未映射或 sars_monitor_code 为空时，不能在 Schema 层拒绝，应由后续占位/规则逻辑返回 MANUAL_CHECK。

### 4.4 新增 work_03 占位规则引擎文件

**R3.5 新增：** `app/workflows/work_03_same_product_shortage.py`（占位骨架，全部返回 MANUAL_CHECK）

**R4.5 实现：** 真实规则引擎，详见第 12 章。

### 4.5 新增 SupplementSameProductRecord Schema

work_03 的输入数据结构与 work_01/02 不同，必须新增。详见第 8 章。

### 4.6 新增 work_03 API 端点（占位）

必须在 `app/main.py` 中新增：

```
POST /workflows/same-product-shortage/analyze
```

调用 `SupplementSameProductWorkflow.analyze()`，与 work_01/02 端点对称。R3.5 阶段返回占位响应。

### 4.7 新增 SupplementSameProductAnalyzeRequest

必须在 `app/schemas/workflow.py` 中新增：

```python
class SupplementSameProductAnalyzeRequest(StrictModel):
    scope: WorkflowScope
    records: list[SupplementSameProductRecord] = Field(..., min_length=1)
    export_format: ExportFormat | None = None  # R3.5 不实现 Excel 导出，留到 R5
```

---

## 5. 哪些地方建议改

### 5.1 shared_rules.py 提取（R4/R4.5 之后可选重构项，不纳入 R3.5）

**说明：** shared_rules.py 只作为 R4/R4.5 之后的可选重构项，不纳入 R3.5 必做范围。

R3.5 必须保证：
- 不修改 work_01 规则逻辑
- 不修改 work_01 规则顺序
- 不迁移 work_01 内部函数
- 不影响 work_01 315 条测试

**未来可选：** 将 work_01 中的 `_is_valid_candidate`、`_normalize_mall`、`_has_valid_price`、去一物多码逻辑、去极值逻辑提取到 `app/workflows/shared_rules.py`。

**影响范围：** work_01 内部改为从 shared_rules import，逻辑不变，测试不变。work_02/work_03 直接引用 shared_rules。

**风险：** 低。纯函数搬迁，不改逻辑。但需要验证 work_01 的 315 tests 全部通过。此重构建议在 R4 或 R4.5 阶段视需要实施，不阻塞 R3.5。

### 5.2 work_02 占位 reason 更新

**建议：** 将 work_02 的 reason 从 "R2 only: work_02 business rules not implemented yet" 更新为 "R3.5: work_02 规则引擎将在 R4 实现，当前阶段请使用 work_01 或 work_03"。

**理由：** 当前 reason 暗示只有 R2，但实际已进入 R3.5 阶段。

### 5.3 router confidence 阈值标注

**建议：** 在 router.py 中添加注释说明 confidence 阈值的选择理由，特别是 work_03 从 0.85 提升到 0.9 的原因。

### 5.4 work_01 evidence 增强

**建议：** 在 work_01 的 evidence 中增加 same_product_type 信息（参考链接/互联网），不参与规则判定但丰富证据链。

**优先级：** P3，可在 R5+ 阶段实施。

---

## 6. 哪些地方后续阶段再改

| 改动 | 说明 | 建议阶段 |
|---|---|---|
| Excel/CSV 读写 | 文件安全校验复杂，需独立阶段 | R5 |
| 真实数据库只读查询 | execution_sql_policy + 只读权限 | R6 |
| 模型匹配接入 | 不接模型，仅建议人工操作 | R7+（需独立安全审查） |
| zhImport 实际生成 | 需 Excel 导出能力 | R5 |
| 目录迁移 | 旧模块从 app/agent 迁移到 app/core | R7（需完整回归） |
| work_04~08 实现 | 6项工作流的详细规则 | R8+ |
| same_product_type 参与规则 | 当前不影响判定，未来可细化 | R5+ |
| 批量审批摘要统计 | summary 中增加来源占比 | R5+ |
| 型号不一致独立规则 | 需模型辅助判断 | R7+ |
| 品牌清洗自动执行 | 需品牌词库或模型 | R7+ |
| 搜索词自动采集 | 需接入SaaS采集API | R8+ |

---

## 7. workflow_knowledge_adapter 调整方案

### 7.1 当前状态

`app/services/workflow_knowledge_adapter.py` 现状：

- `ALLOWED_WORKFLOW_KNOWLEDGE_FILES`：15 个文件白名单，已包含 `cnnc_work_03_supplement_same_product.md`
- `_WORKFLOW_KNOWLEDGE_MAP`：3 个条目（work_01、work_02、diagnose），**缺少 work_03**
- `get_workflow_knowledge_files()`：根据 workflow_name 从 MAP 中查找
- `is_allowed_knowledge_file()`：检查文件名是否在白名单中

### 7.2 必须变更

**新增 work_03 知识映射条目：**

```python
_WORKFLOW_KNOWLEDGE_MAP: dict[str, list[str]] = {
    "work_01_same_product_approval": [
        "cnnc_work_01_same_product_approval.md",
        "cnnc_base_scope.md",
        "cnnc_forbidden_actions.md",
    ],
    "work_02_price_index_feedback": [
        "cnnc_work_02_price_index_feedback.md",
        "cnnc_sars_import_rules.md",
        "cnnc_base_scope.md",
        "cnnc_forbidden_actions.md",
    ],
    "work_03_same_product_shortage": [
        "cnnc_work_03_supplement_same_product.md",
        "cnnc_sars_import_rules.md",
        "cnnc_base_scope.md",
        "cnnc_forbidden_actions.md",
    ],
    "diagnose": [
        "cnnc_sql_diagnosis_knowledge.md",
        "cnnc_base_scope.md",
        "cnnc_forbidden_actions.md",
    ],
}
```

**关键决策：work_03 引用 cnnc_sars_import_rules.md**

理由：work_03 补同款的最终步骤是制作 zhImport 模板导入 SaaS，与 work_02 共享导入规则知识。如果不引用，Agent 无法在建议输出中标注正确的模板字段要求。

### 7.3 不变更的部分

- `ALLOWED_WORKFLOW_KNOWLEDGE_FILES` 不变（已包含 work_03 文件名）
- `get_workflow_knowledge_files()` 函数签名和逻辑不变
- `is_allowed_knowledge_file()` 函数不变
- diagnose 知识映射不变

### 7.4 测试要求

- 新增测试：`get_workflow_knowledge_files("work_03_same_product_shortage")` 返回 4 个文件
- 新增测试：`get_workflow_knowledge_files("work_03_nonexistent")` 返回空列表
- 回归测试：work_01/work_02/diagnose 的知识映射不受影响

---

## 8. schema 扩展方案

### 8.1 新增 Schema 列表

| Schema | 用途 | 章节归属 |
|---|---|---|
| SupplementSameProductRecord | work_03 输入记录 | 本章 |
| BrandInfo | 品牌清洗信息 | 本章 |
| SearchTermConfig | 搜索词配置 | 本章 |
| CollectionConfig | 采集任务配置 | 本章 |
| ImportTemplateField | zhImport 模板字段 | 本章 |
| SupplementSameProductAnalyzeRequest | work_03 分析请求 | 本章 |
| ManualOperationNode | 人工操作节点 | 第9章 |

### 8.2 SupplementSameProductRecord

work_03 的输入数据与 work_01/02 不同。work_03 面向的是"已知同款不足的商品"，需要携带已有同款信息和品牌/型号结构化数据。

```python
class SupplementSameProductRecord(StrictModel):
    scope: WorkflowScope
    sars_monitor_code: str = ""
    standard_commodity_code: str = ""
    variant_code: str = ""
    commodity_name: str = ""
    current_same_count: int = 0
    current_valid_same_count: int = 0
    current_same_products: list[SameProductCandidate] = []
    reference_price: Decimal | None = None
    current_index_price: Decimal | None = None
    brand_info: BrandInfo | None = None
    existing_mall_codes: list[str] = []
    need_code_mapping: bool = False
    mapped_sars_monitor_code: str = ""
    current_status: str = ""
```

**字段说明：**

| 字段 | 类型 | 含义 |
|---|---|---|
| sars_monitor_code | str | SaaS监控商品编码，非必填，编码未映射时为空；后续导入的核心标识 |
| standard_commodity_code | str | 中核商城标准商品编码 |
| current_same_count | int | 当前已有同款总数（含无效） |
| current_valid_same_count | int | 当前有效同款数（仅供参考；当 current_same_products 非空时必须从明细派生；当明细为空时此值不能直接用于判定，需人工核查） |
| current_same_products | list[SameProductCandidate] | 当前已有同款明细（复用已有 Schema） |
| reference_price | Decimal or None | 参考价/标准商品价格 |
| current_index_price | Decimal or None | 当前价格指数 |
| brand_info | BrandInfo or None | 品牌结构化信息 |
| existing_mall_codes | list[str] | 已有同款的商城编码列表，用于判断需要补充哪些商城 |
| need_code_mapping | bool | 是否需要编码映射 |
| mapped_sars_monitor_code | str | 映射后的 SaaS 监控商品编码 |

### 8.3 BrandInfo

```python
class BrandInfo(StrictModel):
    original_brand: str = ""
    cleaned_brand: str = ""
    brand_source: str = ""
    model: str = ""
    specification: str = ""
```

| 字段 | 含义 | 示例 |
|---|---|---|
| original_brand | 原始品牌字段 | "贝米普(BMP)" |
| cleaned_brand | 清洗后中文主品牌 | "贝米普" |
| brand_source | 品牌来源 | "品牌-型号-名称_汇总.xlsx" |
| model | 型号 | "DN20" |
| specification | 规格参数 | "20mm 铜阀门" |

### 8.4 SearchTermConfig

```python
class SearchTermConfig(StrictModel):
    full_search_term: str = ""
    concise_search_term: str = ""
    fallback_search_term: str = ""
    primary_term: str = ""
```

| 字段 | 含义 | 示例 |
|---|---|---|
| full_search_term | 完整搜索词（品牌+名称+型号） | "贝米普 铜阀门 DN20" |
| concise_search_term | 精简搜索词（品牌_处理后+型号） | "贝米普 DN20" |
| fallback_search_term | 兜底搜索词（仅型号） | "DN20" |
| primary_term | 实际使用的主搜索词 | "贝米普 DN20" |

### 8.5 CollectionConfig

```python
class CollectionConfig(StrictModel):
    data_source: str = ""
    recall_count: int = 60
    recall_threshold: int = 60
    suggest_enable_platform_model_matching: bool = True
    mall_scope: str = ""
```

| 字段 | 含义 | 默认值 |
|---|---|---|
| data_source | 采集数据源 | "ES互联网" |
| recall_count | 召回数量 | 60 |
| recall_threshold | 召回阈值 | 60 |
| suggest_enable_platform_model_matching | 建议人工在 SaaS 采集任务配置中勾选"启用模型同款匹配"，Agent 不调用模型 | True |
| mall_scope | 采集商城范围 | "WBB1" |

### 8.6 ImportTemplateField

```python
class ImportTemplateField(StrictModel):
    standard_commodity_code: str = ""
    commodity_name: str = ""
    document_id: str = ""
    internet_title: str = ""
    url: str = ""
    sku_id: str = ""
    supplier_code: str = ""
    supplier_name: str = ""
    shop_name: str = ""
    price: Decimal | None = None
    comment_count: int = 0
```

对应 MD 中的 zhImport 模板 12 个字段：

| ImportTemplateField 字段 | MD 模板字段 | 说明 |
|---|---|---|
| standard_commodity_code | 标准商品编码 | 中核标准编码 |
| commodity_name | 商品名称/标品名称 | 监控商品名称 |
| document_id | 单据id | SaaS监控商品编码 |
| internet_title | 互联网标题 | 召回商品标题 |
| url | URL | 同款链接 |
| sku_id | SKUID | 平台商品ID |
| supplier_code | 供应商编码 | 商城编码（如 jd） |
| supplier_name | 供应商名称 | 商城名称 |
| shop_name | 店铺名称 | 店铺名称 |
| price | 价格 | 页面核验价格 |
| comment_count | 评论数 | 页面评论数 |

### 8.7 SupplementSameProductAnalyzeRequest

```python
class SupplementSameProductAnalyzeRequest(StrictModel):
    scope: WorkflowScope
    records: list[SupplementSameProductRecord] = Field(..., min_length=1)
    export_format: ExportFormat | None = None  # R3.5 不实现 Excel 导出，留到 R5
```

### 8.8 不新增的 Schema

| 不新增 | 理由 |
|---|---|
| SupplementSameProductCandidate | 复用已有的 SameProductCandidate |
| SupplementSameProductResult | 复用已有的 WorkflowRecordResult |
| SupplementSameProductResponse | 复用已有的 WorkflowAnalyzeResponse |

**核心决策：work_03 的输出复用 work_01/work_02 已有的通用 Schema（WorkflowRecordResult + WorkflowAnalyzeResponse），不发明新的输出格式。** 这保证 human_approval_guard、scope_guard 等拦截层可以不加修改地应用于 work_03。

### 8.9 Schema 新增对已有代码的影响

| 影响点 | 变更类型 | 风险 |
|---|---|---|
| app/schemas/workflow.py | 新增 6 个 class | 低（纯新增，不改已有 class） |
| app/schemas/__init__.py | 可能需要更新 export | 低 |
| app/main.py | 新增 import + 端点 | 低（纯新增路由） |
| app/workflows/base.py | 新增 SupplementSameProductWorkflow | 低（纯新增 registry 条目） |
| app/schemas/workflow.py | WorkflowRecordResult 新增 manual_operation_nodes 字段 | 中（影响已有 Schema，但默认空列表不破坏现有测试） |
| app/workflows/human_approval_guard.py | 遍历 manual_operation_nodes 校验 | 中（新增校验逻辑，但只对含节点的结果生效） |

### 8.10 PriceFeedbackRecord 扩展方案

当前 PriceFeedbackRecord 已有 18 个字段。根据 work_02 MD 分析，需要扩展以下字段：

| 字段 | 类型 | 加入阶段 | 说明 |
|---|---|---|---|
| feedback_date | str = "" | **R3.5** | 辅助日清追踪，不参与规则判定 |
| feedback_reason | str = "" | **R3.5** | feedback_type + feedback_detail 的精简汇总，不参与规则判定 |
| feedback_price | Decimal \| None = None | **R4** | 参与价格偏差判定规则（W11/W12/W15） |
| code_mapping_status | str = "" | **R4** | 编码映射状态输出（"mapped"/"unmapped"/"same"） |
| customer_reply_draft | str = "" | **R4** | 客户反馈话术，由规则生成，不自动发送 |

**R3.5 加入的字段：** feedback_date、feedback_reason（两个纯输入辅助字段，不影响已有测试）

**R4 加入的字段：** feedback_price、code_mapping_status、customer_reply_draft（参与规则判定和输出）

**不加的字段：** mapped_saas_monitor_code（已存在）、manual_operation_nodes（统一放在 WorkflowRecordResult.manual_operation_nodes，不在 PriceFeedbackRecord 上重复。work_02 的人工操作步骤由规则引擎输出到 WorkflowRecordResult.manual_operation_nodes）

### 8.11 Schema 变更对已有代码的影响汇总

| 变更 | 影响文件 | 向后兼容 | 说明 |
|---|---|---|---|
| 新增 7 个 Schema class | workflow.py | 是 | 纯新增，不改已有 class |
| WorkflowRecordResult 增加 manual_operation_nodes | workflow.py | 是 | 默认空列表，不破坏现有序列化/反序列化 |
| PriceFeedbackRecord 增加 feedback_date/feedback_reason | workflow.py | 是 | 默认空字符串，StrictModel extra="forbid" 不影响 |
| human_approval_guard 遍历 manual_operation_nodes | human_approval_guard.py | 是 | 只对含节点的结果生效，空列表不触发 |

---

## 9. ManualOperationNode 设计

### 9.1 背景

work_02 和 work_03 都涉及多步人工操作流程。例如 work_02 的"解绑错款→导入新同款→重算→推送"，work_03 的"SQL筛查→编码匹配→品牌清洗→搜索词→采集→模型匹配→人工复核→去一物多码→去极值→制作模板→导入→等待计算"。

Agent 不执行这些步骤，但需要**结构化地表达每一步人工操作的内容、前置条件、预期结果和注意事项**。

### 9.2 ManualOperationNode 定义

```python
class ManualOperationNode(StrictModel):
    step: int
    title: str
    description: str
    prerequisite: str = ""
    expected_outcome: str = ""
    safety_notice: str = ""
    related_action: WorkflowAction | None = None
    is_executable_by_agent: bool = False
    execution_allowed: bool = False
    executed: bool = False
```

| 字段 | 含义 | 示例 |
|---|---|---|
| step | 步骤序号 | 1 |
| title | 步骤标题 | "执行SQL筛查同款不足商品" |
| description | 步骤描述 | "在 saas_onecode_price 数据库执行筛查SQL..." |
| prerequisite | 前置条件 | "需要 saas_onecode_price 只读访问权限" |
| expected_outcome | 预期结果 | "导出同款不足商品清单" |
| safety_notice | 安全提示 | "此SQL为只读查询，不修改数据" |
| related_action | 关联的 WorkflowAction | QUERY |
| is_executable_by_agent | Agent 是否可自动执行 | False（当前阶段全部为 False） |
| execution_allowed | 是否允许执行 | False（永远为 False） |
| executed | 是否已执行 | False（永远为 False） |

### 9.3 使用方式

ManualOperationNode 作为 WorkflowRecordResult 的一等公民字段输出，不放入 summary dict 或 evidence 字符串。

**Schema 变更：**

```python
class WorkflowRecordResult(StrictModel):
    # ... 现有字段不变 ...
    manual_operation_nodes: list[ManualOperationNode] = []  # 新增
```

**human_approval_guard 增强：**

遍历每条 record_result 的 manual_operation_nodes，校验：
- `is_executable_by_agent` 必须为 False
- `execution_allowed` 必须为 False
- `executed` 必须为 False
- 如果发现违规节点，强制修正为安全值

**使用示例：**

```python
result = WorkflowRecordResult(
    standard_commodity_code="SP001",
    primary_action=WorkflowAction.SUPPLEMENT_SAME_PRODUCT,
    suggested_actions=[WorkflowAction.SUPPLEMENT_SAME_PRODUCT],
    reason="同款不足，需补同款",
    rule_id="S02_ZERO_VALID",
    evidence=["valid_same_count=0", "缺少同款明细，需人工核查"],
    manual_operation_nodes=[
        ManualOperationNode(step=1, title="SQL筛查同款不足商品", is_executable_by_agent=False, ...),
        ManualOperationNode(step=2, title="匹配编码关系表", is_executable_by_agent=False, ...),
    ],
)
```

### 9.4 关键约束

- `is_executable_by_agent`、`execution_allowed`、`executed` 在 R3.5/R4/R5 阶段**必须为 False**
- 所有 ManualOperationNode 只表示"建议人工操作"，不表示 Agent 会执行
- 不新增 action executor
- 不新增"人工确认后自动执行"路径
- `manual_operation_nodes` 作为 WorkflowRecordResult 的一等公民字段，必须经过 human_approval_guard 遍历校验
- human_approval_guard 发现任何 `is_executable_by_agent=True` 或 `execution_allowed=True` 或 `executed=True` 的节点时，强制修正为 False
- WorkflowAnalyzeResponse 不需要额外承载 manual_operation_nodes，因为已包含在每条 record_result 中

### 9.5 work_03 典型 ManualOperationNode 序列

| step | title | related_action | is_executable_by_agent |
|---|---|---|---|
| 1 | SQL筛查同款不足商品 | QUERY | False |
| 2 | 匹配编码关系表 | MANUAL_CHECK | False |
| 3 | 整理品牌/名称/型号 | MANUAL_CHECK | False |
| 4 | 生成搜索词 | MANUAL_CHECK | False |
| 5 | 创建采集任务 | MANUAL_CHECK | False |
| 6 | 召回候选审核 | MANUAL_CHECK | False |
| 7 | 去一物多码 | UNBIND_WRONG_RELATIONS | False |
| 8 | 去极值 | MANUAL_CHECK | False |
| 9 | 制作导入模板 | IMPORT_CUSTOMER_LINKS | False |
| 10 | 导入SaaS | IMPORT_CUSTOMER_LINKS | False |
| 11 | 等待周三价格指数计算 | WAIT_COLLECTION | False |

---

## 10. work_01 对齐方案

### 10.1 对齐评估

work_01（14条规则）与 work_01 MD 的对齐评估结果：

| 维度 | 对齐程度 | 说明 |
|---|---|---|
| 核心审批规则 | 100% | 至少2条/不同商城/链接有效/上架/价格一致/去一物多码/去极值 |
| 状态流转规则 | 100% | 同款匹配中/校验失败 |
| 价格偏离判定 | 100%（R3决策） | 仅判>1.3倍，不判<0.7倍 |
| 参考链接处理 | 100% | same_product_type不影响规则判定 |
| 异常处理 | 100% | 链接失效/下架/价格缺失/价差过大 |

### 10.2 无需变更

work_01 当前实现已完全覆盖 MD 核心规则，**不需要任何修改**。

具体确认：

1. R01_WAIT_CRAWLER 覆盖 MD "同款匹配中不要推送"
2. R12_VALIDATE_FAILED 覆盖 MD "校验失败需人工核查"
3. R02_NO_CANDIDATES + R03_VALID_COUNT_ZERO 覆盖 MD "0条有效同款"
4. R04_VALID_COUNT_ONE 覆盖 MD "仅1条有效同款"
5. R05_SAME_MALL_DUPLICATE 覆盖 MD "同一商城不能同时作为最终两条同款"
6. R06A/R06B 覆盖 MD "链接可访问、商品上架"
7. R07_PRICE_INCONSISTENT 覆盖 MD "页面价格与SaaS返回价格需一致"
8. R08_PRICE_INVALID 覆盖 MD "价格异常价驳回"
9. R09_REF_PRICE_TOO_HIGH 覆盖 MD "高于参考价约30%以上需谨慎"
10. R10_PRICE_DIFF_TOO_LARGE 覆盖 MD "价差异常"
11. R13_APPROVE 覆盖 MD "满足条件后审批通过"

### 10.3 可选增强（不在 R3.5 范围内）

| 增强项 | 说明 | 优先级 | 建议阶段 |
|---|---|---|---|
| same_product_type 参与证据输出 | 在 evidence 中标注"参考链接"或"互联网"来源 | P3 | R5+ |
| 批量审批摘要统计 | 在 summary 中增加"参考链接占比"/"互联网占比" | P3 | R5+ |
| 型号/规格不一致独立规则 | 当 is_same_product=False 且有明确型号差异证据时给出具体驳回理由 | P3 | R5+ |

### 10.4 R3.5 对 work_01 的行动

**无行动。** work_01 保持 R3 产出不变。

---

## 11. work_02 R4 设计边界

### 11.1 R4 范围定义

R4 的核心任务是实现 work_02 的离线规则引擎。边界如下：

| 包含 | 不包含 |
|---|---|
| work_02 完整规则引擎（12-16条规则） | Excel/CSV 读取和导出 |
| 编码映射检查规则 | 真实数据库查询 |
| 反馈链接校验规则 | 模型匹配 |
| 动作链判定（UNBIND→RECALCULATE→PUSH） | 自动执行任何动作 |
| 典型场景规则（价格太高/太低） | zhImport 模板文件实际生成 |
| MANUAL_VERIFY 默认升级为规则驱动的判定 | 自动导入 SaaS |
| 客户话术建议（通过 evidence 输出） | 客户反馈消息发送 |

### 11.2 work_02 规则引擎设计要点

**编码映射检查：**

| # | 条件 | 动作 | rule_id |
|---|---|---|---|
| W01 | standard_commodity_code 和 variant_code 均为空 | MANUAL_VERIFY | W01_NO_CODE |
| W02 | need_code_mapping=True 且 mapped_sars_monitor_code 为空 | MANUAL_VERIFY | W02_CODE_UNMAPPED |
| W03 | mapped_sars_monitor_code 不为空且与 sars_monitor_code 相同 | MANUAL_VERIFY | W03_CODE_SAME |

**反馈链接校验：**

| # | 条件 | 动作 | rule_id |
|---|---|---|---|
| W04 | 客户提供0条链接且有效同款<2 | SUPPLEMENT_SAME_PRODUCT + WAIT_COLLECTION | W04_NO_LINK_INSUFFICIENT |
| W05 | 客户提供>=1条链接，其中 is_valid_link=False | ASK_CUSTOMER_RECHECK | W05_INVALID_LINK |
| W06 | 客户提供>=1条链接，其中 is_same_product=False | UNBIND_WRONG_RELATIONS + ASK_CUSTOMER_RECHECK | W06_NOT_SAME_PRODUCT |
| W07 | 客户提供>=2条有效链接来自不同商城价差合理 | IMPORT_CUSTOMER_LINKS + RECALCULATE_INDEX + PUSH_INDEX | W07_LINKS_OK |

**当前同款检查：**

| # | 条件 | 动作 | rule_id |
|---|---|---|---|
| W08 | 当前系统同款有明显错款（is_same_product=False） | UNBIND_WRONG_RELATIONS | W08_WRONG_SAME_PRODUCT |
| W09 | 当前有效同款<2 | SUPPLEMENT_SAME_PRODUCT | W09_INSUFFICIENT_SAME |
| W10 | 当前同款存在高溢价（>参考价*1.3） | UNBIND_WRONG_RELATIONS + MANUAL_VERIFY | W10_HIGH_PREMIUM |

**价格偏差判定：**

| # | 条件 | 动作 | rule_id |
|---|---|---|---|
| W11 | feedback_type=价格偏高，偏差>30%，有客户有效链接 | IMPORT_CUSTOMER_LINKS + UNBIND_WRONG_RELATIONS + RECALCULATE_INDEX | W11_PRICE_HIGH_WITH_LINK |
| W12 | feedback_type=价格偏高，偏差>30%，无客户链接 | MANUAL_VERIFY | W12_PRICE_HIGH_NO_LINK |
| W13 | feedback_type=价格偏低 | MANUAL_VERIFY（检查是否绑了非同款） | W13_PRICE_LOW |
| W14 | feedback_type=异常波动 | MANUAL_VERIFY | W14_VOLATILITY |
| W15 | 偏差<=10% | ASK_CUSTOMER_RECHECK | W15_MINOR_DIFF |
| W16 | 关联同款审批问题 | FORWARD_TO_WORK01 | W16_FORWARD_WORK01 |
| W99 | 兜底 | MANUAL_VERIFY | W99_FALLBACK |

### 11.3 与 work_01 的共享逻辑

| 共享逻辑 | work_01 中的位置 | R4 处理方式 |
|---|---|---|
| 有效同款判定 | _is_valid_candidate | R4 阶段可考虑提取到 shared_rules.py（可选重构项，非 R3.5 必做）或在 work_02 中独立实现同逻辑函数 |
| 商城去重 | _normalize_mall + _derive_metrics | 同上 |
| 去极值阈值 | HIGH_PRICE_RATIO=1.3 | 同上 |
| 价差阈值 | PRICE_DIFF_THRESHOLD=0.3 | 同上 |

### 11.4 work_02 输出文件（R4 离线版仅输出 JSON）

R4 阶段 work_02 的输出通过 WorkflowAnalyzeResponse.record_results 的 evidence 字段携带结构化建议。不生成实际 Excel 文件（R5 才做）。

evidence 示例：

```
[
    "action_chain: IMPORT_CUSTOMER_LINKS -> UNBIND_WRONG_RELATIONS -> RECALCULATE_INDEX -> PUSH_INDEX",
    "unbind_candidates: candidate[0] mall=京东 价格=260.00 > reference_price*1.3=195.00",
    "import_template_fields_required: 标品编码,标准商品编码,商品名称,单据id,互联网标题,URL,SKUID,供应商编码,供应商名称,价格,评论数",
    "customer_reply_suggestion: 已核实您提供的链接，建议替换原高价同款后重新计算价格指数",
    "risk_notice: 编码映射需确认，不能直接用标准商品编码当SaaS监控商品编码导入",
]
```

---

## 12. work_03 独立 workflow 设计

### 12.1 为什么 work_03 必须独立

| 维度 | work_01 | work_03 | 差异 |
|---|---|---|---|
| 触发条件 | 新增监控商品自动进入审批 | 每周主动筛查同款不足商品 | 完全不同 |
| 输入结构 | SameProductRecord | SupplementSameProductRecord | 字段差异大 |
| 流程步骤 | 审批→推送 | 筛查→匹配→清洗→搜索→采集→复核→去重→去极值→模板→导入→等待 | work_03 步骤远多于 work_01 |
| 核心能力 | 规则判定 | 流程引导+SQL生成+模板生成 | 本质不同 |
| 频率 | 每日 | 每周 | 不同 |
| 共享逻辑 | 有效同款判定/去一物多码/去极值 | 复用 work_01 的有效同款判定/去一物多码/去极值 | 共享逻辑层 |

### 12.2 work_03 规则引擎（R4.5 实现）

> **重要：** 本节描述的规则引擎在 R3.5 阶段不实现。R3.5 只创建占位 workflow（全部返回 MANUAL_CHECK）。真实规则在 R4.5 阶段实现。

work_03 的规则引擎逻辑与 work_01 有本质区别：work_01 是"给定数据做判定"，work_03 是"给定不足状态做流程引导"。

**有效同款数派生规则（修订 5）：**

- 如果 `current_same_products` 不为空，`valid_same_count` / `valid_mall_count` 必须从 `current_same_products` 派生（复用 `_is_valid_candidate` 等逻辑）
- 如果只有 `current_valid_same_count` 汇总值，没有明细（`current_same_products` 为空），则 `current_valid_same_count` 只能作为外部参考，不能直接判定 ready/import，必须在 evidence 中标注"缺少同款明细，需人工核查"

**编码缺失/未映射规则（修订 4）：**

| # | 条件 | 动作 | rule_id | 说明 |
|---|---|---|---|---|
| S01a | need_code_mapping=False 且 sars_monitor_code 为空 | MANUAL_CHECK | S01A_CODE_MISSING | 编码缺失，无法定位 SaaS 监控商品 |
| S01b | need_code_mapping=True 且 mapped_sars_monitor_code 为空 | MANUAL_CHECK | S01B_CODE_UNMAPPED | 编码未映射，不能直接导入 |
| S02 | current_valid_same_count=0 | SUPPLEMENT_SAME_PRODUCT + EXPORT | S02_ZERO_VALID | 需全新采集至少2条不同商城同款 |
| S03 | current_valid_same_count=1 | SUPPLEMENT_SAME_PRODUCT + EXPORT | S03_ONE_VALID | 需补充至少1条不同商城同款 |
| S04 | current_valid_same_count>=2 但 valid_mall_count<2 | UNBIND_WRONG_RELATIONS + SUPPLEMENT_SAME_PRODUCT | S04_SAME_MALL | 去一物多码+补不同商城同款 |
| S05 | current_valid_same_count>=2 且存在高溢价（>参考价*1.3） | UNBIND_WRONG_RELATIONS + SUPPLEMENT_SAME_PRODUCT | S05_HIGH_PREMIUM | 剔除高价+补入合理价 |
| S06 | brand_info 为空 | MANUAL_CHECK | S06_NO_BRAND_INFO | 需人工整理品牌/名称/型号 |
| S07 | 补同款完成后 | IMPORT_CUSTOMER_LINKS + EXPORT | S07_READY_TO_IMPORT | 生成zhImport模板 |
| S99 | 兜底 | MANUAL_CHECK | S99_FALLBACK | 需人工核查 |

### 12.3 work_03 的 SQL 建议生成（R4.5 预留设计）

> **R3.5 不实现、不输出 SQL 建议。** 以下为 R4.5 预留设计。

work_03 MD 中包含完整的筛查 SQL（第 2 节）。R4.5 阶段，Agent 可将此 SQL 作为建议输出到 evidence 中，但不执行。

**SQL 来源：** MD 第 2 节的完整 SQL 语句。

**输出方式：** 在 evidence 中标注：

```
"suggested_sql: 参见 cnnc_work_03_supplement_same_product.md 第2节",
"sql_tables: saas_onecode_price.commodity_pool_188605, saas_onecode_price.commodity_pool_relation_188605, saas_onecode_price.monitor_commodity_price_last_change_188605",
"sql_purpose: 筛查同款不足商品（total < 2）",
    "safety_notice: 此SQL为只读查询，不修改数据。当前阶段不执行SQL；未来R6如支持同款不足筛查只读查询，必须经过只读账号、execution_sql_policy、LOCAL_EXEC_TOKEN、本地访问和Codex单独安全审查",
```

**关键约束：**

- R3.5 阶段不输出 SQL 建议
- R4.5 阶段才输出 SQL 建议，且仅作为建议输出
- 需要 R6+ 接入只读数据库后才可能执行
- 执行前必须通过 execution_sql_policy 校验

### 12.4 work_03 的品牌清洗建议（R4.5 预留设计）

> **R3.5 不实现、不输出品牌清洗建议。** 以下为 R4.5 预留设计。

MD 第 4 节描述了品牌清洗规则。R4.5 阶段，Agent 可根据输入的 brand_info.original_brand 判断是否需要清洗，并在 evidence 中输出建议。

**判断逻辑：**

```python
def _needs_brand_cleaning(brand_info: BrandInfo | None) -> bool:
    if brand_info is None:
        return True
    if not brand_info.cleaned_brand and brand_info.original_brand:
        return True
    return False
```

**输出方式：** evidence 中标注：

```
"brand_cleaning_needed: True",
"original_brand: 贝米普(BMP)",
"suggested_cleaned_brand: 贝米普",
"cleaning_rule: 当品牌同时包含中文和英文时，提取中文字符作为处理后品牌",
```

### 12.5 work_03 的搜索词生成建议（R4.5 预留设计）

> **R3.5 不实现、不输出搜索词建议。** 以下为 R4.5 预留设计。

MD 第 4 节描述了三类搜索词。R4.5 阶段，Agent 可根据 brand_info 和 commodity_name 生成搜索词建议。

**生成逻辑（纯字符串处理，不接模型）：**

```python
def _generate_search_terms(brand_info: BrandInfo | None, commodity_name: str, model: str) -> SearchTermConfig:
    brand = brand_info.cleaned_brand if brand_info and brand_info.cleaned_brand else ""
    full = f"{brand} {commodity_name} {model}".strip()
    concise = f"{brand} {model}".strip()
    fallback = model.strip()
    primary = concise if concise else full if full else fallback
    return SearchTermConfig(
        full_search_term=full,
        concise_search_term=concise,
        fallback_search_term=fallback,
        primary_term=primary,
    )
```

**输出方式：** evidence 中标注搜索词。

### 12.6 work_03 的导入模板建议（R4.5 预留设计）

> **R3.5 不实现、不输出导入模板建议。** 以下为 R4.5 预留设计。

MD 第 8 节描述了 zhImport 模板字段。R4.5 阶段，Agent 可根据补同款候选生成模板字段建议。

**输出方式：** evidence 中标注模板字段列表和填写要求。

### 12.7 work_03 API 端点设计

```
POST /workflows/same-product-shortage/analyze
```

**请求体：** SupplementSameProductAnalyzeRequest

**响应体：** WorkflowAnalyzeResponse（复用通用响应）

**端点逻辑：**

1. check_local_access
2. validate_request_scope
3. validate_records_scope（每条记录 scope 与请求 scope 一致）
4. 调用 SupplementSameProductWorkflow.analyze()
5. enforce_recommendation_only
6. 返回响应

### 12.8 work_03 与 work_01 的关系

- work_03 独立运行，不依赖 work_01
- work_03 可以引用 work_01 的 _is_valid_candidate / _normalize_mall / _has_valid_price 逻辑
- work_03 不引用 work_01 的规则判定逻辑（evaluate_record）
- 两者共享的是"有效同款判定"的基础函数，不是业务规则

### 12.9 work_03 R3.5 实现范围

| 包含（R3.5 占位） | 不包含（R4.5 实现） |
|---|---|
| SupplementSameProductWorkflow 占位类 + registry 注册 | work_03 真实规则引擎（S01-S99） |
| 占位 API 端点（全部返回 MANUAL_CHECK） | Excel/CSV 文件读写 |
| Schema 定义（SupplementSameProductRecord 等） | 真实数据库查询 |
| ManualOperationNode Schema 定义 | SQL 只读查询执行（R6 需单独安全审查） |
| 知识映射条目 | 模型匹配 |
| | 实际生成 zhImport.xlsx |

---

## 13. workflow router 触发词调整

### 13.1 当前触发词配置

`app/workflows/router.py` 中 `_ROUTE_RULES`：

| workflow_name | 触发词 | base_confidence |
|---|---|---|
| work_01_same_product_approval | 同款审批、新增监控、同款匹配 | 0.9 |
| work_02_price_index_feedback | 价格异常、勘误、反馈、价格偏高、价格偏低、客户反馈 | 0.9 |
| work_03_same_product_shortage | 同款不足、补同款、无同款 | 0.85 |
| work_04_failed_bus | 失败班车、处理失败 | 0.85 |
| work_05_weekly_index_update | 价格指数更新、周度计算 | 0.85 |
| work_06_index_fluctuation | 波动、正负10%、异常波动 | 0.85 |
| work_07_push_check | 推送、推送失败、推送后核查 | 0.85 |
| work_08_ad_hoc（默认） | 任意 | 0.4 |

### 13.2 必须调整

1. **work_03 workflow_name 保留：** `work_03_same_product_shortage` 不变，通过知识映射关联文件

### 13.3 建议调整

2. **work_03 触发词增强：** 增加"补同款流程"、"同款不足筛查"、"周一补同款"、"周三前补同款"等高频业务用语

3. **work_03 confidence 提升：** 从 0.85 调整到 0.90，因为 work_03 的触发场景非常明确，与 work_01 不易混淆

调整后：

```python
_ROUTE_RULES: list[tuple[list[str], str, float]] = [
    (["同款审批", "新增监控", "同款匹配"], "work_01_same_product_approval", 0.9),
    (["价格异常", "勘误", "反馈", "价格偏高", "价格偏低", "客户反馈"], "work_02_price_index_feedback", 0.9),
    (["同款不足", "补同款", "无同款", "补同款流程", "同款不足筛查", "周一补同款", "周三前补同款"], "work_03_same_product_shortage", 0.9),
    (["失败班车", "处理失败"], "work_04_failed_bus", 0.85),
    (["价格指数更新", "周度计算"], "work_05_weekly_index_update", 0.85),
    (["波动", "正负10%", "异常波动"], "work_06_index_fluctuation", 0.85),
    (["推送", "推送失败", "推送后核查"], "work_07_push_check", 0.85),
]
```

### 13.4 触发词冲突分析

| 潜在冲突 | 分析 | 解决方案 |
|---|---|---|
| "同款"同时出现在 work_01 和 work_03 | work_01 用"同款审批/同款匹配"，work_03 用"同款不足/补同款"，不冲突 | 无需调整 |
| "推送"出现在 work_07 也可能出现在 work_01 上下文 | work_07 的"推送"指推送后核查，work_01 的推送是审批后推送 | 不易混淆 |
| "价格偏高"同时出现在 work_02 和 work_06 | work_02 是反馈处理，work_06 是波动异常，语义不同 | 不易混淆 |

---

## 14. 新的阶段规划（R3.5 / R4 / R5 / R6）

### 14.1 阶段总览

| 阶段 | 核心任务 | 前置依赖 | 安全门禁 |
|---|---|---|---|
| R3.5 | 知识库-Schema-Router-Registry对齐 + 占位 workflow | R3 通过 | R3.5-GATE |
| R4 | work_02 完整规则引擎离线版 | R3.5 通过 | R4-GATE |
| R4.5 | work_03 完整规则引擎离线版 | R4 通过 | R4.5-GATE |
| R5 | Excel/CSV 读取和导出 + 文件安全 | R4.5 通过 | R5-GATE |
| R6 | 接入个人本地只读查询能力 | R5 通过 | R6-GATE |

### 14.2 R3.5 详细计划

**目标：** 补齐知识库-Schema-Router-Registry 对齐缺口，创建 work_03 占位 workflow。

**任务清单：**

| # | 任务 | 产出 | 影响文件 |
|---|---|---|---|
| T01 | router 触发词增强 | 增加4个触发词 + confidence提升，workflow_name 保留 work_03_same_product_shortage | router.py |
| T02 | _WORKFLOW_KNOWLEDGE_MAP 增加 work_03 | 新增4文件映射（key=work_03_same_product_shortage） | workflow_knowledge_adapter.py |
| T03 | 新增 6+1 个 Schema | BrandInfo/SearchTermConfig/CollectionConfig/ImportTemplateField/SupplementSameProductRecord/SupplementSameProductAnalyzeRequest/ManualOperationNode | workflow.py |
| T04 | WorkflowRecordResult 增加 manual_operation_nodes | 新增 list[ManualOperationNode] 字段 | workflow.py |
| T05 | 新增 SupplementSameProductWorkflow 占位 | WorkflowBase 占位实现 + registry 注册 | base.py |
| T06 | 新增 work_03 占位 API 端点 | POST /workflows/same-product-shortage/analyze | main.py |
| T07 | human_approval_guard 增强 | 遍历 manual_operation_nodes 校验 | human_approval_guard.py |
| T08 | PriceFeedbackRecord R3.5 字段扩展 | feedback_date/feedback_reason（manual_operation_nodes 不属于 PriceFeedbackRecord 字段，统一放在 WorkflowRecordResult） | workflow.py |
| T09 | 新增占位/映射测试 | Schema验证 + 知识映射 + router + 占位API | tests/ |
| T10 | 回归测试验证 | 315+ 旧测试全部通过 | - |

**R3.5 约束：**

- 不接 Excel/CSV 读写
- 不接真实数据库
- 不接模型
- 不修改 work_01 任何规则
- 不修改 work_02 占位实现
- 不实现 work_03 真实规则引擎（R4.5 才实现）
- 不自动执行 SQL / 采集 / 导入 / 解绑 / 驳回 / 重算 / 推送
- 所有输出 execution_allowed=False, executed=False
- 测试用 JSON mock 数据

**预计新增测试数量：** 约 25-40 条（Schema 验证 + 知识映射 + Router + 占位 API + ManualOperationNode 校验）

**R3.5-GATE 检查项：**

1. 知识映射完整（work_01/02/03/diagnose 都有映射）
2. router work_03 名称一致（work_03_same_product_shortage）
3. SupplementSameProductRecord Schema 正确（sars_monitor_code 非必填）
4. ManualOperationNode 正确承载在 WorkflowRecordResult 上
5. human_approval_guard 校验 manual_operation_nodes
6. work_03 占位 API 端点可调用
7. 315+ 旧测试全部通过
8. 所有动作 execution_allowed=False, executed=False

### 14.3 R4 详细计划

**目标：** 实现 work_02 完整规则引擎离线版。

**任务清单：**

| # | 任务 | 产出 |
|---|---|---|
| T01 | work_02 规则引擎实现（16+1条规则） | PriceIndexFeedbackWorkflow.analyze() 实际业务逻辑 |
| T02 | 编码映射检查规则 | W01/W02/W03 |
| T03 | 反馈链接校验规则 | W04/W05/W06/W07 |
| T04 | 当前同款检查规则 | W08/W09/W10 |
| T05 | 价格偏差判定规则 | W11/W12/W13/W14/W15/W16 |
| T06 | 动作链输出 | evidence 中标注 UNBIND->RECALC->PUSH |
| T07 | 客户话术建议生成 | evidence 中输出话术草稿 |
| T08 | work_02 测试 | 规则测试 + API测试 |
| T09 | 回归测试验证 | 435+ 旧测试全部通过 |

**R4-GATE 检查项：**

1. work_02 规则覆盖 work_02 MD 核心场景
2. 典型场景（价格太高/太低）正确判定
3. 编码映射检查规则正确
4. 动作链建议正确
5. 所有动作 execution_allowed=False, executed=False

### 14.4 R4.5 详细计划

**目标：** 实现 work_03 完整规则引擎离线版。

**任务清单：**

| # | 任务 | 产出 |
|---|---|---|
| T01 | work_03 规则引擎实现（7+1条规则） | SupplementSameProductWorkflow.analyze() 实际业务逻辑 |
| T02 | 编码缺失/未映射规则 | S01A/S01B |
| T03 | 同款不足判定规则 | S02/S03/S04 |
| T04 | 高溢价/去一物多码规则 | S05/S04 |
| T05 | 品牌信息/搜索词建议 | S06 |
| T06 | 导入模板建议 | S07 |
| T07 | ManualOperationNode 步骤生成 | work_03 特有的11步流程节点 |
| T08 | 有效同款数派生逻辑 | 从 current_same_products 派生，缺少明细时标注人工核查 |
| T09 | work_03 测试 | 规则测试 + API测试 |
| T10 | 回归测试验证 | 435+ 旧测试全部通过 |

**R4.5-GATE 检查项：**

1. work_03 规则覆盖 work_03 MD 核心场景
2. 有效同款<2 时不会误判可推送
3. 缺少同款明细时 evidence 标注"需人工核查"
4. 编码缺失/未映射正确触发 MANUAL_CHECK
5. 去一物多码/去极值逻辑正确
6. ManualOperationNode 每步 is_executable_by_agent=False
7. 所有动作 execution_allowed=False, executed=False

### 14.5 R5 详细计划

**目标：** 接入 Excel/CSV 读取和导出。

| # | 任务 |
|---|---|
| T01 | excel_reader 服务（支持 xlsx/csv/json） |
| T02 | excel_writer 服务（支持 xlsx/csv/md/txt） |
| T03 | 文件安全校验（ZIP bomb/路径穿越/公式注入/大小限制） |
| T04 | work_01/02/03 导出文件生成 |
| T05 | zhImport 模板实际生成 |
| T06 | 文件下载端点 |
| T07 | 文件安全测试 |

**R5-GATE 检查项：** 恶意文件被拒绝 / 并发隔离 / 下载授权 / 草稿标识

### 14.6 R6 详细计划

**目标：** 接入个人本地只读查询能力。

| # | 任务 |
|---|---|
| T01 | 只读数据库客户端 |
| T02 | execution_sql_policy 校验 |
| T03 | SQL 只读执行 |
| T04 | 结果脱敏 |
| T05 | work_03 同款不足筛查只读查询能力（需 R6 单独安全审查） |

**R6-GATE 检查项：** 只允许 SELECT/WITH / bind_host=127.0.0.1 / LOCAL_EXEC_TOKEN / SHOW GRANTS / 结果脱敏

**R6 SQL 安全红线（即使进入 R6 也必须遵守）：**

1. 使用只读数据库账号
2. 仅允许 SELECT / WITH
3. 禁止 INSERT / UPDATE / DELETE / DROP / ALTER / TRUNCATE
4. 通过 execution_sql_policy 校验
5. 通过 LOCAL_EXEC_TOKEN
6. 绑定本地访问（bind_host=127.0.0.1）
7. 经过单独 Codex 安全审查

> **重要：R6 也不是默认允许自动执行 SQL。** work_03 的同款不足筛查查询必须经过上述 7 项安全校验后才可执行。

---

## 15. 安全边界

### 15.1 绝对安全边界（R3.5 ~ R6 全程有效）

| # | 边界 | 说明 |
|---|---|---|
| 1 | 不推倒重写 | 在现有架构上增量扩展 |
| 2 | 不移动旧模块 | 旧模块保持原位置、原接口、原测试 |
| 3 | 不破坏 /diagnose | /diagnose 功能和接口不变 |
| 4 | 不破坏 /health | /health 功能和接口不变 |
| 5 | 不破坏 R2 workflow 安全底座 | local_access_guard/scope_guard/human_approval_guard 不变 |
| 6 | 不破坏 R3 work_01 规则 | 14条规则逻辑和优先级不变 |
| 7 | 不接真实数据库 | R3.5/R4 不接，R6 需单独安全审查 |
| 8 | 不接模型 | 当前阶段不接 |
| 9 | 不自动执行 SQL | SQL 仅作为建议输出 |
| 10 | 不自动创建采集任务 | 采集任务仅作为建议输出 |
| 11 | 不自动导入 zhImport | 导入仅作为建议输出 |
| 12 | 不自动解绑 | UNBIND_WRONG_RELATIONS 仅生成建议 |
| 13 | 不自动驳回 | REJECT 仅生成建议 |
| 14 | 不自动重算 | RECALCULATE_INDEX 仅生成建议 |
| 15 | 不自动推送 | PUSH_INDEX/READY_TO_PUSH 仅生成建议 |
| 16 | 不新增 action executor | 不存在动作执行器 |
| 17 | 不新增"人工确认后自动执行"路径 | 人工确认只表示已阅读，不解锁任何系统动作 |
| 18 | 所有动作都只是建议 | 包括 APPROVE/REJECT/IMPORT/UNBIND/RECALCULATE/PUSH 等 |
| 19 | 所有输出继续 execution_allowed=false, executed=false | 永不为 true |

### 15.2 分层安全调用链（不变）

**workflow 调用链：**

```
/workflows/*
  -> local_access_guard       # L1: 本地绑定和token校验
  -> scope_guard               # L2: 问题文本范围校验
  -> workflow_scope_guard      # L3: 结构化scope和批次记录校验
  -> workflow_router           # L4: 意图路由
  -> workflow.analyze          # L5: 规则引擎分析
  -> human_approval_guard      # L6: 强制仅输出建议
  -> workflow_knowledge_adapter # 知识文件映射
```

**diagnose 调用链（不变）：**

```
/diagnose
  -> scope_guard -> safety_guard -> sql_selector -> knowledge_loader -> result_analyzer
```

### 15.3 R3.5 特有安全约束

| # | 约束 | 说明 |
|---|---|---|
| S1 | SupplementSameProductRecord 必须包含 scope | 与 work_01/02 一致，每条记录必须携带 scope |
| S2 | work_03 API 端点必须经过 local_access_guard | 与 work_01/02 端点一致 |
| S3 | work_03 输出必须经过 enforce_recommendation_only | 与 work_01/02 一致 |
| S4 | ManualOperationNode.is_executable_by_agent 必须为 False | 当前阶段不可自动执行 |
| S5 | SQL 建议不得包含写操作 | SQL 仅限 SELECT/WITH |
| S6 | human_approval_guard 必须校验 manual_operation_nodes | 遍历每个节点，确保 is_executable_by_agent=False |
| S7 | SupplementSameProductAnalyzeRequest 不默认 export_format=XLSX | R3.5 不实现 Excel 导出 |

---

## 16. 测试策略

### 16.1 R3.5 新增单元测试

| 测试对象 | 测试内容 | 预计数量 |
|---|---|---|
| SupplementSameProductRecord | Schema 验证：sars_monitor_code 非必填/类型错误/额外字段 | 5-8 |
| BrandInfo/SearchTermConfig/CollectionConfig/ImportTemplateField | Schema 验证（含 suggest_enable_platform_model_matching 字段名） | 8-10 |
| ManualOperationNode | Schema 验证 + is_executable_by_agent=False 校验 | 5-8 |
| workflow_knowledge_adapter work_03 | 新增映射条目正确性（key=work_03_same_product_shortage） | 3-5 |
| router work_03 | 触发词匹配 + confidence | 3-5 |
| work_03 占位 API 端点 | 完整调用链 + scope 校验 + local_access_guard | 5-8 |
| human_approval_guard 增强 | manual_operation_nodes 遍历校验 | 3-5 |
| PriceFeedbackRecord R3.5 字段 | feedback_date/feedback_reason 验证 | 2-3 |

### 16.2 集成测试

| 测试场景 | 内容 |
|---|---|
| work_03 占位端到端 | 输入同款不足记录 -> 路由 -> 占位分析 -> 输出 MANUAL_CHECK |
| work_03 安全拦截 | 尝试绕过 access/scope/human_approval_guard -> 被拦截 |
| 混合租户批次 | 批次中混入非中核记录 -> 整批 OUT_OF_SCOPE |
| work_01 回归 | work_01 14条规则仍全部通过 |
| work_02 回归 | work_02 占位仍返回 MANUAL_VERIFY |

### 16.2.1 work_03 API 安全测试（显式清单）

| # | 测试项 | 期望结果 |
|---|---|---|
| 1 | /workflows/same-product-shortage/analyze token 未配置时请求 | 拒绝（401/403） |
| 2 | /workflows/same-product-shortage/analyze token 错误时请求 | 拒绝（401/403） |
| 3 | /workflows/same-product-shortage/analyze token 正确且本地访问时请求 | 放行（200） |
| 4 | 非本地访问请求 | 拒绝 |
| 5 | 请求 scope 非 CNNC / 188605 / 222 | 拒绝（400 OUT_OF_SCOPE） |
| 6 | records 中混入非中核 scope | 整批拒绝（400） |
| 7 | records 为空 | 拒绝（422） |
| 8 | work_03 占位响应 primary_action | MANUAL_CHECK |
| 9 | work_03 占位响应 rule_id | R35_PLACEHOLDER |
| 10 | work_03 占位响应 execution_allowed | false |
| 11 | work_03 占位响应 executed | false |
| 12 | work_03 占位响应 needs_human_approval | true |
| 13 | /diagnose 不受 work_03 token 影响 | 正常响应 |
| 14 | /health 不受 work_03 token 影响 | 正常响应 |

保留以下测试项：

- work_03 knowledge_adapter 映射测试（key=work_03_same_product_shortage 返回 4 文件）
- work_03 router 触发词测试（"补同款"/"同款不足"/"周三前补同款" 匹配 work_03）
- SupplementSameProductRecord Schema 测试（sars_monitor_code 非必填、StrictModel extra="forbid"）
- ManualOperationNode human_approval_guard 测试（is_executable_by_agent/execution_allowed/executed 均强制 False）

### 16.3 回归测试

- 315 条现有测试全部通过
- /diagnose 和 /health 接口不受影响
- work_01 规则引擎不变
- 安全层逻辑不变

### 16.4 R3.5 总测试数量预估

| 类别 | 数量 |
|---|---|
| 现有测试 | 315 |
| R3.5 新增单元测试 | 34-52 |
| R3.5 新增集成测试 | 5-8 |
| R3.5 总测试数量 | 354-375 |

### 16.5 R4.5 新增测试（work_03 规则引擎）

| 测试对象 | 测试内容 | 预计数量 |
|---|---|---|
| work_03 规则引擎 | S01A/S01B/S02~S07 + S99，每条规则的正向、反向、边界值 | 40-60 |
| 有效同款数派生 | 有明细时从明细派生 / 无明细时标注人工核查 | 5-8 |
| ManualOperationNode 步骤生成 | work_03 的 11 步流程节点完整性 | 5-8 |

---

## 17. Codex 审查清单

### 17.1 P0 审查项（19 项）

| 序号 | 审查项 | 要求 |
|---|---|---|
| 1 | 保留 P0/P1 底座 | 不破坏现有 P0/P1 逻辑 |
| 2 | 没破坏 /diagnose | /diagnose 功能和接口不变 |
| 3 | 没破坏 /health | /health 功能和接口不变 |
| 4 | 没有 raw_sql 入口 | 不暴露原始 SQL 输入接口 |
| 5 | 没有模型生成 SQL 执行 | 模型不可生成 SQL 并执行 |
| 6 | 没有任何 DB 写操作 | 无 INSERT/UPDATE/DELETE |
| 7 | 没自动解绑 | UNBIND_WRONG_RELATIONS 仅生成建议 |
| 8 | 没自动驳回 | REJECT 仅生成建议 |
| 9 | 没自动推送 | READY_TO_PUSH/PUSH_INDEX 仅生成建议 |
| 10 | 没自动重算 | RECALCULATE_INDEX 仅生成建议 |
| 11 | 没自动导入 | IMPORT_CUSTOMER_LINKS 仅生成模板 |
| 12 | 没自动重推 | 不存在自动重推逻辑 |
| 13 | 没跨租户 | scope_guard 限制仅中核 |
| 14 | 仍只处理中核 | 不扩展到其他租户 |
| 15 | 高风险动作全需人工确认 | human_approval_guard 拦截 |
| 16 | workflow 不绕过 scope_guard | workflow 在 scope_guard 之后 |
| 17 | Excel 导出不生成可自动写库脚本 | 导出仅为数据清单 |
| 18 | work_03 不执行 SQL | SQL 仅作为建议输出 |
| 19 | work_03 不创建采集任务 | 采集任务仅作为建议输出 |

### 17.2 P1 审查项（13 项）

| 序号 | 审查项 | 要求 |
|---|---|---|
| 1 | work_01 规则未修改 | 14条规则逻辑和优先级不变 |
| 2 | work_03 占位与 MD 规则设计一致 | R4.5 真实规则需符合 work_03 MD |
| 3 | 知识映射完整 | work_01/02/03/diagnose 都有映射 |
| 4 | router work_03 名称一致 | workflow_name 与知识映射/registry 一致（work_03_same_product_shortage） |
| 5 | 有效同款<2 时不会误判可推送 | work_03 规则正确处理边界 |
| 6 | 去一物多码逻辑正确 | work_03 复用共享逻辑 |
| 7 | 去极值阈值正确 | >参考价*1.3 剔除，低价保留 |
| 8 | 编码映射检查正确 | need_code_mapping=True 且 mapped 为空时 MANUAL_CHECK；need_code_mapping=False 且 sars_monitor_code 为空时 MANUAL_CHECK |
| 9 | ManualOperationNode.is_executable_by_agent=False | 当前阶段不可自动执行 |
| 10 | SQL 建议不包含写操作 | SQL 仅限 SELECT/WITH |
| 11 | 采集任务不自动创建 | 仅输出建议 |
| 12 | zhImport 不自动导入 | 仅输出模板建议 |
| 13 | 兜底 MANUAL_CHECK | 未匹配时走人工 |

### 17.3 P2 审查项（7 项）

| 序号 | 审查项 | 要求 |
|---|---|---|
| 1 | 文档结构清晰 | 18 章节完整 |
| 2 | Schema 命名统一 | 全文命名风格一致 |
| 3 | workflow_name 一致性 | router/registry/knowledge_adapter 三处一致 |
| 4 | 知识库引用用拆分后 MD | 不引用旧的单体文件 |
| 5 | 测试覆盖新规则 | work_03 每条规则有正向+反向测试 |
| 6 | 回归测试全通过 | 315+ 旧测试不变 |
| 7 | 阶段规划可执行 | R3.5 任务清单具体可落地 |

---

## 18. 最终结论

### 18.1 十个问题回答

| # | 问题 | 回答 | 理由 |
|---|---|---|---|
| 1 | 当前 Agent 架构是否需要推倒重写 | **否** | R2 安全底座+R3 work_01 规则引擎稳定运行，315 tests passing。在现有架构上增量扩展即可，推倒重写风险远大于收益。 |
| 2 | 是否需要新增 R3.5 | **是** | 存在3处缺口（知识映射缺失/registry缺失/Schema扩展），R3.5 先补齐基础设施和占位 workflow，R4 专注 work_02，R4.5 实现 work_03 真实规则。 |
| 3 | 是否建议直接进入 R4 | **否** | R3.5 需要先补齐 Schema（ManualOperationNode/PriceFeedbackRecord扩展）和知识映射，否则 R4 的 work_02 规则缺少承载结构。R3.5 是轻量对齐阶段（25-40 条测试），不影响 R4 进度。 |
| 4 | work_02 是否应按第二份 MD 重新设计 | **是，但不是推倒重做** | work_02 当前是 R2 占位骨架（全部返回 MANUAL_VERIFY），需要按 work_02 MD 升级为完整规则引擎。但 Schema（PriceFeedbackRecord/CustomerLinkRecord）已经按 MD 设计好，不需要改。只需实现 analyze() 的业务规则。这是 R4 的核心任务。 |
| 5 | work_03 是否应独立成 workflow | **是** | work_03 与 work_01 在触发条件、输入结构、流程步骤、核心能力上完全不同。强行合并到 work_01 会导致 work_01 膨胀且职责不清。独立 workflow 可以有独立的 Schema、规则引擎、API 端点，同时通过 shared_rules.py 复用共享逻辑。 |
| 6 | 当前 work_01 是否需要大改 | **否** | work_01 的14条规则已完全覆盖 work_01 MD 的核心场景，315 tests passing。唯一可选增强（same_product_type 参与证据输出/批量审批摘要统计/型号不一致独立规则）都是 P3 优先级，不影响核心功能。 |
| 7 | 是否允许当前阶段接 Excel | **否** | Excel/CSV 读写涉及文件安全校验（ZIP bomb/路径穿越/公式注入/大小限制），需要独立的 R5 阶段和 R5-GATE 安全审查。R3.5/R4 用 JSON mock 数据即可验证业务规则正确性。 |
| 8 | 是否允许当前阶段接数据库 | **否** | 真实数据库接入需要 R6 阶段的 execution_sql_policy、只读权限校验、结果脱敏等完整安全基础设施。R3.5/R4 不接数据库，SQL 仅作为建议输出。 |
| 9 | 是否允许当前阶段接模型 | **否** | 模型匹配在 work_03 MD 中明确提及，但当前阶段不接模型。模型只能用于未来的意图识别/分类/话术润色，不允许生成 SQL 或执行动作。模型接入需要独立的安全审查阶段。 |
| 10 | 是否允许自动推送/解绑/驳回/重算/导入 | **否，绝对不允许** | 这是核心安全边界。所有动作都只是建议，输出永远 execution_allowed=False, executed=False。不新增 action executor，不新增"人工确认后自动执行"路径。Agent 永远不调用数据库写入或系统操作接口。 |

### 18.2 核心决策总结

1. **R3.5 存在的必要性：** 补齐知识库-Schema-Router-Registry 对齐缺口 + 占位 workflow
2. **R3.5 不实现规则引擎：** work_03 规则引擎放到 R4.5，R3.5 只做对齐和占位
3. **work_01 不动：** 14条规则已完整覆盖 MD，315 tests passing
4. **work_02 是 R4 任务：** 从占位骨架升级为完整规则引擎
5. **work_03 是 R4.5 任务：** R3.5 创建占位，R4.5 实现真实规则引擎
6. **work_03 独立 workflow：** 与 work_01 共享基础逻辑但触发条件和流程完全不同
7. **workflow_name 保留：** work_03_same_product_shortage 不变，通过知识映射关联文件
8. **ManualOperationNode 强类型化：** 作为 WorkflowRecordResult 的一等公民字段，经过 human_approval_guard 校验
9. **Schema 新增 7 个：** SupplementSameProductRecord + BrandInfo + SearchTermConfig + CollectionConfig + ImportTemplateField + SupplementSameProductAnalyzeRequest + ManualOperationNode
10. **安全边界不可逾越：** 19 条安全约束全程有效，所有动作永远只是建议

### 18.3 R3.5 允许/不允许清单

**R3.5 允许做：**

1. knowledge map 对齐（_WORKFLOW_KNOWLEDGE_MAP 增加 work_03 条目）
2. Schema 扩展（SupplementSameProductRecord 等新结构 + WorkflowRecordResult 增加 manual_operation_nodes + PriceFeedbackRecord 增加 feedback_date/feedback_reason）
3. ManualOperationNode 强类型输出（承载在 WorkflowRecordResult 上）
4. human_approval_guard 校验 manual_operation_nodes（is_executable_by_agent/execution_allowed/executed 强制 False）
5. Router 触发词增强（work_03 增加 4 个触发词 + confidence 提升）
6. work_03 registry 占位（SupplementSameProductWorkflow 占位类）
7. work_03 API 占位端点（POST /workflows/same-product-shortage/analyze）
8. work_03 占位响应（primary_action=MANUAL_CHECK, rule_id=R35_PLACEHOLDER）

**R3.5 不允许做：**

1. work_02 真实规则
2. work_03 真实规则
3. SQL 建议输出
4. 品牌清洗建议输出
5. 搜索词生成建议输出
6. 导入模板建议输出
7. 采集任务配置建议输出
8. work_03 处理流程建议输出
9. shared_rules.py 提取（可选重构项，不纳入 R3.5）
10. Excel/CSV 读写
11. 数据库连接
12. 模型调用
13. SQL 执行
14. 创建采集任务
15. 导入 zhImport
16. 解绑
17. 驳回
18. 重算
19. 推送
20. action executor

### 18.4 风险提示

| 风险 | 可能性 | 影响 | 缓解措施 |
|---|---|---|---|
| R3.5 Schema 新增与已有代码冲突 | 低 | 低 | StrictModel extra="forbid" + 默认值兼容 |
| WorkflowRecordResult 新增字段破坏序列化 | 低 | 中 | 默认空列表，全量回归测试 |
| human_approval_guard 增强引入回归 | 低 | 中 | 空 manual_operation_nodes 不触发新逻辑 |
| work_03 workflow_name 与知识库文件名不一致 | 低 | 低 | 通过映射关联，不依赖名称一致 |
| ManualOperationNode 被误读为可自动执行 | 低 | 高 | is_executable_by_agent 永远为 False + human_approval_guard 强制校验 |
| R4.5 规则与 MD 不完全对齐 | 中 | 中 | 逐条对照 MD 测试覆盖 |
