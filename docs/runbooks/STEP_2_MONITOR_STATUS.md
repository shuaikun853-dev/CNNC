# 第二步运行手册：截至当前的监控状态查询

> 本步骤完全只读。只查询中核待处理列表和 SaaS 监控，不执行价格审计、招商局、SQL、爬虫、审批或推送。

## 目标

拉取中核 `sassCheckStatuss=["0"]` 的全部待 SaaS 处理记录，以 `documentId` 分批查询 SaaS 监控，合并并输出可推送、需处理、监控缺失和统计 Excel。

## 必需输入

| 输入 | 路径 |
|---|---|
| 中核待处理列表 cURL | `data/latest_cnnc_query.curl` |
| SaaS 监控 cURL | `data/saas_zhuangtai.curl` |
| SaaS 运行时认证 | `data/auth_state.json`；不存在时从现有 SaaS/招商局 cURL 一次性初始化 |
| cURL/无代理工具 | `scripts/curl_utils.py` |
| 固定入口 | `scripts/export_current_monitor_status.py` |

两份 cURL 必须能被 `load_curl_command()` 解析。SaaS cURL 保持不变，仅作为请求模板和状态文件不存在时的初始化来源；状态建立后运行时 Token 只能来自 `auth_state.json`。不得把认证复制到代码、日志、文档、Excel 或临时配置。

## 固定顺序

1. 确认本任务只负责一个新的 `STEP_2` 批次；其他步骤或同一步其他活跃批次不得成为阻塞条件。本步骤在输出目录创建前完全只读，入口确定业务日后立即在输出目录 `workdays/<业务日>/03_step2/` 创建 `BATCH_STATE.md`，记录批次、输入、输出和只读授权。
2. 静态确认中核 body 的 `sassCheckStatuss=["0"]`，SaaS body 的租户为 `188605`、商品池为 `222`。
3. 通过 `scripts/curl_utils.py` 解析 cURL，应用内网无代理环境；启动时刷新 SaaS Token 并原子保存旋转后的两个 Token。禁止手工拆 Token或刷新失败后回退旧 cURL。
4. 中核列表按 `count`、每页 200 条完整分页；实际行数不等于 `count` 就停止。
5. 提取非空且唯一的 `documentId`。SaaS 每批最多 200 个编码，`commodityId` 只放编码本身，不加“文档ID”等前缀。
6. 每批检查 `rspCode=0000`，并验证返回行数等于该批返回 `count`。明确 HTTP 401 时允许刷新后只重试一次；再次失败或任一其他异常则停止，不生成“全量”结果。
7. 以 `documentId == commodityId` 合并。中核存在但 SaaS 未返回的记录必须保留为 `SaaS监控缺失 / 是否可推送=否`。
8. 使用固定判定：`isSync=1 AND isSyncStandard=1 AND syncPriceStatus!=5 AND newPrice 为可解析、有限且大于 0 的数值`。
9. 判定顺序：先采集状态，再同款状态，再拦截 `syncPriceStatus=5` 为“价格检测校验失败”，最后校验 `newPrice`。空、`null`、非数字、非有限、0 和负数均为“推送价格为空或无效”。
10. 输出目录固定为 `workdays/<业务日>/03_step2/`，不做时间戳目录。写入前立即建立状态为 `ACTIVE / STEP_2` 的 `BATCH_STATE.md`，再验证五个 Sheet 和所有数量恒等式；验收通过后改为 `COMPLETE`。若在输出目录创建前查询失败，只汇报本任务失败，不得修改或阻塞其他批次。
11. 运行后核对 manifest：`workdays/<业务日>/manifest.json` 的 `stages.step2.status` 为 `COMPLETED`、`files.*.sha256` 与磁盘三表一致，且三表恒等式 `monitor_total = pushable + non_pushable` 成立；`FAILED` 表示该代不可用。

## 固定命令

```powershell
$env:PYTHONIOENCODING='utf-8'
python scripts\export_current_monitor_status.py --business-date 2026-09-08
```

`--business-date` 缺省取上海时区当前业务日。一次调用基于同一 authoritative snapshot 固定产出三份 xlsx 和 manifest；禁止把时间戳写进文件名。

输出：

```text
workdays/2026-09-08/03_step2/STEP2_监控结果.xlsx
workdays/2026-09-08/03_step2/STEP2_可直接推送.xlsx
workdays/2026-09-08/03_step2/STEP2_不可直接推送.xlsx
workdays/2026-09-08/manifest.json
```

固定 Sheet：`全量监控结果`、`可推送数据`、`需处理数据`、`SaaS监控缺失`、`状态统计`。全量表必须显式保留原始 `newPrice`。`STEP2_可直接推送.xlsx`（单 Sheet `可直接推送`）与 `STEP2_不可直接推送.xlsx`（单 Sheet `不可直接推送`，含 SaaS监控缺失行）是按 `是否可推送` 列的纯投影；同日重跑 generation+1 并原子整代替换三表。

## 必须暂停

- 任一 cURL 缺失、无法解析或返回登录错误。
- 中核实际分页行数与 `count` 不一致。
- SaaS 任一分批返回非 `0000` 或行数与返回 `count` 不一致。
- 租户/商品池/中核筛选不符合本手册。
- 需要审批、推送、SQL、招商局或爬虫才能继续。

不要拿旧状态表、空数据或部分数据冒充“截至当前全量”。

## 验收与汇报

必须满足：

- `saas_matched + saas_missing = cnnc_total`
- `pushable + needs_processing + saas_missing = cnnc_total`
- Excel 全量行数等于 `cnnc_total`
- 五个 Sheet 都存在且可打开
- 三份 `STEP2_*.xlsx` 与 manifest `files.*.sha256` 一致，且 `monitor_total = pushable + non_pushable`

汇报中核总数、SaaS 匹配数、缺失数、可推送数、需处理数、分类分布和输出路径。第二步完成后不得自动进入第三步。
