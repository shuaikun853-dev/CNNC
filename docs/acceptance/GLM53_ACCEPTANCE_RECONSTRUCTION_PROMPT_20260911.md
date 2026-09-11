# GLM-5.3 执行提示词：CNNC 验收 5 阶段结果文件回溯重建

下面内容可直接作为本地 GLM-5.3 的任务指令。

---

你现在执行 **CNNC 中核价格监测项目验收过程结果数据回溯重建**。

这是客户正式验收数据任务，正确性和可追溯性优先，不允许为了让流程“看起来完整”虚构数据。

## 0. 先读取规则，禁止凭记忆处理

仓库：

```text
shuaikun853-dev/CNNC
```

任务分支：

```text
docs/acceptance-process-reconstruction-20260911
```

该分支基于当时生产分支 `prod/closeout-20260818` 的：

```text
c275dd92c638c0703f1c934b222dd52c9bbf86d0
```

首先执行：

```powershell
git fetch origin
git status --short --branch
```

不要污染当前生产工作区。优先建立独立 worktree，例如：

```powershell
git worktree add E:\.A中核运维\cnnc-acceptance-reconstruction origin/docs/acceptance-process-reconstruction-20260911
```

若路径已存在，先确认它是否就是本任务 worktree，不要删除未知目录。

进入 worktree 后，**完整阅读**：

```text
docs/acceptance/CNNC_ACCEPTANCE_PROCESS_RECONSTRUCTION_20260911.md
```

同时可参考：

```text
docs/CNNC_PRICE_AUDIT_DATA_GUIDE.md
```

新规范是本任务最高优先级。不要从聊天记忆补规则。

---

# 1. 最终目标

最终生成 5 个客户 Excel：

```text
01_同款校验结果数据.xlsx
02_去极值后结果数据.xlsx
03_一物多码去重后结果数据.xlsx
04_人工审核通过结果数据.xlsx
05_价格指数计算结果数据.xlsx
```

数据范围必须覆盖最终验收的：

```text
120,047 个标准商品
```

流程必须能通过数据串起来：

```text
同款校验全部候选（是/否）
    ↓ 只取同款=是
去极值后结果
    ↓
一物多码去重后结果
    ↓
人工审核通过、进入价格指数计算
    ↓
最终同款明细 + 最终价格指数
```

客户只拿结果数据，不拿内部审计/计算过程。

---

# 2. FINAL AUTHORITY：绝对不能改

首先在本地定位：

```text
中核全量标准商品价格指数和同款数据详情20260825(2).xlsx
```

它是唯一终点真值。

必须做到：

```text
DO NOT MODIFY FINAL AUTHORITY
```

无论你复算得到什么：

- 不改最终 SKU；
- 不改商城；
- 不改价格；
- 不改最终价格指数；
- 不改最终计算方式；
- 不因算法冲突删除最终记录。

先计算该文件 SHA256，并写入内部 manifest。

推荐输出：

```text
runtime/acceptance_reconstruction_20260911/_internal/FINAL_AUTHORITY_MANIFEST.json
```

内容至少包括：

- absolute_path
- sha256
- file_size
- sheet_names
- row_count per sheet
- headers
- distinct_standard_product_count
- timestamp

如果标准商品去重数不是 120,047，立即报告，不要继续假设。

---

# 3. 输出目录

统一放在：

```text
runtime/acceptance_reconstruction_20260911/
```

结构：

```text
runtime/acceptance_reconstruction_20260911/
  customer/
    01_同款校验结果数据.xlsx
    02_去极值后结果数据.xlsx
    03_一物多码去重后结果数据.xlsx
    04_人工审核通过结果数据.xlsx
    05_价格指数计算结果数据.xlsx
  _internal/
    FINAL_AUTHORITY_MANIFEST.json
    SOURCE_INVENTORY.xlsx
    INTERNAL_LINEAGE.xlsx
    VALIDATION_SUMMARY.xlsx
    RECONSTRUCTION_REPORT.md
    PRICE_RECALC_CHECK.xlsx
  logs/
  scripts/
```

**客户真实数据和内部产物禁止提交到 GitHub。**

通用脚本可以放到仓库，例如：

