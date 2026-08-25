# AGENTS.md — 中核 SaaS 运维 Agent 接手指南

> 本文件面向后续接手的 AI agent 或开发者。请**先读完本文件**再动代码。

---

## 一、项目定位

中核 SaaS（tenant_id=188605、commodity_pool_id=222）的运维自动化工具集，覆盖：

1. **价格审计全流程**：SaaS 待审批数据 + 中核客户数据 → 调中核 detail 接口取客户最高价 → 调大模型校验标题同款 → 多商城爬取页面价格 → 计算"超客户价 30% / 价格变动 / 最终决策" → 输出中文表头 Excel
2. **监控状态查询**：中核待处理数据 → SaaS 监控接口查采集/同款/价格计算状态 → 输出可推送/需处理分类表
3. **离线诊断 API（旧）**：输入运维问题 → 生成只读 SQL 模板 + 知识库片段（不连库、不执行 SQL）

**核心业务目标**：帮助中核运维团队日清"同款审批 + 价格校验"，让商品从"校验中"变成"校验成功"，并拦截超价/价格异常数据。

---

## 二、主要目录说明

```
cnnc-ops-agent/                       本项目根
├── app/                              FastAPI 服务 + 业务逻辑
│   ├── main.py                       FastAPI 入口（/health /diagnose /workflows/*）
│   ├── config.py                     配置（路径、版本、只读模式）
│   ├── agent/                        旧诊断：scope_guard / planner / sql_selector / safety_guard
│   ├── workflows/                    工作流：base / router / scope_guard / local_access_guard / human_approval_guard
│   │   ├── work_01_saas_approval_json_audit.py   ★ SaaS 同款审批 JSON 审查规则（核心）
│   │   ├── work_01_same_product_approval.py      work_01 规则版
│   │   └── work_02_price_index_feedback.py       work_02 价格指数反馈
│   ├── services/
│   │   ├── title_match_model_adapter.py   ★ 大模型标题同款校验（单条+批量）
│   │   ├── knowledge_loader.py            知识库加载/搜索/写库SQL脱敏
│   │   ├── link_audit_worker.py           链接审计
│   │   └── workflow_knowledge_adapter.py
│   ├── schemas/workflow.py            Pydantic 模型（WorkflowAction 枚举等）
│   └── templates/                    5 个 SQL 模板 YAML（旧诊断用）
├── scripts/
│   ├── audit_saas_excel_file.py      ★★ 价格审计主脚本（全流程入口）
│   ├── audit_saas_json_file.py       JSON 审计（无 Excel）
│   ├── crawl_ofs_fallback.py         OFS 爬虫兜底
│   └── export_excluded_malls.py      京东/震坤行模板导出
├── data/                             输入数据（gitignore，本地才有）
│   ├── saas_json.json                SaaS 审批导出
│   ├── customer_json.json            中核客户导出
│   ├── detail_request.curl           中核 detail 接口 cURL（含登录态）
│   ├── model.env                     大模型 API 配置
│   └── docs/cnnc_ops_knowledge_pure.md  知识库
├── runtime/                          运行输出（gitignore）
│   └── saas状态查询/                 ★ 监控状态导出归档目录
├── tests/                            pytest 测试（577 个用例）
├── tools/                            一次性工具脚本（_ 开头，非核心）
├── docs/                             设计/流程文档
├── zhaoshang_priority_boost_integration/   招商局优先级加速集成
├── 爬取模板/                         京东/震坤行爬取模板（按日期）
├── 商城sourceId映射表/               商城映射
└── requirements.txt
```

**外部依赖项目（不在本仓库）：**

| 路径 | 作用 |
|------|------|
| `E:\.A2026\多商城爬取\multi_mall_crawler` | 多商城页面价格爬取（28 个商城） |
| `E:\.A2026\招商局自动建任务` | 招商局自动建任务爬京东/震坤行等 |
| `E:\.A中核运维\自动拉取` | 自动拉取 SaaS + 中核接口数据（含 .env 认证） |
| `E:\.A中核运维\monitor_status_export.py` | 监控状态导出工具（独立脚本） |
| `E:\.A中核运维\cnnc_to_saas_monitor.py` | 中核→SaaS 监控状态汇总（独立脚本） |

---

## 三、项目入口文件

