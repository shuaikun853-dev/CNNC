# 中核 SaaS 运维诊断 Agent

## 这是什么？

这是一个**离线只读诊断工具**，专门为中核 SaaS 运维团队设计。

你输入一个运维问题（比如"商品价格不更新"），它会：

1. 判断问题是否属于中核范围（只处理 tenant_id=188605）
2. 识别问题对应的诊断场景
3. 生成经过安全校验的 SQL 查询模板
4. 检索相关知识库片段
5. 返回诊断步骤、SQL 模板和解读提示

**它不会连接数据库，不会执行任何 SQL，不会自动修改任何数据。**

## 核心约束

| 约束 | 说明 |
|------|------|
| 只读 | 仅生成 SELECT 查询模板，禁止 UPDATE/DELETE/INSERT 等写操作 |
| 离线 | 不连接任何数据库，不执行任何 SQL |
| 中核专属 | 仅支持 tenant_id=188605、commodity_pool_id=222 |
| 拒绝非中核 | 宏伟/南水北调/华盛/非 188605 租户问题会被拒绝 |

## 不支持的功能

- 自动重推、自动修改价格、自动删除数据
- 连接生产数据库执行 SQL
- 定时任务、自动巡检
- 跨租户查询
- LLM/模型生成 SQL（SQL 只来自预设模板）

## 快速开始

### 1. 安装依赖

需要 Python 3.11+。

```bash
cd cnnc-ops-agent
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux/Mac
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. 启动服务

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

启动后可以访问：
- API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

### 3. 调用诊断接口

**不带参数**（返回 SQL 模板，标记缺少的参数）：

```bash
curl -X POST http://localhost:8000/diagnose \
  -H "Content-Type: application/json" \
  -d "{\"question\": \"中核本周巡检\"}"
```

**带参数**（返回渲染后的 SQL）：

```bash
curl -X POST http://localhost:8000/diagnose \
  -H "Content-Type: application/json" \
  -d "{\"question\": \"中核本周巡检\", \"params\": {\"start_time\": \"2026-06-08\", \"end_time\": \"2026-06-15\"}}"
```

**查询单商品**：

```bash
curl -X POST http://localhost:8000/diagnose \
  -H "Content-Type: application/json" \
  -d "{\"question\": \"中核商品价格不更新\", \"params\": {\"commodity_id\": \"597894113655676928\"}}"
