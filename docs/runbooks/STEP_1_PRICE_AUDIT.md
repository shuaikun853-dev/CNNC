# 第一步运行手册：价格审计全流程

> 增量 tick / 18:00 finalize 的调度、状态、freeze 和安全开关见
> [STEP_1_INCREMENTAL_SCHEDULER.md](STEP_1_INCREMENTAL_SCHEDULER.md)。本手册的 legacy 全量流程仍是对照和 rollback 入口。

> 只适用于中核 `tenant_id=188605`、`commodity_pool_id=222`。本会话不得自动进入第二步或第三步。

## 目标

拉取截至当前的完整 SaaS 待审批数据和中核客户数据，补客户最高价，调用标题同款模型，优先通过招商局采集页面价格，重新计算价格规则，并输出本轮最终 Excel。

“跑当前数据”“跑全流程”都表示完整执行本手册，不是只跑模型、只取首页、只建任务或只生成半成品。
审计全流程的默认终点仍是生成建议表，不自动驳回或通过；驳回、通过都是生成最终表后的独立二次授权动作。

## 必需输入

| 输入 | 路径 | 用途 |
|---|---|---|
| SaaS 待审批 cURL | `data/latest_saas_sameAudit_queryAll.curl` | 完整分页接口模板及首次 Token 初始化来源 |
| SaaS 审计后驳回 cURL | `data/sass-bohui.curl` | 受控驳回请求模板；由 reject 请求安全派生只读 queryAll |
| SaaS 审计后通过 cURL | `data/sass-tongguo.curl` | 受控通过请求模板；由 approve 请求安全派生只读 queryAll |
| 中核列表 cURL | `data/latest_cnnc_query.curl` | 完整分页拉取客户记录 |
| 中核 detail cURL | `data/detail_request.curl` | 按 `documentId + fourCategoryId` 取参考价 |
| 招商局 add cURL | `data/latest_zhaoshang_add.curl` | 正式建任务请求模板及首次 Token 初始化来源 |
| 招商局 export cURL | `data/latest_zhaoshang_exportTask.curl` | 导出请求模板 |
| 运行时认证状态 | `data/auth_state.json` | SaaS/招商局分开保存的当前 Token；存在后是唯一认证来源 |
| 模型配置 | `data/model.env` | 标题同款模型 |
| 招商局 DB 配置 | `data/ZHAOSHANG_DB.env` | 通过既有受控加载器注入当前进程的 `ZHAOSHANG_DB_*`，仅供调整本轮当前任务优先级 |
| 商城映射 | `商城sourceId映射表/` | mall/sourceId/域名映射 |
| multi-site 兜底 | `E:\.A中核运维\cnnc-mall-crawler` | 本批次明确授权后处理 8 个受支持商城缺价 |

缺少、无法解析或范围不符时暂停。只检查文件存在、必要键存在和 cURL 可解析；不要显示敏感值。

## 授权范围

用户明确要求跑第一步，即授权本批次的只读查询、模型调用、招商局正式建任务、任务启用、受控优先级调整、等待和导出。

这不授权 multi-site 兜底、史泰博专用脚本或其他本地爬虫。任何本地脚本兜底都要先给出缺价清单，再取得本批次明确确认。`--enable-crawler` 仅是历史 `multi_mall_crawler` 开关，不是新 multi-site 授权门；未经授权不得传它、`--auto-stbchina-fallback` 或 multi-site 续跑参数。

驳回和通过都不在上述默认授权中。用户必须在最终 Excel 生成后，再次指定准确路径、F 列颜色范围和对应动作；一次授权只对该动作、该文件和预览锁定的候选清单有效。

## 固定顺序

