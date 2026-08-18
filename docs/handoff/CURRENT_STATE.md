# CURRENT_STATE.md — 当前项目进度

> 最后更新：2026-06-26

---

## 一、当前项目状态

**生产可用，日常运行中**。项目已从中核 SaaS 运维诊断工具演进为价格审计 + 监控状态查询的完整工具集，每日处理中核待审批商品数据。

- Git 分支：master
- 最新 commit：`186614a fix: use millisecond timestamp in zhaoshang task name to avoid skip_if_exists collision`
- 工作区有大量未提交的 .xlsx 产物（正常，gitignore 未完全覆盖根目录 xlsx）

---

## 二、已完成能力

### 1. 价格审计全流程（`scripts/audit_saas_excel_file.py`）
- SaaS JSON + 客户 JSON 多段解析容错（补缺右花括号、段间垃圾跳过）
- 中核 detail 接口取客户最高价（四级优先级提取 + 缓存 + 间隔限速）
- 大模型批量标题同款校验（去重 + 分批 30 条 + 并发 12 批）
- multi_mall_crawler 爬 28 个商城页面价格（非京东/非震坤行）
- 招商局自动建任务爬京东+震坤行（+ 鑫方盛/阳采/浙江物产/得力/领先未来等）
- 价格回填 + 计算（saas_price_vs_customer / price_changed / over_30pct / final_decision）
- 中文表头 Excel 输出（高亮 + 冻结首行 + 自动筛选）
- cURL 解析（支持 Windows ^ 续行 + shlex 解析）

### 2. 监控状态查询（项目外 `cnnc_to_saas_monitor.py` + `monitor_status_export.py`）
- 中核接口拉取待处理数据 → SaaS 监控接口查状态 → 合并输出
- 状态判断：可推送(计算成功) / 可推送(无价格) / 同款匹配中 / 同款匹配异常 / 价格计算失败 / 未知
- 自动归档到 `runtime/saas状态查询/<时间戳>/`
- 支持本地 JSON 文件输入 + 直接请求接口两种模式
- 分页获取全部数据