```

### 4. 健康检查

```bash
curl http://localhost:8000/health
```

返回示例：

```json
{
  "status": "ok",
  "service": "cnnc-ops-agent",
  "version": "0.1.0-mvp",
  "readonly_mode": true,
  "db_connected": false,
  "knowledge_file_exists": true,
  "templates_dir_exists": true,
  "template_count": 5,
  "knowledge_status": "ok",
  "knowledge_error": "",
  "issues": []
}
```

## 请求参数

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| question | string | 是 | 运维问题描述 |
| params | object | 否 | SQL 模板参数 |

### 支持的参数名和格式

| 参数名 | 允许的值 | 示例 |
|--------|----------|------|
| start_time | YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS | "2026-06-08 00:00:00" |
| end_time | YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS | "2026-06-15 00:00:00" |
| commodity_id | 纯数字（int 或数字字符串） | "597894113655676928" |
| my_commodity_id | 纯数字 | "123456" |
| request_id | 纯数字 | "789" |
| execution_number | 字母+数字+下划线+横线 | "TASK_20260615-001" |
| task_type | 字母+数字+下划线+横线+点 | "price_push" |
| hours | 纯数字 | "24" |

**不允许的类型**：list、dict、tuple、set、float（commodity_id 等）、bool。

**不允许的字符**：分号、反斜杠、注释符号（--、#、/* */）、换行、单引号。

## 响应字段

| 字段 | 类型 | 说明 |
|------|------|------|
| scenario | string | 识别的诊断场景 |
| confidence | float | 场景识别置信度 |
| reason | string | 识别原因 |
| diagnosis_steps | list[string] | 推荐的排查步骤 |
| related_tables | list[string] | 涉及的中核表名 |
| sql_templates | list[object] | SQL 模板列表（见下） |
| result_guide | object | 结果解读提示 |
| knowledge_snippets | list[object] | 知识库检索片段（写库 SQL 已脱敏） |
| knowledge_status | string | 知识库状态：ok/error/degraded |
| knowledge_error | string | 知识库错误信息（无敏感信息） |
| missing_params | list[string] | 缺失的必要参数名 |
| risk_notice | string | 只读安全提示 |

### sql_templates 中每个模板的字段

| 字段 | 类型 | 说明 |
|------|------|------|
| name | string | 模板名称 |
| purpose | string | 用途说明 |
| description | string | 详细描述 |
| params | list[object] | 参数规格（name/required/type/description） |
| sql | string | 渲染后的 SQL（参数完整时）或空（缺参/被阻断时） |
| safe | bool | 是否通过安全检查 |
| reason | string | 状态原因 |
| pending_params | bool | 是否缺少参数 |
| missing_params | list[string] | 缺少的参数名 |
| blocked | bool | 是否被安全检查阻断 |

## 场景列表

| 场景 | 关键词示例 | 说明 |
|------|-----------|------|
| weekly_check | 每周巡检、周报、统计、跑批 | 整体状况统计 |
| single_item_check | 没价格、价格不更新、商品异常 | 单商品排查 |
| crawler_check | 采集中、爬虫未回调、爬取失败 | 爬虫链路排查 |
| relation_check | 无同款、同款不足、同款重复 | 同款关系排查 |
| task_check | 推送失败、客户没收到、任务积压 | 任务链路排查 |
| knowledge_qa | 其他运维知识问题 | 知识问答兜底 |

## 错误响应

非中核问题返回 HTTP 400：

```json
{"detail": {"error_code": "OUT_OF_SCOPE", "message": "当前 Agent 只支持中核 SaaS 运维，tenant_id=188605，commodity_pool_id=222，不处理其他租户或非中核专项问题。"}}
```

## 运行测试

```bash
pytest tests/ -v
```

当前共 244 个测试用例，覆盖：
- SQL 安全校验（白名单表、写库拦截、多语句、注释、task_execute 条件）
- 范围守卫（非中核租户拒绝）
- SQL 模板渲染（参数类型校验、值安全、双重引号、残留占位符）
- 知识库脱敏（fenced block、HTML pre、缩进代码块、HTML 实体）
- /health 健康检查
- /diagnose 集成测试

## 项目结构

```
cnnc-ops-agent/
├── app/
│   ├── main.py               # FastAPI 入口 + 请求/响应模型
│   ├── config.py              # 配置（路径、版本号、只读模式）
│   ├── agent/
│   │   ├── router.py          # scope_guard（中核范围检查）+ 场景识别
│   │   ├── planner.py         # 诊断计划生成（场景→步骤→表）
│   │   ├── sql_selector.py    # SQL 模板加载/参数校验/渲染/安全检查
│   │   ├── safety_guard.py    # SQL 安全守卫（白名单+写库拦截+task_execute条件）
│   │   └── result_analyzer.py # 结果解读提示
│   ├── db/
│   │   └── mock_client.py     # 离线 Mock 客户端（不执行 SQL）
│   ├── services/
│   │   └── knowledge_loader.py # 知识库加载/搜索/写库SQL脱敏
│   └── templates/             # 5 个 SQL 模板 YAML
│       ├── weekly_check.yaml
│       ├── single_item_check.yaml
│       ├── crawler_check.yaml
│       ├── relation_check.yaml
│       └── task_check.yaml
├── data/docs/
│   └── cnnc_ops_knowledge_pure.md  # 中核运维知识库
├── docs/
│   └── stage2-design.md       # 第二阶段设计文档
├── tests/                     # 244 个测试用例
├── requirements.txt           # Python 依赖
├── .env.example               # 环境变量示例
└── README.md                  # 本文件
```

## 安全机制

系统有三层安全防护：

1. **scope_guard**（`router.py`）：拒绝非中核租户的问题
2. **safety_guard**（`safety_guard.py`）：SQL 安全校验，只允许 SELECT/WITH，只允许白名单表，task_execute 必须限定中核条件
3. **knowledge_loader**（`knowledge_loader.py`）：知识库中写库 SQL 自动替换为脱敏提示

## 环境变量

参考 `.env.example`：

```
KNOWLEDGE_MD_PATH=data/docs/cnnc_ops_knowledge_pure.md
TEMPLATES_DIR=app/templates
LOG_LEVEL=INFO
```

所有环境变量都有默认值，无需配置即可运行。