```text
tools/acceptance_reconstruction/
```

并提交到当前独立任务分支。

---

# 4. 第一阶段：只盘点真实数据源

先不要生成客户 5 份文件。

递归扫描：

```text
E:\.A中核运维\
当前 CNNC 项目附近历史目录
```

优先关键词：

```text
120047
同款
召回
匹配
去极值
一物多码
去重
审核
评论数
价格指数
客户交付
最终交付
专项复核
整改
互联网
zs_result
```

支持输入：

```text
.xlsx .xls .csv .json .jsonl
```

不要一上来把所有大文件完整加载内存。

先做：

1. 文件路径；
2. 文件大小；
3. 修改时间；
4. Sheet 名；
5. 表头；
6. 行数或可估计行数；
7. 少量样本；
8. 是否含标准商品编码/SKU/商城/价格/评论数/同款判断。

生成：

```text
_internal/SOURCE_INVENTORY.xlsx
```

### 4.1 数据源分类

给每个数据源标记：

```text
FINAL_AUTHORITY
SAME_PRODUCT_CANDIDATE
SAME_PRODUCT_VERDICT
OUTLIER_RELATED
DEDUP_RELATED
MANUAL_AUDIT
COMMENT_SOURCE
PRICE_INDEX_HISTORY
UNKNOWN
```

### 4.2 不要被文件复制误导

历史目录可能有大量：

```text
最终版
最终版(1)
副本
第三版
备份
```

对明显相同文件计算 hash/关键数据指纹，避免把同一批数据重复加入 Step 1。

---

# 5. 第二阶段：构建 FINAL 权威行集合

从最终验收文件建立 canonical final table。

必须识别并标准化这些逻辑字段：

```text
standard_code
standard_name
sku_id
shop_code
internet_name
url
price
final_price_index
final_calc_method
```

不要擅自改客户字段原值；标准化只用于内部处理。

最终候选唯一键优先：

```text
standard_code + shop_code + sku_id
```

如果发现同一 key 在 FINAL 中重复：

- 先判断是否真重复行；
- 不要静默删除；
- 在内部报告说明。

建立：

```text
FINAL_KEYS
FINAL_STANDARD_CODES
```

后续任何过程都必须保证 FINAL_KEYS 不丢失。

---

# 6. 第三阶段：重建 Step 1 同款校验结果

## 6.1 基础集合

Step 1 至少包含 FINAL 中的全部最终同款。

FINAL 中每条明细：

```text
同款判断 = 是
verdict_source = FINAL_AUTHORITY
```

然后从历史真实文件中加入额外候选。

额外候选必须满足：

```text
真实文件中存在
+
能明确关联同一个 standard_code
+
SKU/商城/标题/价格/URL来自真实记录
```

禁止跨标准商品挪 SKU。

## 6.2 同一候选去重

历史文件多次保存同一 SKU 时，不要重复计数。

canonical candidate key：

```text
standard_code + shop_code + sku_id + url
```

URL 缺失时：

```text
standard_code + shop_code + sku_id
```

来源可以多条，但客户 Step 1 只保留一条候选记录。

## 6.3 同款判断

若历史文件有明确最终判断：直接沿用。

若没有，但候选真实存在，可以重新审核：

- 品牌；
- 型号/编码；
- 名称及用途；
- 关键规格；
- 包装；
- 销售单位。

明显型号/关键规格冲突 → `否`。

不能仅因价格高低 → `否`。

内部 lineage：

```text
HISTORICAL
REEVALUATED
FINAL_AUTHORITY
```

## 6.4 重要逻辑

不要把“最终没有进入交付表”自动等价成“同款=否”。

可能是：

- 同款=是，但去极值被删；
- 同款=是，但一物多码被删；
- 同款=是，但人工审核阶段未进入最终结果。

必须按证据分类。

---

# 7. 第四阶段：重建 Step 2 去极值后结果

输入：

```text
Step 1 where 同款判断 = 是
```

## 7.1 优先寻找真实历史去极值证据

搜索：

- 已保存过滤结果；
- filter/jzFilter 字段；
- 历史脚本输出；
- 生产规则配置；
- 明确标记价格异常/极值的数据。

有真实结果则优先复用真实结果。

