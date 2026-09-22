# 中核 SaaS 运维 Agent

面向中核 SaaS `tenant_id=188605`、`commodity_pool_id=222` 的日常运维工具集，主要完成价格审计、监控状态查询和不能直接推送数据处理，并提供必须二次授权的文档 ID 受控补推入口。旧 FastAPI 诊断服务仍保留，但不是每日三步主流程。

## 每日三步

| 步骤 | 业务目标 | 固定入口 | 输出 |
|---|---|---|---|
| 第一步 | 当前待审批数据的客户价、标题同款、商城价和最终决策 | `scripts/audit_saas_excel_file.py` | `runtime/price_audit_<时间戳>/saas_audit_final_<时间戳>.xlsx` |
| 第二步 | 当前中核待处理数据的 SaaS 采集/同款/价格状态 | `scripts/export_current_monitor_status.py` | `runtime/saas状态查询/<时间戳>/中核待处理_SaaS监控状态_<时间戳>.xlsx` |
| 第三步 | 处理指定监控表中 `是否可推送=否` 的数据 | `scripts/process_monitor_no_push_zhaoshang.py` | `runtime/模板以及推送失败数据/<时间戳>/` 下三文件和缺价清单 |

三步是业务分类，不是串行执行队列：三个步骤可以并行，同一步骤也可以同时运行多个独立批次。

详细执行规则：

- [第一步价格审计](docs/runbooks/STEP_1_PRICE_AUDIT.md)
- [第一步增量调度](docs/runbooks/STEP_1_INCREMENTAL_SCHEDULER.md)
- [第二步监控状态](docs/runbooks/STEP_2_MONITOR_STATUS.md)
- [第三步不能推送数据处理](docs/runbooks/STEP_3_NO_PUSH_PROCESSING.md)
- [SaaS 受控价格补推](docs/runbooks/SAAS_CONTROLLED_PUSH.md)
- [测试和验收](TESTING.md)

## Agent 接管

新会话不再通读全部历史文档。最小读取集为：

1. `AGENTS.md`
2. `CURRENT_STATE.md`
3. 当前步骤对应的一个 `docs/runbooks/STEP_*.md`
4. 接管已有批次时，再读指定运行目录中的 `BATCH_STATE.md`
5. 需要运行或改代码时，再读 `TESTING.md` 对应章节和入口代码

`CURRENT_STATE.md` 只保存并行批次协议和提示词，不保存唯一当前任务。每个批次在自己的运行目录维护 `BATCH_STATE.md`，所以三个步骤及同一步的多个批次可以同时运行，彼此不阻塞。批次暂停等待授权时可以在原任务继续；达到上下文阈值只提醒并准备交接，不会因此停止执行。

## 核心安全边界

- 中核专属，拒绝其他租户或商品池。
- 不自动审批、驳回、保存、提交或推送；指定审计表的受控通过/驳回及指定文档 ID 的受控补推必须分别按 runbook 预览、锁定并二次授权。
- 除既有工具对本轮招商局当前任务执行的受控优先级操作外，不执行任何 SQL。
- 招商局 `/add` 是真实写接口，不能用于认证或连通性探测。
- 招商局后仍缺价时先暂停；本地脚本和 detail 参考补全都需要本批次单独授权。授权本地脚本后，用 `scripts/apply_step3_local_fallback.py` 按完整 URL 回填到新模板；评论数只允许数值或空白。
- cURL 统一由 `scripts/curl_utils.py` 解析，内网请求绕过系统代理。
- SaaS 和招商局运行时认证由 `scripts/auth_manager.py` 管理；首次从 cURL 初始化，之后只读取被忽略的 `data/auth_state.json`。
- 密钥、Cookie、Token、数据库值和登录态文件不写入日志、文档或 Git。
- 每次运行使用新时间戳目录，不覆盖 `data/` 输入和历史 `runtime/` 输出。

完整红线见 `AGENTS.md`。

## 快速准备

```powershell
cd E:\.A中核运维\cnnc-ops-agent
python -m pip install -r requirements.txt
$env:PYTHONIOENCODING='utf-8'
python -m pytest --collect-only -q
```

生产运行前按对应 runbook 检查本地 cURL、模型配置、招商局配置和用户授权。不要直接照抄历史命令，因为认证、批次路径和授权范围都会变化。

## 关键输入

| 文件 | 用途 |
|---|---|
| `data/latest_saas_sameAudit_queryAll.curl` | 第一步 SaaS 待审批分页查询 |
| `data/latest_cnnc_query.curl` | 第一步/第二步/第三步中核列表查询 |
| `data/detail_request.curl` | 第一步/第三步中核 detail 查询 |
| `data/saas_zhuangtai.curl` | 第二步 SaaS 监控查询及受控补推的只读映射模板 |
| `data/latest_zhaoshang_add.curl` | 第一步/第三步招商局正式建任务 |
| `data/latest_zhaoshang_exportTask.curl` | 招商局导出 |
| `data/auth_state.json` | SaaS/招商局当前旋转 Token；原子更新、默认不提交，存在后禁止回退 cURL |
| `data/model.env` | 第一步标题同款模型 |
| `data/ZHAOSHANG_DB.env` 或当前进程 `ZHAOSHANG_DB_*` | 当前招商局任务优先级；不得输出值 |
| `商城sourceId映射表/` | 商城、sourceId、域名映射 |

这些文件大多包含本地认证或运行数据，已由 `.gitignore` 排除。cURL 保持不变并作为接口模板；只有 `auth_state.json` 不存在时才从中初始化 Token。状态刷新失败时暂停并要求人工重新登录，不得回退旧 cURL，也不要把值复制到代码或 Markdown。

## 目录

```text
cnnc-ops-agent/
├── app/                              FastAPI、工作流和业务服务
├── scripts/                          三步入口、受控补推、cURL 工具、Excel/爬价适配
├── tests/                            pytest 回归测试
├── data/                             本地输入和认证，默认不提交
├── runtime/                          分批次运行输出，默认不提交
├── docs/runbooks/                    当前三步及专项维护的权威运行手册
├── 爬取模板/                         招商局导入模板
├── 商城sourceId映射表/               招商局商城映射
├── AGENTS.md                         Agent 总规则和上下文生命周期
├── CURRENT_STATE.md                  并行批次发现和交接协议
├── TESTING.md                        验证矩阵
└── README.md                         项目概览
```

## 外部依赖

| 路径 | 用途 |
|---|---|
| `E:\.A2026\招商局自动建任务` | 招商局任务创建、启用、进度和导出 |
| `E:\.A中核运维\cnnc-mall-crawler` | 首轮锁定 review/manifest，经本批次二次授权后独立续跑的四站安全兜底 |
| `E:\.A中核运维\自动拉取` | 历史/辅助数据拉取能力 |
| `E:\.A中核运维\cnnc_to_saas_monitor.py` | 历史监控汇总工具 |
| `E:\.A中核运维\monitor_status_export.py` | 历史本地 JSON/SaaS 监控导出工具 |

当前每日操作优先使用本仓库 `scripts/` 下的固定入口；外部工具只按 runbook 调用。

## 旧服务

```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

- `GET /health`
- `POST /diagnose`
- `POST /workflows/route`
- `POST /workflows/*/analyze`
- `GET /workflows/{workflow_name}/knowledge`

旧诊断只生成经过安全检查的只读 SQL 模板，不连接数据库、不执行 SQL。
