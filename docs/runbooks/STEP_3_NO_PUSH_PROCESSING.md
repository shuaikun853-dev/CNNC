# 第三步运行手册：处理监控表不能直接推送的数据

> 只处理用户指定的第二步监控表批次中 `是否可推送=否` 的记录。不得重新拉第一步或第二步全量数据，不得调用审批/推送接口。

## 目标

按每条记录的 `documentId + fourCategoryId` 查询中核 detail、提取参考链接、按链接数分流；对可处理链接先只读复用 Shared Crawl Cache 中已经爬好的客户链接结果，未形成完整可用证据的链接才进入招商局；经用户确认后才运行指定脚本或 detail 参考补全；最终生成三类固定文件和缺价清单。第三步需要的商城实采字段固定为商品标题、页面真实 SKU ID、价格和渠道；京东、苏宁还必须有评论数。

## 必需输入

| 输入 | 路径或来源 |
|---|---|
| 指定监控表 | `runtime/saas状态查询/<时间戳>/中核待处理_SaaS监控状态_<时间戳>.xlsx` |
| 中核 query cURL | `data/latest_cnnc_query.curl` |
| 中核 detail cURL | `data/detail_request.curl` |
| 招商局 add cURL | `data/latest_zhaoshang_add.curl` |
| 招商局 export cURL | `data/latest_zhaoshang_exportTask.curl` |
| 招商局运行时认证 | `data/auth_state.json`；存在后是唯一 Token 来源 |
| 招商局 DB 配置 | `data/ZHAOSHANG_DB.env`，仅通过 `--zhaoshang-db-env` 加载到本进程 |
| 商城映射 | `商城sourceId映射表/` |
| 客户链接预爬缓存 | 默认 `runtime/incremental_price_audit/shared_crawl_cache.sqlite3`；第三步仅只读，可用 `--shared-cache-path` 显式覆盖 |
| 固定入口 | `scripts/process_monitor_no_push_zhaoshang.py` |

若目录中有多个 Excel，只选择文件名严格匹配当前批次的监控状态表，不猜最新、不拿历史模板当输入。

## 授权开关

| 用户授权 | 参数 | 结果 |
|---|---|---|
| 默认第三步 | 无额外开关 | detail + 招商局 + 导出回填；缺价后暂停 |
| “先不跑招商局” | `--skip-zhaoshang` | 只产出分流文件和缺价清单，不建任务、不执行 SQL、不跑脚本 |
| “本批次史泰博直接跑” | `--auto-stbchina-fallback` | 仅处理招商局后仍缺价的 `stbchina.cn` URL |
| “本批次允许本地脚本兜底” | 独立运行 `scripts/apply_step3_local_fallback.py` | 先消费当前批次多商城脚本结果，按完整 URL 向新模板回填有效正价格、标题、SKU 和数字评论数；不覆盖前一阶段模板 |
| “本批次允许 detail 参考补全” | `--detail-reference-fallback` | 只补同 `documentId + 完整 URL` 且价格、标题同时为空的行 |

以上各兜底授权相互独立，也不能跨批次沿用。用户没有明确说就不得启用对应参数或独立入口。

## 固定顺序