1. **建立批次**：使用新时间戳创建 `runtime/price_audit_<时间戳>/`，并立即在其中创建状态为 `ACTIVE / STEP_1` 的 `BATCH_STATE.md`。记录授权范围和目标输出，不写认证值；其他步骤或同一步其他批次的状态不得阻塞本批次。
2. **本地预检**：用 `scripts/curl_utils.py` 解析全部 cURL；由 `scripts/auth_manager.py` 在状态不存在时一次性初始化两组 Token，之后仅从状态刷新并原子保存；确认租户和商品池；确认模型配置键；使用 `scripts.monitor_stb_fallback.load_zhaoshang_db_env` 从 `data/ZHAOSHANG_DB.env` 加载并确认四个 `ZHAOSHANG_DB_*` 键齐全，不显示值。禁止用 `/add` 做健康检查。
3. **拉完整快照**：SaaS 与中核 query 都按接口 `count` 分页到结束，使用无代理请求。分别验证 `实际行数 == count`；任何一侧不等立即停止。
4. **保存快照**：只写本批次目录中的 `saas_json.json`、`customer_json.json`、`fetch_summary.json` 和脱敏日志，不覆盖 `data/` 输入。
5. **关联记录**：按现有主脚本规则匹配 SaaS 与中核记录，保留全部原字段；不得放宽租户、类目或商品边界来提高命中率。
6. **补客户最高价**：每条使用自身 `documentId + fourCategoryId`。同一键只请求一次并复用缓存；有效参考价取最大值。接口失败、单参考价、无参考价分别保留状态，不用列表价或 0 冒充。
7. **模型标题校验**：先去重再分批。生产稳定参数为 `TITLE_MATCH_MODEL_BATCH_SIZE=10`、`TITLE_MATCH_MODEL_BATCH_CONCURRENCY=1`。HTTP、超时、解析失败或缺行必须重试；只有模型成功返回时才可出现业务结论“人工复核”。
8. **生成招商局模板**：按商城/sourceId 拆分，有 URL 时优先按域名确定商城，仅 URL 空时才使用供应商关键词；商品编码必填，URL 可空，仅 URL 而无商品编码的行禁止建任务。按商城内商品编码+URL 去重，模板写到 `爬取模板/<日期>/`，不得覆盖原输入。
9. **招商局全链路**：先刷新招商局 Token，再对每个支持商城执行 `add -> 精确 task_name 查询 -> updateState 启用 -> 当前任务优先级轮询/更新 -> 等待完成 -> exportTask`。任务名在正式创建前生成，固定为 `cnnc_current_opt_<商城名>_<至少14位纯数字时间标识>`；并发默认 6。主任务查询必须精确得到唯一的 taskNumber 和与 `MALL_CONFIG` 一致的 mall code，不得取模糊查询首条或默认商城。每次真实 add、启用、提优和导出后立即把 taskId、taskNumber 和结果写入本批次 `BATCH_STATE.md`。`/add` 发出后任何认证或网络异常都暂停，不得自动重试创建。
10. **受控提优**：`taskNumber` 不是唯一任务标识，同一编号可属于多个商城。必须把本次 add payload 的有效子任务数传入提优工具，先用 `mall` 首列的现有 `idx_opt_distinctmall`，结合 `task_num + create_time` 窗口轮询到数量完全一致的当前商城有效 `task_id`；少于预期继续等，多于预期立即失败。随后以主键 `SELECT ... FOR UPDATE` 做当前读和锁定，仅按这些主键分批更新尚未为 `weight_type=-1` 的有效行；`task_status` 只表示采集进度，不得参与 priority 的 SELECT、UPDATE 或回读成功判定。禁止锁定宽范围 mall 扫描，禁止 `task_num` 单条件或 `batch_num LIKE` 回退。只有 `affected_rows>0` 可报告本次已提优；预查确认全部精确行已为 `-1` 时记录独立的幂等状态，不称为本次提优。无论任务正在执行还是已经完成，只有回读确认 `expected == matched == weight_type=-1` 才可记录 priority 成功；其他 0 行、越界或异常立即暂停，不得放宽 SQL。
11. **回填并复算**：所有首次或后补结果都走同一回填逻辑，重新计算 `page_vs_customer_check`、`price_changed`、`over_30pct`、`final_decision`。数值 0 标记“价格为0”并整行高亮；SaaS JSON 价与本次页面价不同必须标记价格变动。
12. **缺价门与 multi-site 兜底**：汇总招商局未覆盖、采集等待失败、导出无价格和未回填 URL。脚本先生成不得当作 formal final 的 review Excel 和 `multi_site_fallback/manifest.xlsx`，锁定候选数、清单 SHA-256、manifest/review SHA-256、规范化的批次目录和唯一预定 final 路径；计划生成时验证已审查爬虫提交 `28870666aeb99a10494ebb91419aadf3bff4c0a8` 且其工作区干净，把 `BATCH_STATE.md` 设为 `WAITING_USER` 后暂停，零本地爬取。用户对该批次准确数量、SHA-256 和 manifest 二次授权后，只能用 `--resume-multi-site-manifest` + `--resume-review-xlsx` + `--expected-fallback-count` + `--expected-fallback-sha256` 独立续跑；该入口不重跑快照、模型或招商局 add/启用/提优。候选计划准备若失败，只能对已有 review 使用 `--recover-multi-site-plan-from-review`，不得重跑原第一步命令，也不得更改原预定 final。兜底只处理 `nbdeli / guangbo / ofs / lxwlmall / qstbg / ycw / comix / tima` 的严格 host allowlist；续跑再次验证 manifest/review/hash、固定 crawler revision、干净工作区、站点/链接、final 路径和批次状态。全部预检通过后以独占创建 `execution.claim.json` 取得一次性执行权，并从已验证的内存计划和 review 字节生成只供本次 worker/backfill 使用的执行快照；claim 一经创建即视为可能已发送请求，崩溃或 worker 启动失败也不得自动删除或重跑。子进程只继承 Windows/Playwright 运行所需白名单环境变量，不继承认证、模型或数据库密钥。输出必须与锁定输入逐项一致；`SUCCESS` 价格必须是有限且非负 Decimal，0 为 `PRICE_IS_ZERO`。exit 2、全局风控 exit 3、部分/外来/重复输出均停止并保留证据，不生成 formal final，也不自动重跑。回填后必须从整张 final 的 `record_results` 检查未决状态，`PENDING_CRAWL`、`FAILED`、`CRAWLER_PROCESS_FAILED` 等不能标记批次 `COMPLETE`；既有 `NO_RESULT` 仍按原终态语义保留。完整 STEP_1 的 `WAITING_USER + count + SHA-256` 人工授权门不因增量自动兜底而改变。
13. **最终验收**：最终 Excel 行数必须等于本轮 SaaS 总数，文件可打开，所有阶段均有状态证据。完成后把本批次 `BATCH_STATE.md` 改为 `COMPLETE`；不得修改其他批次状态。

