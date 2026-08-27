# CNNC STEP_1 实时中文只读监控驾驶舱 — 设计 Spec(V2)

- 日期:2026-08-26 · 修订:V2(首页信息架构按用户反馈整体重做:Current-stage-first + Exception-first + Progressive disclosure;后端架构不变)
- 修订:V2.1(2026-08-27 Independent Review Fix Round 1):§4.2 R1 不再把合法 RECOVERY_EXHAUSTED 计入红色人工、新增 R4.5「等待最终收口」;§4.5 颜色口径同步;§8.2 服务常驻降为「登录自启一次」的 ACCEPTED_RISK;§11 隐藏暂停改为真实实现并由 node 行为测试覆盖
- 状态:待用户重审(V1 的信息架构未通过;V2 重提)
- 基线:BASE_BRANCH `origin/prod/closeout-20260818` @ `ccdd194c8dc1ecce7199cfc793b4a4596f61ec61`
- 开发分支:`feat/step1-realtime-dashboard-20260826`(独立 worktree `E:\.A中核运维\cnnc-dashboard-dev`)
- 研究依据:`E:\.A中核运维\dashboard-design\RESEARCH_REPORT_20260826.md`
- 原型:V2 `E:\.A中核运维\dashboard-design\prototype\v2\index.html`(默认展示异常场景,可一键切换正常场景;假数据)

## 1. 背景与目标

用户(非程序员)打开页面 3 秒内、只看第一屏即可回答四问:**今天正常吗?现在在哪一步?我要处理什么吗?为什么还没出表?** 设计三原则:正常信息压缩、异常信息展开、技术信息默认隐藏。所有刷新零业务写入。

## 2. 范围

**做**:STEP_1 单业务日实时只读 Dashboard(V1 不含历史视图)。
**不做(HARD)**:STEP_2/3、总控中心、写操作控制台、登录权限、云部署、Docker、微服务、Scheduler 重构、状态机重构、招商局业务逻辑重构;Dashboard 不得触发任何写操作(创建/重启/禁用/启用/优先级/closeout/finalize/requeue/审批/Scheduler 启停),不得写 SQLite/runtime/任何生产文件。

## 3. 架构(V1 只读,与 V1 spec 相同,未改)

```
Scheduler 与现有 STEP_1 引擎(不动)
  → runtime/incremental_price_audit/<date>/state.sqlite3(唯一状态权威,不动)
      │ SQLite URI mode=ro,短连接,不持锁,零网络,零写入
      ▼
只读投影层(app/services/dashboard_projection.py)
  = collect_runtime_status()(原样复用)+ 4 个只读扩展投影(§5)
      ▼
只读 Web API(app/dashboard_api.py,APIRouter 挂载进现有 FastAPI app,/dashboard 前缀)
  GET /dashboard                       单页
  GET /dashboard/api/summary?date=&since=<指纹>(304 支持)
      ▼
单页前端(app/static/dashboard/,vendored,零 CDN/零构建/零新依赖)
```

## 4. 首页信息架构(V2 核心)

### 4.1 第一层(且仅第一层)允许的区块与信息预算

| 区块 | 内容 | 预算 |
|---|---|---|
| 页头(一行) | 标题·业务日·`● 实时 · 最后更新 X 秒前`·计划任务芯片 | 0 数字膨胀 |
| A 主状态卡(视觉最重) | 一个主状态词 + 一句系统生成中文解释(≤2 行) | 1 词 + ≤2 句 |
| D 需要关注 | 空态=一行"✓ 当前无需人工处理";非空=每条 4 行(问题/影响/系统动作/建议),首屏最多 3 条 + "其余 N 项" | 空态 1 行 |
| B 阶段条(4 段) | 每段:名称 + 状态词(完成/进行中/等待/需人工)+ 1 行关键数字 | 4 段 × ≤2 数字 |
| C 招商局摘要(一行条) | 执行中 X · 等待回收 X · 等待提优 X · 自动恢复中 X · 需要人工 X · 已完成 X + [查看全部招商局任务] | 6 计数 + 1 按钮 |
| E 最终表 | 1-2 句自然中文 | ≤2 句 |

首屏总预算:**≤6 个视觉块、≤15 个数字、0 个英文 enum、0 张 KPI 卡、0 张表格、0 个抽屉**。Detail/模型合并为"数据准备 151/151";一切 sub-state、完整表格、抽屉、技术详情、历史均在二级。

### 4.2 OVERALL_STATUS_PRIORITY(主状态词判定,按序首个命中)

字段全部来自 `collect_runtime_status()`:

```
R0 状态读取失败   [红]  SQLite/API 不可读(服务自身异常,与业务无关)
R1 需要人工处理   [红]  remote.manual>0 ∨ crawl.manual>0 ∨ crawl.unplanned>0
                        ∨ model.terminal>0 ∨ recovery_exhausted 证据不完整>0
R2 今日无数据     [灰]  state=MISSING(工作日未开始/周末)
R3 今日结果已生成 [绿]  final.finalized(forced=true 附黄提示"曾强制收尾")
R4 正在最终收口   [蓝]  final.frozen ∧ ¬finalized
R4.5 等待最终收口 [蓝]  数据准备完成 ∧ 招商局/采集子状态全清(unknown=0、detail.terminal=0)
                        ∧ ¬frozen ∧ ¬finalized(业务工作已结束,等 18:00 口径冻结)
R5 正在回收招商局结果 [蓝] 数据准备完成 ∧ remote.awaiting_harvest>0
R6 正在等待招商局 [蓝]  数据准备完成 ∧ 其余招商局子状态未清(intent/提优/执行)
R7 正在准备数据   [蓝]  detail.pending+running>0 ∨ model.pending+running>0
```

- "数据准备完成" = detail.pending+running=0 ∧ model.pending+running=0(terminal/manual 不降低完成度,只进异常通道)。
- **生产语义修正(2026-08-26 执行期裁定)**:R3(finalized)判定先于 R1 中的 recovery_exhausted——RECOVERY_EXHAUSTED 在出表后仍会在 status 中计数(属可接受终态收敛),若 R1 全面优先会把已完成的业务日误报为"需要人工处理";其余 R1 条件(remote.manual/crawl.manual/crawl.unplanned/model.terminal)在 FINALIZED 日按 readiness 门不可能非零,故该排序无副作用。
- **生产语义修正(2026-08-27 Review Fix Round 1)**:带完整 durable proof(确认停用+确认启用+恢复时间+recovery_attempts=1,与 finalize `_valid_recovery_exhausted_evidence` 同一判定)的 RECOVERY_EXHAUSTED 是安全收口——关联 crawl 已转 NO_RESULT、不阻塞 ordinary finalize、不伪造价格,因此**不再等价于 MANUAL_REQUIRED**,按黄(关注)展示、补采仅属可选人工操作;仅当 proof 不合法(finalize 将判 remote_invalid、NON_TOLERABLE)时才按真实阻塞升级红色人工。
- **存在明确异常(黄)不改主状态词**:remote.unknown>0 ∨ crawl.unknown>0 ∨ remote_health.stall_detected → 强制进入"需要关注"(黄)并写入解释句。
- **系统自动恢复中(蓝)不改主状态词**:计入招商局摘要"自动恢复中 X"并写入解释句;仅 RECOVERY_EXHAUSTED 才升级红。
- **解释句生成铁律(V1 冲突修复)**:存在任何"需要关注"项时,解释句必须逐项提及,禁止出现"无异常/无需人工处理"表述;无关注项时才允许"当前无需人工处理"。
- 阶段条状态:每段 ∈ {完成(绿), 当前(蓝), 等待(灰), 异常(红=需人工 / 黄=系统处理中)};当前段=首个未完成阶段(数据准备→招商局处理→最终收口→最终出表);异常色优先于"当前"显示。

### 4.3 四个业务阶段与 sub-state(点击展开,默认收起)

- ①**数据准备**:SaaS 发现(行数/新增/变更)、Detail、模型校验。
- ②**招商局处理**:等待提交 / 等待提优 / 执行中(含自动恢复)/ 等待回收 / 已完成 / 需要人工 + 恢复用尽。
- ③**最终收口**:数据冻结状态、阻塞清零进度。
- ④**最终出表**:输出文件与校验。

### 4.4 渐进披露规则

- **L0 第一屏**:§4.1 六区块,别无其他。
- **L1 一次点击(同页展开)**:阶段卡 sub-state 计数;[查看全部招商局任务]→完整任务表(商城/任务/中文状态/子任务进度/运行时长/最后更新);最终表"查看详情"→阻塞中文列表 + 文件校验。
- **L2 二级详情**:任务行点击→抽屉(taskId/taskNumber/remote_identity/harvest 次数/恢复时间线:观察≠异常→停滞证明→停用→启用→恢复观察/progress signature/错误证据);技术详情(折叠,英文原值:state、next_actions、remote/crawl/final 原始 JSON)。
- **V1 首页删除项**:近 14 天心跳条(移至后续版本"历史"区,数据投影接口保留但 V1 不渲染)。

### 4.5 色彩(五色,单源 CSS 令牌 + 图标双通道)