1. 使用唯一时间戳建立本批次输出目录，并立即创建状态为 `ACTIVE / STEP_3` 的 `BATCH_STATE.md`，写明监控表绝对路径、批次和三个授权开关。其他步骤或同一步其他活跃批次不得成为阻塞条件。
2. 打开监控表并只筛选 `是否可推送=否`；保存否行数作为总账。保留 `documentId`、四级类目、客户描述、原始状态和失败原因。
3. 优先使用表内 `documentId/fourCategoryId`；字段缺失时才用中核 query 补充。detail 同一请求键只调用一次并缓存。
4. 同时检查 detail 参考数组和顶层链接字段，保留原始查询参数，按完整 URL 去重。接口失败或解析异常必须进入日志。
5. 有效链接 `>=2` 的记录进入 zhimport 范围；0 或 1 条进入“直接推送失败”。两侧记录数必须与监控表否行数对账。
6. 优先使用现有 `SKUID`、商品编码、uid；缺失时才从 URL 的 `itemId`、`skuId`、`id` 或详情路径提取。创建任务前按“商城 + 商品编码/URL”去重；仍无法提取的写待确认清单。
7. 在任何招商局创建动作之前，以“商城标识 + 完整 URL”只读查询 Shared Crawl Cache。缓存命中只有同时具备有效正价格、非空商城商品标题和 `real_sku_id` 才可直接用于第三步；缓存中的 mall 是渠道边界，必须与当前 URL 映射商城一致。京东 `jd`、苏宁 `suning` 还必须存在评论数；整数评论使用 `comment_count`，招商局返回 `1万+` 等缩写时可读取证据 payload 的 `comment_count_raw`，但写模板前必须换算为数字。完整命中后回填 `SKUID / 互联网标题 / 价格 / 销量或评论数`，保持供应商编码/名称作为渠道，并把该 URL 从本批招商局待爬集合剔除。缓存缺失、不完整、渠道冲突，或京东/苏宁缺评论时不得伪装命中，继续原招商局流程。
8. 除 `--skip-zhaoshang` 外，把未命中缓存的链接标准化为 `CrawlRequest`，正常链接经共享 `run_batch_zhaoshang_requests` 执行 `autoAddTask -> exact executeNumber materialization -> priority -> queryTaskProgress -> exact SQL result`；`purpose=step3-no-push` 只用于审计和恢复。状态写入本批次目录的 `manual_zhaoshang_batch.sqlite3`，必须 intent before POST；响应未知只 reconcile，不得盲目重复 POST。`--zs-create-only` 对 Batch 只推进到 priority 已确认。
9. Batch 支持集合和严格 URL gate 只来自共享 adapter。无法提取商品编码、商城映射不完整或 URL 未通过 gate 的单条链接才进入原 legacy `create-only -> priority -> wait/export` 安全 fallback；不得让 fallback 扩大到同商城的正常链接。legacy fallback 在创建前刷新并原子保存招商局 Token，按商城顺序创建、启用和受控提优，再有界并发等待和导出；继续保留至少 14 位 marker、`ADD_INTENT`、exact taskName/taskNumber/mall、按精确主键提优、已有任务不重复 add/启用/提优和未知响应不重试规则。等待 worker 只允许 query/wait/export，不得 add、启用或执行 SQL；`--zs-create-only` 保持原 legacy 语义。
10. Batch 结果直接按 source-scoped exact executeNumber 和提交 URL 映射到原业务记录，不伪造旧导出 Excel；legacy fallback 仍验证并读取真实导出文件。两条路径都必须保留页面真实 SKU、标题、价格、商城标识和评论。写任何模板前，评论数统一规范为非负数字；`未识别`、其他非数字、布尔值或非有限数一律写为空白。只有同一次有效采集同时得到价格时才回填商城标题；没有价格不得用客户标题、历史标题或推测标题填“商城商品标题”。京东/苏宁有价格但无评论数时必须标记“评论数缺失”，**不得写入可导入 zhImport 的完整结果模板**，而应进入待确认清单保留已取得的价格、标题和 SKU 证据；评论数补齐后才可作为第三步完整结果。
11. 招商局阶段后先生成各商城成功/缺价/失败清单。没有脚本授权时把本批次 `BATCH_STATE.md` 设为 `WAITING_USER` 并暂停当前批次；用户授权后可以在原任务继续，其他批次不受影响。
12. 已授权史泰博时，先读外部 `AGENT_STBCHINA_FAST.md`，只使用 `scripts/stbchina_fast_crawler.py`，且只传本批次仍缺价的 `stbchina.cn` URL。遇到限制立即停止。
13. 用户授权多商城本地脚本后，只消费本批次脚本结果中 `采集状态=成功` 且价格为有限正数的记录；用 `scripts/apply_step3_local_fallback.py` 按完整 URL 精确回填到新模板。已有和新取评论数都经过同一数字规范化，取不到或无法识别时留空；单据 ID 和 SKUID 保持 Excel 文本。
14. 已授权 detail 参考补全时，只补价格和互联网标题同时为空、且本次 detail 中同 `documentId + 完整 URL` 精确命中的行。来源标记 `中核detail参考回填`，不得称为商城实采价。
15. 每个补价阶段都另存新文件并重新生成处理日志和剩余清单，不覆盖招商局阶段文件。
16. 最终核对否行数、detail 状态、预爬缓存复用数、分流、商城任务、提优、回填、京东/苏宁缺评论数和缺价数；完成后把本批次 `BATCH_STATE.md` 改为 `COMPLETE`，不得修改其他批次状态。

