# 中核 SaaS 运维诊断 Agent

## 第一阶段：离线只读诊断版

**当前版本为第一阶段 MVP，严格限制为离线只读诊断，不具备任何写库或自动运维能力。**

### 核心约束

| 约束 | 说明 |
|------|------|
| **只读** | 仅生成 SELECT 查询模板，禁止 UPDATE/DELETE/INSERT 等写操作 |
| **离线** | 不连接生产数据库，不执行任何 SQL |
| **不连生产库** | db 模块为 mock_client，仅返回提示信息 |
| **不执行写库** | safety_guard 强制拦截所有非 SELECT/WITH 语句 |
| **中核专属** | 仅支持 tenant_id=188605、commodity_pool_id=222 |
| **拒绝非中核** | 宏伟/南水北调/华盛/非 188605 租户问题会被拒绝 |

### 不支持的功能

- 自动重推、自动 UPDATE/DELETE/INSERT
- 连接生产数据库执行 SQL
- 定时任务、自动巡检
- 跨租户查询

## 安装依赖

```bash
cd cnnc-ops-agent
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
```

## 启动服务

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

- API 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

## 调用 /diagnose

### 请求

```bash
curl -X POST http://localhost:8000/diagnose \
  -H "Content-Type: application/json" \
  -d "{\"question\": \"中核本周巡检\", \"params\": {\"start_time\": \"2026-06-08\", \"end_time\": \"2026-06-15\"}}"
```

### 请求体

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| question | string | 是 | 用户问题 |
| params | object | 否 | SQL 模板参数，如 commodity_id、start_time 等 |

### 响应体

| 字段 | 类型 | 说明 |
|------|------|------|
| scenario | string | 识别的场景类型 |
| confidence | float | 识别置信度 |
| reason | string | 识别原因 |
| diagnosis_steps | list[string] | 排查步骤 |
| related_tables | list[string] | 涉及的中核表 |
| sql_templates | list[object] | SQL 模板列表（含 safe/blocked/pending_params 状态） |
| result_guide | object | 结果解读提示 |
| knowledge_snippets | list[object] | 知识库检索片段（写库 SQL 已脱敏） |
| missing_params | list[string] | 缺失的必要参数 |
| risk_notice | string | 只读安全提示 |

### 场景列表

| 场景 | 关键词示例 |
|------|-----------|
| weekly_check | 每周巡检、周报、统计、跑批 |
| single_item_check | 没价格、价格不更新、商品异常 |
| crawler_check | 采集中、爬虫未回调、爬取失败 |
| relation_check | 无同款、同款不足、同款重复 |
| task_check | 推送失败、客户没收到、任务积压 |
| knowledge_qa | 其他运维知识问题 |

### 错误响应

非中核问题返回 HTTP 400：

```json
{"detail": {"error_code": "OUT_OF_SCOPE", "message": "当前 Agent 只支持中核 SaaS 运维..."}}
```

## 运行测试

```bash
pytest tests/ -v
```

## 项目结构

```
cnnc-ops-agent/
├── app/
│   ├── main.py              # FastAPI 入口 + 响应模型
│   ├── config.py             # 配置
│   ├── agent/
│   │   ├── router.py         # 场景识别 + scope_guard
│   │   ├── planner.py        # 诊断计划生成
│   │   ├── sql_selector.py   # SQL 模板选择 + 缺参检测
│   │   ├── result_analyzer.py # 结果解读提示
│   │   └── safety_guard.py   # SQL 安全守卫（表白名单 + task_execute 条件）
│   ├── db/
│   │   └── mock_client.py    # 离线 Mock 客户端
│   ├── services/
│   │   └── knowledge_loader.py # 知识库加载 + 写库 SQL 脱敏
│   └── templates/            # SQL 模板 YAML（中核范围限定）
├── data/docs/                # 知识库 Markdown
├── tests/
├── outputs/
├── requirements.txt
├── .env.example
└── README.md
```