### 3. 离线诊断 API（旧，`app/main.py`）
- /health 健康检查
- /diagnose 运维诊断（6 个场景 + SQL 模板 + 知识库）
- /workflows/* 工作流 API（work_01 / work_02 / work_03）
- 三层安全防护（scope_guard / safety_guard / knowledge脱敏）

### 4. 测试
- 577 个 pytest 用例（SQL安全/范围守卫/模板渲染/知识脱敏/workflow API/审计规则）

---

## 三、未完成事项

| 事项 | 说明 |
|------|------|
| work_03 同款不足补同款 | `base.py` 中是 R3.5 placeholder，真正规则待 R4.5 实现 |
| README.md 过时 | 仍描述"离线只读诊断工具"，未涵盖价格审计和监控查询 |
| .gitignore 不完整 | 根目录大量 .xlsx 产物未被忽略，git status 很乱 |
| docs/SAAS_AUDIT_FLOW.md 重复内容 | 同一文档内"流程步骤详解"重复了 6 次以上，需清理 |
| 招商局 token 硬编码 | `auto_crawl_task.py` 内置 `_DEFAULT_BLADE_AUTH`，过期需手动改代码 |
| SaaS监控缺失数据处理 | `cnnc_to_saas_monitor.py` 发现部分 documentId 在 SaaS 查不到，原因待确认 |

---

## 四、当前已知问题

| 问题 | 影响 | 临时处理 |
|------|------|---------|
| 根目录 xlsx 文件未 gitignore | git status 噪音大 | 建议补充 .gitignore |
| SAAS_AUDIT_FLOW.md 内容重复 | 文档冗长难读 | 需去重整理 |
| work_01_saas_approval_json_audit.py 中有未使用变量 | `results` vs `results_list` 混用（见旧代码 1228 行） | 待确认是否影响 summary |
| 模型偶尔超时 | 批量并发 12 批时 | TITLE_MATCH_MODEL_TIMEOUT 可调大 |

---

## 五、最近一次重要修改

### 2026-06-26：新增监控状态查询工具
- 新增 `E:\.A中核运维\monitor_status_export.py` — SaaS 监控状态导出（本地JSON/接口两种模式）
- 新增 `E:\.A中核运维\cnnc_to_saas_monitor.py` — 中核→SaaS 监控状态汇总
- 修改 `monitor_status_export.py` 状态判断逻辑：`syncPriceStatus` 为空算"可推送(无价格)"而非问题数据
- 两个脚本默认输出到 `runtime/saas状态查询/<时间戳>/` 下

### 2026-06-24：招商局集成优化
- 招商局任务名改用毫秒时间戳避免 skip_if_exists 冲突
- 新增 stbchina 专用爬虫 + epec 接入招商局
- lxwl（66123123.com）接入招商局

---

## 六、当前模型调用情况

- **模型**：doubao-seed-2-0-mini-260215（火山方舟 Ark）
- **用途**：标题同款校验（queryStr vs skuName）
- **配置**：`data/model.env`
- **批量参数**：batch_size=30，concurrency=12，timeout=180s
- **状态**：正常可用（SET）

---

## 七、当前依赖的外部服务

| 服务 | 地址 | 认证 | 过期处理 |
|------|------|------|---------|
| SaaS 监控接口 | `http://saasportal.iio.com/improxy/saas/iio2/commodityMonitorResult/query` | Cookie: x-access-token | 浏览器 F12 重新抓 |
| SaaS 同款审批接口 | `http://saasportal.iio.com/improxy/saas/iio2/sameAudit/queryAll` | Cookie: x-access-token | 同上 |
| 中核商品列表接口 | `https://www.cnncmall.com/emp/onecode/improxy/iio2/quasiStandardCommodityDocumentModel/query` | y1AmuISe(URL+header) + Cookie | 浏览器 F12 重新抓 |
| 中核商品详情接口 | `https://www.cnncmall.com/.../detail` | 同上 | 同上 |
| 招商局任务系统 | `http://zhaosj.iioprod.com/improxy/iio/crawl/crawlTaskManage/exportTask` | Blade-Auth: bearer + Cookie | 浏览器 F12 重新抓 |
| 大模型 API | `https://ark.cn-beijing.volces.com/api/v3/chat/completions` | API KEY | data/model.env |
| multi_mall_crawler | `E:\.A2026\多商城爬取\multi_mall_crawler` | 各商城 cookies | cookies/<域名>.json |

**认证存放位置**：
- SaaS + 中核：`E:\.A中核运维\自动拉取\.env`（SAAS_COOKIE / CNNC_COOKIE / CNNC_QUERY_Y1AMUISE / CNNC_HEADER_Y1AMUISE）
- 招商局：`E:\.A2026\招商局自动建任务\external_tools\crawl_task_tool\auto_crawl_task.py` 内置变量
- 模型：`E:\.A中核运维\cnnc-ops-agent\data\model.env`
- detail cURL：`E:\.A中核运维\cnnc-ops-agent\data\detail_request.curl`

---

## 八、接下来建议优先处理的任务

1. **更新 README.md**：补充价格审计和监控查询的说明，当前 README 只描述旧诊断功能
2. **清理 docs/SAAS_AUDIT_FLOW.md**：去除重复 6 次以上的"流程步骤详解"
3. **完善 .gitignore**：添加根目录 `*.xlsx`、`爬取模板/`、`商城sourceId映射表/`
4. **实现 work_03 真正规则**：同款不足补同款（当前是 placeholder）
5. **招商局 token 外置化**：把 `auto_crawl_task.py` 硬编码 token 改为环境变量
6. **排查 SaaS 监控缺失数据**：部分 documentId 在 SaaS 监控查不到，需确认是时序还是关联问题

---

## 九、哪些地方后续 AI 不能乱改

| 文件/模块 | 原因 |
|-----------|------|
| `app/agent/safety_guard.py` | SQL 安全白名单，放宽会导致写库 SQL 泄漏 |
| `app/agent/router.py` 的 scope_guard | 租户范围限制，放宽会处理非中核数据 |
| `app/workflows/human_approval_guard.py` | 确保"建议-only"模式，禁止自动执行 |
| `app/services/title_match_model_adapter.py` 的 prompt 规则 | 标题校验业务规则，改了影响同款判定准确率 |
| `scripts/audit_saas_excel_file.py` 的 final_decision 逻辑 | 价格审计决策核心，改了影响驳回/通过判定 |
| `.gitignore` 中已排除的 data/runtime 条目 | 不要移除，否则会提交 secrets |
| `data/model.env` / `data/detail_request.curl` / `.env` | 含真实密钥，禁止提交 git |