## 固定命令

```powershell
python scripts\process_monitor_no_push_zhaoshang.py `
  --monitor-xlsx "runtime\saas状态查询\<时间戳>\中核待处理_SaaS监控状态_<时间戳>.xlsx" `
  --cnnc-query-curl-file "data\latest_cnnc_query.curl" `
  --detail-curl-file "data\detail_request.curl" `
  --zhaoshang-export-curl-file "data\latest_zhaoshang_exportTask.curl" `
  --zhaoshang-db-env "data\ZHAOSHANG_DB.env" `
  --timestamp "<时间戳>" `
  --zs-wait-timeout 600 `
  --zs-wait-concurrency 6
```

仅在对应授权存在时追加 `--skip-zhaoshang`、`--auto-stbchina-fallback` 或 `--detail-reference-fallback`。不要为每个批次生成临时执行脚本。

## 固定输出

目录：`runtime/模板以及推送失败数据/<时间戳>/`

基础文件：

1. `导入zhimport模版_<时间戳>.xlsx`
2. `处理日志_<时间戳>.xlsx`
3. `直接推送失败的数据_<时间戳>.xlsx`
4. `待确认脚本兜底清单_<时间戳>.xlsx` 或等价明确清单

史泰博授权后的文件另存为 `导入zhimport模版_<时间戳>_招商局_STB补价后.xlsx` 和 `STB补价后仍无价格清单_<时间戳>.xlsx`。

多商城本地脚本授权后的文件另存为 `导入zhimport模版_<时间戳>_本地脚本兜底后.xlsx`、`本地脚本兜底日志_<时间戳>.xlsx` 和 `本地脚本兜底后仍无价格清单_<时间戳>.xlsx`。

detail 参考补全授权后的文件另存为 `导入zhimport模版_<时间戳>_招商局_STB_DETAIL参考补全后.xlsx`、对应阶段日志和 `DETAIL参考补全后仍无价格标题清单_<时间戳>.xlsx`。

## 必须暂停

- 监控表不存在、无法识别 `是否可推送` 或否行数无法核对。
- 中核 query/detail 认证失败、四级类目无法确定或 detail 异常。
- 招商局 refreshToken 失效、创建/启用失败、模板字段错误，或优先级操作越界；创建阶段立即停止后续 `/add`，不得回退 cURL 或重试。
- 活动任务 `affected_rows=0`、需要其他 SQL、等待/导出失败或缺价而用户尚未授权兜底；等待阶段应先收敛已启动的其他只读 worker，再暂停。
- 脚本遇到 403、405、验证码、登录限制或封禁风险。
- Excel/WPS 占用输出，或模板行数、链接数、分流数无法对账。

## 完成汇报

至少给出：监控表否行数、detail 成功/失败、参考链接数分布、模板记录数、直接推送失败数、各商城任务数、确认提优数、完成未确认提优数、招商局回填数、脚本回填数、detail 参考补全数、最终缺价数，以及所有输出和日志路径。