| 入口 | 作用 | 启动命令 |
|------|------|---------|
| `app/main.py` | FastAPI 服务（旧诊断 + workflow API） | `uvicorn app.main:app --reload --port 8000` |
| `scripts/audit_saas_excel_file.py` | ★ 价格审计全流程（最常用） | 见 TESTING.md |
| `cnnc_to_saas_monitor.py`（项目外上级） | 中核→SaaS 监控状态汇总 | `python cnnc_to_saas_monitor.py` |
| `monitor_status_export.py`（项目外上级） | 本地JSON/直接查SaaS监控 | `python monitor_status_export.py --input x.json` |

---

## 四、关键业务流程

### 流程 A：价格审计全流程（`audit_saas_excel_file.py`）

```
读 SaaS JSON + 客户 JSON
 → 生成京东/震坤行爬取模板
 → 调中核 detail 接口拿客户最高价（缓存，同 documentId 只调一次）
 → 调大模型批量校验标题同款（去重→分批30条→并发12批）
 → multi_mall_crawler 爬非京东/非震坤行链接（并发3）
 → 招商局建任务爬京东+震坤行（并发6，轮询到完成）
 → 回填所有价格
 → 计算：saas_price_vs_customer / price_changed / over_30pct / final_decision
 → 输出 runtime/saas_audit_final.xlsx（中文表头 + 高亮 + 冻结首行）
```

final_decision 取值：PASS / REJECT_SAAS_HIGH / REJECT_PRICE_CHANGED / REJECT_BOTH

### 流程 B：监控状态查询（`cnnc_to_saas_monitor.py`）

```
调中核接口拉取"待SaaS处理"数据（sassCheckStatuss=["0"]）
 → 拿 documentId 列表
 → 调 SaaS 监控接口查采集/同款/价格计算状态
 → 合并输出 Excel（可推送 / 需处理 / SaaS监控缺失 / 统计）
 → 归档到 runtime/saas状态查询/<时间戳>/
```

可推送条件：isSync=1 AND isSyncStandard=1 AND (syncPriceStatus=1 OR syncPriceStatus="")

### 流程 C：离线诊断 API（旧，`app/main.py`）

```
POST /diagnose {question}
 → scope_guard 检查是否中核范围
 → planner 识别场景（weekly/single_item/crawler/relation/task/knowledge_qa）
 → sql_selector 加载 YAML 模板 + 参数校验 + 渲染
 → safety_guard SQL 安全检查（只允许 SELECT，白名单表）
 → knowledge_loader 检索知识库（写库SQL脱敏）
 → 返回诊断步骤 + SQL 模板 + 知识片段
```

---

## 五、模型调用方式

**配置文件**：`data/model.env`

```env
TITLE_MATCH_MODEL_API_KEY=<key>
TITLE_MATCH_MODEL_BASE_URL=https://ark.cn-beijing.volces.com/api/v3/chat/completions
TITLE_MATCH_MODEL_NAME=doubao-seed-2-0-mini-260215
```

**调用入口**：`app/services/title_match_model_adapter.py`

- `call_title_match_model(req)` — 单条调用
- `call_title_match_model_batch(reqs)` — 批量调用（去重 + 分批 + 并发）

**返回**：`TitleMatchModelResult`
- `match_result`: SAME / NOT_SAME / MANUAL_REVIEW / MODEL_UNAVAILABLE
- `confidence`: 0.0-1.0
- `brand_match` / `model_match` / `spec_match` / `risk_points` / `reason`

**环境变量调参**（审计脚本用）：
- `TITLE_MATCH_MODEL_BATCH_SIZE`（默认 30）
- `TITLE_MATCH_MODEL_BATCH_CONCURRENCY`（默认 12）
- `TITLE_MATCH_MODEL_TIMEOUT`（默认 180 秒）

**安全约束**：不打印 API KEY（只显示 SET / NOT_SET）；不把 model.env 提交 git。

---

## 六、数据库或文件操作边界