## 审计入口

先取得本轮快照，再运行。首次运行不要带任何本地脚本参数：

```powershell
$env:PYTHONIOENCODING='utf-8'
$env:TITLE_MATCH_MODEL_BATCH_SIZE='10'
$env:TITLE_MATCH_MODEL_BATCH_CONCURRENCY='1'
$env:TITLE_MATCH_MODEL_TIMEOUT='180'
$env:TITLE_MATCH_MODEL_RETRY_SLEEP='5'

python scripts\audit_saas_excel_file.py `
  --input-saas-json "runtime\price_audit_<时间戳>\saas_json.json" `
  --input-customer-json "runtime\price_audit_<时间戳>\customer_json.json" `
  --curl-file "data\detail_request.curl" `
  --output-xlsx "runtime\price_audit_<时间戳>\saas_audit_final_<时间戳>.xlsx" `
  --use-model `
  --request-interval 0.5 `
  --zhaoshang-task `
  --zhaoshang-concurrency 6 `
  --zhaoshang-wait-timeout 1800 `
  --zhaoshang-progress-interval 30
```

当前仓库没有“拉快照 + 审计”的单一总入口，不能假装审计脚本会自动拉取最新数据。拉取阶段必须使用现有 cURL 解析/无代理工具并留下 `fetch_summary.json`；不要为每个批次新增永久临时脚本。