绿=完成/正常 · 蓝=当前运行(含自动恢复中) · 灰=等待/未开始 · 黄=系统可自动处理但值得关注(SUBMIT_UNKNOWN、断路器触发、提交待确认、合法 RECOVERY_EXHAUSTED 已安全收口) · 红=人工介入/明确阻塞(MANUAL_REQUIRED、证据不完整的 RECOVERY_EXHAUSTED、模型终态失败、crawl manual/unplanned)。不设青色恢复色系;断路器触发=黄 + 文案"系统已暂停新建任务以保护远端,属保护机制非故障"。(2026-08-27 修订:合法恢复用尽由红改黄,依据 §4.2 生产语义修正。)

## 5. 只读投影(与 V1 spec 相同)

1. `project_remote_jobs(business_date)`:remote_jobs 行级 + crawl 聚合计数 + RUNNING 时最后 observation signature 的子任务进度。
2. `project_recovery_timeline(remote_job_id)`:恢复六分类(正常采集/观察中(≠异常)/已证明停滞/自动恢复中/恢复成功/需要人工处理·恢复已用尽)+ 观察点时间线 + restart 阶段链 + probe_health。
3. `project_recent_days(runtime_root, n=14)`:V1 保留接口,首页不渲染。
4. `project_scheduler_status()`:schtasks 只读查询 + 结果码翻译(267009/0x41301→"当前计划任务正在运行");失败降级"不可用"。
5. 新增 `compute_overall_status(status)`:§4.2 规则的纯函数实现(输入 collect_runtime_status 输出,输出主状态词/颜色/解释句素材/阶段条状态),TDD 全分支覆盖。

## 6. API(与 V1 spec 相同)

`GET /dashboard`、`GET /dashboard/api/summary?date=&since=`(200/304/400/503);响应含 generated_at/fingerprint/status/jobs/recovery/recent_days/scheduler;无任何写端点;不触碰现有路由。

## 7. 测试策略(与 V1 spec 相同,新增)

- `compute_overall_status` 逐条优先级用例(R0-R7、黄不改词、解释句铁律、阶段条状态、forced 提示);
- 投影/API/文案映射全枚举;临时 SQLite fixture,显式 `--basetemp`,不碰生产 runtime;TDD。

## 8. 已决事项(原开放问题,用户已答复)

1. 服务绑定:`127.0.0.1` 仅本机。
2. 服务常驻:**Scheduler 处于启用状态时,页面服务必须可用**。机制:为 Dashboard 服务增加独立的登录自启(独立计划任务或启动项),**绝不触碰现有 `CNNC-PriceAudit-Incremental` 任务**;注册同样走"预览→显式 -Apply"门控;具体形式在 writing-plans 阶段细化。
   - **V1 实际口径(2026-08-27 Review Fix Round 1 裁定 / ACCEPTED_RISK)**:登录自启只在每次登录时启动一次,不是进程 watchdog——uvicorn 中途退出后不会自动拉起,需重新登录或按 runbook 手动启动。曾评估 Task Scheduler restart-on-failure:`schtasks /Create` 命令行无法表达该设置,改用 `Register-ScheduledTask` 的 RestartCount/RestartInterval/ExecutionTimeLimit 组合属本轮无法在不执行 `-Apply` 的前提下验证的系统注册变更,不符合低风险门槛,故不强行实现;如后续需要持续可用,按本条重新立项并走预览→`-Apply` 门控。

## 9. 验收标准(更新)

1. 四问 3 秒/第一屏可答;首屏区块不超出 §4.1 预算。
2. 构造 R0-R7 各状态 + 黄/恢复场景,主状态词与需要关注逐例正确(测试证据)。
3. 连续刷新 1 小时零业务写入;断线横幅/恢复补刷正常。
4. 每字段可溯源字段字典;英文原值仅技术详情。
5. pytest 全绿,证据留档(HEAD SHA/diff/测试输出)供独立 review。

## 10. 交付与 Git

仅 worktree 开发;完成=fresh 全相关套件+commit+push dev 分支后停止;不 merge、不部署、不 push prod、不改现有 Scheduler;生产 checkout 永不作开发目录。

## 11. 实时行为(real-time behavior)

- 前端条件轮询:活跃(生产写入器 active 或 final.computed_pending>0 且未 finalized)5 秒,否则 30 秒;ETag/304 稳态续排;页面不可见暂停、可见即补刷。
- (2026-08-27 Review Fix Round 1:隐藏暂停/回前台补刷/在途去重由 `tests/js/run_dashboard_js_tests.js` 的 node 行为测试证实;首页"实时·最后同步"角标基于最后一次成功 200 或 304,而非 payload generated_at,连续 304 不再误显"最后更新 X 分钟前"。)
- Execution-period ruling (2026-08-26 final review): FINALIZED no longer fully stops polling; instead switches to 30-second slow polling (serving always-on dashboards; cost ≈ one 304 per 30 seconds).