| 操作 | 允许 | 禁止 |
|------|------|------|
| 数据库 | 无（项目不连库） | 任何数据库连接、SQL 执行 |
| SQL 模板 | 只生成 SELECT 模板供人工执行 | UPDATE/DELETE/INSERT/TRUNCATE/DROP/ALTER |
| 文件读 | data/*.json, data/*.curl, data/*.env | — |
| 文件写 | runtime/*.xlsx, 爬取模板/*.xlsx | 不写 data/ 下任何文件 |
| 审批接口 | 不调用 | 禁止调用通过/驳回/保存/提交接口 |
| 爬虫 | 只爬商品详情页价格 | 不绕验证码、不自动登录、不代理池 |

---

## 七、禁止事项（硬性红线）

1. **不绕过验证码 / 不自动登录 / 不注入 stealth / 不轮换 UA / 不用代理池**
2. **不调用审批/驳回/保存/提交类接口**（只允许查询接口）
3. **不写数据库 / 不执行 SQL**
4. **不持久化 cookie/token/auth-token/access-token/y1AmuISe 到任何文件**
5. **不打印 cookie/token/auth-token/access-token/y1AmuISe 到日志**
6. **不把 detail_request.curl / model.env / .env 提交到 git**
7. **不处理非中核租户**（非 tenant_id=188605 一律拒绝）
8. **不删除已有文件**（除非用户明确要求）
9. **修改 .env / 配置 / 注册表 / 清缓存前必须用中文说明风险并等用户确认**

---

## 八、修改代码时的原则

1. **先读后改**：编辑前先 Read 目标文件和邻近文件，理解现有惯例
2. **不丢字段**：处理接口数据时保留所有原始字段，未映射的字段保持原英文名
3. **中文列名**：Excel 输出字段用中文（见 COLUMN_DISPLAY_NAMES 映射）
4. **异常处理**：JSON 解析失败提示具体文件；接口失败打印状态码和返回内容；Excel 导出失败提示路径
5. **不写死密钥**：token/cookie 只从命令行参数、环境变量或 .env 读取
6. **不加注释**：除非用户明确要求，不要添加代码注释
7. **遵循现有结构**：新函数放对位置，复用现有工具函数（如 `_norm`、`_extract_rows`）
8. **测试先行**：修改核心逻辑后必须跑 `pytest tests/ -v` 确认不破坏现有测试

---

## 九、高风险操作说明

| 操作 | 风险 | 要求 |
|------|------|------|
| 修改 `data/detail_request.curl` | 影响客户价获取 | 确认登录态有效 |
| 修改 `data/model.env` | 影响标题校验 | 确认 KEY 有效 |
| 修改 `.env`（自动拉取） | 影响 SaaS+中核接口认证 | 用中文说明风险后执行 |
| 修改 `audit_saas_excel_file.py` 核心逻辑 | 影响审计结果正确性 | 必须跑测试 |
| 修改 `safety_guard.py` | 可能放开写库 SQL | 禁止放宽白名单 |
| 修改 `scope_guard.py` | 可能放开非中核租户 | 禁止放宽范围 |
| 招商局建任务 | 消耗招商局配额 | 不要重复建同名任务 |
| 全量查询 SaaS 监控（17万条） | 卡死 | 必须加 --max-rows |

---

## 十、后续 AI 接手时必须先看的文件

**必读顺序**（从上到下）：

1. `AGENTS.md`（本文件）
2. `CURRENT_STATE.md` — 当前项目进度和已知问题
3. `TESTING.md` — 如何验证项目
4. `docs/cnnc_price_data_fetch_guide.md` — 中核价格数据获取完整流程
5. `docs/SAAS_AUDIT_FLOW.md` — SaaS 审计 + 爬虫 + 招商局完整流程
6. `docs/cnnc_work_01_same_product_approval.md` — 同款审批业务规则
7. `app/services/title_match_model_adapter.py` — 模型调用（看 prompt 规则）
8. `scripts/audit_saas_excel_file.py` — 审计主脚本（前 200 行看结构）

---

## 十一、每次修改后必须输出的内容

每次完成代码修改后，必须向用户输出以下 4 项：

```markdown
## 改动摘要
- 改动文件：列出每个修改/新增的文件路径
- 改动目的：一句话说明为什么改
- 测试方式：给出验证命令（如 pytest tests/ -v 或具体运行命令）
- 风险点：说明可能影响的范围和注意事项
```

---

## 十二、Git 提交规则

- **只在用户明确要求时才 commit/push**
- commit 前必须检查 `git status` / `git diff`，不要提交 secrets
- commit message 用中文，匹配仓库现有风格（`feat:` / `fix:` / `docs:` 前缀）
- 不要 force-push、不要 amend 已推送的 commit、不要 empty commit
- `.gitignore` 已排除：`.env`、`data/*.curl`、`data/*token*`、`data/*.env`、`data/*.json`、`runtime/`、`*.xlsx`
