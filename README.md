# 中核 SaaS 运维诊断 Agent（MVP - 离线只读版）

## 当前版本限制

| 限制项 | 说明 |
|--------|------|
| **只读** | 仅生成 SELECT 查询模板，禁止 UPDATE/DELETE/INSERT 等写操作 |
| **离线** | 不连接生产数据库，不执行任何 SQL |
| **不连生产库** | db 模块为 mock_client，仅返回提示信息 |
| **不执行写库** | safety_guard 强制拦截所有非 SELECT/WITH 语句 |

## 安装依赖

```bash
cd cnnc-ops-agent
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/Mac:
# source .venv/bin/activate

pip install -r requirements.txt
```

## 启动服务

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

启动后访问：
- API 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

## 调用 /diagnose

### 请求

```bash
curl -X POST http://localhost:8000/diagnose \
  -H "Content-Type: application/json" \
  -d "{\"question\": \"最近一周巡检数据怎么样\"}"
```

### 请求体

```json
{
  "question": "你的问题"
}
```

### 响应体

```json
{
  "scenario": "weekly_check",
  "confidence": 0.95,
  "reason": "命中关键词: 每周巡检",
  "diagnosis_steps": ["1. 确认巡检时间范围...", "..."],
  "related_tables": ["commodity_pool_188605", "..."],
  "sql_templates": [
    {
      "name": "pool_total_count",
      "purpose": "统计商品总数与启用数",
      "params": [],
      "sql": "SELECT COUNT(*) ...",
      "safe": true,
      "safety_reason": "安全检查通过",
      "result_hint": "关注 enabled_cnt / total_cnt 占比..."
    }
  ],
  "result_guide": {
    "interpretation": "巡检结果关注...",
    "per_sql_hints": [...]
  },
  "knowledge_snippets": [...],
  "risk_notice": "【只读安全提示】..."
}
```

## 支持的场景

| 场景 | 关键词示例 |
|------|-----------|
| weekly_check | 每周巡检、周报、统计、跑批 |
| single_item_check | 没价格、价格不更新、商品异常 |
| crawler_check | 采集中、爬虫未回调、爬取失败 |
| relation_check | 无同款、同款不足、同款重复 |
| task_check | 推送失败、客户没收到、任务积压 |
| knowledge_qa | 其他运维知识问题 |

## 运行测试

```bash
pytest tests/ -v
```

## 项目结构

```
cnnc-ops-agent/
├── app/
│   ├── main.py              # FastAPI 入口
│   ├── config.py             # 配置
│   ├── agent/
│   │   ├── router.py         # 场景识别
│   │   ├── planner.py        # 诊断计划生成
│   │   ├── sql_selector.py   # SQL 模板选择 + 安全校验
│   │   ├── result_analyzer.py # 结果解读提示
│   │   └── safety_guard.py   # SQL 安全守卫
│   ├── db/
│   │   └── mock_client.py    # 离线 Mock 客户端
│   ├── services/
│   │   └── knowledge_loader.py # 知识库加载
│   └── templates/            # SQL 模板 YAML
├── data/docs/                # 知识库 Markdown
├── tests/
├── outputs/
├── requirements.txt
├── .env.example
└── README.md
```