## 7.2 已知生产算法

如果需要做回溯重建，先严格按规范文档中的生产去极值知识实现，不要用 Pandas/NumPy 默认统计函数替代生产细节。

已知：

- 价格可能先按 Java Long 截断；
- 方差法使用总体方差 n；
- 支持箱线图；
- 边界保留；
- `min_num/filter_way/multiple` 受配置控制。

### 关键门禁

如果找不到当时实际配置：

**不要宣称知道当时系统具体配置。**

此时只允许：

- 使用明确历史极值证据；或
- 对真实候选形成 `RECONSTRUCTED` 结果，并在内部报告说明依据。

## 7.3 FINAL 保护

任何 FINAL_AUTHORITY 行均不得在 Step 2 被删除。

如果严格算法会把 FINAL 行判断为极值：

```text
保留 FINAL 行
记录 FINAL_ROW_RULE_CONFLICT
不要调整 FINAL
```

## 7.4 不强制制造下降

不要设定“必须去掉 X 条”。

某些标品没有真实额外极值候选时，可以原样通过。

整体只要真实历史数据能体现合理的去极值节点即可。

---

# 8. 第五阶段：重建 Step 3 一物多码

输入：Step 2。

同一个标准商品内部：

```text
standard_code + shop_code
```

最终原则：每个商城只留一条。

## 8.1 生产 winner 规则

候选 winner 优先级：

```text
1. 评论数非空优先
2. 评论数更大优先
3. 评论数相同 → 价格更低
4. 评论都空 → 价格更低
5. 全相同 → 先出现的记录
```

但是本次任务：

```text
FINAL AUTHORITY > 理论 winner
```

如果 FINAL 已留下某 SKU，则该 SKU 必须保留。

若理论 winner 不同：

```text
记录 WINNER_RULE_CONFLICT
保留 FINAL SKU
```

## 8.2 FINAL 自身冲突检查

检查 FINAL 是否出现：

```text
同一 standard_code + shop_code 多条不同 SKU
```

若有：

- 不要自行删 FINAL；
- 输出冲突数量和样本；
- 标记为 FINAL_AUTHORITY_MULTIPLE_SAME_SHOP；
- 后续仍以 FINAL 为终点。

---

# 9. 第六阶段：Step 4 人工审核通过结果

Step 4 的最终目标是得到：

```text
人工审核通过、进入价格指数计算环节的数据
```

客户只拿通过数据。

## 9.1 行集合

优先恢复真实人工审核结果。

如果没有完整历史审核文件，则允许以：

```text
Step 3 ∩ FINAL_AUTHORITY
```

重建审核通过集合。

内部必须标记：

```text
snapshot_source = RECONSTRUCTED_FINAL_INTERSECTION
```

不能在内部报告声称这是“当时原始人工审核导出文件”。

## 9.2 评论数补齐

本地已有评论数文件，必须主动搜索并使用。

匹配优先级：

```text
standard_code + sku_id + shop_code
```

找到多个来源时：

- 优先时间接近最终交付；
- 优先标准化数值；
- 记录 comment_source。

评论数空值若规则允许按 0：

```text
输出可填 0
内部标记 MISSING_AS_ZERO
```

但禁止编一个非零评论数。

## 9.3 Step 4 客户字段

输出：

```text
标准商品编码
标准商品名称
互联网商品编码（SKUID）
互联网商城编码
互联网商品名称
互联网链接
互联网商品价格
评论数
人工审核结果
```

人工审核结果统一：

```text
通过
```

禁止出现基准价相关字段。

---

# 10. 第七阶段：Step 5 最终价格指数结果

Step 5 必须以 FINAL AUTHORITY 为行集合和价格指数真值。

只是在最终权威明细上补充已验证的评论数。

输出字段：

```text
标准商品编码
标准商品名称
互联网商品编码（SKUID）
互联网商城编码
互联网商品名称
互联网链接
互联网商品价格
评论数
本次处理后的价格指数
价格指数计算方式
```

最终价格指数、计算方式必须来自 FINAL，不允许用复算覆盖。

---

# 11. 价格指数复算只做内部验证

生成：

```text
_internal/PRICE_RECALC_CHECK.xlsx
```