## 审计后受控驳回

只有二次授权成立时才可使用 `scripts/reject_saas_audit_excel.py`。认证固定使用 `data/sass-bohui.curl`。脚本默认取 `record_results` 中 F 列固定填充色 `FFFFC7CE` 和 `FFFFFF00` 的行；用户只授权黄色时必须传 `--fill-colors yellow-only`，要求 U 列“客户价”非空时还必须传 `--require-customer-price`。脚本按表头“客户价”定位，空值或空白字符串不得进入清单，数值 0 视为非空。全部模式都锁定 `tenantId=188605` 和 `commodityPoolId=222`，执行时必须提供预览得到的候选数与清单 SHA-256。

写入前必须完整分页查询当前待审集，候选任一不在待审集就暂停；若用户后续授权排除已处理行，必须重新锁定排除数、剩余数和新清单 SHA-256，写前数量再变化就再次暂停。每批只有 `rspCode=0000` 才记录成功，任一异常就停止后续批次。全部返回成功后再查待审集，候选仍有遗留时不得报告全部完成。日志只写新 `runtime/saas_reject_<时间戳>/`，不记录 Cookie 或 Token，不覆盖审计表。

## 审计后受控通过

只有用户对指定最终 Excel 二次明确授权“通过”后，才可使用 `scripts/approve_saas_audit_excel.py`。认证固定使用 `data/sass-tongguo.curl`，不得改用其他 cURL；脚本通过 `scripts/curl_utils.py` 解析其 approve 请求并安全派生无筛选的只读 queryAll。脚本只取 `record_results` 中 F 列不是固定红色 `FFFFC7CE`、也不是固定黄色 `FFFFFF00` 且完整分页查询后当前仍待审的行；用户要求 U 列“客户价”非空时必须传 `--require-customer-price`，脚本按表头定位，空值或空白字符串排除，数值 0 视为非空。全部模式都锁定 `tenantId=188605` 和 `commodityPoolId=222`；红/黄色行无论当前状态如何都不得进入通过清单。

先运行不带 `--execute` 的只读预览，由脚本通过 `scripts/curl_utils.py` 解析本地 cURL、绕过代理并分页取得当前待审集，输出候选数和清单 SHA-256。同一 `vCommodityId + skuId` 在 Excel 出现多行时，清单保留全部来源单元格，写入 payload 只提交一个业务键。执行时必须同时提供预览的候选数与 SHA-256；任一变化立即暂停。每批调用 `sameAudit/approve` 后只有 `rspCode=0000` 才记录成功，任一异常停止后续批次。全部成功后重新完整分页，目标候选遗留数必须为 0。证据只写新 `runtime/saas_approve_<时间戳>/` 的 `approve_manifest.json`、`approve_events.jsonl` 和 `approve_summary.json`，不记录 Cookie 或 Token，不覆盖审计表。

## 必须暂停

- SaaS 或中核实际分页行数与 `count` 不一致。
- 中核 query/detail 认证、代理或请求头大小写异常。
- 模型配置存在但持续失败。
- 招商局 `/add` 认证失败、payload/模板错误、任务创建或启用失败。
- 优先级复合身份或主键范围异常、存在待更新行但实际更新 `affected_rows=0`、回读未全部为 `-1`、需要放宽 SQL 才能继续。
- 招商局未覆盖、导出无价格或仍有未回填链接，且用户尚未授权脚本兜底。
- multi-site manifest/输入错误、worker 异常，或任一站触发全局风控停止。
- 最终 Excel 行数、价格回填数或决策统计无法对账。

## 完成汇报

至少给出：SaaS 总数、中核总数、客户价状态分布、模型结论分布、招商局成功/失败商城、priority 更新/幂等确认任务数、招商局回填数、multi-site 兜底站点/exit code/回填数、0 价格数、明确 `NO_RESULT` 数、价格变动数、最终决策分布、缺价数、输出和日志路径。`NO_RESULT` 必须保留原缺失证据且价格留空，不得与真实 0 价格合并。