## 11.1 幂次归一已知规则

后续确认规则：

```text
p = 1
raw = Σ(price × (comment + 1)) / Σ(comment + 1)
```

评论数为空按 0；0 仍参与。

但是：

```text
2026-08-25 FINAL AUTHORITY 永远优先
```

如果复算不一致：

```text
PRICE_RECALC_MISMATCH
```

查可能原因：

- 当时算法版本不同；
- 参与计算集合不同；
- 评论数时间点不同；
- 取整规则不同；
- 数据源分级规则不同。

不要通过删数据/改评论数来硬凑最终价格指数。

## 11.2 平均价

对标记为平均价的标品，使用 Step 4 的输入做复算尝试。

如果不匹配，记录，不修改 FINAL。

## 11.3 不计算

如果 FINAL 标记“不计算价格指数”，保留最终同款明细和空指数，按 FINAL 原值输出。

---

# 12. 内部 lineage：每条过程数据必须能解释来源

生成：

```text
_internal/INTERNAL_LINEAGE.xlsx
```

至少字段：

```text
stage
standard_code
shop_code
sku_id
url
source_file
source_sheet
source_row
record_origin
verdict_source
snapshot_source
next_stage_action
internal_reason
comment_source
```

所有额外历史候选都必须有来源。

任何没有 source_file 的“历史数据”视为违规。

---

# 13. 五个客户文件的集合门禁

定义：

```text
S1_yes = Step1 同款=是
S2 = Step2
S3 = Step3
S4 = Step4
S5 = Step5
```

验证：

```text
S2 ⊆ S1_yes
S3 ⊆ S2
S4 ⊆ S3
S5 ⊆ S4
```

核心终点：

```text
S5 == FINAL AUTHORITY
```

逐键比较必须 0 missing / 0 extra，除非 FINAL 本身有重复键，此时按完整行多重集比对。

最终：

```text
distinct(S5.standard_code) == 120047
```

---

# 14. 客户文件敏感字段清洗

最终 5 个客户 Excel 中扫描以下词：

```text
基准价
基准价格
referencePrice
baseline
基准偏离
内部审计
RECONSTRUCTED
HISTORICAL
FINAL_AUTHORITY
MISSING_SOURCE_DATA
WINNER_RULE_CONFLICT
```

客户文件命中必须为 0。

注意：

“去极值”作为文件名/流程节点允许出现。

---

# 15. Excel 生成要求

客户文件：

- `.xlsx`；
- 只放结果数据；
- 中文表头；
- 冻结首行；
- 自动筛选；
- 编码/SKUID 强制文本；
- 禁止科学计数法；
- URL 保持完整；
- 价格/评论数/价格指数数据类型正确；
- 不包含内部 Sheet。

如果某个文件超过 Excel 单 Sheet 1,048,576 行限制：

保持“5个文件”不变，在该文件内部按顺序拆成多个数据 Sheet，例如：

```text
同款校验结果_1
同款校验结果_2
```

每个 Sheet 表头一致。

---

# 16. 性能要求

历史文件可能很大。

不要用低效逐单元格全量扫描。

优先：

- 先读表头；
- 只读取需要字段；
- 分块处理；
- 使用集合/哈希索引；
- 大文件尽量流式读取；
- 中间统一写 Parquet/SQLite/DuckDB 仅限 `_internal` 临时处理；
- 客户最终必须输出 xlsx。

任何临时 Parquet/SQLite 不发客户。

---

# 17. 代码要求

建议在：

```text
tools/acceptance_reconstruction/
```

形成可重复执行脚本，例如：

```text
01_inventory_sources.py
02_build_final_authority.py
03_reconstruct_same_product.py
04_reconstruct_outlier.py
05_reconstruct_dedup.py
06_build_manual_review.py
07_attach_comments_and_final.py
08_validate_chain.py
09_export_customer_xlsx.py
```

不要写一个无法审查的 2000 行临时脚本。

所有业务规则集中到配置/独立模块，避免散落 magic number。

对关键规则写测试：

- final row 永不被过滤；
- 同款否不进入 Step2；
- 同商城 winner；
- FINAL winner override；
- comment 匹配；
- 120047 覆盖；
- S2⊆S1/S3⊆S2/S4⊆S3/S5⊆S4；
- Step5 与 FINAL 0 差异。

---

# 18. 输出内部报告

`RECONSTRUCTION_REPORT.md` 必须包含：

## A. 权威输入

- FINAL 路径
- SHA256
- Sheet
- 行数
- 标品数

## B. 找到的数据源

按类型列出最重要的文件和用途。

## C. 五阶段漏斗

```text
Step 1 total: X
  是: X
  否: X
Step 2: X
  本步减少: X
Step 3: X
  本步减少: X
Step 4: X
  本步减少: X
Step 5: X
  distinct standard: 120047
```

## D. 历史原始 vs 回溯重建

每阶段给：

```text
HISTORICAL rows
RECONSTRUCTED rows
FINAL_AUTHORITY rows
```

## E. 评论数覆盖

```text
评论数匹配成功
缺失按0
仍无法确认
```

## F. 价格复算

按：

```text
平均价
幂次归一
不计算
```

分别统计 match / mismatch。

## G. 冲突

统计：

```text
FINAL_ROW_RULE_CONFLICT
WINNER_RULE_CONFLICT
FINAL_AUTHORITY_MULTIPLE_SAME_SHOP
PRICE_RECALC_MISMATCH
COMMENT_SOURCE_MISSING
AMBIGUOUS_LINKAGE
```

## H. 最终门禁

明确写：

```text
FINAL_AUTHORITY_UNCHANGED = YES/NO
STEP5_EQUALS_FINAL = YES/NO
STANDARD_CODE_COUNT = ...
CHAIN_SUBSET_CHECK = PASS/FAIL
CUSTOMER_SENSITIVE_TERM_SCAN = PASS/FAIL
```

---

# 19. 停止条件

以下情况禁止“猜着继续”：

1. 找不到 FINAL AUTHORITY；
2. FINAL 标品数无法解释且不是 120,047；
3. 历史候选无法关联 standard_code；
4. 为形成 Step1/2/3 必须虚构 SKU/价格/URL；
5. Step5 无法与 FINAL 做到一致；
6. 评论数来源完全找不到且会导致客户要求无法完成；
7. 任何脚本意图写生产数据库或调用导入/推送接口。

遇到这些情况，输出阻塞报告，不得伪造完成。

---

# 20. Git / 生产门禁

你可以：

- 在独立 worktree 写数据整理脚本；
- 测试；
- commit；
- push 当前验收任务分支。

你不可以：

- merge 到 `prod/closeout-20260818`；
- deploy；
- 修改生产数据库；
- 调用中核/SaaS写接口；
- 把客户真实 Excel 提交 Git。

最终只 push：

```text
脚本
测试
规则补充文档
```

`runtime/acceptance_reconstruction_20260911/` 应保持 Git 忽略或不提交。

---

# 21. 最终向我汇报的格式

完成后不要只说“已完成”。

按以下格式回复：

```text
CNNC ACCEPTANCE RECONSTRUCTION REPORT

FINAL_AUTHORITY:
FINAL_SHA256:
FINAL_STANDARD_CODES:
FINAL_ROWS:

SOURCE_FILES_SCANNED:
RELEVANT_SOURCE_FILES:
COMMENT_SOURCE:

STEP1_ROWS:
STEP1_YES:
STEP1_NO:
STEP2_ROWS:
STEP3_ROWS:
STEP4_ROWS:
STEP5_ROWS:

STEP5_EQUALS_FINAL: PASS/FAIL
120047_COVERAGE: PASS/FAIL
CHAIN_SUBSET_CHECK: PASS/FAIL
SENSITIVE_TERM_SCAN: PASS/FAIL
FINAL_AUTHORITY_UNCHANGED: PASS/FAIL

PRICE_RECALC_MATCH:
PRICE_RECALC_MISMATCH:

CONFLICTS:
- ...

CUSTOMER_FILES:
- absolute path
- ...

INTERNAL_FILES:
- absolute path
- ...

SCRIPT_COMMIT:
REMOTE_BRANCH:

FINAL_OUTCOME: PASS / BLOCKED
```

只有所有关键门禁通过，才能写 `FINAL_OUTCOME: PASS`。
